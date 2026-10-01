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
import base64
import hashlib
import os
import sys
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
    release_write_path_lock,
    snapshot_for_write_file,
)
from nous.api.execution_context import ExecutionContext
from nous.api.tool_classes import TOOL_CLASSES
from nous.api.tool_policy import evaluate
from nous.dag.schemas import DAGCreateRequest, DAGEdgeSpec, DAGNodeSpec


def _h(text: str) -> str:
    """The ``written_content_hash`` the runner records for ``text``."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _prior(text: str) -> dict:
    """The byte snapshot ``snapshot_for_write_file`` records for ``text``."""
    raw = text.encode("utf-8")
    return {"prior_b64": base64.b64encode(raw).decode("ascii"), "prior_sha256": hashlib.sha256(raw).hexdigest()}


def _prior_text(snap: dict) -> str | None:
    """The prior content a snapshot recorded, decoded for assertions."""
    b64 = snap.get("prior_b64")
    return None if b64 is None else base64.b64decode(b64).decode("utf-8")


async def _held(path: str, workspace: str):
    """A CallOutcome holding ``path``'s write lock, as the runner passes one."""
    from nous.api.call_outcome import CallOutcome
    from nous.api.compensation import write_path_lock

    lock = write_path_lock(path, workspace)
    await lock.acquire()
    return CallOutcome(write_lock=lock)


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
        assert _prior_text(snap) == "original content"
    finally:
        os.unlink(path)


@pytest.mark.asyncio
async def test_snapshot_for_write_file_new() -> None:
    path = os.path.join(tempfile.gettempdir(), f"test_comp_{uuid4().hex[:8]}.txt")
    assert not os.path.exists(path)
    snap = await snapshot_for_write_file(path, "/")
    assert snap["existed"] is False
    assert snap["prior_b64"] is None


@pytest.mark.asyncio
async def test_compensate_write_file_restores_content() -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("new content after write")
        path = f.name

    try:
        snapshot_data = {
            "path": path,
            "full_path": path,
            "workspace_root": os.path.dirname(path),
            "existed": True,
            **_prior("original content"),
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
        "workspace_root": os.path.dirname(path),
        "existed": False,
        "prior_b64": None,
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
        "workspace_root": os.path.dirname(path),
        "existed": False,
        "prior_b64": None,
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
        snapshot_data = {"path": path, "full_path": path, "existed": True, **_prior("old")}
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
        snapshot_data={"path": "/tmp/x", "full_path": "/tmp/x", "existed": False, "prior_b64": None},
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
        snapshot_data={"path": "/tmp/x", "full_path": "/tmp/x", "existed": False, "prior_b64": None},
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
        "workspace_root": os.path.dirname(path),
        "existed": True,
        **_prior("the original content"),
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
        "workspace_root": os.path.dirname(path),
        "existed": True,
        "prior_b64": None,  # capture was not possible
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


def test_check_node_downstream_of_proceed_default_refused_even_when_undoable() -> None:
    """codex P1 on #652 (dag/schemas.py:505): a check node runs as a dynamic
    heartbeat check whose execution context never carries the node's
    undoable flag, so declaring it undoable enforces nothing -- it must not
    satisfy the proceed-default requirement."""
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
        _check_node(undoable=True),  # declared, but unenforceable
    ]
    edges = [DAGEdgeSpec(from_node="approve", to_node="check_step", edge_type="context_flow")]
    with _mock_settings(dag_approval_proceed_default_enabled=True):
        with pytest.raises(ValueError, match="check nodes can never be undoable"):
            DAGCreateRequest(name="check_test", nodes=nodes, edges=edges)


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
            "workspace_root": os.path.dirname(path),
            "existed": True,
            **_prior("original content"),
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
            "workspace_root": os.path.dirname(path),
            "existed": True,
            **_prior("original content"),
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
        assert snap["prior_b64"] is None
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
        assert _prior_text(snap) == "small content"
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
    runner._settings = SimpleNamespace(compensation_auto_review_enabled=False)
    runner._action_review_pusher = None
    runner._snap_store = AsyncMock()
    runner._workspace_dir = "/"
    runner._dispatcher = SimpleNamespace()  # no repaired_args: input passes through

    ctx = ExecutionContext(kind="dag_node", undoable=True)

    oversized_snap = {
        "path": "big.bin",
        "full_path": "/big.bin",
        "existed": True,
        "prior_b64": None,
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
    runner._settings = SimpleNamespace(compensation_auto_review_enabled=False)
    runner._action_review_pusher = None
    runner._snap_store = AsyncMock()
    runner._workspace_dir = "/ws"
    runner._dispatcher = dispatcher

    snap = AsyncMock(
        return_value={
            "path": "notes.txt",
            "full_path": "/ws/notes.txt",
            "existed": False,
            "prior_b64": None,
            "oversized": False,
        }
    )
    with patch("nous.api.compensation.snapshot_for_write_file", new=snap):
        captured = await runner._capture_compensation_snapshot(
            ExecutionContext(kind="dag_node", undoable=True),
            "write_file",
            {"content": 'hello world</content>\n<parameter name="path">notes.txt'},
            uuid4(),
            outcome=await _held("notes.txt", "/ws"),
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
        get_by_ledger_entry=AsyncMock(
            return_value=SimpleNamespace(
                tool_name="write_file", reverted_at=None, snapshot_data={**_written("x"), "workspace_root": "/ws"}
            )
        )
    )
    registry = CompensationRegistry()
    register_compensators(registry)
    service = SimpleNamespace(push_built=AsyncMock(return_value="surf-1"))

    push = make_action_review_pusher(service, snap_store, registry)
    assert await push("write_file", entry_id, "s1") == "surf-1"
    built = service.push_built.await_args.args[0]
    kwargs = service.push_built.await_args.kwargs
    assert kwargs["dedup_key"] == f"review:{entry_id}"
    assert "session_id" not in kwargs, "session_id must not be forwarded (codex P2 on #652)"
    assert "review.revert" in built.allowed_actions
    assert built.trace_id == str(entry_id)

    # A snapshot already reverted: the card is still published, without Revert.
    snap_store.get_by_ledger_entry.return_value = SimpleNamespace(tool_name="write_file", reverted_at=object())
    await push("write_file", entry_id, "s1")
    assert "review.revert" not in service.push_built.await_args.args[0].allowed_actions


@pytest.mark.asyncio
async def test_action_review_pusher_bypasses_session_blocks() -> None:
    """Codex P2 on #652: when a micro-app is closed while its app.act worker
    runs, _retire_action_subtask() blocks the session from pushes. Compensation
    cards must bypass this block so the snapshot has a visible revert path.

    The fix is to not forward session_id to push_built — a push without a
    session_id is never blocked."""
    from unittest.mock import AsyncMock

    from nous.a2ui.tools import make_action_review_pusher

    entry_id = uuid4()
    snap_store = SimpleNamespace(
        get_by_ledger_entry=AsyncMock(
            return_value=SimpleNamespace(
                tool_name="write_file", reverted_at=None, snapshot_data={**_written("x"), "workspace_root": "/ws"}
            )
        )
    )
    registry = CompensationRegistry()
    register_compensators(registry)

    blocked_session = "subtask-deadbeef"
    push_calls: list = []

    async def capturing_push_built(built, *, dedup_key=None, session_id=None, **kw):
        push_calls.append({"session_id": session_id, "dedup_key": dedup_key})
        if session_id == blocked_session:
            raise PermissionError("session blocked")
        return "surf-1"

    service = SimpleNamespace(push_built=capturing_push_built)
    push = make_action_review_pusher(service, snap_store, registry)

    result = await push("write_file", entry_id, blocked_session)
    assert result == "surf-1"
    assert len(push_calls) == 1
    assert push_calls[0]["session_id"] is None, "session_id must not be forwarded"
    assert push_calls[0]["dedup_key"] == f"review:{entry_id}"


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


_WRITTEN_CHECK = {"check_id": "cid", "enabled_state_token": "tok"}


@pytest.mark.asyncio
async def test_compensate_heartbeat_check_manage_re_enables_a_disabled_check() -> None:
    """The loader signature is manage_check(action, name=...)."""
    from unittest.mock import AsyncMock

    from nous.api.compensation import compensate_heartbeat_check_manage

    loader = SimpleNamespace(enable_if_unchanged=AsyncMock(return_value=True))
    deps = SimpleNamespace(heartbeat_loader=loader)
    res = await compensate_heartbeat_check_manage(
        uuid4(), {"check_name": "c", "action": "disable", "prior_enabled": True, "written": _WRITTEN_CHECK}, deps
    )
    assert res.success
    loader.enable_if_unchanged.assert_awaited_once_with("c", "cid", "tok")

    loader.enable_if_unchanged.reset_mock()
    res = await compensate_heartbeat_check_manage(
        uuid4(), {"check_name": "c", "action": "enable", "prior_enabled": True, "written": _WRITTEN_CHECK}, deps
    )
    assert not res.success
    loader.enable_if_unchanged.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("inp", "expected"),
    [({"name": "c", "action": "disable"}, True), ({"name": "c", "action": "enable"}, False)],
)
async def test_capture_snapshots_heartbeat_check_manage_disable_only(inp, expected) -> None:
    from unittest.mock import AsyncMock

    from nous.api.runner import AgentRunner

    runner = object.__new__(AgentRunner)
    runner._settings = SimpleNamespace(compensation_auto_review_enabled=False)
    runner._action_review_pusher = None
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
    runner._settings = SimpleNamespace(compensation_auto_review_enabled=False)
    runner._action_review_pusher = None
    runner._snap_store = AsyncMock()
    runner._workspace_dir = tempfile.gettempdir()
    runner._dispatcher = SimpleNamespace()
    inp = {"path": f"p2_{uuid4().hex[:8]}.txt", "content": "x"}

    for kind in ("subtask", "scheduled", "heartbeat_callback", "background"):
        outcome = await _held(inp["path"], runner._workspace_dir)
        assert await runner._capture_compensation_snapshot(
            ExecutionContext(kind=kind), "write_file", inp, uuid4(), outcome=outcome
        )
        release_write_path_lock(outcome.write_lock)
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
    runner._settings = SimpleNamespace(compensation_auto_review_enabled=False)
    runner._action_review_pusher = None
    runner._snap_store = AsyncMock()
    runner._workspace_dir = "/"
    runner._dispatcher = SimpleNamespace()
    big = {"path": "big.bin", "full_path": "/big.bin", "existed": True, "prior_b64": None, "oversized": True}
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

    loader = SimpleNamespace(enable_if_unchanged=AsyncMock(return_value=True))
    res = await compensate_heartbeat_check_manage(
        uuid4(),
        {"check_name": "c", "action": "disable", "written": _WRITTEN_CHECK, **snap},
        SimpleNamespace(heartbeat_loader=loader),
    )
    assert res.success is success
    assert loader.enable_if_unchanged.await_count == int(enabled)


