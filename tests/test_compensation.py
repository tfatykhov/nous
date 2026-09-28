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
    }
    result = await compensate_write_file(uuid4(), snapshot_data, None)
    assert result.success
    assert not os.path.exists(path)


@pytest.mark.asyncio
async def test_compensate_write_file_already_absent() -> None:
    path = os.path.join(tempfile.gettempdir(), f"test_comp_gone_{uuid4().hex[:8]}.txt")
    snapshot_data = {"path": path, "full_path": path, "existed": False, "prior_content": None}
    result = await compensate_write_file(uuid4(), snapshot_data, None)
    assert result.success
    assert "already absent" in result.message


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
            "compensation": {"revertible": True, "handler": "compensate_write_file", "note": ""},
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
async def test_compensate_write_file_recreates_when_existed_and_now_absent() -> None:
    """File existed + was deleted → compensator must recreate from prior_content."""
    import tempfile

    path = os.path.join(tempfile.gettempdir(), f"test_comp_recreate_{uuid4().hex[:8]}.txt")
    assert not os.path.exists(path)

    snapshot_data = {
        "path": path,
        "full_path": path,
        "existed": True,
        "prior_content": "the original content",
    }
    result = await compensate_write_file(uuid4(), snapshot_data, None)
    try:
        assert result.success, f"Expected success but got: {result.message}"
        assert os.path.exists(path), "Compensator should have recreated the file"
        with open(path) as f:
            assert f.read() == "the original content"
    finally:
        if os.path.exists(path):
            os.unlink(path)


@pytest.mark.asyncio
async def test_compensate_write_file_fails_when_existed_no_prior_content() -> None:
    """File existed but prior_content not captured → compensator must fail (not silently succeed)."""
    import tempfile

    path = os.path.join(tempfile.gettempdir(), f"test_comp_noprior_{uuid4().hex[:8]}.txt")
    assert not os.path.exists(path)

    snapshot_data = {
        "path": path,
        "full_path": path,
        "existed": True,
        "prior_content": None,  # capture was not possible
    }
    result = await compensate_write_file(uuid4(), snapshot_data, None)
    assert result.success is False
    assert "prior content not captured" in result.message


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

    with pytest.raises(ValidationError, match="compensation_auto_review_enabled"):
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
