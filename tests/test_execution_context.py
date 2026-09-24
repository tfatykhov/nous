"""Harness Phase 1a: every turn knows which harness path runs it."""

import uuid
from types import SimpleNamespace

import pytest

from nous.api.execution_context import (
    CONTEXT_KINDS,
    FOREGROUND_KINDS,
    ExecutionContext,
    resolve_context,
)


def _subtask(*, metadata=None, dag_node_id=None, sid=None, parent=None):
    return SimpleNamespace(
        id=sid or uuid.uuid4(),
        metadata_=metadata if metadata is not None else {},
        dag_node_id=dag_node_id,
        parent_session_id=parent,
    )


def test_foreground_kinds_are_exactly_interactive_and_mcp():
    assert FOREGROUND_KINDS == frozenset({"interactive", "mcp"})
    for kind in CONTEXT_KINDS:
        assert ExecutionContext(kind=kind).is_background is (kind not in FOREGROUND_KINDS)


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError, match="unknown execution context kind"):
        ExecutionContext(kind="daemon")  # type: ignore[arg-type]


def test_context_is_immutable():
    ctx = ExecutionContext(kind="subtask")
    with pytest.raises(AttributeError):
        ctx.kind = "interactive"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("metadata", "has_node", "expected"),
    [
        ({"dag_id": str(uuid.uuid4()), "node_name": "fetch"}, True, "dag_node"),
        ({"dag_id": str(uuid.uuid4()), "node_name": "fetch"}, False, "dag_node"),
        ({"a2ui_surface_id": "s1", "a2ui_action_id": "rebalance", "max_attempts": 1}, False, "agent_action"),
        ({"schedule_id": "ab12"}, False, "scheduled"),
        ({"schedule_id": "ab12", "session_id": "schedule-ab12"}, False, "scheduled"),
        ({}, False, "subtask"),
    ],
)
def test_for_subtask_derives_kind_from_the_row(metadata, has_node, expected):
    node_id = uuid.uuid4() if has_node else None
    ctx = ExecutionContext.for_subtask(_subtask(metadata=metadata, dag_node_id=node_id), "subtask-1")
    assert ctx.kind == expected and ctx.session_id == "subtask-1"


def test_for_subtask_carries_ids_and_the_spawning_session():
    dag_id, node_id, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    ctx = ExecutionContext.for_subtask(
        _subtask(
            metadata={"dag_id": str(dag_id), "node_name": "send"},
            dag_node_id=node_id,
            sid=sid,
            parent="dag-summary-1a2b3c4d",
        ),
        "subtask-x",
    )
    assert (ctx.subtask_id, ctx.dag_id, ctx.dag_node_id, ctx.dag_node_name) == (sid, dag_id, node_id, "send")
    assert ctx.parent_session_id == "dag-summary-1a2b3c4d"


def test_for_subtask_tolerates_legacy_rows():
    assert ExecutionContext.for_subtask(SimpleNamespace(id=uuid.uuid4()), "s").kind == "subtask"
    row = SimpleNamespace(id=uuid.uuid4(), metadata_=None, dag_node_id=None)
    assert ExecutionContext.for_subtask(row, "s").kind == "subtask"
    ctx = ExecutionContext.for_subtask(_subtask(metadata={"dag_id": "not-a-uuid"}), "s")
    assert ctx.kind == "dag_node" and ctx.dag_id is None


def test_resolve_context_defaults():
    assert resolve_context(None, is_background=False, session_id="s").kind == "interactive"
    bg = resolve_context(None, is_background=True, session_id="s")
    assert bg.kind == "background" and bg.session_id == "s"


def test_resolve_context_passes_an_explicit_context_through():
    ctx = ExecutionContext(kind="heartbeat_triage", session_id="h")
    assert resolve_context(ctx, is_background=True, session_id="other") is ctx
    assert resolve_context(ctx, is_background=False, session_id="h") is ctx


def test_resolve_context_rejects_a_contradiction():
    with pytest.raises(ValueError, match="contradicts"):
        resolve_context(ExecutionContext(kind="interactive"), is_background=True, session_id="s")
    with pytest.raises(ValueError, match="contradicts"):
        resolve_context(ExecutionContext(kind="mcp"), is_background=True, session_id="s")
