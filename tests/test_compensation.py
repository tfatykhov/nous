"""Harness Phase 2.8: compensation registry, snapshots, and undoable enforcement.

Tests cover:
  1. ToolClass.compensable flag on the correct tools
  2. CompensationRegistry registration and lookup
  3. Compensator implementations (write_file, schedule, check, decision)
  4. SnapshotStore capture + mark_reverted idempotency
  5. Undoable node enforcement via tool_policy.evaluate
  6. DAG schema validation: proceed-default rules
  7. action_review builder: Revert button presence/absence
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

from nous.api.compensation import (
    CompensationRegistry,
    CompensationResult,
    compensate_write_file,
    register_compensators,
    snapshot_for_write_file,
)
from nous.api.execution_context import ExecutionContext
from nous.api.tool_classes import TOOL_CLASSES
from nous.api.tool_policy import evaluate
from nous.dag.schemas import DAGCreateRequest, DAGEdgeSpec, DAGNodeSpec


def _h(text: str) -> str:
    """The ``written_content_hash`` the runner records for ``text``."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _written(text: str) -> dict:
    """The ``written_content_hash`` + ``written_size`` the runner records for ``text``."""
    return {"written_content_hash": _h(text), "written_size": len(text.encode("utf-8"))}


# ---------------------------------------------------------------------------
# 1. ToolClass.compensable flag
# ---------------------------------------------------------------------------


def test_compensable_tools_are_marked() -> None:
    """Option A: only the clearly-reversible, non-spawning tools are compensable
    (heartbeat_check_manage per-call -- see is_compensable_call)."""
    expected = {"write_file", "heartbeat_check_manage", "resolve_decision"}
    actual = {name for name, cls in TOOL_CLASSES.items() if cls.compensable}
    assert actual == expected


def test_external_tools_are_not_compensable() -> None:
    """send_email and send_file cannot be unsent."""
    for name in ("send_email", "send_file"):
        assert not TOOL_CLASSES[name].compensable


def test_compensable_implies_non_none_side_effect() -> None:
    """A compensable tool must have a side effect (you can't undo a read)."""
    for name, cls in TOOL_CLASSES.items():
        if cls.compensable:
            assert cls.side_effect != "none", f"{name} is compensable but has no side effect"


# ---------------------------------------------------------------------------
# 2. CompensationRegistry
# ---------------------------------------------------------------------------


def test_registry_register_and_lookup() -> None:
    registry = CompensationRegistry()

    async def my_compensator(eid, data, deps):
        return CompensationResult(True, "ok")

    registry.register("write_file", my_compensator)
    assert registry.is_registered("write_file")
    assert registry.get("write_file") is my_compensator
    assert not registry.is_registered("send_email")
    assert registry.get("send_email") is None


def test_registry_rejects_non_compensable_tool() -> None:
    registry = CompensationRegistry()

    async def noop(eid, data, deps):
        return CompensationResult(True, "ok")

    with pytest.raises(ValueError, match="compensable"):
        registry.register("send_email", noop)


def test_register_compensators_registers_the_compensable_surface() -> None:
    registry = CompensationRegistry()
    register_compensators(registry)
    for name in ("write_file", "heartbeat_check_manage", "resolve_decision"):
        assert registry.is_registered(name), f"{name} not registered"
    for name in ("schedule_task", "heartbeat_check_create"):
        assert not registry.is_registered(name), f"{name} must not be revertible"


# ---------------------------------------------------------------------------
# 3. Compensator: write_file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_for_write_file_existing() -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("original content")
        path = f.name
    try:
        snap = await snapshot_for_write_file(path, "/")
        assert snap["existed"] is True
        assert snap["prior_content"] == "original content"
    finally:
        os.unlink(path)


@pytest.mark.asyncio
async def test_snapshot_for_write_file_new() -> None:
    path = os.path.join(tempfile.gettempdir(), f"test_comp_{uuid4().hex[:8]}.txt")
    assert not os.path.exists(path)
    snap = await snapshot_for_write_file(path, "/")
    assert snap["existed"] is False
    assert snap["prior_content"] is None


@pytest.mark.asyncio
async def test_compensate_write_file_restores_content() -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("new content after write")
        path = f.name

    try:
        snapshot_data = {
            "path": path,
            "full_path": path,
            "existed": True,
            "prior_content": "original content",
            **_written("new content after write"),
        }
        result = await compensate_write_file(uuid4(), snapshot_data, None)
        assert result.success
        with open(path) as f:
            assert f.read() == "original content"
    finally:
        if os.path.exists(path):
            os.unlink(path)


@pytest.mark.asyncio
async def test_compensate_write_file_deletes_new_file() -> None:
    path = os.path.join(tempfile.gettempdir(), f"test_comp_new_{uuid4().hex[:8]}.txt")
    with open(path, "w") as f:
        f.write("was created by write_file")

    snapshot_data = {
        "path": path,
        "full_path": path,
        "existed": False,
        "prior_content": None,
        **_written("was created by write_file"),
    }
    result = await compensate_write_file(uuid4(), snapshot_data, None)
    assert result.success
    assert not os.path.exists(path)


@pytest.mark.asyncio
async def test_compensate_write_file_already_absent() -> None:
    path = os.path.join(tempfile.gettempdir(), f"test_comp_gone_{uuid4().hex[:8]}.txt")
    snapshot_data = {
        "path": path,
        "full_path": path,
        "existed": False,
        "prior_content": None,
        **_written("x"),
    }
    result = await compensate_write_file(uuid4(), snapshot_data, None)
    assert result.success
    assert "already absent" in result.message