@pytest.mark.asyncio
@pytest.mark.parametrize(("state", "recorded"), [(True, True), (False, False), (None, None), (RuntimeError, None)])
async def test_capture_records_prior_enabled_state_of_the_check(state, recorded) -> None:
    from unittest.mock import AsyncMock

    from nous.api.runner import AgentRunner

    runner = object.__new__(AgentRunner)
    runner._settings = SimpleNamespace(compensation_auto_review_enabled=False)
    runner._action_review_pusher = None
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
            snapshot_data={"check_name": "c", "action": "disable", "prior_enabled": True, "written": _WRITTEN_CHECK},
            reverted_at=None,
        )
    )
    snap_store.mark_reverted = AsyncMock()
    loader = SimpleNamespace(enable_if_unchanged=AsyncMock(return_value=True))
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
    loader.enable_if_unchanged.assert_awaited_once_with("c", "cid", "tok")


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
    runner._settings = SimpleNamespace(compensation_auto_review_enabled=False)
    runner._action_review_pusher = None
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
    def __init__(self, rowcount: int, current_row: tuple | None = None) -> None:
        self.rowcount = rowcount
        self.current_row = current_row  # what a SELECT of the decision returns
        self.stmts: list = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        self.stmts.append(stmt)
        return SimpleNamespace(rowcount=self.rowcount, first=lambda: self.current_row)

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

    def __init__(self, rowcount: int, current_row: tuple | None = None) -> None:
        self.session_obj = _FakeSession(rowcount, current_row)
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
    store.get_by_ledger_entry.return_value = SimpleNamespace(
        tool_name="write_file", reverted_at=None, snapshot_data={**_written("x"), "workspace_root": "/ws"}
    )
    registry = CompensationRegistry()
    register_compensators(registry)
    comp = await _server_compensation({"revertible": True, "handler": "rm_everything"}, str(uuid4()), store, registry)
    assert comp["revertible"] is True and comp["handler"] == "write_file"
    # a snapshot missing its guard state: the compensator would refuse, so no Revert
    store.get_by_ledger_entry.return_value = SimpleNamespace(
        tool_name="resolve_decision", reverted_at=None, snapshot_data={"prior": {}}
    )
    comp = await _server_compensation({"revertible": True}, str(uuid4()), store, registry)
    assert comp["revertible"] is False
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
    snap = {
        "full_path": str(target),
        "workspace_root": str(tmp_path),
        "existed": True,
        **_prior("old"),
        **_written("short"),
    }
    with patch("builtins.open", side_effect=AssertionError("must not read a size-mismatched file")):
        res = await compensate_write_file(uuid4(), snap, None)
    assert not res.success and "modified after" in res.message
    assert target.read_text() == "x" * 10_000


