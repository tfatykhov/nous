"""What each execution context may do (harness Phase 2a).

One table, consulted once per call at AgentRunner._authorize_tool_call. Rows
are the INTENDED policy; in `warn` mode a deviation is logged and persisted as
`harness_context_policy_violation` and the call still runs.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from nous.api.execution_context import FOREGROUND_KINDS, ExecutionContext
from nous.api.tool_classes import is_compensable_call, tool_class
from nous.brain.intentions import AUTHORITY_INTERNAL
from nous.cognitive.bash_side_effect import simple_commands
from nous.cognitive.execution_ledger import classify_side_effect

_ALL = frozenset({"none", "write", "external", "irreversible"})
_WORK = frozenset({"none", "write", "external"})
_LOCAL = frozenset({"none", "write"})


@dataclass(frozen=True)
class ContextPolicy:
    levels: frozenset[str]  # side-effect levels this context may use
    spawn: bool | frozenset[str]  # True: any spawn tool; a set: only these; False: none


CONTEXT_POLICY: Mapping[str, ContextPolicy] = MappingProxyType(
    {
        "interactive": ContextPolicy(_ALL, spawn=True),
        "mcp": ContextPolicy(_ALL, spawn=True),
        "subtask": ContextPolicy(_WORK, spawn=False),
        "dag_node": ContextPolicy(_WORK, spawn=False),
        "scheduled": ContextPolicy(_WORK, spawn=False),
        "agent_action": ContextPolicy(_WORK, spawn=False),
        # Prod delivers DAG results by email through spawn_task from this turn:
        # that one spawn tool is allowed explicitly, the rest are not (roadmap §4).
        "dag_summary": ContextPolicy(_WORK, spawn=frozenset({"spawn_task"})),
        "heartbeat_triage": ContextPolicy(_LOCAL, spawn=False),
        # A check/callback may spawn only what it declared (check pipelines
        # declare heartbeat_check_create) -- see evaluate().
        "heartbeat_check": ContextPolicy(_WORK, spawn=False),
        "heartbeat_callback": ContextPolicy(_WORK, spawn=False),
        "background": ContextPolicy(_LOCAL, spawn=False),
        # F099: Nous's own turn on a background result. Local levels only, and the
        # two spawn tools; _offered_tools and the strict path in
        # _authorize_tool_call narrow it further (the policy is the floor).
        "continuation": ContextPolicy(_LOCAL, spawn=frozenset({"spawn_task", "dag_create"})),
        # F099: one owner-approved call, declared_tools=(tool,). Levels wide because the
        # proposed call is by definition outward or a denylisted local tool.
        "approved_action": ContextPolicy(_ALL, spawn=True),
    }
)

# F099 section 4.4: tools an internal_only turn may NOT use although their class is
# none or write. They schedule, persist policy, reach the host, or resolve the Brain's
# own records.
INTERNAL_ONLY_DENYLIST: frozenset[str] = frozenset(
    {
        "schedule_task",
        "heartbeat_check_create",
        "heartbeat_check_manage",
        "create_censor",
        "learn_skill",
        "store_identity",
        "complete_initiation",
        "dag_manage",
        "push_surface",
        "compose_surface",
        "bash",
        "run_python",
        "spawn_sync",
        "resolve_decision",
        "resolve_decisions",
    }
)
# Offered to continuation turns only; removed when ctx.spawn_blocked.
INTERNAL_ONLY_SPAWN_TOOLS: frozenset[str] = frozenset({"spawn_task", "dag_create"})
# A continuation's per-turn extra tools: offered only as extra_tools, appended after the narrowing.
# Classified for the ledger, never in the allowed set. A name here is a per-turn extra tool: it is
# appended after the narrowing and is never registered with a dispatcher.
INTERNAL_ONLY_EXTRA_TOOLS: frozenset[str] = frozenset({"resolve_intention", "propose_action"})
# Per-call rules (path and lineage), evaluated at dispatch.
INTERNAL_ONLY_CHECKED_TOOLS: frozenset[str] = frozenset({"write_file", "cancel_task"})
# Logged with their root when called from a lineage (spec section 9).
INTERNAL_ONLY_LOGGED_TOOLS: frozenset[str] = frozenset({"web_fetch", "web_search"})

# F099 lineage shell (NOUS_F099_LINEAGE_SHELL): bash is offered to an internal_only turn, and every call is
# held to an ALLOWLIST of simple commands (lineage_bash_allowed), not to the side-effect classifier, which
# rates `rm -rf` a mere write. Read-only commands that cannot write, run or reach the network whatever
# their arguments (no sort -o, sed -i, find -exec, date -s, tee):
LINEAGE_READ_COMMANDS: frozenset[str] = frozenset(
    {"cat", "ls", "head", "tail", "wc", "grep", "jq", "echo", "pwd", "cd", "sleep", "true"}
)


@dataclass(frozen=True)
class _ArgRoles:
    """How a read command's options take values. ``short`` / ``long`` map an option to ``flag``, ``value`` (one
    word that names no file), ``file`` (one word that does), ``pair`` (two words, neither a file) or ``pair_file``
    (two words, the second a file). ``closed``: an option not in the maps is refused, since GNU accepts any
    abbreviation of a long option and an unknown one may be ``--recursive`` or ``--file``; otherwise it is a
    flag. ``refused_long`` is refused with every abbreviation of it."""

    short: Mapping[str, str]
    long: Mapping[str, str]
    closed: bool = False
    refused_long: frozenset[str] = frozenset()


_HEAD_ROLES = _ArgRoles({"n": "value", "c": "value"}, {"--lines": "value", "--bytes": "value"})
_GREP_FLAG_LONG = (
    "--extended-regexp --fixed-strings --basic-regexp --perl-regexp --ignore-case --no-ignore-case --word-regexp "
    "--line-regexp --null-data --no-messages --invert-match --byte-offset --line-number --line-buffered "
    "--with-filename --no-filename --only-matching --quiet --silent --text --files-without-match "
    "--files-with-matches --count --initial-tab --null --color --colour --no-group-separator --help --version"
)
_GREP_VALUE_LONG = (
    "--regexp --max-count --after-context --before-context --context --directories --devices --binary-files "
    "--label --include --exclude --exclude-dir --group-separator"
)
_JQ_FLAG_LONG = (
    "--null-input --raw-input --slurp --compact-output --tab --ascii-output --unbuffered --sort-keys --color-output "
    "--monochrome-output --raw-output --raw-output0 --join-output --exit-status --seq --stream --stream-errors "
    "--help --version --build-configuration"
)
# The read commands whose arguments can name a file: each such argument must resolve under the workspace with no
# hidden component (_readable). grep's recursion (-r, -R, -d recurse) is refused: it descends into hidden
# directories. jq's -f/--from-file and -L are refused (a program file, or a module search path, can import any
# .json file), and so are --args/--jsonargs. cd takes exactly one directory.
_READ_ARG_ROLES: Mapping[str, _ArgRoles] = MappingProxyType(
    {
        "cat": _ArgRoles({}, {}),
        "ls": _ArgRoles({}, {}),
        "wc": _ArgRoles({}, {}, refused_long=frozenset({"--files0-from"})),
        "head": _HEAD_ROLES,
        "tail": _ArgRoles(
            {**_HEAD_ROLES.short, "s": "value"},
            {**_HEAD_ROLES.long, **dict.fromkeys(("--pid", "--sleep-interval", "--max-unchanged-stats"), "value")},
        ),
        "grep": _ArgRoles(
            {
                **dict.fromkeys("EFGPiywxzsvVbHhnTZaIUcLloqu0123456789", "flag"),
                **dict.fromkeys("eABCmdD", "value"),
                "f": "file",
            },
            {
                **dict.fromkeys(_GREP_FLAG_LONG.split(), "flag"),
                **dict.fromkeys(_GREP_VALUE_LONG.split(), "value"),
                "--file": "file",
                "--exclude-from": "file",
            },
            closed=True,
        ),
        "jq": _ArgRoles(
            dict.fromkeys("nrscejaSCMhV", "flag"),
            {
                **dict.fromkeys(_JQ_FLAG_LONG.split(), "flag"),
                "--indent": "value",
                "--arg": "pair",
                "--argjson": "pair",
                "--rawfile": "pair_file",
                "--slurpfile": "pair_file",
            },
            closed=True,
        ),
    }
)
# A jq filter that reads the environment (GITHUB_TOKEN is left in it) or imports a module or data file.
_JQ_REFUSED = re.compile(r"\$ENV|\benv\b|\bimport\b|\binclude\b")
_MAX_LINEAGE_CWDS = 16  # directories a chain of cd could have left the shell in; more is refused
# Read-only gh subcommands; `gh api` is checked on its own (_gh_api_get).
LINEAGE_GH_READS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "pr": frozenset({"view", "checks", "list", "diff", "status"}),
        "issue": frozenset({"view", "list"}),
        "run": frozenset({"view", "list"}),
        "repo": frozenset({"view"}),
    }
)
_LINEAGE_JOINS = frozenset({";", "\n", "&&", "||", "|", ""})  # no `&`, no subshell or group
_LINEAGE_SINKS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr"})
_GH_API_VALUE_FLAGS = frozenset({"--jq", "-q", "--template", "-t"})
_GH_API_FLAGS = frozenset({"--paginate", "--slurp", "--silent", "--include", "-i", "--verbose"})
_GH_OUTWARD_FLAGS = ("--web", "-w", "--watch", "--hostname")
# A runner.sh repo name or option value: interpolated into a `python3 -c` string by runner.sh, so no quotes.
_RUNNER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_RUNNER_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./-]*")
_RUNNER_JOB_ID = re.compile(r"job-\d{8}-\d{6}-[0-9a-f]{8}")


def internal_only_allowed(name: str, *, ctx: ExecutionContext, lineage_shell: bool = False) -> bool:
    """Whether an internal_only context may be OFFERED ``name``.

    Fails closed on an unclassified tool: a tool nobody classified is not
    allowed, so a new tool is denied to a lineage until someone adds it on purpose.
    ``lineage_shell`` (``NOUS_F099_LINEAGE_SHELL``) lifts the denylist for ``bash``
    alone; its calls are then held to ``lineage_bash_allowed``.
    """
    cls = tool_class(name)
    if cls is None or cls.side_effect not in ("none", "write"):
        return False
    if name == "bash" and lineage_shell:
        return True
    if name in INTERNAL_ONLY_DENYLIST or name in INTERNAL_ONLY_EXTRA_TOOLS:
        return False
    if name in INTERNAL_ONLY_SPAWN_TOOLS:
        return ctx.kind == "continuation" and not ctx.spawn_blocked
    return True


def _write_path_allowed(ctx: ExecutionContext, tool_input: Mapping[str, Any], workspace_dir: str) -> bool:
    """A write_file target is inside ``<workspace_dir>/intentions/<root_id>/`` once resolved.

    Resolves the way the tool does (``builtin_tools._validate_path``): relative to the
    workspace, symlinks and ``..`` followed. A lineage with no root id, or a call with
    no usable path, is refused.
    """
    path = tool_input.get("path")
    if ctx.root_intention_id is None or not isinstance(path, str) or not path.strip():
        return False
    if "\x00" in path:  # Windows' realpath drops a NUL instead of raising (gh-106242)
        return False
    try:
        workspace = Path(workspace_dir).resolve()
        root_dir = workspace / "intentions" / str(ctx.root_intention_id)
        target = Path(path).resolve() if Path(path).is_absolute() else (workspace / path).resolve()
    except (OSError, ValueError):  # an unresolvable name
        return False
    return target.is_relative_to(root_dir)


def _under(path: str, dirs: list[Path]) -> bool:
    """``path`` is absolute and inside one of ``dirs`` once resolved (symlinks and ``..`` followed)."""
    if not path.startswith("/") or "\x00" in path:
        return False
    try:
        target = Path(path).resolve()
        return any(target.is_relative_to(d.resolve()) for d in dirs)
    except (OSError, ValueError):
        return False


def _readable(path: str, cwds: list[Path], workspace: Path) -> list[Path] | None:
    """Where ``path`` resolves from each of ``cwds`` (symlinks and ``..`` followed), or None when one of them is
    outside ``workspace`` or has a hidden component below it (``.ssh``, ``.env``, ``claude-jobs/.claude-home``,
    ``.nous-undo``). ``cwds`` and ``workspace`` are resolved. ``~`` is refused."""
    if path.startswith("~") or "\x00" in path:
        return None
    try:
        targets = [Path(path).resolve()] if path.startswith("/") else [(d / path).resolve() for d in cwds]
        if all(
            t.is_relative_to(workspace) and not any(p.startswith(".") for p in t.relative_to(workspace).parts)
            for t in targets
        ):
            return targets
    except (OSError, ValueError, RuntimeError):  # an unresolvable name, a symlink loop
        pass
    return None


def _split_read_args(args: list[str], roles: _ArgRoles) -> tuple[list[str], list[str], list[tuple[str, str]]] | None:
    """``(operands, file values, (option, value) pairs)`` of a read command's arguments, or None when an option
    is refused, unknown to a closed ``roles``, or missing its value."""
    operands: list[str] = []
    files: list[str] = []
    values: list[tuple[str, str]] = []
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--":
            operands += args[i:]
            break
        if a == "-" or not a.startswith("-"):
            operands.append(a)
            continue
        if a.startswith("--"):
            name, eq, value = a.partition("=")
            role = roles.long.get(name, None if roles.closed else "flag")
            if role is None or any(r.startswith(name) for r in roles.refused_long):
                return None
            if role.startswith("pair"):
                if eq or i + 1 >= len(args):
                    return None
                option, value = name, args[i + 1]
                i += 2
            elif role == "flag":
                continue
            elif eq:
                option = name
            elif i < len(args):
                option, value = name, args[i]
                i += 1
            else:
                return None
        else:
            for j, letter in enumerate(a[1:], 2):
                role = roles.short.get(letter, None if roles.closed else "flag")
                if role is None:
                    return None
                if role != "flag":
                    break
            else:
                continue
            option, value = f"-{letter}", a[j:]
            if not value:
                if i >= len(args):
                    return None
                value = args[i]
                i += 1
        if role.endswith("file"):
            files.append(value)
        else:
            values.append((option, value))
    return operands, files, values


def _read_paths(prog: str, args: list[str]) -> list[str] | None:
    """The arguments of a read command that name a file (for ``cd``, its directory), or None when the call is
    refused whatever its paths name."""
    if prog == "cd":  # bare `cd` goes to $HOME, `cd -` to $OLDPWD; no option (-P) either
        return args if len(args) == 1 and not args[0].startswith("-") else None
    split = _split_read_args(args, _READ_ARG_ROLES[prog])
    if split is None:
        return None
    operands, files, values = split
    if prog == "grep":
        if any(opt in ("-d", "--directories") and value not in ("read", "skip") for opt, value in values):
            return None
        if not files and not any(opt in ("-e", "--regexp") for opt, _ in values):
            operands = operands[1:]  # the pattern
    elif prog == "jq":
        if operands and _JQ_REFUSED.search(operands[0]):
            return None
        operands = operands[1:]  # the filter
    return files + [o for o in operands if o != "-"]


def _gh_allowed(args: list[str]) -> bool:
    """A read-only gh call: a subcommand in LINEAGE_GH_READS, or ``gh api`` GET."""
    if any(a.startswith(_GH_OUTWARD_FLAGS) for a in args):
        return False
    if args[:1] == ["api"]:
        return _gh_api_get(args[1:])
    return len(args) >= 2 and args[1] in LINEAGE_GH_READS.get(args[0], frozenset())


def _gh_api_get(args: list[str]) -> bool:
    """``gh api ENDPOINT`` as a GET: no method but GET, no field, input or header (each makes it a write or
    can override the method), no graphql (a query can mutate), no full URL (another host)."""
    endpoint: str | None = None
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-X", "--method"):
            if i + 1 >= len(args) or args[i + 1].upper() != "GET":
                return False
            i += 2
        elif a.startswith(("-X", "--method=")):
            if a.split("=", 1)[-1].removeprefix("-X").upper() != "GET":
                return False
            i += 1
        elif a in _GH_API_VALUE_FLAGS:
            if i + 1 >= len(args):
                return False
            i += 2
        elif a in _GH_API_FLAGS or a.startswith(("--jq=", "--template=")):
            i += 1
        elif a.startswith("-") or endpoint is not None:
            return False
        else:
            endpoint = a
            i += 1
    return endpoint is not None and "graphql" not in endpoint.lower() and "://" not in endpoint


def _printf_allowed(args: list[str]) -> bool:
    """printf is safe except for -v, which assigns to a shell variable on bash hosts
    and can override exported variables like PATH. Reject any invocation that passes -v."""
    return not any(a == "-v" or (a.startswith("-") and not a.startswith("--") and "v" in a.lstrip("-")) for a in args)


def _runner_allowed(args: list[str], *, workspace: Path, readable: list[Path]) -> bool:
    """runner.sh: ``status|result|cancel <job id>``, ``status``, ``list [--active|--all]``, or ``launch <repo>
    <prompt>|--prompt-file <file>`` with ``--model``, ``--effort``, ``--base-branch``, ``--plugins`` (installed
    plugins only) and ``--prompt-file`` (one of ``readable``). No ``--sys-prompt``; ``cleanup`` and ``gc``
    are not a lineage's to run."""
    if not args:
        return False
    action, rest = args[0], args[1:]
    if action == "list":
        return rest in ([], ["--active"], ["--all"])
    if action == "status" and not rest:
        return True
    # cancel is NOT allowed here: a lineage can list --all to enumerate ALL jobs
    # (including other lineages') then cancel them; ownership cannot be verified
    # without a runner.sh protocol change (see #725). Use propose_action instead.
    if action in ("status", "result"):
        return len(rest) == 1 and _RUNNER_JOB_ID.fullmatch(rest[0]) is not None
    if action != "launch" or not rest or not _RUNNER_NAME.fullmatch(rest[0]):
        return False
    rest = rest[1:]
    if rest and rest[0] != "--prompt-file":
        if rest[0].startswith("-"):
            return False
        rest = rest[1:]  # the prompt itself: runner.sh writes it to a file, never into code
    if len(rest) % 2:
        return False
    plugins = [workspace / "plugins"]
    for opt, value in zip(rest[::2], rest[1::2], strict=True):
        if opt in ("--model", "--effort"):
            ok = _RUNNER_NAME.fullmatch(value) is not None
        elif opt == "--base-branch":
            ok = _RUNNER_BRANCH.fullmatch(value) is not None
        elif opt == "--plugins":
            ok = all(_under(p, plugins) for p in value.split(","))
        elif opt == "--prompt-file":
            ok = _under(value, readable)
        else:
            ok = False
        if not ok:
            return False
    return True


