"""What each execution context may do (harness Phase 2a).

One table, consulted once per call at AgentRunner._authorize_tool_call. Rows
are the INTENDED policy; in `warn` mode a deviation is logged and persisted as
`harness_context_policy_violation` and the call still runs.
"""

from __future__ import annotations

import glob
import os
import re
import shlex
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from types import MappingProxyType
from typing import Any

from nous.api.execution_context import FOREGROUND_KINDS, ExecutionContext
from nous.api.tool_classes import is_compensable_call, tool_class
from nous.brain.intentions import AUTHORITY_INTERNAL
from nous.cognitive.bash_side_effect import (
    GlobWord,
    classify_bash_command,
    command_invocations,
    command_string,
    simple_commands,
)
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
            dict.fromkeys("nrRscejaSCMhV", "flag"),
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
_MAX_GLOB_MATCHES = 200  # files one glob operand may expand to, from one directory; more is refused
_GLOB_CHAR = re.compile(r"[*?\[]")
_GLOB_CLASS = re.compile(r"\[[A-Za-z0-9_]+\]")  # the one bracket form allowed: a plain set, no !, ^, range or class
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
# F099 lineage build commands (NOUS_F099_LINEAGE_BUILD_COMMANDS): a lineage may verify a build itself. These run
# only in a repo's top level, reached by `cd <repo> &&` (_build_repo), and with a closed option map each. pytest and
# make run the repo's own code, so the repo must be one the lineage cannot write into; the command runs without the
# GitHub token (builtin_tools._LINEAGE_BUILD_ENV, set by dispatch through lineage_runs_build).
LINEAGE_BUILD_PROGRAMS: frozenset[str] = frozenset({"git", "pytest", "ruff", "make"})
LINEAGE_MAKE_TARGETS: frozenset[str] = frozenset({"test", "lint", "check", "typecheck", "format-check", "build"})
_MAKE_FLAGS = frozenset({"-k", "-s", "--keep-going", "--silent", "--no-print-directory"})


@dataclass(frozen=True)
class _GitRoles:
    """A read-only git subcommand's options. ``flags``: long options, with or without ``=value``. ``values``: long
    options that must carry ``=value``; the next word is never taken as a value, because `git diff -U /etc/passwd x`
    would read it as a path and compare a file outside the repo. ``short``: one letter per word, ``flag`` or ``value``
    (attached, ``-n5``). ``counts``: ``-<number>`` (``git log -5``)."""

    flags: frozenset[str]
    values: frozenset[str]
    short: Mapping[str, str]
    counts: bool = False


_GIT_DIFF_FLAGS = (
    "--stat --shortstat --numstat --dirstat --summary --name-only --name-status --patch --no-patch --raw --color "
    "--no-color --word-diff --no-ext-diff --no-textconv --ignore-all-space --ignore-space-change --ignore-blank-lines "
    "--ignore-cr-at-eol --minimal --patience --histogram --full-index --abbrev --no-renames --find-renames"
)
_GIT_LOG_FLAGS = (
    "--oneline --graph --decorate --no-decorate --all --branches --tags --remotes --reverse --merges --no-merges "
    "--first-parent --follow --date-order --topo-order --abbrev-commit --no-abbrev-commit --left-right --source "
    "--pretty"
)
_GIT_LOG_VALUES = (
    "--max-count --skip --since --after --until --before --author --committer --grep --format --date --diff-filter "
    "--unified"
)
_GIT_DIFF_SHORT = {**dict.fromkeys("pusw", "flag"), "U": "value"}
_GIT_LOG_ROLES = _GitRoles(
    frozenset(f"{_GIT_DIFF_FLAGS} {_GIT_LOG_FLAGS}".split()),
    frozenset(_GIT_LOG_VALUES.split()),
    {**_GIT_DIFF_SHORT, "n": "value"},
    counts=True,
)
# No --output, --ext-diff, --textconv, --no-index (a diff of any two files), -O<orderfile>, --submodule, and no
# option before the subcommand (-C, -c, --git-dir, --work-tree, --exec-path, -p).
LINEAGE_GIT_READS: Mapping[str, _GitRoles] = MappingProxyType(
    {
        "status": _GitRoles(
            frozenset(
                "--short --branch --porcelain --long --verbose --show-stash --ahead-behind --no-ahead-behind "
                "--ignored --untracked-files --no-renames --renames".split()
            ),
            frozenset(),
            {**dict.fromkeys("sbvz", "flag"), "u": "value"},
        ),
        "log": _GIT_LOG_ROLES,
        "show": _GIT_LOG_ROLES,
        "diff": _GitRoles(
            frozenset(f"{_GIT_DIFF_FLAGS} --cached --staged --merge-base --exit-code --quiet --check".split()),
            frozenset({"--diff-filter", "--unified"}),
            _GIT_DIFF_SHORT,
        ),
    }
)
# pytest: no -p (a plugin), -c / --rootdir / --confcutdir / -o (another config or root), --pyargs (an installed
# package's tests), --basetemp / --junitxml / --resultlog (writes), --import-mode.
_PYTEST_ROLES = _ArgRoles(
    {**dict.fromkeys("qvxsl", "flag"), **dict.fromkeys("kmr", "value")},
    {
        **dict.fromkeys(
            (
                "--quiet --verbose --exitfirst --showlocals --co --collect-only --lf --last-failed --ff "
                "--failed-first --sw --stepwise --no-header --strict-markers --runxfail"
            ).split(),
            "flag",
        ),
        **dict.fromkeys(("--tb", "--maxfail", "--durations", "--deselect"), "value"),
    },
    closed=True,
)
# ruff: no --fix / --unsafe-fixes / --add-noqa / --watch (writes or never ends), --config / --extend (another
# config), -o / --output-file, --cache-dir. `ruff format` only checks: --check or --diff is required.
_RUFF_ROLES: Mapping[str, _ArgRoles] = MappingProxyType(
    {
        "check": _ArgRoles(
            {"q": "flag"},
            {
                **dict.fromkeys(
                    "--quiet --statistics --no-cache --no-fix --no-fix-only --show-fixes --exit-zero --diff".split(),
                    "flag",
                ),
                **dict.fromkeys(("--output-format", "--select", "--ignore", "--extend-select"), "value"),
            },
            closed=True,
        ),
        "format": _ArgRoles(
            {"q": "flag"}, dict.fromkeys("--check --diff --quiet --no-cache".split(), "flag"), closed=True
        ),
    }
)
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