@pytest.mark.asyncio
async def test_file_revert_refused_without_written_size(tmp_path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("new")
    snap = {"full_path": str(target), "existed": True, **_prior("old"), "written_content_hash": _h("new")}
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

        async def capture(self, *, ledger_entry_id, tool_name, snapshot_data, card_pending=False):
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
    priors = sorted(_prior_text(c) for c in captured)
    assert len(captured) == 2
    # one call saw the original, the other saw the first call's write -- never both "original"
    assert priors in (["A", "original"], ["B", "original"])


@pytest.mark.asyncio
async def test_concurrent_writes_revert_restores_first_not_original(tmp_path) -> None:
    """codex P1 #652 (runner.py:684): reverting the second of two concurrent
    writes must restore the state left by the first write, not the state before
    either. Without per-path serialization both calls snapshotted the same prior
    content and reverting B would silently erase A."""
    from nous.api import compensation as comp

    target = tmp_path / "data.txt"
    target.write_text("original")

    # Serialize: A acquires lock, snapshots "original", writes "A", releases
    lock_a = comp.write_path_lock("data.txt", str(tmp_path))
    await lock_a.acquire()
    snap_a = await comp.snapshot_for_write_file("data.txt", str(tmp_path))
    assert _prior_text(snap_a) == "original"
    target.write_text("A")
    snap_a["written_content_hash"] = _h("A")
    snap_a["written_size"] = 1
    comp.release_write_path_lock(lock_a)

    # B acquires lock after A releases: snapshots "A", writes "B", releases
    lock_b = comp.write_path_lock("data.txt", str(tmp_path))
    await lock_b.acquire()
    snap_b = await comp.snapshot_for_write_file("data.txt", str(tmp_path))
    assert _prior_text(snap_b) == "A"  # key: B saw A's content, not original
    target.write_text("B")
    snap_b["written_content_hash"] = _h("B")
    snap_b["written_size"] = 1
    comp.release_write_path_lock(lock_b)

    # File now contains "B"; reverting B must restore "A"
    res = await comp.compensate_write_file(uuid4(), snap_b, None)
    assert res.success
    assert target.read_text() == "A"  # restored to A, not original

    # Reverting A now must restore to original
    res = await comp.compensate_write_file(uuid4(), snap_a, None)
    assert res.success
    assert target.read_text() == "original"


@pytest.mark.asyncio
async def test_after_compensable_call_only_confirms_the_transactional_record() -> None:
    """codex P1 #652 (runner.py:607): the written AND prior review states are
    recorded by the resolving transaction itself (capture["persist"]); the
    post-dispatch hook never writes them -- it only confirms the mutation
    did, and reports the change unrevertible when it did not."""
    from unittest.mock import AsyncMock

    from nous.api.call_outcome import CallOutcome

    store = AsyncMock()
    runner = _bare_runner(store)
    runner._settings = SimpleNamespace(compensation_auto_review_enabled=False)
    runner._action_review_pusher = None
    entry = uuid4()
    kwargs = dict(snapshotted=True, tool_input={"decision_id": str(uuid4()), "outcome": "noise"})
    persisted = CallOutcome(review_capture={"prior": {}, "written": _WRITTEN_DECISION, "persisted": True})
    note = await runner._after_compensable_call(
        ExecutionContext(kind="subtask"), "resolve_decision", entry, "s1", status="success", outcome=persisted, **kwargs
    )
    assert note is None
    assert store.method_calls == []  # no snapshot write after the call
    # captured but never persisted in the transaction -> not revertible
    for outcome in (CallOutcome(review_capture={"prior": {}, "written": _WRITTEN_DECISION}), CallOutcome()):
        note = await runner._after_compensable_call(
            ExecutionContext(kind="subtask"),
            "resolve_decision",
            entry,
            "s1",
            status="success",
            outcome=outcome,
            **kwargs,
        )
        assert note and "NOT be made revertible" in note
    # a failed call changed nothing: no note
    note = await runner._after_compensable_call(
        ExecutionContext(kind="subtask"),
        "resolve_decision",
        entry,
        "s1",
        status="error",
        outcome=CallOutcome(),
        **kwargs,
    )
    assert note is None and store.method_calls == []


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
        "workspace_root": str(target.parent),
        "existed": True,
        **_prior("original"),
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

    def _denied(fd, *a, **k):
        os.close(fd)
        raise PermissionError("write-only file")

    with patch("os.fdopen", _denied):
        snap = await comp.snapshot_for_write_file("wo.txt", str(tmp_path))
        assert snap["existed"] and snap["prior_b64"] is None and "PermissionError" in snap["capture_error"]
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


@pytest.mark.asyncio
async def test_snapshot_never_reads_a_path_outside_the_workspace(tmp_path) -> None:
    """codex P1 #652 (compensation.py:280): a write_file target outside the
    workspace (absolute or ``..``) is rejected with the handler's own path
    check BEFORE any filesystem access, so its contents never reach a
    snapshot; the runner then stores no snapshot (write_file refuses the
    call itself)."""
    from nous.api import compensation as comp

    workspace = tmp_path / "ws"
    workspace.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("host secret")
    for path in (str(secret), "../secret.txt"):
        snap = await comp.snapshot_for_write_file(path, str(workspace))
        assert "prior_b64" not in snap and "existed" not in snap
        assert "outside workspace" in snap["invalid_path"]

    store = SimpleNamespace(capture=None)
    runner = _bare_runner(store)
    runner._workspace_dir = str(workspace)
    runner._handler_args = lambda name, inp: inp
    assert not await runner._capture_compensation_snapshot(
        ExecutionContext(kind="dag_node", undoable=True),
        "write_file",
        {"path": "../secret.txt", "content": "x"},
        uuid4(),
    )


@pytest.mark.asyncio
async def test_check_revert_refused_after_a_later_disable() -> None:
    """codex P1 #652 (compensation.py:432): a revert re-enables the check only
    while it still carries the state token its own disable wrote. Before the
    fix it called manage_check("enable") unconditionally, overriding a later
    explicit disable. A snapshot without the written token is refused."""
    from unittest.mock import AsyncMock

    from nous.api.compensation import compensate_heartbeat_check_manage

    snap = {"check_name": "c", "action": "disable", "prior_enabled": True, "written": _WRITTEN_CHECK}
    # a later disable replaced the token: the conditional enable touches nothing
    loader = SimpleNamespace(
        enable_if_unchanged=AsyncMock(return_value=False),
        is_enabled=AsyncMock(return_value=False),
        manage_check=AsyncMock(),
    )
    res = await compensate_heartbeat_check_manage(uuid4(), snap, SimpleNamespace(heartbeat_loader=loader))
    assert not res.success and "changed or removed" in res.message
    loader.manage_check.assert_not_awaited()

    loader.enable_if_unchanged.reset_mock()
    legacy = {k: v for k, v in snap.items() if k != "written"}
    res = await compensate_heartbeat_check_manage(uuid4(), legacy, SimpleNamespace(heartbeat_loader=loader))
    assert not res.success and "not recorded" in res.message
    loader.enable_if_unchanged.assert_not_awaited()
    loader.manage_check.assert_not_awaited()


@pytest.mark.asyncio
async def test_disable_captures_its_state_token_and_persists_it_before_commit() -> None:
    """codex P1 #652 (compensation.py:432, runner.py:607): manage_check(disable,
    capture=) reports the token it stamped and awaits capture["persist"] on
    its own session BEFORE committing, so the snapshot record shares the
    disable's transaction."""
    from unittest.mock import AsyncMock, MagicMock

    from nous.heartbeat.dynamic import DynamicCheckLoader

    loader = object.__new__(DynamicCheckLoader)
    loader._registry = MagicMock(get_check=MagicMock(return_value=None))
    loader._signatures, loader._loaded_ids, loader._id_to_name = {}, set(), {}
    loader._active_runs, loader._mutation_lock = {}, asyncio.Lock()
    model = SimpleNamespace(id=uuid4(), enabled=True, metadata_={"other": 1}, updated_at=None)
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=model)))
    order: list[str] = []
    session.commit = AsyncMock(side_effect=lambda: order.append("commit"))
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    loader._db = SimpleNamespace(session=lambda: ctx)
    loader._agent_id = "a"

    async def persist(sess, cap):
        assert sess is session and "written" in cap and "prior_enabled" in cap
        order.append("persist")

    capture: dict = {"persist": persist}
    await loader.manage_check("disable", name="c", capture=capture)
    token = model.metadata_["enabled_state_token"]
    assert model.metadata_["other"] == 1 and model.enabled is False
    assert capture["prior_enabled"] is True
    assert capture["written"] == {"check_id": str(model.id), "enabled_state_token": token}
    assert order == ["persist", "commit"]


@pytest.mark.asyncio
async def test_snapshot_reads_the_validated_path_not_a_retargeted_symlink(tmp_path) -> None:
    """codex P1 #652 (compensation.py:277): a symlink that points inside the
    workspace when validated and is retargeted outside before the read must
    never have the outside file captured. Every read uses the resolved path
    from _validate_path, and the path is re-validated against the file read."""
    from nous.api import builtin_tools
    from nous.api import compensation as comp

    workspace = tmp_path / "ws"
    workspace.mkdir()
    inside = workspace / "inside.txt"
    inside.write_text("inside content")
    secret = tmp_path / "secret.txt"
    secret.write_text("host secret")
    link = workspace / "link.txt"
    link.symlink_to(inside)

    real_validate = builtin_tools._validate_path
    calls = 0

    def _validate_then_retarget(path_str, workspace_dir):
        nonlocal calls
        calls += 1
        resolved = real_validate(path_str, workspace_dir)
        if calls == 1:
            link.unlink()
            link.symlink_to(secret)
        return resolved

    with patch.object(builtin_tools, "_validate_path", _validate_then_retarget):
        snap = await comp.snapshot_for_write_file("link.txt", str(workspace))
    assert _prior_text(snap) != "host secret"
    assert "host secret" not in repr(snap)
    assert snap["full_path"] == str(inside.resolve())


@pytest.mark.asyncio
async def test_check_revert_uses_the_disable_transactions_prior_state() -> None:
    """codex P1 #652 (runner.py:557): the pre-dispatch read said the check was
    enabled, but a concurrent disable landed first, so THIS disable's
    transaction saw prior_enabled=False. The persister records that actual
    prior state, on the mutation's session."""
    from unittest.mock import AsyncMock

    store = AsyncMock()
    runner = _bare_runner(store)
    entry = uuid4()
    session = object()
    capture = {"prior_enabled": False, "written": _WRITTEN_CHECK}
    await runner._written_state_persister("heartbeat_check_manage", entry)(session, capture)
    store.record_written_state_in.assert_awaited_once_with(
        session, entry, _WRITTEN_CHECK, extra={"prior_enabled": False}
    )
    assert capture["persisted"] is True

    store.reset_mock()
    prior = {**_WRITTEN_DECISION, "outcome": None}
    capture = {"prior": prior, "written": _WRITTEN_DECISION}
    await runner._written_state_persister("resolve_decision", entry)(session, capture)
    store.record_written_state_in.assert_awaited_once_with(session, entry, _WRITTEN_DECISION, prior=prior)
    assert capture["persisted"] is True


@pytest.mark.asyncio
async def test_write_is_bound_to_the_snapshotted_path(tmp_path) -> None:
    """codex P2 #652 (compensation.py:292): a symlink retargeted between the
    snapshot and the write must not let write_file mutate a file other than
    the one snapshotted -- the revert would restore the wrong file."""
    from unittest.mock import AsyncMock

    from nous.api import call_outcome
    from nous.api.builtin_tools import write_file_tool

    workspace = tmp_path / "ws"
    workspace.mkdir()
    first = workspace / "first.txt"
    first.write_text("first")
    other = workspace / "other.txt"
    other.write_text("other")
    link = workspace / "link.txt"
    link.symlink_to(first)

    runner = _bare_runner(AsyncMock())
    runner._workspace_dir = str(workspace)
    outcome = await _held("link.txt", str(workspace))
    assert await runner._capture_compensation_snapshot(
        ExecutionContext(kind="subtask"), "write_file", {"path": "link.txt", "content": "new"}, uuid4(), outcome=outcome
    )
    assert outcome.write_target == str(first.resolve())

    link.unlink()
    link.symlink_to(other)
    token = call_outcome._current.set(outcome)
    try:
        result = await write_file_tool("link.txt", "new", _workspace_dir=str(workspace))
    finally:
        call_outcome._current.reset(token)
    assert result.get("is_error") is True
    assert other.read_text() == "other" and first.read_text() == "first"