@pytest.mark.asyncio
async def test_compensate_write_file_refuses_without_written_hash() -> None:
    """A snapshot that does not record what was written cannot tell our write
    from a newer one: refuse instead of overwriting the file."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("someone's newer edit")
        path = f.name
    try:
        snapshot_data = {"path": path, "full_path": path, "existed": True, "prior_content": "old"}
        result = await compensate_write_file(uuid4(), snapshot_data, None)
        assert result.success is False
        with open(path) as f:
            assert f.read() == "someone's newer edit"
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# 5. Tool policy: not_compensable violation
# ---------------------------------------------------------------------------


def test_undoable_node_allows_compensable_tool() -> None:
    ctx = ExecutionContext(kind="dag_node", undoable=True)
    result = evaluate(ctx, "write_file", {"path": "/tmp/x", "content": "y"})
    assert result is None


def test_undoable_node_blocks_non_compensable_tool() -> None:
    ctx = ExecutionContext(kind="dag_node", undoable=True)
    result = evaluate(ctx, "learn_fact", {"content": "x", "category": "preference"})
    assert result == "not_compensable"


def test_non_undoable_node_allows_non_compensable_tool() -> None:
    ctx = ExecutionContext(kind="dag_node", undoable=False)
    result = evaluate(ctx, "learn_fact", {"content": "x", "category": "preference"})
    assert result is None


def test_undoable_allows_reads() -> None:
    ctx = ExecutionContext(kind="dag_node", undoable=True)
    result = evaluate(ctx, "recall_deep", {"query": "test"})
    assert result is None


# ---------------------------------------------------------------------------
# 6. DAG schema: proceed-default rules
# ---------------------------------------------------------------------------


def _approval_node(
    name: str = "ask",
    default_outcome: str = "stop",
    undoable_successor: bool = False,
) -> tuple[list[DAGNodeSpec], list[DAGEdgeSpec]]:
    """Build a minimal approval + acting node graph."""
    nodes = [
        DAGNodeSpec(
            name="draft",
            type="subtask",
            instructions="Draft something",
            undoable=undoable_successor,
        ),
        DAGNodeSpec(
            name=name,
            type="approval",
            instructions="Do you approve?",
            options=[
                {"id": "yes", "label": "Yes", "outcome": "proceed"},
                {"id": "no", "label": "No", "outcome": "stop"},
            ],
            default_option="yes" if default_outcome == "proceed" else "no",
        ),
        DAGNodeSpec(
            name="act",
            type="subtask",
            instructions="Do the thing",
            undoable=undoable_successor,
        ),
    ]
    edges = [
        DAGEdgeSpec(from_node="draft", to_node=name, edge_type="context_flow"),
        DAGEdgeSpec(from_node=name, to_node="act", edge_type="context_flow"),
    ]
    return nodes, edges


def _mock_settings(**overrides):
    """Patch nous.config.Settings for DAG schema validation."""
    defaults = {
        "dag_approval_proceed_default_enabled": False,
        "dag_workspace_safety_enabled": False,
    }
    defaults.update(overrides)
    return patch("nous.config.Settings", return_value=SimpleNamespace(**defaults))


def test_proceed_default_rejected_when_flag_off() -> None:
    """Without the flag, a proceed default is always rejected."""
    nodes, edges = _approval_node(default_outcome="proceed", undoable_successor=True)
    with _mock_settings(dag_approval_proceed_default_enabled=False):
        with pytest.raises(ValueError, match="stop.*option|proceed"):
            DAGCreateRequest(name="test", nodes=nodes, edges=edges)


def test_proceed_default_accepted_when_flag_on_and_all_undoable() -> None:
    """With the flag and undoable downstream nodes, proceed default is OK."""
    nodes, edges = _approval_node(default_outcome="proceed", undoable_successor=True)
    with _mock_settings(dag_approval_proceed_default_enabled=True):
        dag = DAGCreateRequest(name="test", nodes=nodes, edges=edges)
        assert len(dag.nodes) == 3


def test_proceed_default_rejected_when_downstream_not_undoable() -> None:
    """With the flag but non-undoable downstream, proceed default is rejected."""
    nodes, edges = _approval_node(default_outcome="proceed", undoable_successor=False)
    with _mock_settings(dag_approval_proceed_default_enabled=True):
        with pytest.raises(ValueError, match="not declared undoable"):
            DAGCreateRequest(name="test", nodes=nodes, edges=edges)


def test_stop_default_still_works() -> None:
    """A stop default continues to work regardless of flags."""
    nodes, edges = _approval_node(default_outcome="stop", undoable_successor=False)
    with _mock_settings(dag_approval_proceed_default_enabled=False):
        dag = DAGCreateRequest(name="test", nodes=nodes, edges=edges)
        assert len(dag.nodes) == 3


# ---------------------------------------------------------------------------
# 7. action_review builder: Revert button
# ---------------------------------------------------------------------------


def test_action_review_shows_revert_when_revertible_and_handler() -> None:
    from nous.a2ui.builders.action_review import action_review

    built = action_review(
        {
            "title": "Wrote config file",
            "did": "Created /workspace/config.yaml",
            "trace_id": str(uuid4()),
            "compensation": {"revertible": True, "handler": "write_file", "note": ""},
        }
    )
    assert "review.revert" in built.allowed_actions
    component_ids = [c["id"] for c in built.components]
    assert "revert" in component_ids


def test_action_review_no_revert_when_not_revertible() -> None:
    from nous.a2ui.builders.action_review import action_review

    built = action_review(
        {
            "title": "Sent email",
            "did": "Sent report to user",
            "compensation": {"revertible": False, "handler": None, "note": "Cannot unsend"},
        }
    )
    assert "review.revert" not in built.allowed_actions
    component_ids = [c["id"] for c in built.components]
    assert "revert" not in component_ids


def test_action_review_no_revert_when_revertible_but_no_handler() -> None:
    """revertible=True but handler=None: no Revert button (nothing to call)."""
    from nous.a2ui.builders.action_review import action_review

    built = action_review(
        {
            "title": "Some action",
            "did": "Did something",
            "compensation": {"revertible": True, "handler": None, "note": ""},
        }
    )
    assert "review.revert" not in built.allowed_actions


def test_action_review_defaults_to_not_revertible() -> None:
    from nous.a2ui.builders.action_review import action_review

    built = action_review({"title": "Action", "did": "Did it"})
    assert "review.revert" not in built.allowed_actions


# ---------------------------------------------------------------------------
# 8. ExecutionContext.for_subtask threads undoable
# ---------------------------------------------------------------------------


def test_execution_context_for_subtask_reads_undoable() -> None:
    subtask = SimpleNamespace(
        id=uuid4(),
        parent_session_id="parent-1",
        dag_node_id=uuid4(),
        metadata_={"dag_id": str(uuid4()), "node_name": "act", "undoable": True},
    )
    ctx = ExecutionContext.for_subtask(subtask, "session-1")
    assert ctx.undoable is True
    assert ctx.kind == "dag_node"


def test_execution_context_for_subtask_defaults_undoable_false() -> None:
    subtask = SimpleNamespace(
        id=uuid4(),
        parent_session_id="parent-1",
        dag_node_id=uuid4(),
        metadata_={"dag_id": str(uuid4()), "node_name": "act"},
    )
    ctx = ExecutionContext.for_subtask(subtask, "session-1")
    assert ctx.undoable is False


# ---------------------------------------------------------------------------
# Regression tests for Codex findings (must FAIL before the fix)
# ---------------------------------------------------------------------------


# Finding #1 — config.py: _validate_compensation_dependencies
# dag_approval_proceed_default_enabled=True requires compensation_enabled=True.
# Before the fix there was no cross-field validator so this combination was
# silently accepted.


def test_proceed_default_enabled_requires_compensation_enabled() -> None:
    """Setting proceed_default=True without compensation_enabled=True must fail."""
    from pydantic import ValidationError

    from nous.config import Settings

    with pytest.raises(ValidationError, match="compensation_enabled"):
        Settings(
            _env_file=None,
            ANTHROPIC_API_KEY="test-key",
            dag_approval_nodes_enabled=True,
            dag_approval_proceed_default_enabled=True,
            compensation_enabled=False,
        )


def test_proceed_default_enabled_with_compensation_enabled_is_ok() -> None:
    """When compensation is also on, the combination is valid."""
    from nous.config import Settings

    s = Settings(
        _env_file=None,
        ANTHROPIC_API_KEY="test-key",
        dag_approval_nodes_enabled=True,
        dag_approval_proceed_default_enabled=True,
        compensation_enabled=True,
        compensation_auto_review_enabled=True,
    )
    assert s.dag_approval_proceed_default_enabled is True


# Finding #3 — a2ui/actions.py: review.revert must NOT call mark_reverted on failure
# Before the fix mark_reverted was called unconditionally, permanently locking
# out retries even when the compensator returned success=False.


@pytest.mark.asyncio
async def test_review_revert_does_not_mark_reverted_on_failure() -> None:
    """A failed compensation must not set reverted_at (so retry is possible)."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock
    from uuid import uuid4 as _uuid4

    from nous.a2ui.actions import ActionContext, ActionRouter
    from nous.api.compensation import CompensationRegistry, CompensationResult

    # Build a minimal ActionRouter with compensation wired.
    registry = CompensationRegistry()

    async def failing_compensator(eid, snap_data, deps):
        return CompensationResult(success=False, message="disk full")

    # Temporarily allow registering write_file (it IS compensable).
    registry.register("write_file", failing_compensator)

    entry_id = _uuid4()
    snap_id = _uuid4()
    fake_snapshot = SimpleNamespace(
        id=snap_id,
        tool_name="write_file",
        snapshot_data={"path": "/tmp/x", "full_path": "/tmp/x", "existed": False, "prior_content": None},
        reverted_at=None,
    )

    snap_store = MagicMock()
    snap_store.get_by_ledger_entry = AsyncMock(return_value=fake_snapshot)
    snap_store.mark_reverted = AsyncMock()

    settings = SimpleNamespace(a2ui_action_rate_per_minute=100, a2ui_trust_forwarded_identity=False)
    router = ActionRouter(
        database=None,
        settings=settings,
        surface_service=None,
        compensation_registry=registry,
        snapshot_store=snap_store,
    )

    handler_meta = router._handlers["review.revert"]
    surface = SimpleNamespace(trace_id=str(entry_id), surface_id="surf-1", data_model={})
    ctx = ActionContext(surface=surface, name="review.revert", context={}, data_model={}, services=router)

    result = await handler_meta.fn(ctx)

    # The compensator returned failure → mark_reverted must NOT be called.
    snap_store.mark_reverted.assert_not_called()
    assert result.ok is False
    assert "disk full" in result.message