def lineage_write_dirs(ctx: ExecutionContext, workspace_dir: str, *, lineage_shell: bool) -> list[Path]:
    """The resolved directories an internal_only turn may write into: the root's own
    ``<workspace_dir>/intentions/<root_id>/`` when it has a root, and ``<workspace_dir>/claude-jobs/prompts/``
    only with ``lineage_shell`` (a prompt file there is for ``runner.sh launch``, which only the lineage shell
    can run). Shared by write_file (``_write_path_allowed``) and the lineage shell's redirections."""
    workspace = Path(workspace_dir).resolve()
    dirs: list[Path] = []
    if ctx.root_intention_id is not None:
        dirs.append(workspace / "intentions" / str(ctx.root_intention_id))
    if lineage_shell:
        dirs.append(workspace / "claude-jobs" / "prompts")
    return dirs


def write_path_refusal_hint(ctx: ExecutionContext, workspace_dir: str, *, lineage_shell: bool) -> str:
    """Where write_file may write in this turn, appended to its ``write_path`` refusal."""
    if ctx.root_intention_id is None:
        return "write_file may not write in this turn: it has no root intention."
    dirs = " or ".join(f"{d}/" for d in lineage_write_dirs(ctx, workspace_dir, lineage_shell=lineage_shell))
    return f"write_file may only write under {dirs}."