def lineage_bash_allowed(ctx: ExecutionContext, command: str, *, workspace_dir: str) -> bool:
    """Whether an internal_only turn may run this bash ``command`` (F099 lineage shell).

    An allowlist, failing closed: every simple command must be a LINEAGE_READ_COMMANDS command, a read-only
    ``gh`` call, or ``<workspace_dir>/claude-jobs/runner.sh`` (the owner's Claude Code delegation path), joined
    by ``; && || |`` or newlines only. An output redirection may target ``/dev/null`` or a file under the
    root's own ``<workspace_dir>/intentions/<root_id>/`` or ``<workspace_dir>/claude-jobs/prompts/``.
    Anything the reader cannot see exactly (``$``, a backtick, a glob, an unquoted heredoc that expands, an
    unbalanced quote) is refused, so a chain or substitution that reaches a refused command is refused.

    Reads are confined too: a file a read command or an input redirection names, and a ``cd`` target, must
    resolve under ``<workspace_dir>`` with no hidden component (``_readable``), from the workspace (the shell's
    cwd) and from every directory an earlier ``cd`` in the command could have reached. ``CDPATH`` is not in a
    lineage's environment (``builtin_tools._LINEAGE_BASH_ENV``), so ``cd`` goes where it says.
    """
    commands = simple_commands(command)
    if commands is None or not any(words for words, _, _, _ in commands):
        return False
    workspace = Path(workspace_dir)
    jobs = workspace / "claude-jobs"
    writable = [jobs / "prompts"]
    if ctx.root_intention_id is not None:
        writable.append(workspace / "intentions" / str(ctx.root_intention_id))
    root = workspace.resolve()
    cwds = [root]
    for words, targets, sources, op in commands:
        if op not in _LINEAGE_JOINS:
            return False
        if not words:
            if targets or sources:
                return False
            continue
        if not all(t in _LINEAGE_SINKS or _under(t, writable) for t in targets):
            return False
        if not all(s in _LINEAGE_SINKS or _readable(s, cwds, root) is not None for s in sources):
            return False
        prog, args = words[0], words[1:]
        if prog in _READ_ARG_ROLES or prog == "cd":
            paths = _read_paths(prog, args)
            reached = None if paths is None else [_readable(p, cwds, root) for p in paths]
            if reached is None or None in reached:
                return False
            if prog == "cd":
                cwds = list(dict.fromkeys([*cwds, *reached[0]]))
                if len(cwds) > _MAX_LINEAGE_CWDS:
                    return False
        if prog == str(jobs / "runner.sh"):
            if not _runner_allowed(args, workspace=workspace, readable=writable):
                return False
        elif prog == "gh":
            if not _gh_allowed(args):
                return False
        elif prog == "printf":
            if not _printf_allowed(args):
                return False
        elif prog not in LINEAGE_READ_COMMANDS:
            return False
    return True