@pytest.mark.asyncio
async def test_review_revert_marks_reverted_on_success() -> None:
    """A successful compensation must set reverted_at exactly once."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock
    from uuid import uuid4 as _uuid4

    from nous.a2ui.actions import ActionContext, ActionRouter
    from nous.api.compensation import CompensationRegistry, CompensationResult

    registry = CompensationRegistry()

    async def ok_compensator(eid, snap_data, deps):
        return CompensationResult(success=True, message="restored")

    registry.register("write_file", ok_compensator)

    entry_id = _uuid4()
    snap_id = _uuid4()
    fake_snapshot = SimpleNamespace(
        id=snap_id,
        tool_name="write_file",
        snapshot_data={"path": "/tmp/x", "full_path": "/tmp/x", "existed": False, "prior_content": None},
        reverted_at=None,
    )

    snap_store = MagicMock()
    snap_store.get_by_ledger_entry = AsyncMock(return_value=fake_snapshot)
    snap_store.mark_reverted = AsyncMock()

    settings = SimpleNamespace(a2ui_action_rate_per_minute=100, a2ui_trust_forwarded_identity=False)
    router = ActionRouter(
        database=None,
        settings=settings,
        surface_service=None,
        compensation_registry=registry,
        snapshot_store=snap_store,
    )

    handler_meta = router._handlers["review.revert"]
    surface = SimpleNamespace(trace_id=str(entry_id), surface_id="surf-1", data_model={})
    ctx = ActionContext(surface=surface, name="review.revert", context={}, data_model={}, services=router)

    result = await handler_meta.fn(ctx)

    snap_store.mark_reverted.assert_called_once_with(snap_id, result_message="restored")
    assert result.ok is True


# Finding #4 — compensation.py: recreate file when existed=True but now absent
# Before the fix the compensator returned success ("already absent") even when the
# file had existed, silently dropping the prior content instead of restoring it.


@pytest.mark.asyncio
async def test_compensate_write_file_refuses_when_removed_after_write() -> None:
    """Our write left the file present; its absence is a NEWER change (someone
    deleted it), so the revert must not resurrect the old content over it."""
    path = os.path.join(tempfile.gettempdir(), f"test_comp_recreate_{uuid4().hex[:8]}.txt")
    assert not os.path.exists(path)

    snapshot_data = {
        "path": path,
        "full_path": path,
        "existed": True,
        "prior_content": "the original content",
        **_written("what write_file wrote"),
    }
    result = await compensate_write_file(uuid4(), snapshot_data, None)
    try:
        assert result.success is False
        assert "removed after the original write" in result.message
        assert not os.path.exists(path)
    finally:
        if os.path.exists(path):
            os.unlink(path)


@pytest.mark.asyncio
async def test_compensate_write_file_fails_when_existed_no_prior_content() -> None:
    """File existed but prior_content not captured → compensator must fail (not silently succeed)."""
    path = os.path.join(tempfile.gettempdir(), f"test_comp_noprior_{uuid4().hex[:8]}.txt")
    with open(path, "w") as f:
        f.write("written")

    snapshot_data = {
        "path": path,
        "full_path": path,
        "existed": True,
        "prior_content": None,  # capture was not possible
        **_written("written"),
    }
    try:
        result = await compensate_write_file(uuid4(), snapshot_data, None)
        assert result.success is False
        assert "prior content not captured" in result.message
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Codex findings — new regression tests (must FAIL before the fix)
# ---------------------------------------------------------------------------


# Finding #1 — dag/schemas.py: check nodes must be included in proceed-default
# validation. Before the fix, check nodes were silently ignored and a graph
# with approval → check (no undoable) was accepted.


def _check_node(name: str = "check_step", **overrides):  # type: ignore[return]
    from nous.dag.schemas import DAGNodeSpec, DAGNodeType

    base = dict(name=name, type=DAGNodeType.check, instructions="Run a check")
    base.update(overrides)
    return DAGNodeSpec(**base)


def test_check_node_downstream_of_proceed_default_requires_undoable() -> None:
    """A check node downstream of a proceed-default approval must be undoable."""
    from nous.dag.schemas import DAGCreateRequest, DAGEdgeSpec, DAGNodeSpec, DAGNodeType

    nodes = [
        DAGNodeSpec(
            name="approve",
            type=DAGNodeType.approval,
            instructions="Do you want to run the check?",
            options=[
                {"id": "yes", "label": "Yes", "outcome": "proceed"},
                {"id": "no", "label": "No", "outcome": "stop"},
            ],
            default_option="yes",
        ),
        _check_node(undoable=False),  # NOT declared undoable
    ]
    edges = [DAGEdgeSpec(from_node="approve", to_node="check_step", edge_type="context_flow")]
    with _mock_settings(dag_approval_proceed_default_enabled=True):
        with pytest.raises(ValueError, match="not declared undoable"):
            DAGCreateRequest(name="check_test", nodes=nodes, edges=edges)


def test_check_node_downstream_of_proceed_default_accepted_when_undoable() -> None:
    """A check node declared undoable satisfies the proceed-default requirement."""
    from nous.dag.schemas import DAGCreateRequest, DAGEdgeSpec, DAGNodeSpec, DAGNodeType

    nodes = [
        DAGNodeSpec(
            name="approve",
            type=DAGNodeType.approval,
            instructions="Do you want to run the check?",
            options=[
                {"id": "yes", "label": "Yes", "outcome": "proceed"},
                {"id": "no", "label": "No", "outcome": "stop"},
            ],
            default_option="yes",
        ),
        _check_node(undoable=True),  # properly declared
    ]
    edges = [DAGEdgeSpec(from_node="approve", to_node="check_step", edge_type="context_flow")]
    with _mock_settings(dag_approval_proceed_default_enabled=True):
        dag = DAGCreateRequest(name="check_test", nodes=nodes, edges=edges)
        assert len(dag.nodes) == 2


# Finding #2 — config.py: persist ledger required for compensation.
# Before the fix there was no check for execution_ledger_persist_enabled.


def test_proceed_default_requires_ledger_persist_enabled() -> None:
    """proceed_default + compensation + no ledger persistence must fail at Settings."""
    from pydantic import ValidationError

    from nous.config import Settings

    with pytest.raises(ValidationError, match="execution_ledger_persist_enabled"):
        Settings(
            _env_file=None,
            ANTHROPIC_API_KEY="test-key",
            dag_approval_nodes_enabled=True,
            dag_approval_proceed_default_enabled=True,
            compensation_enabled=True,
            execution_ledger_persist_enabled=False,
        )


def test_proceed_default_with_ledger_persist_and_compensation_is_ok() -> None:
    """All three flags on together should succeed."""
    from nous.config import Settings

    s = Settings(
        _env_file=None,
        ANTHROPIC_API_KEY="test-key",
        dag_approval_nodes_enabled=True,
        dag_approval_proceed_default_enabled=True,
        compensation_enabled=True,
        compensation_auto_review_enabled=True,
        execution_ledger_persist_enabled=True,
    )
    assert s.dag_approval_proceed_default_enabled is True


# Finding #3 — compensation.py: stale-revert guard.
# Before the fix, compensate_write_file would overwrite newer contents without checking.


@pytest.mark.asyncio
async def test_compensate_write_file_refuses_stale_revert() -> None:
    """If the file was modified after the original write, revert must be refused."""
    import hashlib

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("content written by a LATER edit — not by write_file")
        path = f.name

    try:
        # Simulate: write_file wrote "write_file content"; hash is recorded.
        written_content = "content written by write_file"
        written_hash = hashlib.sha256(written_content.encode("utf-8")).hexdigest()

        snapshot_data = {
            "path": path,
            "full_path": path,
            "existed": True,
            "prior_content": "original content",
            "written_content_hash": written_hash,
            "written_size": len(written_content.encode("utf-8")),
        }
        result = await compensate_write_file(uuid4(), snapshot_data, None)
        assert result.success is False
        assert "modified after" in result.message or "newer content" in result.message
    finally:
        if os.path.exists(path):
            os.unlink(path)


@pytest.mark.asyncio
async def test_compensate_write_file_proceeds_when_hash_matches() -> None:
    """When the file still matches what write_file wrote, the revert should succeed."""
    import hashlib

    written_content = "content written by write_file"

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(written_content)
        path = f.name

    try:
        written_hash = hashlib.sha256(written_content.encode("utf-8")).hexdigest()
        snapshot_data = {
            "path": path,
            "full_path": path,
            "existed": True,
            "prior_content": "original content",
            "written_content_hash": written_hash,
            "written_size": len(written_content.encode("utf-8")),
        }
        result = await compensate_write_file(uuid4(), snapshot_data, None)
        assert result.success
        with open(path) as f2:
            assert f2.read() == "original content"
    finally:
        if os.path.exists(path):
            os.unlink(path)


# Finding #4 — compensation.py: oversized file snapshot.
# Before the fix, snapshot_for_write_file ran a synchronous f.read() and had no size cap.


@pytest.mark.asyncio
async def test_snapshot_for_write_file_flags_oversized() -> None:
    """Files larger than _FILE_SNAPSHOT_MAX_BYTES must return oversized=True."""
    from nous.api.compensation import _FILE_SNAPSHOT_MAX_BYTES, snapshot_for_write_file

    with tempfile.NamedTemporaryFile(mode="wb", suffix=".bin", delete=False) as f:
        # Write a file one byte over the limit.
        f.write(b"x" * (_FILE_SNAPSHOT_MAX_BYTES + 1))
        path = f.name

    try:
        snap = await snapshot_for_write_file(path, "/")
        assert snap["oversized"] is True
        assert snap["prior_content"] is None
        assert snap["existed"] is True
    finally:
        os.unlink(path)


@pytest.mark.asyncio
async def test_snapshot_for_write_file_reads_small_file() -> None:
    """Files within the size cap must be read normally and not flagged oversized."""
    from nous.api.compensation import snapshot_for_write_file

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("small content")
        path = f.name

    try:
        snap = await snapshot_for_write_file(path, "/")
        assert snap["oversized"] is False
        assert snap["prior_content"] == "small content"
    finally:
        os.unlink(path)


@pytest.mark.asyncio
async def test_capture_compensation_snapshot_raises_for_oversized_undoable() -> None:
    """_capture_compensation_snapshot raises SnapshotBlocksDispatch for oversized+undoable."""
    from unittest.mock import AsyncMock, patch

    from nous.api.compensation import SnapshotBlocksDispatch
    from nous.api.execution_context import ExecutionContext
    from nous.api.runner import AgentRunner

    # Bypass __init__ — we only need the few attributes the method reads.
    runner = object.__new__(AgentRunner)
    runner._snap_store = AsyncMock()
    runner._workspace_dir = "/"
    runner._dispatcher = SimpleNamespace()  # no repaired_args: input passes through

    ctx = ExecutionContext(kind="dag_node", undoable=True)

    oversized_snap = {
        "path": "big.bin",
        "full_path": "/big.bin",
        "existed": True,
        "prior_content": None,
        "oversized": True,
    }

    with patch(
        "nous.api.compensation.snapshot_for_write_file",
        new=AsyncMock(return_value=oversized_snap),
    ):
        with pytest.raises(SnapshotBlocksDispatch):
            await runner._capture_compensation_snapshot(
                ctx,
                "write_file",
                {"path": "big.bin", "content": "new content"},
                uuid4(),
            )


@pytest.mark.asyncio
async def test_capture_compensation_snapshot_uses_repaired_args() -> None:
    """codex P1 on #652: a `path` salvaged from leaked XML at the end of
    `content` is what dispatch writes to -- the snapshot must record THAT path
    and the trimmed payload's hash, or the revert can never match the write."""
    import hashlib
    from unittest.mock import AsyncMock

    from nous.api.runner import AgentRunner
    from nous.api.tools import ToolDispatcher

    dispatcher = ToolDispatcher()
    dispatcher.register(
        "write_file",
        lambda **_: None,
        {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    )
    runner = object.__new__(AgentRunner)
    runner._snap_store = AsyncMock()
    runner._workspace_dir = "/ws"
    runner._dispatcher = dispatcher

    snap = AsyncMock(return_value={"path": "notes.txt", "existed": False, "prior_content": None, "oversized": False})
    with patch("nous.api.compensation.snapshot_for_write_file", new=snap):
        captured = await runner._capture_compensation_snapshot(
            ExecutionContext(kind="dag_node", undoable=True),
            "write_file",
            {"content": 'hello world</content>\n<parameter name="path">notes.txt'},
            uuid4(),
        )
    assert captured is True
    snap.assert_awaited_once_with("notes.txt", "/ws")
    data = runner._snap_store.capture.await_args.kwargs["snapshot_data"]
    assert data["written_content_hash"] == hashlib.sha256(b"hello world").hexdigest()


@pytest.mark.asyncio
async def test_action_review_pusher_publishes_a_revertible_card() -> None:
    """codex P2 on #652: the auto-review callback publishes an action_review
    card whose Revert eligibility is derived server-side from the snapshot."""
    from unittest.mock import AsyncMock

    from nous.a2ui.tools import make_action_review_pusher

    entry_id = uuid4()
    snap_store = SimpleNamespace(
        get_by_ledger_entry=AsyncMock(return_value=SimpleNamespace(tool_name="write_file", reverted_at=None))
    )
    registry = CompensationRegistry()
    register_compensators(registry)
    service = SimpleNamespace(push_built=AsyncMock(return_value="surf-1"))

    push = make_action_review_pusher(service, snap_store, registry)
    assert await push("write_file", entry_id, "s1") == "surf-1"
    built = service.push_built.await_args.args[0]
    kwargs = service.push_built.await_args.kwargs
    assert kwargs["dedup_key"] == f"review:{entry_id}" and kwargs["session_id"] == "s1"
    assert "review.revert" in built.allowed_actions
    assert built.trace_id == str(entry_id)

    # A snapshot already reverted: the card is still published, without Revert.
    snap_store.get_by_ledger_entry.return_value = SimpleNamespace(tool_name="write_file", reverted_at=object())
    await push("write_file", entry_id, "s1")
    assert "review.revert" not in service.push_built.await_args.args[0].allowed_actions


@pytest.mark.parametrize("off", [{"a2ui_enabled": False}, {"execution_ledger_persist_enabled": False}])
def test_auto_review_requires_a2ui_and_persisted_ledger(off) -> None:
    """Without A2UI there is no card to push and without a persisted ledger no
    snapshot exists, so the flag would be silently inert: refuse at startup."""
    from pydantic import ValidationError

    from nous.config import Settings

    with pytest.raises(ValidationError, match="compensation_enabled=True requires"):
        Settings(
            _env_file=None,
            ANTHROPIC_API_KEY="test-key",
            compensation_enabled=True,
            compensation_auto_review_enabled=True,
            **off,
        )


# ---------------------------------------------------------------------------
# Option A: spawning tools are not compensable; heartbeat_check_manage is
# compensable for action="disable" only.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", ["schedule_task", "heartbeat_check_create"])
def test_spawning_tools_are_not_compensable(tool) -> None:
    """Cancelling a schedule/check after it fired does not undo the subtask it
    spawned, so neither may run on an undoable node."""
    assert not TOOL_CLASSES[tool].compensable
    with pytest.raises(ValueError, match="compensable"):
        CompensationRegistry().register(tool, compensate_write_file)
    ctx = ExecutionContext(kind="dag_node", undoable=True)
    assert evaluate(ctx, tool, {"name": "x", "task": "t"}) == "not_compensable"


@pytest.mark.parametrize("tool", ["schedule_task", "heartbeat_check_create"])
def test_undoable_node_refuses_spawning_tool_even_in_warn_mode(tool) -> None:
    """The existing not_compensable force-block refuses them at the choke point."""
    from test_runner_authorization import _runner

    from nous.api.runner import Refusal

    r, _ = _runner([tool], tool_context_policy_mode="warn")
    ctx = ExecutionContext(kind="dag_node", undoable=True, session_id="s1")
    refusal = r._authorize_tool_call(ctx, tool, {tool}, "s1", {"name": "x"})
    assert isinstance(refusal, Refusal) and "not_compensable" in refusal.text


@pytest.mark.parametrize("action", ["enable", "update", "delete", "Disable", None])
def test_heartbeat_check_manage_only_disable_is_compensable(action) -> None:
    from nous.api.tool_classes import is_compensable_call

    inp = {"name": "c"} if action is None else {"name": "c", "action": action}
    assert not is_compensable_call("heartbeat_check_manage", inp)
    ctx = ExecutionContext(kind="dag_node", undoable=True)
    assert evaluate(ctx, "heartbeat_check_manage", inp) == "not_compensable"


def test_heartbeat_check_manage_disable_allowed_on_undoable_node() -> None:
    from nous.api.tool_classes import is_compensable_call

    inp = {"name": "c", "action": "disable"}
    assert is_compensable_call("heartbeat_check_manage", inp)
    assert evaluate(ExecutionContext(kind="dag_node", undoable=True), "heartbeat_check_manage", inp) is None


@pytest.mark.asyncio
async def test_compensate_heartbeat_check_manage_re_enables_a_disabled_check() -> None:
    """The loader signature is manage_check(action, name=...)."""
    from unittest.mock import AsyncMock

    from nous.api.compensation import compensate_heartbeat_check_manage

    loader = SimpleNamespace(manage_check=AsyncMock(return_value={"status": "enabled"}))
    deps = SimpleNamespace(heartbeat_loader=loader)
    res = await compensate_heartbeat_check_manage(
        uuid4(), {"check_name": "c", "action": "disable", "prior_enabled": True}, deps
    )
    assert res.success
    loader.manage_check.assert_awaited_once_with("enable", name="c")

    loader.manage_check.reset_mock()
    res = await compensate_heartbeat_check_manage(
        uuid4(), {"check_name": "c", "action": "enable", "prior_enabled": True}, deps
    )
    assert not res.success
    loader.manage_check.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("inp", "expected"),
    [({"name": "c", "action": "disable"}, True), ({"name": "c", "action": "enable"}, False)],
)
async def test_capture_snapshots_heartbeat_check_manage_disable_only(inp, expected) -> None:
    from unittest.mock import AsyncMock

    from nous.api.runner import AgentRunner

    runner = object.__new__(AgentRunner)
    runner._snap_store = AsyncMock()
    runner._workspace_dir = "/"
    runner._dispatcher = SimpleNamespace()
    got = await runner._capture_compensation_snapshot(
        ExecutionContext(kind="subtask"), "heartbeat_check_manage", inp, uuid4()
    )
    assert got is expected
    assert runner._snap_store.capture.await_count == int(expected)