def _write_path_allowed(
    ctx: ExecutionContext, tool_input: Mapping[str, Any], workspace_dir: str, *, lineage_shell: bool = False
) -> bool:
    """A write_file target is inside one of ``lineage_write_dirs`` once resolved.

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
        dirs = lineage_write_dirs(ctx, workspace_dir, lineage_shell=lineage_shell)
        target = Path(path).resolve() if Path(path).is_absolute() else (workspace / path).resolve()
    except (OSError, ValueError):  # an unresolvable name
        return False
    return any(target.is_relative_to(d) for d in dirs)


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


def _glob_matches(pattern: str, cwds: list[Path], workspace: Path) -> list[Path] | None:
    """Where an unquoted glob file operand of a read command resolves (``_readable``) once expanded from each of
    ``cwds``, or None when the shell's expansion could differ from this one or reach a file ``_readable`` refuses.

    The command string is run as checked (asyncio's create_subprocess_shell, /bin/sh: dash in the image), so the
    shell expands the pattern again; this expansion has to be the one it makes. Python's ``glob`` (not recursive)
    and dash agree on ``*`` and ``?`` within one component, on a plain ``[abc]`` set, on following symlinked
    directories, and on a leading dot: a name starting with ``.`` is matched by neither unless the pattern's own
    component starts with ``.``, which is refused (as are ``..`` and ``.``). Refused because the two may not agree:
    ``**`` (bash's globstar), ``//``, a bracket that is not a plain set of letters, digits and ``_`` (``[!a]``,
    ``[^a]``, ranges, which follow the locale, ``[:alpha:]``, an unmatched ``[`` or ``]``), and a name the pattern
    matches as characters but not as bytes, or the other way (dash matches bytes, bash in a UTF-8 locale
    characters): both are expanded and must give the same names. No match is refused, since the shell would pass
    the pattern itself, and so are more than _MAX_GLOB_MATCHES, a pattern or a match that starts with ``-`` (an
    option), and a pattern whose literal prefix (the components before the first glob) is not strictly below
    ``workspace``: ``ls *`` at its top level stays refused. Every match must pass ``_readable``. The shell expands
    after this check, so a file created between the two is not checked; the window is bounded by what a lineage
    may write (its own directories, ``lineage_write_dirs``)."""
    parts = pattern.split("/")
    if (
        pattern.startswith("-")
        or "**" in pattern
        or "//" in pattern
        or any(p.startswith(".") for p in parts)
        or re.search(r"[\[\]]", _GLOB_CLASS.sub("", pattern))
    ):
        return None
    first = next(i for i, p in enumerate(parts) if _GLOB_CHAR.search(p))
    prefix = "/".join(parts[:first]) or "."
    targets: list[Path] = []
    for cwd in cwds[:1] if pattern.startswith("/") else cwds:
        base = _readable(prefix, [cwd], workspace)
        if base is None or base[0] == workspace:
            return None
        try:
            names = set(islice(glob.iglob(pattern, root_dir=cwd), _MAX_GLOB_MATCHES + 1))
            raw = islice(glob.iglob(os.fsencode(pattern), root_dir=os.fsencode(cwd)), _MAX_GLOB_MATCHES + 1)
            same = names == {os.fsdecode(name) for name in raw}
        except (OSError, ValueError):
            return None
        if not names or len(names) > _MAX_GLOB_MATCHES or not same:
            return None
        for name in names:
            reached = None if name.startswith("-") else _readable(name, [cwd], workspace)
            if reached is None:
                return None
            targets += reached
    return targets


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


def _read_paths(prog: str, args: list[str]) -> tuple[list[str], list[str]] | None:
    """The arguments of a read command that name a file, as ``(option values, operands)`` (for ``cd``, its
    directory is the operand), or None when the call is refused whatever its paths name."""
    if prog == "cd":
        # Bare `cd` goes to $HOME, `cd -` to $OLDPWD; no option (-L, -P) either. No `..` component: cd resolves
        # it LOGICALLY (`l/..` is where the link l sits), _readable physically (the link target's parent), so
        # after a link `cd l/../..` leaves the workspace while _readable sees it stay. A read argument's `..` is
        # resolved by the kernel, physically, and needs no such rule.
        ok = len(args) == 1 and not args[0].startswith("-") and ".." not in args[0].split("/")
        return ([], args) if ok else None
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
        if operands and not _jq_filter_allowed(operands[0]):
            return None
        operands = operands[1:]  # the filter
    return files, [o for o in operands if o != "-"]


def _jq_filter_allowed(text: str) -> bool:
    """A jq filter (jq's own, or gh's ``--jq``, run by gojq) that reads no environment and imports nothing."""
    return _JQ_REFUSED.search(text) is None


def _gh_jq_filters(args: list[str]) -> list[str]:
    """Every word that may be a gh ``--jq`` / ``-q`` filter: ``--jq X``, ``--jq=X``, and the value of a ``q``
    in a shorthand cluster (``-q X``, ``-qX``, ``-q=X``, ``-cq X``). A word that only might be one is included:
    a false match only refuses. ``--template`` / ``-t`` are not checked: gh's Go template functions (text/template's
    builtins plus color, join, pluck, tablerow, timeago, timefmt, truncate, hyperlink) cannot read the environment."""
    filters: list[str] = []
    for i, a in enumerate(args):
        nxt = args[i + 1 : i + 2]
        if a == "--jq":
            filters += nxt
        elif a.startswith("--jq="):
            filters.append(a.removeprefix("--jq="))
        elif not a.startswith("--") and a.startswith("-") and "q" in a:
            rest = a[a.index("q") + 1 :].removeprefix("=")
            filters += [rest] if rest else nxt
    return filters


def _gh_allowed(args: list[str]) -> bool:
    """A read-only gh call: a subcommand in LINEAGE_GH_READS, or ``gh api`` GET."""
    if any(a.startswith(_GH_OUTWARD_FLAGS) for a in args):
        return False
    if not all(_jq_filter_allowed(f) for f in _gh_jq_filters(args)):
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
    and can override exported variables like PATH. Only the leading option can be -v:
    ``--`` ends the options, and the first other word is the format, after which a
    ``-v`` is an argument printf prints. (Bash refuses any other option, so a ``-v``
    after one never assigns.)"""
    first = args[0] if args else ""
    return not (first != "--" and first.startswith("-") and len(first) > 1 and "v" in first)


def _runner_allowed(args: list[str], *, workspace: Path, readable: list[Path]) -> bool:
    """runner.sh: ``status|result <job id>``, ``status``, ``list [--active|--all]``, or ``launch <repo>
    <prompt>|--prompt-file <file>`` with ``--model``, ``--effort``, ``--base-branch``, ``--plugins`` (installed
    plugins only) and ``--prompt-file`` (one of ``readable``). No ``--sys-prompt``; ``cancel``, ``cleanup`` and
    ``gc`` are not a lineage's to run (a lineage cancels a job through ``propose_action``)."""
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


def _build_repo(cwd: Path, root: Path, writable: list[Path]) -> bool:
    """``cwd`` (resolved) is a repo's top level a build command may run in: below the workspace, holding ``.git``,
    and neither inside nor around a directory a lineage may write (``lineage_write_dirs``, and ``intentions/`` as a
    whole: another root's lineage writes its own ``intentions/<root>/``), so no lineage can plant the
    ``.git/config``, Makefile or conftest.py that runs."""
    if cwd == root or not cwd.is_relative_to(root) or not (cwd / ".git").exists():
        return False
    dirs = [d.resolve() for d in (*writable, root / "intentions")]
    return not any(cwd.is_relative_to(d) or d.is_relative_to(cwd) for d in dirs)