def internal_only_call_violation(
    ctx: ExecutionContext, name: str, tool_input: Mapping[str, Any], *, workspace_dir: str
) -> str | None:
    """The per-call rules of an internal_only turn, for a tool that WAS offered.

    ``"external"``: ``classify_side_effect`` rates the call external (or
    irreversible), such as a URL in run_python; for bash, the command is not on
    the lineage allowlist (``lineage_bash_allowed``). ``"write_path"``: write_file outside
    its root's directory. ``"foreign_cancel"``: cancel_task with an id that is not a
    UUID. The cancel_task lineage check needs the target's row, so it lives in the
    handler (``tools.py``); this sync function only rejects what needs no read.
    """
    if name == "bash":
        command = tool_input.get("command")
        if not isinstance(command, str) or not lineage_bash_allowed(ctx, command, workspace_dir=workspace_dir):
            return "external"
        return None
    if classify_side_effect(name, dict(tool_input)) in ("external", "irreversible"):
        return "external"
    if name == "write_file" and not _write_path_allowed(ctx, tool_input, workspace_dir):
        return "write_path"
    if name == "cancel_task":
        try:
            uuid.UUID(str(tool_input.get("task_id", "")))
        except (TypeError, ValueError):
            return "foreign_cancel"
    return None


def undoable_violation(ctx: ExecutionContext, tool_name: str, tool_input: Mapping[str, Any]) -> str | None:
    """``"not_compensable"`` when an ``undoable`` context makes a call that
    has a side effect and cannot be undone, else None.

    Separate from :func:`evaluate` because it is a safety invariant, not a
    policy preference: the runner enforces it even when the context policy
    is ``off`` or ``warn`` -- an undoable claim with no undo is false."""
    if not ctx.undoable:
        return None
    if classify_side_effect(tool_name, dict(tool_input)) == "none":
        return None
    if not is_compensable_call(tool_name, tool_input):
        return "not_compensable"
    return None


