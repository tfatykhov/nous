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
        "{R} cancel job-20261010-222549-6e37c52b",
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