def _in_repo(operand: str, repo: Path) -> bool:
    """A build command's operand stays in ``repo``: not absolute, no ``~`` or ``..`` component, and it resolves under
    ``repo`` with no hidden component (a symlink out, ``.git``), whether or not it exists yet. A git revision
    (``HEAD``, ``main..HEAD``) resolves to a name in the repo too; git and pytest reject what names nothing."""
    if operand.startswith(("/", "~")) or ".." in operand.split("/"):
        return False
    return _readable(operand.split("::", 1)[0], [repo], repo) is not None  # a pytest node id: path::Class::test


def _git_allowed(args: list[str], repo: Path) -> bool:
    """``git status|log|show|diff`` with the options in LINEAGE_GIT_READS, and operands that stay in ``repo``."""
    if not args or args[0] not in LINEAGE_GIT_READS:
        return False
    roles = LINEAGE_GIT_READS[args[0]]
    rest = args[1:]
    for i, a in enumerate(rest):
        if a == "--":
            return all(_in_repo(o, repo) for o in rest[i + 1 :])
        if a.startswith("--"):
            name, eq, _ = a.partition("=")
            if not (name in roles.flags or (eq and name in roles.values)):
                return False
        elif a.startswith("-") and len(a) > 1:
            role = roles.short.get(a[1])
            if roles.counts and a[1:].isdigit():
                continue
            if role is None or (role == "flag" and len(a) > 2) or (role == "value" and len(a) == 2):
                return False
        elif not _in_repo(a, repo):
            return False
    return True


def _build_allowed(prog: str, args: list[str], repo: Path) -> bool:
    """A build command (LINEAGE_BUILD_PROGRAMS) whose options are all in its closed map and whose paths stay in
    ``repo``. ``make`` takes only LINEAGE_MAKE_TARGETS (at least one: a bare make runs the default goal) and
    ``-k``/``-s``: no ``-f``, ``-C``, ``-I``, ``--eval`` or ``VAR=value``. ``ruff format`` needs ``--check`` or
    ``--diff``, and ``ruff check`` both ``--no-fix`` and ``--no-fix-only``, before any ``--``: a repo's ``fix`` or
    ``fix-only`` setting would otherwise write. No pytest or ruff word may start with ``@``: both read ``@file`` as a
    response file whose lines become arguments, anywhere in the command (after ``--`` too); git and make do not, so
    a git revision like ``@{u}..`` stays. No pytest word may start with ``-p``: pytest loads ``-p NAME``/``-pNAME``
    as a plugin from the raw arguments before it parses them, so after ``--`` too."""
    if prog in ("pytest", "ruff") and any(a.startswith("@") for a in args):
        return False
    if prog == "pytest" and any(a.startswith("-p") for a in args):
        return False
    if prog == "git":
        return _git_allowed(args, repo)
    if prog == "make":
        return any(a in LINEAGE_MAKE_TARGETS for a in args) and all(
            a in LINEAGE_MAKE_TARGETS or a in _MAKE_FLAGS for a in args
        )
    if prog == "ruff":
        if not args or args[0] not in _RUFF_ROLES:
            return False
        opts = set(args[: args.index("--")] if "--" in args else args)  # after `--` a word is a file
        if args[0] == "format" and not opts & {"--check", "--diff"}:
            return False
        if args[0] == "check" and not {"--no-fix", "--no-fix-only"} <= opts:  # a repo's fix/fix-only would write
            return False
        split = _split_read_args(args[1:], _RUFF_ROLES[args[0]])
    else:
        split = _split_read_args(args, _PYTEST_ROLES)
    return split is not None and all(_in_repo(o, repo) for o in split[0])


def lineage_runs_build(command: str) -> bool:
    """Whether a lineage's bash ``command`` runs a build program, so dispatch gives it the build environment (no
    GitHub token). True when the command cannot be read exactly: the scrub fails closed."""
    commands = simple_commands(command)
    if commands is None:
        return True
    return any(words and Path(words[0]).name in LINEAGE_BUILD_PROGRAMS for words, _, _, _ in commands)


def lineage_bash_refusal_hint(*, build_commands: bool) -> str:
    """What the lineage shell allows, appended to its refusal."""
    text = (
        "The lineage shell allows only: plain reads (cat, ls, head, tail, wc, grep, jq, echo, printf, pwd, cd, "
        "sleep, true) inside the workspace; read-only gh calls (pr view|checks|list|diff|status, issue view|list, "
        "run view|list, repo view, gh api GET); claude-jobs/runner.sh launch|status|list|result"
    )
    if build_commands:
        text += (
            "; and, in a git repo's top level reached by `cd <repo> &&`: git status|log|diff|show, pytest, "
            "ruff check --no-fix --no-fix-only, ruff format --check|--diff, "
            f"make {'|'.join(sorted(LINEAGE_MAKE_TARGETS))}, "
            "each with a fixed set of options and paths inside the repo, in a bash call without gh or runner.sh"
        )
    return text + ". Join commands with ; && || | only. Propose anything else to the owner with propose_action."