# ---------------------------------------------------------------------------
# codex P2 (runner.py:525): background contexts snapshot regardless of undoable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capture_snapshot_in_background_non_undoable_context() -> None:
    """Before the fix, `if not ctx.undoable: return False` meant a write_file
    from a subtask/scheduled/heartbeat turn was never snapshotted."""
    from unittest.mock import AsyncMock

    from nous.api.runner import AgentRunner

    runner = object.__new__(AgentRunner)
    runner._snap_store = AsyncMock()
    runner._workspace_dir = tempfile.gettempdir()
    runner._dispatcher = SimpleNamespace()
    inp = {"path": f"p2_{uuid4().hex[:8]}.txt", "content": "x"}

    for kind in ("subtask", "scheduled", "heartbeat_callback", "background"):
        assert await runner._capture_compensation_snapshot(ExecutionContext(kind=kind), "write_file", inp, uuid4())
    # a foreground turn has a human in the loop: unchanged, no snapshot
    assert not await runner._capture_compensation_snapshot(
        ExecutionContext(kind="interactive"), "write_file", inp, uuid4()
    )
    assert runner._snap_store.capture.await_count == 4


@pytest.mark.asyncio
async def test_oversized_write_blocks_only_when_undoable() -> None:
    """A non-undoable background write of a big file proceeds without a
    snapshot (fail-open) instead of being newly refused."""
    from unittest.mock import AsyncMock

    from nous.api.runner import AgentRunner

    runner = object.__new__(AgentRunner)
    runner._snap_store = AsyncMock()
    runner._workspace_dir = "/"
    runner._dispatcher = SimpleNamespace()
    big = {"path": "big.bin", "full_path": "/big.bin", "existed": True, "prior_content": None, "oversized": True}
    with patch("nous.api.compensation.snapshot_for_write_file", new=AsyncMock(return_value=big)):
        got = await runner._capture_compensation_snapshot(
            ExecutionContext(kind="subtask"), "write_file", {"path": "big.bin", "content": "y"}, uuid4()
        )
    assert got is False
    runner._snap_store.capture.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "background", "pushed"), [("subtask", True, 1), ("interactive", False, 0)])