@pytest.mark.asyncio
async def test_unknown_outcome_still_gets_a_review_card() -> None:
    """codex P1 #652 (runner.py:597, 645): a call closed `unknown` -- a worker
    thread or a commit that may still land -- gets the review card, for every
    compensable tool; its revert applies only if the recorded write landed.
    A call that failed changed nothing: no card."""
    from unittest.mock import AsyncMock

    runner = _bare_runner(AsyncMock())
    runner._settings = SimpleNamespace(compensation_auto_review_enabled=True)
    runner._action_review_pusher = pusher = AsyncMock()
    ctx = ExecutionContext(kind="subtask")
    entry = uuid4()
    for tool in ("write_file", "resolve_decision", "heartbeat_check_manage"):
        pusher.reset_mock()
        await runner._maybe_push_action_review(ctx, tool, entry, "s1", snapshotted=True, status="unknown")
        pusher.assert_awaited_once_with(tool, entry, "s1")

    pusher.reset_mock()
    await runner._maybe_push_action_review(ctx, "write_file", entry, "s1", snapshotted=True, status="error")
    pusher.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["resolve_decision", "heartbeat_check_manage"])
async def test_unknown_db_outcome_with_a_transactional_record_is_revertible(tool) -> None:
    """codex P1 #652 (runner.py:645): a DB call cancelled after its commit
    landed is closed `unknown`; its written state committed with it, so it
    gets a card and no "not revertible" note."""
    from unittest.mock import AsyncMock

    from nous.api.call_outcome import CallOutcome

    runner = _bare_runner(AsyncMock())
    runner._settings = SimpleNamespace(compensation_auto_review_enabled=True)
    runner._action_review_pusher = pusher = AsyncMock()
    entry = uuid4()
    if tool == "resolve_decision":
        outcome = CallOutcome(
            review_capture={"prior": {"outcome": None}, "written": _WRITTEN_DECISION, "persisted": True}
        )
    else:
        outcome = CallOutcome(check_capture={"prior_enabled": True, "written": _WRITTEN_CHECK, "persisted": True})
    note = await runner._after_compensable_call(
        ExecutionContext(kind="dag_node", undoable=True),
        tool,
        entry,
        "s1",
        snapshotted=True,
        status="unknown",
        tool_input={},
        outcome=outcome,
    )
    assert note is None
    pusher.assert_awaited_once_with(tool, entry, "s1")


@pytest.mark.asyncio
async def test_unrecorded_written_state_is_never_advertised_as_revertible() -> None:
    """codex P1 #652 (runner.py:545): a call whose transaction did not record
    its written state is reported applied but not revertible, and the card
    (still published, so the change is on record) offers no Revert."""
    from unittest.mock import AsyncMock

    from nous.api.call_outcome import CallOutcome

    runner = _bare_runner(AsyncMock())
    runner._settings = SimpleNamespace(compensation_auto_review_enabled=True)
    runner._action_review_pusher = pusher = AsyncMock()
    outcome = CallOutcome(check_capture={"prior_enabled": True, "written": _WRITTEN_CHECK})
    note = await runner._after_compensable_call(
        ExecutionContext(kind="dag_node", undoable=True),
        "heartbeat_check_manage",
        uuid4(),
        "s1",
        snapshotted=True,
        status="success",
        tool_input={"name": "c", "action": "disable"},
        outcome=outcome,
    )
    assert note and "NOT be made revertible" in note
    pusher.assert_awaited_once()


# ---------------------------------------------------------------------------
# Codex findings 2026-09-30 — regression tests (must FAIL before the fix)
# ---------------------------------------------------------------------------


# Finding #1 — runner.py: durable card publication
# If push_builtin() fails transiently, mark_card_pending ensures retry on tick.


@pytest.mark.asyncio
async def test_push_compensation_card_clears_the_intent_only_once_published() -> None:
    """codex P1 on #652: the intent (stored before dispatch) is cleared only
    after the card is published; a failed push leaves it for the sweep."""
    from unittest.mock import AsyncMock

    runner = _bare_runner(AsyncMock())
    runner._settings = SimpleNamespace(compensation_auto_review_enabled=True)
    runner._action_review_pusher = pusher = AsyncMock()
    snap_store = runner._snap_store

    entry = uuid4()
    ctx = ExecutionContext(kind="subtask")

    await runner._maybe_push_action_review(ctx, "write_file", entry, "s1", snapshotted=True, status="success")
    pusher.assert_awaited_once()
    snap_store.mark_card_published.assert_awaited_once_with(entry)

    snap_store.reset_mock()
    pusher.reset_mock()
    pusher.side_effect = TimeoutError("connection lost")
    await runner._maybe_push_action_review(ctx, "write_file", entry, "s1", snapshotted=True, status="success")
    snap_store.mark_card_published.assert_not_awaited()

    # A call that failed changed nothing: its intent is cleared, no card.
    snap_store.reset_mock()
    pusher.reset_mock()
    await runner._maybe_push_action_review(ctx, "write_file", entry, "s1", snapshotted=True, status="error")
    pusher.assert_not_awaited()
    snap_store.mark_card_published.assert_awaited_once_with(entry)


@pytest.mark.asyncio
async def test_sweep_pending_cards_follows_the_ledger_outcome() -> None:
    """codex P1 on #652: the sweep publishes a card for every intent whose
    call may have changed state (success / unknown / no ledger row) and just
    clears the intent of a call that failed or was blocked."""
    from unittest.mock import AsyncMock

    runner = _bare_runner(AsyncMock())
    runner._settings = SimpleNamespace(
        compensation_auto_review_enabled=True,
        execution_ledger_pending_unknown_after_seconds=7800,
        tool_timeout=120,
    )
    runner._action_review_pusher = pusher = AsyncMock()
    snap_store = runner._snap_store

    ok, unknown, absent, failed = uuid4(), uuid4(), uuid4(), uuid4()
    snap_store.get_pending_cards.return_value = [
        (ok, "write_file", "success"),
        (unknown, "resolve_decision", "unknown"),
        (absent, "write_file", None),
        (failed, "write_file", "error"),
    ]

    assert await runner.sweep_pending_cards() == 3
    assert [c.args[1] for c in pusher.await_args_list] == [ok, unknown, absent]
    assert [c.args[0] for c in snap_store.mark_card_published.await_args_list] == [ok, unknown, absent, failed]
    assert snap_store.get_pending_cards.await_args.kwargs["absent_ledger_after_seconds"] == 7800


# Finding #2 — a2ui/tools.py: compensation cards hide course_correct
# trace_id is a ledger UUID, not a Decision id.


def test_compensation_card_hides_course_correct_and_make_rule() -> None:
    """codex P2 on #652: compensation cards set compensation_card=True,
    so the builder skips course_correct/make_rule (trace_id is not a decision)."""
    from nous.a2ui.builders.action_review import action_review

    # compensation card: no course_correct, no make_rule
    built = action_review(
        {
            "title": "Background write",
            "did": "wrote file",
            "trace_id": str(uuid4()),
            "compensation_card": True,
            "compensation": {"revertible": True, "handler": "write_file"},
        }
    )
    assert "review.course_correct" not in built.allowed_actions
    assert "review.make_rule" not in built.allowed_actions
    assert "review.acknowledge" in built.allowed_actions
    assert "review.revert" in built.allowed_actions
    # no correction_field in components
    assert "correction_field" not in [c["id"] for c in built.components]

    # regular decision review: has course_correct and make_rule
    built = action_review(
        {
            "title": "Decision review",
            "did": "made a decision",
            "trace_id": str(uuid4()),
            "compensation": {"revertible": False, "handler": None},
        }
    )
    assert "review.course_correct" in built.allowed_actions
    assert "review.make_rule" in built.allowed_actions
    assert "correction_field" in [c["id"] for c in built.components]


@pytest.mark.asyncio
async def test_make_action_review_pusher_sets_compensation_card_flag() -> None:
    """codex P2 on #652: the auto-review pusher sets compensation_card=True
    so the builder hides course_correct (trace_id is a ledger entry, not decision)."""
    from unittest.mock import AsyncMock

    from nous.a2ui.tools import make_action_review_pusher
    from nous.api.compensation import CompensationRegistry, register_compensators

    entry_id = uuid4()
    snap_store = SimpleNamespace(
        get_by_ledger_entry=AsyncMock(
            return_value=SimpleNamespace(
                tool_name="write_file", reverted_at=None, snapshot_data={**_written("x"), "workspace_root": "/ws"}
            )
        )
    )
    registry = CompensationRegistry()
    register_compensators(registry)

    built_surface = None

    async def capturing_push(surface, **kw):
        nonlocal built_surface
        built_surface = surface
        return "surf-1"

    service = SimpleNamespace(push_built=capturing_push)
    push = make_action_review_pusher(service, snap_store, registry)
    await push("write_file", entry_id, "s1")

    # The built surface should not have course_correct
    assert built_surface is not None
    assert "review.course_correct" not in built_surface.allowed_actions
    assert "review.make_rule" not in built_surface.allowed_actions


# Finding #3 — dynamic.py: preserve reverts when sync fails