def lineage_bash_allowed(
    ctx: ExecutionContext, command: str, *, workspace_dir: str, build_commands: bool = False
) -> bool:
    """Whether an internal_only turn may run this bash ``command`` (F099 lineage shell).

    An allowlist, failing closed: every simple command must be a LINEAGE_READ_COMMANDS command, a read-only
    ``gh`` call, or ``<workspace_dir>/claude-jobs/runner.sh`` (the owner's Claude Code delegation path), joined
    by ``; && || |`` or newlines only. An output redirection may target ``/dev/null`` or a file under the
    root's own ``<workspace_dir>/intentions/<root_id>/`` or ``<workspace_dir>/claude-jobs/prompts/``.
    Anything the reader cannot see exactly (``$``, a backtick, a brace, ``~``, an unquoted heredoc that expands,
    an unbalanced quote) is refused, so a chain or substitution that reaches a refused command is refused. A glob
    (``*``, ``?``, ``[abc]``) is refused too, except unquoted as a whole word that is a file operand of a read
    command that takes files (``_READ_ARG_ROLES``: not a grep pattern, a jq filter or any option's value, which
    a glob expanding to more than one word would shift), expanded here as the shell will (``_glob_matches``);
    quoted, it is a literal path, as in the shell.

    Reads are confined too: a file a read command or an input redirection names, and a ``cd`` target, must
    resolve under ``<workspace_dir>`` with no hidden component (``_readable``), from the workspace (the shell's
    cwd) and from every directory an earlier ``cd`` in the command could have reached. ``CDPATH`` is not in a
    lineage's environment (``builtin_tools._LINEAGE_BASH_ENV``), so ``cd`` goes where it says.

    ``build_commands`` (``NOUS_F099_LINEAGE_BUILD_COMMANDS``) also allows LINEAGE_BUILD_PROGRAMS (``_build_allowed``)
    where the shell's cwd is known to be a repo's top level (``_build_repo``). A ``cd`` sets that cwd when it is not
    skipped by an ``||`` or run in a pipeline (a subshell), and a relative one needs the cwd known; it stays known
    only while every later join is ``&&`` or ``|``, so the command runs only if the ``cd`` succeeded. A build
    command runs the whole command without the GitHub token (``lineage_runs_build``), so it may not be joined with
    ``gh`` or ``runner.sh``, which need the token.
    """
    commands = simple_commands(command, globs=True)
    if commands is None or not any(words for words, _, _, _ in commands):
        return False
    builds = needs_token = False
    workspace = Path(workspace_dir)
    jobs = workspace / "claude-jobs"
    writable = lineage_write_dirs(ctx, workspace_dir, lineage_shell=True)  # bash is offered only with the shell
    root = workspace.resolve()
    cwds = [root]
    cwd: Path | None = root  # where the shell surely is, for a build command
    moved = False  # cwd is where a cd went: sure only while that cd's success gates what runs
    prev: str | None = None
    for words, targets, sources, op in commands:
        if op not in _LINEAGE_JOINS:
            return False
        after, prev = prev, op  # the join before this command; this one's is the next one's
        if moved and after not in ("&&", "|"):
            cwd = None  # `cd x; pytest` runs pytest where the shell was if the cd failed
        if not words:
            if targets or sources:
                return False
            continue
        if not all(t in _LINEAGE_SINKS or _under(t, writable) for t in targets):
            return False
        if not all(s in _LINEAGE_SINKS or _readable(s, cwds, root) is not None for s in sources):
            return False
        prog, args = words[0], words[1:]
        globbed = [w for w in words if isinstance(w, GlobWord)]
        if globbed and prog not in _READ_ARG_ROLES:
            return False
        if prog in _READ_ARG_ROLES or prog == "cd":
            split = _read_paths(prog, args)
            if split is None or not all(any(w is o for o in split[1]) for w in globbed):
                return False  # a glob that is not a file operand: compared by identity, a quoted '*' is equal
            reached = [
                _glob_matches(p, cwds, root) if isinstance(p, GlobWord) else _readable(p, cwds, root)
                for p in split[0] + split[1]
            ]
            if None in reached:
                return False
            if prog == "cd":
                cwds = list(dict.fromkeys([*cwds, *reached[0]]))
                if len(cwds) > _MAX_LINEAGE_CWDS:
                    return False
                base = [root] if args[0].startswith("/") else [cwd] if cwd is not None else None
                runs = after in (None, "&&", ";", "\n") and op != "|"  # not skipped, not in a subshell
                target = _readable(args[0], base, root) if runs and base is not None else None
                cwd, moved = (target[0] if target else None), True
        if prog == str(jobs / "runner.sh"):
            if not _runner_allowed(args, workspace=workspace, readable=writable):
                return False
            needs_token = True
        elif prog == "gh":
            if not _gh_allowed(args):
                return False
            needs_token = True
        elif prog == "printf":
            if not _printf_allowed(args):
                return False
        elif build_commands and prog in LINEAGE_BUILD_PROGRAMS:
            if cwd is None or not _build_repo(cwd, root, writable):
                return False
            if not _build_allowed(prog, args, cwd):
                return False
            builds = True
        elif prog not in LINEAGE_READ_COMMANDS:
            return False
    return not (builds and needs_token)


