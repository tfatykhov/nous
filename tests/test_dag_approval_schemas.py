"""Harness Phase 3 §3.1: approval-node validation and insert."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from pydantic import ValidationError

from nous.config import Settings
from nous.dag.schemas import DAGCreateRequest, DAGEdgeSpec, DAGNodeSpec, DAGNodeType
from nous.dag.store import DAGStore

_OPTIONS = [
    {"id": "send", "label": "Send it", "outcome": "proceed"},
    {"id": "hold", "label": "Don't send", "outcome": "stop"},
]


def _approval(name: str = "approve", **overrides) -> DAGNodeSpec:
    base = dict(
        name=name, type=DAGNodeType.approval, instructions="Send the drafted email?",
        options=_OPTIONS, default_option="hold",
    )
    base.update(overrides)
    return DAGNodeSpec(**base)


def _send(**overrides) -> DAGNodeSpec:
    base = dict(name="send", type=DAGNodeType.subtask, instructions="send it")
    base.update(overrides)
    return DAGNodeSpec(**base)


def _gated(approval: DAGNodeSpec | None = None, extra=(), edges=None) -> DAGCreateRequest:
    return DAGCreateRequest(
        name="gated",
        nodes=[approval or _approval(), _send(), *extra],
        edges=edges
        if edges is not None
        else [DAGEdgeSpec(from_node="approve", to_node="send", edge_type="context_flow")],
    )


def test_a_well_formed_approval_dag_validates():
    assert _gated().nodes[0].default_option == "hold"


@pytest.mark.parametrize(
    "overrides, match",
    [
        ({"default_option": None}, "default_option"),
        ({"default_option": "nope"}, "default_option"),
        ({"options": [_OPTIONS[1], {"id": "x", "label": "X", "outcome": "stop"}]}, "'proceed'"),
        ({"options": [_OPTIONS[1]]}, "2-4"),
        ({"options": _OPTIONS * 3}, "2-4"),
        ({"options": [_OPTIONS[0], dict(_OPTIONS[1], id="send")]}, "unique"),
        ({"recommended_option": "nope"}, "recommended_option"),
        ({"instructions": "   "}, "question"),
        ({"instructions": "x" * 2001}, "2000"),
        ({"answer_timeout_seconds": 899}, "900"),
        ({"timeout_seconds": 60}, "timeout_seconds"),
        ({"tools": ["bash"]}, "tools"),
        ({"model": "m"}, "model"),
    ],
)
def test_bad_approval_nodes_are_rejected(overrides, match):
    with pytest.raises(ValidationError, match=match):
        _approval(**overrides)


def test_proceed_default_is_rejected_at_dag_level_when_flag_is_off():
    """A proceed-default passes node-level validation but is rejected when
    the full DAG is assembled and the feature flag is off (default)."""
    with pytest.raises(ValidationError, match="must be a 'stop' option"):
        _gated(_approval(default_option="send"))


def test_empty_values_count_as_not_given():
    """LLM-authored JSON emits [] / 0 for "none"; that must not fail the DAG."""
    assert _approval(tools=[], stall_timeout_seconds=0, fix_actions=[]).tools == []
    assert _send(options=[]).options == []


def test_bad_option_id_is_rejected():
    with pytest.raises(ValidationError):
        _approval(options=[{"id": "Send It", "label": "x", "outcome": "proceed"}, _OPTIONS[1]])


def test_approval_fields_are_rejected_on_other_types():
    with pytest.raises(ValidationError, match="only on approval nodes"):
        _send(default_option="hold")


def test_an_approval_that_gates_nothing_is_rejected():
    with pytest.raises(ValidationError, match="gates nothing"):
        _gated(edges=[])


def test_a_fix_node_cannot_attach_to_an_approval():
    fix = DAGNodeSpec(
        name="fix", type=DAGNodeType.fix, parent_node="approve", fix_actions=["retry_as_is"]
    )
    edges = [
        DAGEdgeSpec(from_node="approve", to_node="send", edge_type="context_flow"),
        DAGEdgeSpec(from_node="approve", to_node="fix", edge_type="on_failure"),
    ]
    with pytest.raises(ValidationError, match="cannot attach to approval"):
        _gated(extra=[fix], edges=edges)


@pytest.mark.parametrize("action, ok", [("retry_with_amended_prompt", False), ("retry_as_is", True)])
def test_a_fix_below_an_approval_may_not_amend_the_approved_step(action, ok):
    fix = DAGNodeSpec(name="fix", type=DAGNodeType.fix, parent_node="send", fix_actions=[action])
    edges = [
        DAGEdgeSpec(from_node="approve", to_node="send", edge_type="context_flow"),
        DAGEdgeSpec(from_node="send", to_node="fix", edge_type="on_failure"),
    ]
    if ok:
        _gated(extra=[fix], edges=edges)
    else:
        with pytest.raises(ValidationError, match="retry_with_amended_prompt"):
            _gated(extra=[fix], edges=edges)


def test_settings_reject_a_default_wait_above_the_max():
    with pytest.raises(ValidationError, match="dag_approval_default_wait_seconds"):
        Settings(
            _env_file=None, dag_approval_default_wait_seconds=10_000,
            dag_approval_max_wait_seconds=5_000,
        )


@pytest_asyncio.fixture
async def store(db):
    settings = Settings(
        _env_file=None, dag_node_default_timeout=120, dag_node_max_timeout=3600,
        dag_approval_default_wait_seconds=3600, dag_approval_max_wait_seconds=7200,
    )
    return DAGStore(db, f"test-p3schema-{uuid.uuid4().hex[:8]}", settings)


async def test_create_stores_the_spec_and_keeps_the_not_null_timeout(store):
    dag = await store.create(_gated(_approval(answer_timeout_seconds=90_000)))
    node = next(n for n in dag.nodes if n.name == "approve")

    assert node.node_type == "approval"
    assert node.timeout_seconds == 120  # NOT NULL column keeps its default
    assert node.stall_timeout_seconds is None
    assert node.approval_spec["default_option"] == "hold"
    assert node.approval_spec["answer_timeout_seconds"] == 7200  # clamped
    assert [o["outcome"] for o in node.approval_spec["options"]] == ["proceed", "stop"]