@pytest.mark.asyncio
async def test_enable_if_unchanged_succeeds_despite_sync_failure() -> None:
    """codex P2 on #652: if sync() raises after the conditional update commits,
    treat it as success (the DB row was re-enabled) rather than failure."""
    from unittest.mock import AsyncMock

    from nous.heartbeat.dynamic import DynamicCheckLoader

    loader = object.__new__(DynamicCheckLoader)
    loader._agent_id = "a"
    loader._mutation_lock = asyncio.Lock()

    # Mock the session to return rowcount=1 (successful update)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=SimpleNamespace(rowcount=1))
    session.commit = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    loader._db = SimpleNamespace(session=lambda: ctx)

    # Make sync raise AFTER the commit
    loader._sync_locked = AsyncMock(side_effect=RuntimeError("DB down"))

    # Despite sync failure, should return True (the update committed)
    result = await loader.enable_if_unchanged("c", str(uuid4()), "tok")
    assert result is True
    session.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# codex P1 on #652: durable pending marker + production sweep wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "auto_review", "expected"),
    [("subtask", True, True), ("subtask", False, False), ("dag_node", True, True)],
)
async def test_card_intent_is_stored_with_the_pre_dispatch_snapshot(kind, auto_review, expected) -> None:
    """codex P1 on #652 (runner.py:658): the review-card intent used to be
    written only after the side effect returned, so a process that died in
    between left a mutation no sweep could ever card. It is now part of the
    snapshot insert itself -- before dispatch."""
    from unittest.mock import AsyncMock

    runner = _bare_runner(AsyncMock())
    runner._settings = SimpleNamespace(compensation_auto_review_enabled=auto_review)
    runner._action_review_pusher = AsyncMock()
    runner._snap_store.check_enabled.return_value = True
    assert await runner._capture_compensation_snapshot(
        ExecutionContext(kind=kind), "heartbeat_check_manage", {"name": "c", "action": "disable"}, uuid4()
    )
    assert runner._snap_store.capture.await_args.kwargs["card_pending"] is expected


@pytest.mark.asyncio
async def test_capture_insert_carries_the_card_intent() -> None:
    """The intent lands in the very row ``capture`` inserts."""
    from nous.api.compensation import SnapshotStore

    added: list = []

    class _S:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def add(self, row):
            added.append(row)

        async def commit(self):
            pass

    store = SnapshotStore(SimpleNamespace(session=lambda: _S()), "agent")
    await store.capture(ledger_entry_id=uuid4(), tool_name="write_file", snapshot_data={"a": 1}, card_pending=True)
    assert added[0].snapshot_data == {"a": 1, "_card_pending": True, "_card_tool_name": "write_file"}
    await store.capture(ledger_entry_id=uuid4(), tool_name="write_file", snapshot_data={"a": 1})
    assert added[1].snapshot_data == {"a": 1}


def test_sweep_pending_cards_wired_in_main() -> None:
    """codex P1 on #652: sweep_pending_cards must be called from a production loop.
    This test verifies the wiring exists in main.py (startup + maintenance loop)."""
    import ast
    from pathlib import Path

    main_path = Path(__file__).parent.parent / "nous" / "main.py"
    source = main_path.read_text()

    # Must appear in the maintenance loop
    assert "runner.sweep_pending_cards()" in source, "sweep_pending_cards not wired in maintenance loop"

    # Must also have a startup sweep (bounded by wait_for)
    tree = ast.parse(source)
    startup_sweep_found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            # Look for asyncio.wait_for(runner.sweep_pending_cards(), ...)
            if isinstance(node.func, ast.Attribute) and node.func.attr == "wait_for" and len(node.args) >= 1:
                arg = node.args[0]
                if (
                    isinstance(arg, ast.Call)
                    and isinstance(arg.func, ast.Attribute)
                    and arg.func.attr == "sweep_pending_cards"
                ):
                    startup_sweep_found = True
                    break
    assert startup_sweep_found, "startup sweep_pending_cards not wired with wait_for timeout"


# ---------------------------------------------------------------------------
# Round 8 (team review): one compare-and-replace primitive for write_file and
# its revert, write fences, idempotent reverts. Each test fails without its fix.
# ---------------------------------------------------------------------------


async def _snapshotted_write(workspace, path: str, content: str, *, entry=None):
    """Snapshot ``path`` as the runner does, then run write_file bound to it.
    Returns (entry_id, snapshot_data, tool result)."""
    from unittest.mock import AsyncMock

    from nous.api import call_outcome
    from nous.api.builtin_tools import write_file_tool

    entry = entry or uuid4()
    runner = _bare_runner(AsyncMock())
    runner._workspace_dir = str(workspace)
    runner._handler_args = lambda name, inp: inp
    outcome = await _held(path, str(workspace))
    try:
        assert await runner._capture_compensation_snapshot(
            ExecutionContext(kind="subtask"), "write_file", {"path": path, "content": content}, entry, outcome=outcome
        )
        snap = runner._snap_store.capture.await_args.kwargs["snapshot_data"]
        token = call_outcome._current.set(outcome)
        try:
            result = await write_file_tool(path, content, _workspace_dir=str(workspace))
        finally:
            call_outcome._current.reset(token)
    finally:
        release_write_path_lock(outcome.write_lock)
    return entry, snap, result


@pytest.mark.asyncio
async def test_short_os_write_is_completed_not_truncated(tmp_path, monkeypatch) -> None:
    """codex P1 #652 (builtin_tools.py:220): a short os.write() was ignored,
    so a truncated temp file replaced the target and write_file reported
    success. Every byte is now written."""
    from nous.api.builtin_tools import write_file_tool

    real_write = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: real_write(fd, bytes(data[:3])))
    result = await write_file_tool("out.txt", "hello partial world", _workspace_dir=str(tmp_path))
    assert not result.get("is_error")
    assert (tmp_path / "out.txt").read_text() == "hello partial world"


@pytest.mark.asyncio
async def test_new_file_gets_the_umask_default_mode_not_0600(tmp_path) -> None:
    """Regression from the temp-file write: mkstemp created every NEW file
    0600, where write_text gave the umask default (0644 under umask 022)."""
    import stat

    from nous.api.builtin_tools import write_file_tool

    umask = os.umask(0o022)
    os.umask(umask)
    await write_file_tool("new.txt", "x", _workspace_dir=str(tmp_path))
    assert stat.S_IMODE((tmp_path / "new.txt").stat().st_mode) == 0o666 & ~umask


@pytest.mark.asyncio
async def test_non_utf8_file_is_restored_byte_for_byte(tmp_path) -> None:
    """codex P1 #652 (compensation.py:407): the snapshot decoded the prior
    content with errors="replace", so a revert "succeeded" writing U+FFFD
    over bytes that were not valid UTF-8."""
    target = tmp_path / "blob.bin"
    original = b"\xff\xfe\x00binary\x80\xc3("
    target.write_bytes(original)
    entry, snap, result = await _snapshotted_write(tmp_path, "blob.bin", "text now")
    assert not result.get("is_error") and target.read_text() == "text now"
    res = await compensate_write_file(entry, snap, None)
    assert res.success, res.message
    assert target.read_bytes() == original


@pytest.mark.asyncio
async def test_forward_write_refuses_a_file_changed_since_its_snapshot(tmp_path) -> None:
    """codex P1 #652 (builtin_tools.py:228): a change another writer made
    between the snapshot and the write was overwritten -- and a later revert
    "restored" the older snapshot over it. The write now verifies the
    snapshotted state right before its rename and refuses otherwise."""
    from unittest.mock import AsyncMock

    from nous.api import call_outcome
    from nous.api.builtin_tools import write_file_tool

    target = tmp_path / "f.txt"
    target.write_text("original")
    runner = _bare_runner(AsyncMock())
    runner._workspace_dir = str(tmp_path)
    runner._handler_args = lambda name, inp: inp
    outcome = await _held("f.txt", str(tmp_path))
    assert await runner._capture_compensation_snapshot(
        ExecutionContext(kind="subtask"), "write_file", {"path": "f.txt", "content": "agent"}, uuid4(), outcome=outcome
    )
    target.write_text("concurrent edit")  # e.g. a bash tool, which takes no lock
    token = call_outcome._current.set(outcome)
    try:
        result = await write_file_tool("f.txt", "agent", _workspace_dir=str(tmp_path))
    finally:
        call_outcome._current.reset(token)
        release_write_path_lock(outcome.write_lock)
    assert result.get("is_error") is True
    assert target.read_text() == "concurrent edit"


@pytest.mark.asyncio
async def test_forward_write_to_an_absent_file_does_not_clobber_one_that_appeared(tmp_path) -> None:
    """The ABSENT pre-state is enforced no-clobber: a file created between the
    snapshot and the write is never replaced."""
    from nous.api.builtin_tools import ABSENT, PreconditionFailed, atomic_replace_bytes

    target = tmp_path / "late.txt"
    target.write_text("someone else's")
    with pytest.raises(PreconditionFailed):
        atomic_replace_bytes(target, b"mine", expected=ABSENT, root=tmp_path)
    assert target.read_text() == "someone else's"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["late.txt"]