def internal_only_call_violation(
    ctx: ExecutionContext,
    name: str,
    tool_input: Mapping[str, Any],
    *,
    workspace_dir: str,
    lineage_shell: bool = False,
    build_commands: bool = False,
) -> str | None:
    """The per-call rules of an internal_only turn, for a tool that WAS offered.

    ``"external"``: ``classify_side_effect`` rates the call external (or
    irreversible), such as a URL in run_python; for bash, the command is not on
    the lineage allowlist (``lineage_bash_allowed``, with the build commands when ``build_commands``, i.e.
    ``NOUS_F099_LINEAGE_BUILD_COMMANDS``). ``"write_path"``: write_file outside
    ``lineage_write_dirs`` (``claude-jobs/prompts/`` only with ``lineage_shell``, i.e.
    ``NOUS_F099_LINEAGE_SHELL``). ``"foreign_cancel"``: cancel_task with an id that is not a
    UUID. The cancel_task lineage check needs the target's row, so it lives in the
    handler (``tools.py``); this sync function only rejects what needs no read.
    """
    if name == "bash":
        command = tool_input.get("command")
        if not isinstance(command, str) or not lineage_bash_allowed(
            ctx, command, workspace_dir=workspace_dir, build_commands=build_commands
        ):
            return "external"
        return None
    if classify_side_effect(name, dict(tool_input)) in ("external", "irreversible"):
        return "external"
    if name == "write_file" and not _write_path_allowed(ctx, tool_input, workspace_dir, lineage_shell=lineage_shell):
        return "write_path"
    if name == "cancel_task":
        try:
            uuid.UUID(str(tool_input.get("task_id", "")))
        except (TypeError, ValueError):
            return "foreign_cancel"
    return None


# Gate criterion 1 (PR #721, 2026-10-10): a heartbeat triage turn merged a PR 36 minutes after the owner parked
# it. heartbeat_triage's CONTEXT_POLICY row is local-only, but the policy mode is `warn` in prod, so the merge ran.
# heartbeat_outward_violation is a floor the runner enforces whatever the mode, as it does F099's: a triage turn
# may not do what a continuation turn would have to propose. Reads stay allowed (a triage reads PRs and the local
# API all the time); outward writes are refused: a send, a gh call that is not a read (_gh_allowed), a git push,
# and any other program the shell classifier rates external (a curl/wget/httpie write, ssh, scp, nc, aws ...).
HEARTBEAT_OUTWARD_TEXT = (
    "Tool error: a heartbeat turn may not act outward (merge, comment, push or send) on its own: that needs the "
    "owner's approval. Do not retry it in another form; tell the owner what you would do and why, and stop."
)
# What the shell walk cannot read exactly (a `$`, a glob, a brace) and run_python's code, read as text: a gh call
# with a write verb, `gh api` in any form, a git push, or a curl/wget that sends data. Narrow on purpose; the walk
# is the rule.
_OUTWARD_TEXT = re.compile(
    r"\bgh\b[\s\S]*\b(?:merge|comment|close|create|edit|review|reopen|ready|delete|rerun|lock)\b"
    r"|\bgh\s+api\b|\bgit\b[\s\S]*\bpush\b"
    r"|\b(?:curl|wget)\b[\s\S]*(?:\s-[A-Za-z]*[dFTK]|--(?:data|form|json|post|body|upload|config)"
    r"|-X\s*(?:POST|PUT|PATCH|DELETE))"
)
_SSH = re.compile(r"(?:^|[\s;&|(/'\"])ssh\s")
_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh"})
_MAX_SHELL_NESTING = 3
# curl, wget and httpie (with its `https`, `xh` and `xhs` forms) are default-deny (PR #753 review round 4): every
# call is a write unless it is a known read form, a GET or HEAD with only the options below and no body. Four
# rounds each found one more way to send a body (`http --raw`, `@file`, stdin), so the rule names the reads
# instead. A flag missing here is a false write: add it once it is known not to send anything.
_HTTP_CLIENTS = frozenset({"curl", "wget", "http", "https", "httpie", "xh", "xhs"})
_READ_METHODS = frozenset({"GET", "HEAD"})
# Per client: (short flags with no value, short flags taking a value, long flags with no value, long flags taking
# a value). The method flags (curl -X/--request, wget --method) are read by _http_client_writes itself.
_HTTP_READ_FLAGS: dict[str, tuple[str, str, frozenset[str], frozenset[str]]] = {
    "curl": (
        "sSLfiIvkgGO46",
        "HomAweruxX",
        frozenset({
            "--silent", "--show-error", "--location", "--fail", "--fail-with-body", "--include", "--head",
            "--verbose", "--insecure", "--compressed", "--globoff", "--get", "--remote-name", "--no-progress-meter",
        }),
        frozenset({
            "--header", "--output", "--max-time", "--connect-timeout", "--user-agent", "--write-out", "--referer",
            "--range", "--user", "--retry", "--max-redirs", "--url", "--request",
        }),
    ),
    "wget": (
        "qSc",
        "OTtUP",
        frozenset({"--quiet", "--spider", "--server-response", "--no-verbose", "-nv", "--no-check-certificate"}),
        frozenset({
            "--output-document", "--timeout", "--tries", "--user-agent", "--header", "--directory-prefix",
            "--max-redirect", "--method",
        }),
    ),
    "http": (
        "hbvFqdI",
        "posa",
        frozenset({
            "--headers", "--body", "--verbose", "--follow", "--check-status", "--download", "--quiet",
            "--ignore-stdin",
        }),
        frozenset({"--print", "--output", "--style", "--auth", "--timeout", "--pretty", "--verify"}),
    ),
}
# Stdin a client may send as the body: any input redirect (`< f`, `<<`, `<<<`) where a client runs, or a pipe
# into one (`echo x | curl -d @- u`, `cat f | http u`, `... | xargs curl`). Read as text: the walk drops both.
_CLIENT_WORD = r"(?:^|[\s;&|(/'\"`])(?:curl|wget|https?|httpie|xhs?)(?=[\s;&|)'\"`]|$)"
_HTTP_CLIENT_TEXT = re.compile(_CLIENT_WORD)
_PIPE_INTO_CLIENT = re.compile(r"\|&?[^;&|\n]*?" + _CLIENT_WORD)
# A httpie request item: the first separator in it decides; only a header (`H:v`) and a query (`k==v`) read.
_HTTPIE_SEP = re.compile(r":=@|:=|==|=@|=|:|@")