async def test_background_non_undoable_write_snapshots_and_pushes_review_once(kind, background, pushed) -> None:
    """End to end through _tool_loop: one snapshot and one action_review card
    per compensable background call, none for a foreground call."""
    from unittest.mock import AsyncMock

    from test_runner_authorization import _one_tool_call_then_done_with
    from test_runner_ledger import _FakeStore, _run_loop
    from test_runner_ledger import _runner as _ledger_runner

    store = _FakeStore()
    r, _ = _ledger_runner(store, compensation_enabled=True, compensation_auto_review_enabled=True)
    snap_store = AsyncMock()
    r.set_snapshot_store(snap_store, tempfile.gettempdir())
    pusher = AsyncMock()
    r.set_action_review_pusher(pusher)
    r._call_api = _one_tool_call_then_done_with("write_file", {"path": f"p2_{uuid4().hex[:8]}.txt", "content": "x"})
    await _run_loop(r, is_background=background, context=ExecutionContext(kind=kind, session_id="s1"))

    assert snap_store.capture.await_count == pushed
    assert pusher.await_count == pushed
    if pushed:
        assert pusher.await_args.args[:2] == ("write_file", "id-write_file")


# ---------------------------------------------------------------------------
# codex P2 (compensation.py:269): a no-op disable reverts to disabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("snap", "success", "enabled"),
    [
        ({"prior_enabled": True}, True, True),
        ({"prior_enabled": False}, True, False),  # already disabled: left disabled
        ({"prior_enabled": None}, False, False),  # unknown: never enable
        ({}, False, False),  # not recorded: never enable
    ],
)
async def test_revert_check_disable_restores_prior_enabled_state(snap, success, enabled) -> None:
    """Before the fix every revert called manage_check("enable"), starting a
    check that was already disabled when the compensated call ran."""
    from unittest.mock import AsyncMock

    from nous.api.compensation import compensate_heartbeat_check_manage

    loader = SimpleNamespace(manage_check=AsyncMock())
    res = await compensate_heartbeat_check_manage(
        uuid4(), {"check_name": "c", "action": "disable", **snap}, SimpleNamespace(heartbeat_loader=loader)
    )
    assert res.success is success
    assert loader.manage_check.await_count == int(enabled)


@pytest.mark.asyncio
@pytest.mark.parametrize(("state", "recorded"), [(True, True), (False, False), (None, None), (RuntimeError, None)])
async def test_capture_records_prior_enabled_state_of_the_check(state, recorded) -> None:
    from unittest.mock import AsyncMock

    from nous.api.runner import AgentRunner

    runner = object.__new__(AgentRunner)
    runner._snap_store = AsyncMock()
    if state is RuntimeError:
        runner._snap_store.check_enabled.side_effect = RuntimeError("db down")
    else:
        runner._snap_store.check_enabled.return_value = state
    runner._workspace_dir = "/"
    runner._dispatcher = SimpleNamespace()
    assert await runner._capture_compensation_snapshot(
        ExecutionContext(kind="subtask"), "heartbeat_check_manage", {"name": "c", "action": "disable"}, uuid4()
    )
    runner._snap_store.check_enabled.assert_awaited_once_with("c")
    assert runner._snap_store.capture.await_args.kwargs["snapshot_data"] == {
        "check_name": "c",
        "action": "disable",
        "prior_enabled": recorded,
    }