def test_absent_target_that_appears_during_the_write_is_not_clobbered(tmp_path, monkeypatch) -> None:
    """The window after the last state check: a file created there is still
    never replaced (the ABSENT case links no-clobber instead of renaming)."""
    from nous.api import builtin_tools
    from nous.api.builtin_tools import ABSENT, PreconditionFailed, atomic_replace_bytes

    target = tmp_path / "race.txt"
    real = builtin_tools._unchanged_since

    def appears_right_after_the_check(dfd, name, pre):
        ok = real(dfd, name, pre)
        target.write_text("appeared")
        return ok

    monkeypatch.setattr(builtin_tools, "_unchanged_since", appears_right_after_the_check)
    with pytest.raises(PreconditionFailed):
        atomic_replace_bytes(target, b"mine", expected=ABSENT, root=tmp_path)
    assert target.read_text() == "appeared"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["race.txt"]


@pytest.mark.asyncio
async def test_revert_never_follows_a_symlink_swapped_in(tmp_path) -> None:
    """codex P1 #652 (compensation.py:497): the stale check and the restore
    followed a symlink, so a target swapped for a link after the write could
    have its link target -- outside the workspace -- overwritten."""
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("agent")  # same bytes as the write: the hash check alone would pass
    target = ws / "f.txt"
    target.write_text("original")
    entry, snap, result = await _snapshotted_write(ws, "f.txt", "agent")
    assert not result.get("is_error")
    target.unlink()
    target.symlink_to(outside)
    res = await compensate_write_file(entry, snap, None)
    assert not res.success
    assert outside.read_text() == "agent" and target.is_symlink()
    # the state read itself refuses the link rather than hash its target
    from nous.api.builtin_tools import PreconditionFailed, file_digest

    with pytest.raises(PreconditionFailed, match="symlink"):
        file_digest(target, 100, root=ws)


@pytest.mark.asyncio
async def test_failed_restore_leaves_the_file_intact_and_retryable(tmp_path, monkeypatch) -> None:
    """codex P1 #652 (compensation.py:522): the restore truncated the file
    before writing; an I/O failure left it damaged, and every retry was then
    refused as stale. It is now a temp-file + atomic rename."""
    import errno

    target = tmp_path / "f.txt"
    target.write_text("original")
    entry, snap, _ = await _snapshotted_write(tmp_path, "f.txt", "agent write")
    real_fsync = os.fsync

    def enospc(fd):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "fsync", enospc)
    res = await compensate_write_file(entry, snap, None)
    assert not res.success
    assert target.read_text() == "agent write"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["f.txt"]
    monkeypatch.setattr(os, "fsync", real_fsync)
    res = await compensate_write_file(entry, snap, None)
    assert res.success, res.message
    assert target.read_text() == "original"


@pytest.mark.asyncio
async def test_revert_fences_off_a_write_orphaned_by_a_cancelled_call(tmp_path) -> None:
    """codex P1 #652 (compensation.py:506): a cancelled write_file's worker
    thread can outlive the call. A revert that found the new file still
    absent marked it reverted -- and the orphan then created it. The revert
    now revokes the call's write fence first, so the orphan can never land."""
    from nous.api.builtin_tools import (
        ABSENT,
        PreconditionFailed,
        _write_fences,
        atomic_replace_bytes,
        register_write_fence,
    )

    entry = uuid4()
    target = tmp_path / "new.txt"
    fence = register_write_fence(str(entry))
    fence.started = True  # the worker thread is running, not yet renamed
    snap = {
        "full_path": str(target),
        "workspace_root": str(tmp_path),
        "existed": False,
        "prior_b64": None,
        **_written("late"),
    }
    res = await compensate_write_file(entry, snap, None)
    assert res.success and "already absent" in res.message
    # the orphaned worker reaches its rename only now
    with pytest.raises(PreconditionFailed, match="revoked"):
        atomic_replace_bytes(target, b"late", expected=ABSENT, fence=fence, root=tmp_path)
    assert not target.exists()
    _write_fences.pop(str(entry), None)


@pytest.mark.asyncio
async def test_symlink_retargeted_while_waiting_for_the_lock_is_not_revertible(tmp_path) -> None:
    """codex P1 #652 (runner.py:539): the per-path lock is keyed on the path
    as it resolved before the call waited for it. Retargeted in between, the
    call held one file's lock while snapshotting and writing another."""
    from unittest.mock import AsyncMock

    from nous.api.compensation import SnapshotBlocksDispatch

    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("a")
    b.write_text("b")
    link = tmp_path / "link.txt"
    link.symlink_to(a)
    outcome = await _held("link.txt", str(tmp_path))  # lock keyed on a.txt
    link.unlink()
    link.symlink_to(b)
    runner = _bare_runner(AsyncMock())
    runner._workspace_dir = str(tmp_path)
    runner._handler_args = lambda name, inp: inp
    try:
        with pytest.raises(SnapshotBlocksDispatch, match="different file"):
            await runner._capture_compensation_snapshot(
                ExecutionContext(kind="dag_node", undoable=True),
                "write_file",
                {"path": "link.txt", "content": "x"},
                uuid4(),
                outcome=outcome,
            )
        runner._snap_store.capture.assert_not_awaited()
    finally:
        release_write_path_lock(outcome.write_lock)


@pytest.mark.asyncio
async def test_write_file_revert_is_idempotent(tmp_path) -> None:
    """codex P2 #652 (a2ui/actions.py:710): a revert whose completion could
    not be recorded left the card live, and every retry was refused as stale
    because the file already held its prior content. A retry now succeeds."""
    target = tmp_path / "f.txt"
    target.write_text("original")
    entry, snap, _ = await _snapshotted_write(tmp_path, "f.txt", "agent")
    assert (await compensate_write_file(entry, snap, None)).success
    again = await compensate_write_file(entry, snap, None)
    assert again.success and "already holds its prior content" in again.message
    assert target.read_text() == "original"


@pytest.mark.asyncio
async def test_resolve_decision_revert_is_idempotent() -> None:
    """codex P2 #652 (a2ui/actions.py:710): the guarded UPDATE matches nothing
    once the decision is back at its prior state; that is success, not a
    stale refusal. Any other state is still refused."""
    from datetime import datetime

    from nous.api.compensation import compensate_resolve_decision

    prior = {
        "outcome": "pending",
        "outcome_result": "earlier",
        "reviewed_at": "2026-09-01T12:00:00+00:00",
        "reviewer": "agent",
        "superseded_by": None,
    }
    at_prior = ("pending", "earlier", datetime.fromisoformat(prior["reviewed_at"]), "agent", None)
    snap = {"decision_id": str(uuid4()), "prior": prior, "written": _WRITTEN_DECISION}
    res = await compensate_resolve_decision(uuid4(), snap, SimpleNamespace(brain=_FakeBrain(0, at_prior)))
    assert res.success and "already carries" in res.message
    other = ("success", "later", datetime.fromisoformat(prior["reviewed_at"]), "agent", None)
    res = await compensate_resolve_decision(uuid4(), snap, SimpleNamespace(brain=_FakeBrain(0, other)))
    assert not res.success and "reviewed again" in res.message


@pytest.mark.asyncio
async def test_check_revert_is_idempotent() -> None:
    """codex P2 #652 (a2ui/actions.py:710): once re-enabled, the token no
    longer matches; the check being enabled again is the revert's goal."""
    from unittest.mock import AsyncMock

    from nous.api.compensation import compensate_heartbeat_check_manage

    snap = {"check_name": "c", "action": "disable", "prior_enabled": True, "written": _WRITTEN_CHECK}
    loader = SimpleNamespace(enable_if_unchanged=AsyncMock(return_value=False), is_enabled=AsyncMock(return_value=True))
    res = await compensate_heartbeat_check_manage(uuid4(), snap, SimpleNamespace(heartbeat_loader=loader))
    assert res.success and "already enabled" in res.message
    loader.is_enabled.assert_awaited_once_with("c", "cid")


@pytest.mark.asyncio
async def test_review_revert_keeps_the_card_live_when_the_revert_cannot_be_recorded() -> None:
    """codex P2 #652 (a2ui/actions.py:710): mark_reverted raising after a
    successful compensator was an unhandled 500. It is retried, and if it
    still fails the card stays live (a retried Revert is idempotent)."""
    from unittest.mock import AsyncMock, patch

    from nous.a2ui.actions import ActionRouter, _register_default_handlers

    router = object.__new__(ActionRouter)
    router._handlers = {}
    router._compensation_registry = registry = CompensationRegistry()
    registry.register("write_file", AsyncMock(return_value=CompensationResult(True, "restored")))
    router._snapshot_store = store = SimpleNamespace(
        get_by_ledger_entry=AsyncMock(
            return_value=SimpleNamespace(id=uuid4(), tool_name="write_file", reverted_at=None, snapshot_data={})
        ),
        mark_reverted=AsyncMock(side_effect=RuntimeError("db down")),
    )
    router._heart = router._brain = router._heartbeat = None
    handlers: dict = {}
    router.register = lambda verb, fn, **kw: handlers.__setitem__(verb, fn)
    _register_default_handlers(router)
    ctx = SimpleNamespace(surface=SimpleNamespace(trace_id=str(uuid4())))
    with patch("nous.a2ui.actions.asyncio.sleep", new=AsyncMock()):
        result = await handlers["review.revert"](ctx)
    assert result.ok is False and not result.resolve_surface
    assert store.mark_reverted.await_count == 3

    store.mark_reverted = AsyncMock(return_value=True)
    result = await handlers["review.revert"](ctx)
    assert result.ok is not False and result.resolve_surface


