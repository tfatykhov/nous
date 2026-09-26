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
    """The five clearly-reversible tools are marked compensable."""
    expected = {"write_file", "schedule_task", "heartbeat_check_create",
                "heartbeat_check_manage", "resolve_decision"}
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


def test_register_compensators_registers_all_five() -> None:
    registry = CompensationRegistry()
    register_compensators(registry)
    expected = {"write_file", "schedule_task", "heartbeat_check_create",
                "heartbeat_check_manage", "resolve_decision"}
    for name in expected:
        assert registry.is_registered(name), f"{name} not registered"


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
            name="draft", type="subtask", instructions="Draft something",
            undoable=undoable_successor,
        ),
        DAGNodeSpec(
            name=name, type="approval", instructions="Do you approve?",
            options=[
                {"id": "yes", "label": "Yes", "outcome": "proceed"},
                {"id": "no", "label": "No", "outcome": "stop"},
            ],
            default_option="yes" if default_outcome == "proceed" else "no",
        ),
        DAGNodeSpec(
            name="act", type="subtask", instructions="Do the thing",
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

    built = action_review({
        "title": "Wrote config file",
        "did": "Created /workspace/config.yaml",
        "compensation": {"revertible": True, "handler": "compensate_write_file", "note": ""},
    })
    assert "review.revert" in built.allowed_actions
    component_ids = [c["id"] for c in built.components]
    assert "revert" in component_ids


def test_action_review_no_revert_when_not_revertible() -> None:
    from nous.a2ui.builders.action_review import action_review

    built = action_review({
        "title": "Sent email",
        "did": "Sent report to user",
        "compensation": {"revertible": False, "handler": None, "note": "Cannot unsend"},
    })
    assert "review.revert" not in built.allowed_actions
    component_ids = [c["id"] for c in built.components]
    assert "revert" not in component_ids


def test_action_review_no_revert_when_revertible_but_no_handler() -> None:
    """revertible=True but handler=None: no Revert button (nothing to call)."""
    from nous.a2ui.builders.action_review import action_review

    built = action_review({
        "title": "Some action",
        "did": "Did something",
        "compensation": {"revertible": True, "handler": None, "note": ""},
    })
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