@pytest.mark.asyncio
async def test_review_revert_passes_the_heartbeat_dynamic_loader() -> None:
    """HeartbeatRunner exposes ``dynamic_loader``; reading ``_loader`` gave
    the compensator None, so no check revert could ever run."""
    from unittest.mock import AsyncMock, MagicMock

    from nous.a2ui.actions import ActionContext, ActionRouter
    from nous.api.compensation import CompensationRegistry, register_compensators

    registry = CompensationRegistry()
    register_compensators(registry)
    entry_id = uuid4()
    snap_store = MagicMock()
    snap_store.get_by_ledger_entry = AsyncMock(
        return_value=SimpleNamespace(
            id=uuid4(),
            tool_name="heartbeat_check_manage",
            snapshot_data={"check_name": "c", "action": "disable", "prior_enabled": True},
            reverted_at=None,
        )
    )
    snap_store.mark_reverted = AsyncMock()
    loader = SimpleNamespace(manage_check=AsyncMock())
    router = ActionRouter(
        database=None,
        settings=SimpleNamespace(a2ui_action_rate_per_minute=100, a2ui_trust_forwarded_identity=False),
        surface_service=None,
        heartbeat_runner=SimpleNamespace(dynamic_loader=loader),
        compensation_registry=registry,
        snapshot_store=snap_store,
    )
    surface = SimpleNamespace(trace_id=str(entry_id), surface_id="surf-1", data_model={})
    ctx = ActionContext(surface=surface, name="review.revert", context={}, data_model={}, services=router)
    result = await router._handlers["review.revert"].fn(ctx)
    assert result.ok, result.message
    loader.manage_check.assert_awaited_once_with("enable", name="c")


# ---------------------------------------------------------------------------
# codex P2 (main.py:1230): the heartbeat fork gets compensation wiring
# ---------------------------------------------------------------------------


def test_fork_inherits_and_receives_compensation_wiring() -> None:
    """HeartbeatRunner.start() forks before main.py wires compensation; the
    fork must still snapshot and push review cards."""
    from unittest.mock import MagicMock

    from test_runner_ledger import _FakeStore
    from test_runner_ledger import _runner as _ledger_runner

    parent, _ = _ledger_runner(_FakeStore(), compensation_enabled=True)
    early = parent.fork(MagicMock())  # predates wiring
    store, pusher = MagicMock(), MagicMock()
    parent.set_snapshot_store(store, "/ws")
    parent.set_action_review_pusher(pusher)
    late = parent.fork(MagicMock())  # postdates wiring

    for fork in (early, late):
        assert fork._snap_store is store
        assert fork._workspace_dir == "/ws"
        assert fork._action_review_pusher is pusher


# ---------------------------------------------------------------------------
# PR #652 round 2: compensation integrated at the dispatch layer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["off", "warn"])
@pytest.mark.parametrize(
    ("tool", "inp"),
    [("send_email", {"to": "a@b.c", "subject": "s", "body": "b"}), ("bash", {"command": "touch x"})],
)
def test_undoable_refuses_non_compensable_even_when_policy_is_off(mode, tool, inp) -> None:
    """codex P1 (runner.py:446): with the context policy OFF the early return
    made the not_compensable force-block unreachable."""
    from test_runner_authorization import _runner

    from nous.api.runner import Refusal

    r, _ = _runner([tool], tool_context_policy_mode=mode)
    ctx = ExecutionContext(kind="dag_node", undoable=True, session_id="s1")
    refusal = r._authorize_tool_call(ctx, tool, {tool}, "s1", inp)
    assert isinstance(refusal, Refusal) and "not_compensable" in refusal.text
    # ...while the same call from a node that claims no undoability still runs.
    plain = ExecutionContext(kind="dag_node", session_id="s1")
    assert r._authorize_tool_call(plain, tool, {tool}, "s1", inp) is None


def test_undoable_allows_compensable_and_reads_when_policy_is_off() -> None:
    from test_runner_authorization import _runner

    r, _ = _runner(["write_file", "read_file"], tool_context_policy_mode="off")
    ctx = ExecutionContext(kind="dag_node", undoable=True, session_id="s1")
    assert r._authorize_tool_call(ctx, "write_file", {"write_file"}, "s1", {"path": "x", "content": "y"}) is None
    assert r._authorize_tool_call(ctx, "read_file", {"read_file"}, "s1", {"path": "x"}) is None


def _bare_runner(snap_store=None):
    from nous.api.runner import AgentRunner

    runner = object.__new__(AgentRunner)
    runner._snap_store = snap_store
    runner._workspace_dir = tempfile.gettempdir()
    runner._dispatcher = SimpleNamespace()
    return runner


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["store", "ledger_row"])
async def test_undoable_call_refused_when_no_snapshot_can_be_recorded(missing) -> None:
    """codex P1 (config/main): with compensation unwired or no durable ledger
    row an undoable node's write ran with nothing to revert it. It is now
    refused; a non-undoable background call still runs (fail-open)."""
    from unittest.mock import AsyncMock

    from nous.api.compensation import SnapshotBlocksDispatch

    runner = _bare_runner(None if missing == "store" else AsyncMock())
    entry_id = uuid4() if missing == "store" else None
    inp = {"path": f"u_{uuid4().hex[:8]}.txt", "content": "x"}
    with pytest.raises(SnapshotBlocksDispatch):
        await runner._capture_compensation_snapshot(
            ExecutionContext(kind="dag_node", undoable=True), "write_file", inp, entry_id
        )
    assert (
        await runner._capture_compensation_snapshot(ExecutionContext(kind="subtask"), "write_file", inp, entry_id)
        is False
    )


@pytest.mark.asyncio
async def test_undoable_call_refused_when_snapshot_write_fails() -> None:
    from unittest.mock import AsyncMock

    from nous.api.compensation import SnapshotBlocksDispatch

    store = AsyncMock()
    store.capture.side_effect = TimeoutError()
    runner = _bare_runner(store)
    inp = {"path": f"u_{uuid4().hex[:8]}.txt", "content": "x"}
    with pytest.raises(SnapshotBlocksDispatch):
        await runner._capture_compensation_snapshot(
            ExecutionContext(kind="dag_node", undoable=True), "write_file", inp, uuid4()
        )
    assert (
        await runner._capture_compensation_snapshot(ExecutionContext(kind="subtask"), "write_file", inp, uuid4())
        is False
    )


@pytest.mark.asyncio
async def test_undoable_check_disable_refused_when_prior_state_unreadable() -> None:
    """Without the prior enabled state the revert refuses to enable, so the
    disable is not revertible: an undoable node may not make it."""
    from unittest.mock import AsyncMock

    from nous.api.compensation import SnapshotBlocksDispatch

    store = AsyncMock()
    store.check_enabled.return_value = None  # no such check
    runner = _bare_runner(store)
    with pytest.raises(SnapshotBlocksDispatch):
        await runner._capture_compensation_snapshot(
            ExecutionContext(kind="dag_node", undoable=True),
            "heartbeat_check_manage",
            {"name": "c", "action": "disable"},
            uuid4(),
        )
    store.capture.assert_not_awaited()


@pytest.mark.asyncio
async def test_capture_resolve_decision_records_prior_review_state() -> None:
    """The snapshot used to hard-code prior_outcome=None; it now records the
    fields Brain.review overwrites. The state the call writes is recorded
    after it succeeds (_after_compensable_call), not guessed beforehand."""
    from unittest.mock import AsyncMock

    prior = {"outcome": "pending", "outcome_result": None, "reviewed_at": None, "reviewer": None, "superseded_by": None}
    store = AsyncMock()
    store.decision_state.return_value = prior
    runner = _bare_runner(store)
    did = str(uuid4())
    assert await runner._capture_compensation_snapshot(
        ExecutionContext(kind="subtask"), "resolve_decision", {"decision_id": did, "outcome": "noise"}, uuid4()
    )
    store.decision_state.assert_awaited_once_with(did)
    data = store.capture.await_args.kwargs["snapshot_data"]
    assert data == {"decision_id": did, "prior": prior}