@pytest.mark.asyncio
async def test_disable_capture_survives_a_cancel_during_its_commit() -> None:
    """codex P1 #652 (runner.py:645): the disable filled its capture only
    after the commit, so a call cancelled mid-commit (outcome unknown, the
    commit may have landed) reported nothing to make it revertible."""
    from unittest.mock import AsyncMock, MagicMock

    from nous.heartbeat.dynamic import DynamicCheckLoader

    loader = object.__new__(DynamicCheckLoader)
    loader._registry = MagicMock(get_check=MagicMock(return_value=None))
    loader._signatures, loader._loaded_ids, loader._id_to_name = {}, set(), {}
    loader._active_runs, loader._mutation_lock = {}, asyncio.Lock()
    model = SimpleNamespace(id=uuid4(), enabled=True, metadata_={}, updated_at=None)
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=model)))
    session.commit = AsyncMock(side_effect=asyncio.CancelledError)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    loader._db = SimpleNamespace(session=lambda: ctx)
    loader._agent_id = "a"
    capture: dict = {}
    with pytest.raises(asyncio.CancelledError):
        await loader.manage_check("disable", name="c", capture=capture)
    assert capture["prior_enabled"] is True
    assert capture["written"]["enabled_state_token"] == model.metadata_["enabled_state_token"]


def test_card_offers_revert_only_when_the_guard_state_is_recorded() -> None:
    """codex P1 #652: a DB call whose written state was lost (outcome
    unknown, or its record failed) gets a card, but never a Revert the
    compensator would refuse."""
    from nous.api.compensation import snapshot_is_revertible

    assert snapshot_is_revertible("write_file", {**_written("x"), "workspace_root": "/ws"})
    # without its workspace root the revert cannot walk to the file safely
    assert not snapshot_is_revertible("write_file", _written("x"))
    assert not snapshot_is_revertible("write_file", {})
    for tool in ("resolve_decision", "heartbeat_check_manage"):
        assert not snapshot_is_revertible(tool, {"prior": {}})
        assert snapshot_is_revertible(tool, {"written": {}})


@pytest.mark.asyncio
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFOs only")
async def test_write_file_over_a_fifo_is_refused_without_blocking(tmp_path) -> None:
    """A FIFO with no writer must not hang the write (or its snapshot) on the
    open that precedes the regular-file check."""
    from nous.api.builtin_tools import write_file_tool

    os.mkfifo(tmp_path / "pipe")
    result = await asyncio.wait_for(write_file_tool("pipe", "x", _workspace_dir=str(tmp_path)), timeout=5)
    assert result.get("is_error") is True
    snap = await asyncio.wait_for(snapshot_for_write_file("pipe", str(tmp_path)), timeout=5)
    assert snap.get("capture_error")


@pytest.mark.parametrize("err", ["EPERM", "ENOTSUP", "EOPNOTSUPP", "EXDEV"])
def test_absent_create_refuses_when_links_are_unsupported(tmp_path, err) -> None:
    """codex P2 #652 (builtin_tools.py:467): with no hard links a create-only
    write fell back to a plain rename, clobbering a file created after the
    absence check (its revert then deleted that file). It now refuses and
    writes nothing; any other link failure still propagates."""
    import errno

    from nous.api.builtin_tools import ABSENT, PreconditionFailed, atomic_replace_bytes

    code = getattr(errno, err)

    def _link_after_concurrent_create(*a, **k):
        (tmp_path / "a.txt").write_bytes(b"theirs")  # created after the absence check
        raise OSError(code, os.strerror(code))

    with (
        patch("nous.api.builtin_tools.os.link", _link_after_concurrent_create),
        pytest.raises(PreconditionFailed, match="no-clobber"),
    ):
        atomic_replace_bytes(tmp_path / "a.txt", b"ours", expected=ABSENT, root=tmp_path)
    assert (tmp_path / "a.txt").read_bytes() == b"theirs"

    def _link_unsupported(*a, **k):
        raise OSError(code, os.strerror(code))

    with patch("nous.api.builtin_tools.os.link", _link_unsupported), pytest.raises(PreconditionFailed):
        atomic_replace_bytes(tmp_path / "b.txt", b"b", expected=ABSENT, root=tmp_path)
    assert not (tmp_path / "b.txt").exists()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".write_file_")]


def test_absent_create_link_failures_and_success(tmp_path) -> None:
    """Other link failures propagate and write nothing; with working links an
    ABSENT create still lands, and an overwrite (hash precondition) never
    needs a link."""
    import errno
    import hashlib

    from nous.api.builtin_tools import ABSENT, atomic_replace_bytes

    def _enospc(*a, **k):
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    with patch("nous.api.builtin_tools.os.link", _enospc), pytest.raises(OSError):
        atomic_replace_bytes(tmp_path / "b.txt", b"b", expected=ABSENT, root=tmp_path)
    assert not (tmp_path / "b.txt").exists()

    atomic_replace_bytes(tmp_path / "c.txt", b"c", expected=ABSENT, root=tmp_path)
    assert (tmp_path / "c.txt").read_bytes() == b"c"
    with patch("nous.api.builtin_tools.os.link", _enospc):
        atomic_replace_bytes(tmp_path / "c.txt", b"c2", expected=hashlib.sha256(b"c").hexdigest(), root=tmp_path)
    assert (tmp_path / "c.txt").read_bytes() == b"c2"
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".write_file_")]


@pytest.mark.asyncio
async def test_write_file_tool_refuses_absent_create_without_links(tmp_path) -> None:
    """Through the tool: a snapshotted create-only write on a filesystem with
    no hard links is reported failed (so no card claims a revertible change)
    and leaves a concurrently created file untouched."""
    import errno
    from unittest.mock import AsyncMock

    from nous.api import call_outcome
    from nous.api.builtin_tools import write_file_tool

    runner = _bare_runner(AsyncMock())
    runner._workspace_dir = str(tmp_path)
    outcome = await _held("new.txt", str(tmp_path))
    assert await runner._capture_compensation_snapshot(
        ExecutionContext(kind="subtask"), "write_file", {"path": "new.txt", "content": "ours"}, uuid4(), outcome=outcome
    )

    def _link(*a, **k):
        (tmp_path / "new.txt").write_text("theirs")
        raise OSError(errno.EPERM, os.strerror(errno.EPERM))

    token = call_outcome._current.set(outcome)
    try:
        with patch("nous.api.builtin_tools.os.link", _link):
            result = await write_file_tool("new.txt", "ours", _workspace_dir=str(tmp_path))
    finally:
        call_outcome._current.reset(token)
    assert result.get("is_error") is True
    assert "nothing was written" in result["content"][0]["text"]
    assert (tmp_path / "new.txt").read_text() == "theirs"
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".write_file_")]


@pytest.mark.asyncio
async def test_write_cancelled_before_its_worker_began_revokes_and_drops_the_fence(tmp_path) -> None:
    """A write_file cancelled before its worker thread began may never run,
    so its fence would never be dropped. It is revoked (a late start refuses)
    and dropped."""
    from nous.api import call_outcome
    from nous.api.builtin_tools import ABSENT, _write_fences, register_write_fence, write_file_tool
    from nous.api.call_outcome import CallOutcome

    key = str(uuid4())
    fence = register_write_fence(key)
    outcome = CallOutcome(write_target=str(tmp_path / "n.txt"), write_expected=ABSENT, write_fence=fence)
    token = call_outcome._current.set(outcome)
    try:
        with patch("nous.api.builtin_tools.asyncio.to_thread", side_effect=asyncio.CancelledError):
            with pytest.raises(asyncio.CancelledError):
                await write_file_tool("n.txt", "x", _workspace_dir=str(tmp_path))
    finally:
        call_outcome._current.reset(token)
    assert fence.revoked and key not in _write_fences
    assert not (tmp_path / "n.txt").exists()


# ---------------------------------------------------------------------------
# codex P1 #652 round 9: every directory below the workspace is walked
# O_NOFOLLOW, and a cancelled write keeps its path lock until its thread ends
# ---------------------------------------------------------------------------


def _swap_ancestor_for_symlink(ws, outside, rel_file: str, content: str | None) -> None:
    """Replace ``ws/a`` (two levels above ``rel_file``) with a symlink to
    ``outside``, where the same relative file holds ``content``."""
    import shutil

    shutil.rmtree(ws / "a")
    dest = outside / rel_file.split("/", 1)[1]
    dest.parent.mkdir(parents=True, exist_ok=True)
    if content is not None:
        dest.write_text(content)
    (ws / "a").symlink_to(outside, target_is_directory=True)