def evaluate(
    ctx: ExecutionContext, tool_name: str, tool_input: Mapping[str, Any], *, workspace_dir: str | None = None
) -> str | None:
    """None if ``ctx`` may make this call; otherwise the violation code:
    ``unclassified``, ``undeclared``, ``level:<level>``, ``spawn``, ``reenable``.

    With ``workspace_dir``, an internal_only bash call on the lineage allowlist is judged at ``write``: the
    read-only gh calls it permits are ``external`` only to the classifier, and a continuation's row is local."""
    if ctx.kind in FOREGROUND_KINDS:
        return None
    policy = CONTEXT_POLICY[ctx.kind]
    cls = tool_class(tool_name)
    if cls is None:
        return "unclassified"
    declared = ctx.declared_tools
    if declared is not None and tool_name not in declared:
        return "undeclared"
    level = classify_side_effect(tool_name, dict(tool_input))
    if (
        level == "external"
        and tool_name == "bash"
        and ctx.authority == AUTHORITY_INTERNAL
        and workspace_dir is not None
        and lineage_bash_allowed(ctx, str(tool_input.get("command", "")), workspace_dir=workspace_dir)
    ):
        level = "write"
    if level not in policy.levels:
        return f"level:{level}"
    # Phase 2.8: check undoability BEFORE the spawn gate so that a tool that
    # both spawns and lacks a compensator (e.g. dag_create) returns
    # "not_compensable" — the true constraint — rather than "spawn", which
    # masks the undoability violation and prevents force_block from activating
    # in warn mode.
    if undoable_violation(ctx, tool_name, tool_input) is not None:
        return "not_compensable"
    if cls.spawns and not (
        policy.spawn is True
        or (isinstance(policy.spawn, frozenset) and tool_name in policy.spawn)
        or (declared is not None and tool_name in declared)
    ):
        return "spawn"
    # a callback may not resurrect the check that triggered it: neither by
    # enabling it nor by creating one under its name (after deleting it)
    own = (ctx.check_name or "").strip().lower()
    if ctx.kind == "heartbeat_callback" and own:
        named = str(tool_input.get("name", "")).strip().lower() == own
        action = str(tool_input.get("action", "")).strip().lower()
        if named and (
            (tool_name == "heartbeat_check_manage" and action == "enable") or tool_name == "heartbeat_check_create"
        ):
            return "reenable"
    return None