class _FakeSession:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount
        self.stmts: list = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        self.stmts.append(stmt)
        return SimpleNamespace(rowcount=self.rowcount)

    async def commit(self):
        self.committed = True


_WRITTEN_DECISION = {
    "outcome": "noise",
    "outcome_result": "later reviewer",
    "reviewed_at": "2026-09-20T08:00:00+00:00",
    "reviewer": "agent",
    "superseded_by": None,
}


class _FakeBrain:
    """The real Brain interface: public ``db`` and ``agent_id`` only."""

    def __init__(self, rowcount: int) -> None:
        self.session_obj = _FakeSession(rowcount)
        self.db = SimpleNamespace(session=lambda: self.session_obj)
        self.agent_id = "agent-x"


@pytest.mark.asyncio
async def test_compensate_resolve_decision_uses_brain_public_interface_and_real_columns() -> None:
    """codex P1 (compensation.py:321): brain._db / brain._agent_id do not
    exist on Brain, and resolution_note / resolved_at are not Decision
    columns -- every revert raised."""
    from nous.api.compensation import compensate_resolve_decision

    brain = _FakeBrain(rowcount=1)
    did = str(uuid4())
    prior = {
        "outcome": "pending",
        "outcome_result": "earlier note",
        "reviewed_at": "2026-09-01T12:00:00+00:00",
        "reviewer": "agent",
        "superseded_by": None,
    }
    res = await compensate_resolve_decision(
        uuid4(),
        {"decision_id": did, "prior": prior, "written": _WRITTEN_DECISION},
        SimpleNamespace(brain=brain),
    )
    assert res.success, res.message
    assert brain.session_obj.committed
    stmt = brain.session_obj.stmts[0]
    values = {c.key: v.value for c, v in stmt._values.items()}
    assert values["outcome"] == "pending"
    assert values["outcome_result"] == "earlier note"
    assert values["reviewed_at"].isoformat() == "2026-09-01T12:00:00+00:00"
    assert values["reviewer"] == "agent" and values["superseded_by"] is None
    params = stmt.compile().params
    assert "agent-x" in params.values() and "noise" in params.values()  # agent scope + stale guard
    # the stale guard covers EVERY written review field, not just the outcome
    where = str(stmt.compile())
    for col in ("outcome", "outcome_result", "reviewed_at", "reviewer", "superseded_by"):
        assert f"decisions.{col} IS NOT DISTINCT FROM" in where, col
    assert "later reviewer" in params.values()


@pytest.mark.asyncio
async def test_compensate_resolve_decision_refuses_stale_or_unrecorded() -> None:
    from nous.api.compensation import compensate_resolve_decision

    did = str(uuid4())
    snap = {"decision_id": did, "prior": {"outcome": "pending"}, "written": _WRITTEN_DECISION}
    # reviewed again since: the guarded UPDATE matches nothing
    res = await compensate_resolve_decision(uuid4(), snap, SimpleNamespace(brain=_FakeBrain(rowcount=0)))
    assert not res.success and "reviewed again" in res.message
    # a snapshot that never recorded the written state cannot rule out a
    # later same-outcome review, so it never writes (the old outcome-only guard)
    brain = _FakeBrain(rowcount=1)
    res = await compensate_resolve_decision(
        uuid4(), {"decision_id": did, "prior": {"outcome": "pending"}}, SimpleNamespace(brain=brain)
    )
    assert not res.success and "not recorded" in res.message and brain.session_obj.stmts == []
    # a snapshot without the prior state never writes
    brain = _FakeBrain(rowcount=1)
    res = await compensate_resolve_decision(
        uuid4(), {"decision_id": did, "prior_outcome": None}, SimpleNamespace(brain=brain)
    )
    assert not res.success and brain.session_obj.stmts == []


@pytest.mark.parametrize("off", [{"a2ui_enabled": False}, {"execution_ledger_persist_enabled": False}])
def test_compensation_requires_persisted_ledger_and_a2ui(off) -> None:
    """codex P1 (config.py:2960, main.py:1224): compensation without a durable
    ledger (no snapshot key) or without A2UI (no revert path) would let
    proceed-default approvals through with no undo."""
    from pydantic import ValidationError

    from nous.config import Settings

    with pytest.raises(ValidationError, match="compensation_enabled=True requires"):
        Settings(_env_file=None, ANTHROPIC_API_KEY="test-key", compensation_enabled=True, **off)


def test_compensation_wiring_is_not_nested_in_the_a2ui_gate() -> None:
    """The snapshot store is wired at the dispatch layer, independently of the
    A2UI block that consumes it."""
    import ast
    import inspect

    import nous.main as main_mod

    tree = ast.parse(inspect.getsource(main_mod))
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node

    def _is_flag_if(node: ast.AST, flag: str) -> bool:
        return isinstance(node, ast.If) and ast.unparse(node.test) == f"settings.{flag}"

    wiring = [n for n in ast.walk(tree) if _is_flag_if(n, "compensation_enabled")]
    assert wiring, "compensation wiring not found in nous/main.py"
    for node in wiring:
        assert "set_snapshot_store" in ast.unparse(node)
        cur = parents.get(id(node))
        while cur is not None:
            assert not _is_flag_if(cur, "a2ui_enabled"), "compensation wiring is nested in the A2UI gate"
            cur = parents.get(id(cur))


@pytest.mark.asyncio
async def test_server_compensation_ignores_caller_handler() -> None:
    """The Revert eligibility AND its handler come from the snapshot, never
    from the caller's compensation dict."""
    from unittest.mock import AsyncMock

    from nous.a2ui.tools import _server_compensation

    store = AsyncMock()
    store.get_by_ledger_entry.return_value = SimpleNamespace(tool_name="write_file", reverted_at=None)
    registry = CompensationRegistry()
    register_compensators(registry)
    comp = await _server_compensation({"revertible": True, "handler": "rm_everything"}, str(uuid4()), store, registry)
    assert comp["revertible"] is True and comp["handler"] == "write_file"
    store.get_by_ledger_entry.return_value = None
    comp = await _server_compensation({"revertible": True, "handler": "x"}, str(uuid4()), store, registry)
    assert comp["revertible"] is False and comp["handler"] is None


# ---------------------------------------------------------------------------
# PR #652 round 3
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("compensation", "with_trace"),
    [
        ({"revertible": True, "handler": "anything"}, True),  # invented handler string
        ({"revertible": True, "handler": "send_email"}, True),  # a real but irreversible tool
        ({"revertible": "yes", "handler": "write_file"}, True),  # truthy, not True
        ({"revertible": True, "handler": "write_file"}, False),  # no ledger row to revert
    ],
)
def test_action_review_builder_never_trusts_caller_revert_fields(compensation, with_trace) -> None:
    """codex P2 (action_review.py:27): any truthy revertible + nonempty
    handler used to yield an 'Undo this action' button."""
    from nous.a2ui.builders.action_review import action_review

    params = {"title": "t", "did": "d", "compensation": compensation}
    if with_trace:
        params["trace_id"] = str(uuid4())
    built = action_review(params)
    assert "review.revert" not in built.allowed_actions
    assert "revert" not in [c["id"] for c in built.components]


def test_proceed_default_requires_auto_review() -> None:
    """codex P1 (config.py:2960): without the auto review card a proceed-default
    mutation offers the user no revert surface at all."""
    from pydantic import ValidationError

    from nous.config import Settings

    with pytest.raises(ValidationError, match="requires compensation_auto_review_enabled"):
        Settings(
            _env_file=None,
            ANTHROPIC_API_KEY="test-key",
            dag_approval_nodes_enabled=True,
            dag_approval_proceed_default_enabled=True,
            compensation_enabled=True,
            compensation_auto_review_enabled=False,
        )


