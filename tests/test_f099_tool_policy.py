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
    assert _allowed(cont) == LINEAGE_ALLOWED | INTERNAL_ONLY_SPAWN_TOOLS
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