def _http_client_writes(prog: str, args: list[str]) -> bool:
    """Whether a curl, wget or httpie call may send something: True unless every argument is on the read-only
    allowlist (``_HTTP_READ_FLAGS``) and the method, if given, is GET or HEAD. A word the walk cannot know (``$``,
    a backtick) could be a flag, so it is a write too. httpie: ``[flags] [METHOD] URL [items]``, where an item
    other than a header or a query is a body, which also makes the call a POST."""
    kind = "http" if prog not in ("curl", "wget") else prog
    short, short_value, long_flags, long_value = _HTTP_READ_FLAGS[kind]
    positionals: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if "$" in a or "`" in a:
            return True
        if a.startswith("--") or a in long_flags:
            name, eq, value = a.partition("=")
            if name in long_flags and not eq:
                i += 1
                continue
            if name not in long_value:
                return True
            if not eq:
                if i + 1 >= len(args):
                    return True
                i += 1
                value = args[i]
            if name in ("--request", "--method") and value.upper() not in _READ_METHODS:
                return True
            i += 1
            continue
        if a.startswith("-") and a != "-":
            j = 1
            while j < len(a):
                c = a[j]
                if c in short:
                    j += 1
                    continue
                if c not in short_value:
                    return True
                value = a[j + 1 :]
                if not value:
                    if i + 1 >= len(args):
                        return True
                    i += 1
                    value = args[i]
                if kind == "curl" and c == "X" and value.upper() not in _READ_METHODS:
                    return True
                break
            i += 1
            continue
        positionals.append(a)
        i += 1
    if kind != "http":
        return False
    if len(positionals) >= 2 and positionals[0].isalpha():
        if positionals[0].upper() not in _READ_METHODS:
            return True
        positionals = positionals[1:]
    for item in positionals[1:]:
        sep = _HTTPIE_SEP.search(item)
        if sep is None or sep.group() not in (":", "=="):
            return True
    return False


# Git subcommands that cannot update a remote or run another program: an allowlist, so `send-pack`, `http-push`,
# `svn dcommit`, `p4 submit`, `rebase --exec`, an alias and anything new are refused.
_GIT_LOCAL = frozenset({
    "status", "log", "diff", "show", "fetch", "pull", "rev-parse", "rev-list", "ls-files", "ls-tree", "ls-remote",
    "blame", "grep", "describe", "shortlog", "cat-file", "show-ref", "for-each-ref", "merge-base", "name-rev",
    "diff-tree", "diff-files", "diff-index", "count-objects", "check-ignore", "whatchanged", "reflog", "request-pull",
    "add", "commit", "checkout", "switch", "restore", "merge", "stash", "tag", "init", "clone", "mv", "rm",
})
_GIT_OPTS_WITH_VALUE = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"})
_GIT_REMOTE_READ = frozenset({"-v", "--verbose", "show", "get-url"})