def test_proceed_default_rejects_downstream_completion_check() -> None:
    """codex P1 (schemas.py:507): a completion_check is a raw shell command run
    by the orchestrator outside tool authorization and snapshot capture."""
    nodes, edges = _approval_node(default_outcome="proceed", undoable_successor=True)
    nodes[2] = nodes[2].model_copy(update={"completion_check": "curl -X POST https://example.com/hook"})
    with _mock_settings(dag_approval_proceed_default_enabled=True):
        with pytest.raises(ValueError, match="completion_check"):
            DAGCreateRequest(name="test", nodes=nodes, edges=edges)
    # upstream of the approval (runs before any default) is unaffected
    nodes, edges = _approval_node(default_outcome="proceed", undoable_successor=True)
    nodes[0] = nodes[0].model_copy(update={"completion_check": "test -f /tmp/x"})
    with _mock_settings(dag_approval_proceed_default_enabled=True):
        assert len(DAGCreateRequest(name="test", nodes=nodes, edges=edges).nodes) == 3


@pytest.mark.asyncio
async def test_stale_check_refuses_a_grown_file_without_reading_it(tmp_path) -> None:
    """codex P2 (compensation.py:267): the stale check did an unbounded
    synchronous f.read() on the event loop before refusing."""
    target = tmp_path / "f.txt"
    target.write_text("x" * 10_000)
    snap = {"full_path": str(target), "existed": True, "prior_content": "old", **_written("short")}
    with patch("builtins.open", side_effect=AssertionError("must not read a size-mismatched file")):
        res = await compensate_write_file(uuid4(), snap, None)
    assert not res.success and "modified after" in res.message
    assert target.read_text() == "x" * 10_000


@pytest.mark.asyncio
async def test_file_revert_refused_without_written_size(tmp_path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("new")
    snap = {"full_path": str(target), "existed": True, "prior_content": "old", "written_content_hash": _h("new")}
    res = await compensate_write_file(uuid4(), snap, None)
    assert not res.success and target.read_text() == "new"


@pytest.mark.asyncio
async def test_write_path_lock_is_shared_per_resolved_path(tmp_path) -> None:
    """codex P1 (runner.py:577): relative and absolute spellings of one target
    serialize on the same lock, and the map does not grow unbounded."""
    from nous.api import compensation as comp

    ws = str(tmp_path)
    a = comp.write_path_lock("sub/../f.txt", ws)
    assert a is comp.write_path_lock(str(tmp_path / "f.txt"), ws)
    assert a is not comp.write_path_lock("g.txt", ws)
    await a.acquire()
    comp.release_write_path_lock(a)
    assert comp.write_path_key("f.txt", ws) not in comp._write_path_locks


@pytest.mark.asyncio
async def test_concurrent_writes_to_one_path_snapshot_and_write_in_turn(tmp_path) -> None:
    """codex P1 (runner.py:577): two background writes to one path both
    snapshotted the pre-A content; reverting B then erased A while passing
    the stale check. Capture + write now share one critical section, so B's
    snapshot records A's content."""
    from test_runner_authorization import _one_tool_call_then_done_with
    from test_runner_ledger import _FakeStore, _run_loop
    from test_runner_ledger import _runner as _ledger_runner

    from nous.api.compensation import SnapshotStore

    target = tmp_path / "shared.txt"
    target.write_text("original")
    captured: list[dict] = []

    class _Store(SnapshotStore):
        def __init__(self) -> None:
            pass

        async def capture(self, *, ledger_entry_id, tool_name, snapshot_data):
            captured.append(snapshot_data)
            return uuid4()

    runners = []
    for content in ("A", "B"):
        r, d = _ledger_runner(_FakeStore(), compensation_enabled=True)
        r.set_snapshot_store(_Store(), str(tmp_path))

        async def dispatch(name, inp, _content=content, **kw):
            await asyncio.sleep(0.05)  # the write is slow: the other call must wait
            target.write_text(_content)
            return "ok", False

        d.dispatch = dispatch
        r._call_api = _one_tool_call_then_done_with("write_file", {"path": "shared.txt", "content": content})
        runners.append(r)
    await asyncio.gather(
        *(
            _run_loop(r, is_background=True, context=ExecutionContext(kind="subtask", session_id=f"s{i}"))
            for i, r in enumerate(runners)
        )
    )
    priors = sorted(c["prior_content"] for c in captured)
    assert len(captured) == 2
    # one call saw the original, the other saw the first call's write -- never both "original"
    assert priors in (["A", "original"], ["B", "original"])


@pytest.mark.asyncio
async def test_after_compensable_call_records_the_transactional_decision_state() -> None:
    """codex P1 #652 (runner.py:530/660): the written AND prior review states
    come from the resolving transaction (CallOutcome.review_capture) -- never
    a post-dispatch re-read that could adopt a later review."""
    from unittest.mock import AsyncMock

    from nous.api.call_outcome import CallOutcome

    store = AsyncMock()
    store.decision_state.return_value = {**_WRITTEN_DECISION, "outcome_result": "a later review"}
    runner = _bare_runner(store)
    runner._settings = SimpleNamespace(compensation_auto_review_enabled=False)
    runner._action_review_pusher = None
    did = str(uuid4())
    entry = uuid4()
    prior = {**_WRITTEN_DECISION, "outcome_result": "concurrent review", "reviewer": "someone"}
    outcome = CallOutcome(review_capture={"prior": prior, "written": _WRITTEN_DECISION})
    kwargs = dict(snapshotted=True, tool_input={"decision_id": did, "outcome": "noise"})
    await runner._after_compensable_call(
        ExecutionContext(kind="subtask"), "resolve_decision", entry, "s1", status="success", outcome=outcome, **kwargs
    )
    store.decision_state.assert_not_awaited()
    store.record_written_state.assert_awaited_once_with(entry, _WRITTEN_DECISION, prior=prior)
    # no transactional capture -> nothing recorded (the revert is refused)
    store.reset_mock()
    await runner._after_compensable_call(
        ExecutionContext(kind="subtask"),
        "resolve_decision",
        entry,
        "s1",
        status="success",
        outcome=CallOutcome(),
        **kwargs,
    )
    store.record_written_state.assert_not_awaited()
    # a failed call records nothing
    await runner._after_compensable_call(
        ExecutionContext(kind="subtask"), "resolve_decision", entry, "s1", status="error", outcome=outcome, **kwargs
    )
    store.record_written_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_file_revert_waits_for_an_in_flight_write(tmp_path) -> None:
    """codex P1 #652 (compensation.py:332): the revert holds the write_file
    path lock across its stale check AND restore, so a write in flight lands
    first and the revert then refuses instead of overwriting it."""
    from nous.api import compensation as comp

    target = tmp_path / "f.txt"
    target.write_text("ours")
    snap = {
        "full_path": str(target),
        "existed": True,
        "prior_content": "original",
        **_written("ours"),
    }
    lock = comp.write_path_lock("f.txt", str(tmp_path))
    await lock.acquire()  # a concurrent write_file holds the path
    task = asyncio.create_task(comp.compensate_write_file(uuid4(), snap, None))
    await asyncio.sleep(0.05)
    assert not task.done()
    target.write_text("newer")
    comp.release_write_path_lock(lock)
    res = await task
    assert not res.success and "modified" in res.message
    assert target.read_text() == "newer"


@pytest.mark.asyncio
async def test_unreadable_prior_file_blocks_an_undoable_write(tmp_path) -> None:
    """codex P1 #652 (compensation.py:275): an existing file whose content
    cannot be read yields an explicit capture failure, and an undoable node
    refuses the write rather than overwrite what it cannot restore."""
    from nous.api import compensation as comp
    from nous.api.compensation import SnapshotBlocksDispatch

    (tmp_path / "wo.txt").write_text("secret")

    def _denied(*a, **k):
        raise PermissionError("write-only file")

    with patch.object(comp, "open", _denied, create=True):
        snap = await comp.snapshot_for_write_file("wo.txt", str(tmp_path))
        assert snap["existed"] and snap["prior_content"] is None and "PermissionError" in snap["capture_error"]
        runner = _bare_runner(SimpleNamespace(capture=None))
        runner._workspace_dir = str(tmp_path)
        runner._handler_args = lambda name, inp: inp
        with pytest.raises(SnapshotBlocksDispatch, match="could not be read"):
            await runner._capture_compensation_snapshot(
                ExecutionContext(kind="dag_node", undoable=True),
                "write_file",
                {"path": "wo.txt", "content": "x"},
                uuid4(),
            )