def test_forward_write_refuses_an_ancestor_swapped_for_a_symlink(tmp_path) -> None:
    """codex P1 (builtin_tools.py:276): O_NOFOLLOW on the full parent path
    guarded only its last component. An ancestor two levels up swapped for a
    symlink after validation now fails the walk; nothing outside is touched."""
    from nous.api.builtin_tools import PreconditionFailed, atomic_replace_bytes

    ws, outside = tmp_path / "ws", tmp_path / "outside"
    ws.mkdir()
    outside.mkdir()
    target = ws / "a" / "b" / "f.txt"
    target.parent.mkdir(parents=True)
    target.write_text("inside")
    _swap_ancestor_for_symlink(ws, outside, "a/b/f.txt", "outside original")
    for expected in (None, _h("outside original")):
        with pytest.raises(PreconditionFailed, match="symlink"):
            atomic_replace_bytes(target, b"evil", expected=expected, root=ws)
    with pytest.raises(PreconditionFailed, match="symlink"):
        atomic_replace_bytes(ws / "a" / "b" / "new.txt", b"evil", expected=None, root=ws)
    assert (outside / "b" / "f.txt").read_text() == "outside original"
    assert sorted(p.name for p in (outside / "b").iterdir()) == ["f.txt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("existed", [True, False])
async def test_revert_refuses_an_ancestor_swapped_for_a_symlink(tmp_path, existed) -> None:
    """The revert's hash read, restore and delete walk from the workspace
    root: a matching file reached through a swapped ancestor is neither
    replaced nor deleted."""
    from nous.api.builtin_tools import PreconditionFailed, file_digest, remove_if_matches

    ws, outside = tmp_path / "ws", tmp_path / "outside"
    ws.mkdir()
    outside.mkdir()
    (ws / "a" / "b").mkdir(parents=True)
    if existed:
        (ws / "a" / "b" / "f.txt").write_text("original")
    entry, snap, result = await _snapshotted_write(ws, "a/b/f.txt", "agent")
    assert not result.get("is_error")
    assert snap["workspace_root"] == str(ws.resolve())
    _swap_ancestor_for_symlink(ws, outside, "a/b/f.txt", "agent")  # same bytes: the hash alone would pass
    res = await compensate_write_file(entry, snap, None)
    assert not res.success
    assert (outside / "b" / "f.txt").read_text() == "agent"
    target = ws / "a" / "b" / "f.txt"
    with pytest.raises(PreconditionFailed, match="symlink"):
        file_digest(target, 100, root=ws)
    with pytest.raises(PreconditionFailed, match="symlink"):
        remove_if_matches(target, _h("agent"), limit=100, root=ws)
    assert (outside / "b" / "f.txt").read_text() == "agent"


@pytest.mark.asyncio
async def test_nested_writes_and_reverts_still_work(tmp_path) -> None:
    """Missing directories are created by the walk; a nested file reverts."""
    entry, snap, result = await _snapshotted_write(tmp_path, "x/y/z/new.txt", "hello")
    assert not result.get("is_error"), result
    assert (tmp_path / "x" / "y" / "z" / "new.txt").read_text() == "hello"
    res = await compensate_write_file(entry, snap, None)
    assert res.success, res.message
    assert not (tmp_path / "x" / "y" / "z" / "new.txt").exists()


def _gate_first_write(monkeypatch):
    """Block the worker thread of the write whose data is b"first"."""
    import threading

    from nous.api import builtin_tools

    real = builtin_tools.atomic_replace_bytes
    entered, gate = threading.Event(), threading.Event()

    def gated(target, data, **kw):
        if data == b"first":
            entered.set()
            assert gate.wait(10)
        return real(target, data, **kw)

    monkeypatch.setattr(builtin_tools, "atomic_replace_bytes", gated)
    return entered, gate


async def _start_write(runner, ws, path: str, content: str):
    """The runner's sequence up to dispatch: lock, snapshot, then the handler
    as a task (it inherits the bound CallOutcome)."""
    from nous.api import call_outcome
    from nous.api.builtin_tools import write_file_tool

    outcome = await _held(path, str(ws))
    entry = uuid4()
    assert await runner._capture_compensation_snapshot(
        ExecutionContext(kind="subtask"), "write_file", {"path": path, "content": content}, entry, outcome=outcome
    )
    snap = runner._snap_store.capture.await_args.kwargs["snapshot_data"]
    token = call_outcome._current.set(outcome)
    try:
        task = asyncio.create_task(write_file_tool(path, content, _workspace_dir=str(ws)))
    finally:
        call_outcome._current.reset(token)
    return outcome, entry, snap, task


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["cancel", "timeout"])
async def test_cancelled_write_keeps_its_path_lock_until_the_worker_finishes(tmp_path, monkeypatch, how) -> None:
    """codex P1 (runner.py:3182): the path lock was released as soon as the
    cancelled call returned, while its worker thread could still rename. A
    second write then snapshotted the same pre-state and the orphan landed
    over it. The lock is now released only when the worker is done."""
    from unittest.mock import AsyncMock

    from nous.api.compensation import release_write_path_lock_after

    (tmp_path / "f.txt").write_text("v0")
    entered, gate = _gate_first_write(monkeypatch)
    runner1 = _bare_runner(AsyncMock())
    runner1._workspace_dir = str(tmp_path)
    runner1._handler_args = lambda name, inp: inp
    out1, entry1, snap1, task1 = await _start_write(runner1, tmp_path, "f.txt", "first")
    assert await asyncio.to_thread(entered.wait, 5)
    if how == "cancel":
        task1.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task1
    else:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(task1, 0.05)
    # the runner's finally: the caller has already returned, the lock has not
    release_write_path_lock_after(out1.write_lock, out1.write_worker)
    assert out1.write_lock.locked()

    runner2 = _bare_runner(AsyncMock())
    runner2._workspace_dir = str(tmp_path)
    runner2._handler_args = lambda name, inp: inp

    async def second():
        out2, entry2, snap2, task2 = await _start_write(runner2, tmp_path, "f.txt", "second")
        try:
            return entry2, snap2, await task2
        finally:
            release_write_path_lock_after(out2.write_lock, out2.write_worker)

    t2 = asyncio.create_task(second())
    await asyncio.sleep(0.2)
    assert not t2.done()
    runner2._snap_store.capture.assert_not_awaited()  # no snapshot while the orphan can rename
    assert (tmp_path / "f.txt").read_text() == "v0"

    gate.set()
    entry2, snap2, result2 = await asyncio.wait_for(t2, 10)
    assert not result2.get("is_error"), result2
    assert (tmp_path / "f.txt").read_text() == "second"
    # both snapshots stay consistent: each records what the other left behind
    assert _prior_text(snap1) == "v0" and _prior_text(snap2) == "first"
    assert (await compensate_write_file(entry2, {**snap2, **_written("second")}, None)).success
    assert (tmp_path / "f.txt").read_text() == "first"
    assert (await compensate_write_file(entry1, {**snap1, **_written("first")}, None)).success
    assert (tmp_path / "f.txt").read_text() == "v0"


@pytest.mark.asyncio
async def test_deferred_path_lock_is_released_when_the_orphan_finishes(tmp_path, monkeypatch) -> None:
    from unittest.mock import AsyncMock

    from nous.api.compensation import _write_path_locks, release_write_path_lock_after, write_path_key

    entered, gate = _gate_first_write(monkeypatch)
    runner = _bare_runner(AsyncMock())
    runner._workspace_dir = str(tmp_path)
    runner._handler_args = lambda name, inp: inp
    out, _, _, task = await _start_write(runner, tmp_path, "g.txt", "first")
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release_write_path_lock_after(out.write_lock, out.write_worker)
    assert out.write_lock.locked() and not out.write_worker.done()
    gate.set()
    await asyncio.wait_for(asyncio.shield(out.write_worker), 10)
    await asyncio.sleep(0)  # the done-callback runs on the next loop pass
    assert not out.write_lock.locked()
    assert write_path_key("g.txt", str(tmp_path)) not in _write_path_locks
    assert (tmp_path / "g.txt").read_text() == "first"


def test_runner_releases_write_locks_only_after_the_worker() -> None:
    """Both runner tool loops route their path-lock release through
    release_write_path_lock_after with the call's worker."""
    import inspect

    from nous.api import runner

    src = inspect.getsource(runner)
    assert src.count("release_write_path_lock_after(_write_lock, outcome.write_worker)") == 1
    assert src.count("release_write_path_lock_after(_write_lock2, outcome.write_worker)") == 1
    assert "release_write_path_lock(" not in src


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the container is Linux")
def test_descriptor_relative_operations_are_enabled_on_linux() -> None:
    """os.replace is never listed in os.supports_dir_fd; probing it silently
    turned every write/revert into the path-based fallback."""
    from nous.api import builtin_tools

    assert builtin_tools._DIR_FD