def _git_read_or_local(args: list[str]) -> bool:
    """Whether a ``git`` call is on the allowlist: global options skipped (``-C dir``, ``-c k=v``), then a read-only or
    local subcommand. ``branch`` and ``remote`` only when they list."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in _GIT_OPTS_WITH_VALUE else 1
    if i >= len(args):
        return True  # bare `git` or `git --version`
    sub, rest = args[i], args[i + 1 :]
    if sub == "branch":
        return all(a in ("-l", "--list", "-a", "-r", "-v", "-vv", "--show-current", "--contains") for a in rest)
    if sub == "remote":
        return all(a in _GIT_REMOTE_READ for a in rest)
    return sub in _GIT_LOCAL


def _invocation_outward(prog: str, args: list[str], depth: int) -> bool:
    """One resolved ``(program, args)`` of a shell command: ``gh`` that is not a read, ``git ... push``, a shell
    given a command string (read again, to a bounded depth), or any other program the classifier rates external
    (curl, wget, httpie when they write; ssh, scp, nc, aws, mail ... always)."""
    name = prog.rsplit("/", 1)[-1]
    if name.startswith("$") or "`" in name:
        return True  # a program the walk cannot name
    if name == "gh":
        return not _gh_allowed(args)
    if name == "git":
        return not _git_read_or_local(args)
    if name in _SHELLS or name == "eval":
        script = command_string(name, args)
        if script is None:
            return False
        return depth >= _MAX_SHELL_NESTING or _shell_outward(script, depth + 1)
    if name in _HTTP_CLIENTS:
        return _http_client_writes(name, args)
    return classify_bash_command(shlex.join([name, *args])) == "external"


def _shell_outward(command: str, depth: int = 0) -> bool:
    """Whether a shell command makes an outward write. Every simple command is read the way the side-effect
    classifier reads it (``command_invocations``: wrappers, ``env``, ``xargs``, ``bash -c`` with any flag cluster,
    ``eval``, ``;`` ``&&`` ``||`` ``|`` chains, nested strings), then judged by ``_invocation_outward``. A command it
    cannot read is judged by ``_OUTWARD_TEXT``."""
    if _SSH.search(command):
        return True  # command_invocations reads through ssh to the remote command; ssh itself is the outward act
    invocations = command_invocations(command)
    if invocations is None:
        return _OUTWARD_TEXT.search(command) is not None or _HTTP_CLIENT_TEXT.search(command) is not None
    if any(prog.rsplit("/", 1)[-1] in _HTTP_CLIENTS for prog, _ in invocations) and (
        "<" in command or _PIPE_INTO_CLIENT.search(command)
    ):
        return True  # the body may come from stdin
    return any(_invocation_outward(prog, args, depth) for prog, args in invocations)


def heartbeat_outward_violation(tool_name: str, tool_input: Mapping[str, Any]) -> bool:
    """Whether a heartbeat_triage call acts outward: a tool whose class is external or irreversible (send_email,
    send_file), a bash command the classifier rates outward that writes (``_shell_outward``), or run_python code
    that reads as one (``_OUTWARD_TEXT``)."""
    cls = tool_class(tool_name)
    if cls is not None and cls.side_effect in ("external", "irreversible"):
        return True
    if tool_name == "bash":
        command = tool_input.get("command")
        return isinstance(command, str) and _shell_outward(command)
    if tool_name == "run_python":
        code = tool_input.get("code")
        return isinstance(code, str) and _OUTWARD_TEXT.search(code) is not None
    return False


# Codex P1 on PR #773: the triage prompt says local-only, but `warn` let a triage turn call dag_create, and a DAG's
# nodes run at work levels (email, web). A triage turn may not start, schedule or re-arm work, whatever the modes.
HEARTBEAT_SPAWN_TEXT = (
    "Tool error: a heartbeat turn may not start, schedule or re-arm work (a DAG, task, schedule or check) on its "
    "own: that needs the owner's approval. Do not retry it in another form; tell the owner what you would start "
    "and why, and stop."
)
# The actions a triage turn may take on a manage tool, compared exactly as the handlers do. Anything else
# (retry_node, enable, update, an action added later) is refused, and so are a check's disable and delete: on a
# DAG node's check the DAG loop reads them as the node's completion and starts the next wave.
_HEARTBEAT_MANAGE_ACTIONS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "dag_manage": frozenset({"list", "recent", "status", "node_result", "cancel"}),
        "heartbeat_check_manage": frozenset({"list"}),
    }
)


def is_spawn_tool(tool_name: str) -> bool:
    """Whether ``tool_name`` starts other agent work (its class ``spawns``)."""
    cls = tool_class(tool_name)
    return cls is not None and cls.spawns


def heartbeat_spawn_violation(tool_name: str, tool_input: Mapping[str, Any]) -> bool:
    """Whether a heartbeat_triage call starts or delegates work: a spawn tool (dag_create, spawn_task, ...), or a
    dag_manage / heartbeat_check_manage action outside ``_HEARTBEAT_MANAGE_ACTIONS``."""
    if is_spawn_tool(tool_name):
        return True
    allowed = _HEARTBEAT_MANAGE_ACTIONS.get(tool_name)
    if allowed is None:
        return False
    action = tool_input.get("action")
    return not isinstance(action, str) or action not in allowed


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
