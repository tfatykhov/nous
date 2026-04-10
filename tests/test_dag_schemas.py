"""Tests for F038 DAG Orchestration: ORM models and Pydantic schemas."""

from __future__ import annotations

import uuid

import pytest

from nous.dag.schemas import (
    DAGCreateRequest,
    DAGEdgeSpec,
    DAGNodeSpec,
    DAGNodeStatus,
    DAGNodeType,
    DAGStatus,
)
from nous.storage.models import DAGEdge, DAGNode, ExecutionDAG


# ---------------------------------------------------------------------------
# ORM model defaults (in-memory, no DB required)
# ---------------------------------------------------------------------------


class TestExecutionDAGModel:
    """Test ExecutionDAG ORM model defaults."""

    def test_defaults(self):
        dag = ExecutionDAG(agent_id="test", name="test-dag")
        assert dag.status == "pending"
        assert dag.source == "conversation"
        assert dag.tokens_consumed == 0
        assert dag.description == ""
        assert dag.original_request is None
        assert dag.token_budget is None
        assert dag.result_summary is None
        assert dag.postmortem is None

    def test_fields(self):
        dag = ExecutionDAG(
            agent_id="agent-1",
            name="my-dag",
            description="A test DAG",
            source="heartbeat",
            token_budget=5000,
        )
        assert dag.agent_id == "agent-1"
        assert dag.name == "my-dag"
        assert dag.description == "A test DAG"
        assert dag.source == "heartbeat"
        assert dag.token_budget == 5000


class TestDAGNodeModel:
    """Test DAGNode ORM model defaults."""

    def test_defaults(self):
        node = DAGNode(
            dag_id=uuid.uuid4(),
            name="node-1",
            node_type="subtask",
        )
        assert node.status == "pending"
        assert node.wave == 0
        assert node.timeout_seconds == 120
        assert node.tokens_used == 0
        assert node.description == ""
        assert node.instructions is None
        assert node.tools is None
        assert node.frame_type is None
        assert node.model is None
        assert node.result is None
        assert node.error is None

    def test_fields(self):
        dag_id = uuid.uuid4()
        node = DAGNode(
            dag_id=dag_id,
            name="checker",
            node_type="check",
            check_name="health-check",
            wave=2,
            timeout_seconds=60,
            frame_type="debug",
            model="claude-haiku-4-5-20251001",
        )
        assert node.dag_id == dag_id
        assert node.node_type == "check"
        assert node.check_name == "health-check"
        assert node.wave == 2
        assert node.timeout_seconds == 60


class TestDAGEdgeModel:
    """Test DAGEdge ORM model defaults."""

    def test_defaults(self):
        edge = DAGEdge(
            dag_id=uuid.uuid4(),
            from_node_id=uuid.uuid4(),
            to_node_id=uuid.uuid4(),
        )
        assert edge.edge_type == "dependency"

    def test_fields(self):
        edge = DAGEdge(
            dag_id=uuid.uuid4(),
            from_node_id=uuid.uuid4(),
            to_node_id=uuid.uuid4(),
            edge_type="cancel_cascade",
        )
        assert edge.edge_type == "cancel_cascade"


# ---------------------------------------------------------------------------
# Pydantic enums
# ---------------------------------------------------------------------------


class TestEnums:
    """Test enum values."""

    def test_node_types(self):
        assert set(DAGNodeType) == {
            DAGNodeType.subtask,
            DAGNodeType.check,
            DAGNodeType.gate,
            DAGNodeType.callback,
        }

    def test_dag_statuses(self):
        assert set(DAGStatus) == {
            DAGStatus.pending,
            DAGStatus.running,
            DAGStatus.completed,
            DAGStatus.failed,
            DAGStatus.cancelled,
            DAGStatus.partial,
        }

    def test_node_statuses(self):
        assert set(DAGNodeStatus) == {
            DAGNodeStatus.pending,
            DAGNodeStatus.ready,
            DAGNodeStatus.running,
            DAGNodeStatus.completed,
            DAGNodeStatus.failed,
            DAGNodeStatus.blocked,
            DAGNodeStatus.cancelled,
        }


# ---------------------------------------------------------------------------
# DAGCreateRequest validation
# ---------------------------------------------------------------------------


def _node(name: str, type: str = "subtask") -> DAGNodeSpec:
    """Helper to create a minimal node spec."""
    return DAGNodeSpec(name=name, type=DAGNodeType(type))


class TestDAGCreateRequest:
    """Test DAGCreateRequest validation and wave computation."""

    def test_valid_simple(self):
        req = DAGCreateRequest(
            name="simple",
            nodes=[_node("a"), _node("b")],
        )
        assert req.name == "simple"
        assert len(req.nodes) == 2
        assert len(req.edges) == 0

    def test_valid_with_edges(self):
        req = DAGCreateRequest(
            name="chained",
            nodes=[_node("a"), _node("b"), _node("c")],
            edges=[
                DAGEdgeSpec(from_node="a", to_node="b"),
                DAGEdgeSpec(from_node="b", to_node="c"),
            ],
        )
        waves = req.compute_waves()
        assert waves["a"] == 0
        assert waves["b"] == 1
        assert waves["c"] == 2

    def test_cycle_detection(self):
        with pytest.raises(ValueError, match="cycle"):
            DAGCreateRequest(
                name="cyclic",
                nodes=[_node("a"), _node("b"), _node("c")],
                edges=[
                    DAGEdgeSpec(from_node="a", to_node="b"),
                    DAGEdgeSpec(from_node="b", to_node="c"),
                    DAGEdgeSpec(from_node="c", to_node="a"),
                ],
            )

    def test_too_many_nodes(self):
        nodes = [_node(f"n{i}") for i in range(11)]
        with pytest.raises(ValueError, match="more than 10"):
            DAGCreateRequest(name="big", nodes=nodes)

    def test_no_nodes(self):
        with pytest.raises(ValueError):
            DAGCreateRequest(name="empty", nodes=[])

    def test_duplicate_node_names(self):
        with pytest.raises(ValueError, match="[Dd]uplicate"):
            DAGCreateRequest(
                name="dupes",
                nodes=[_node("a"), _node("a")],
            )

    def test_unknown_edge_from_node(self):
        with pytest.raises(ValueError, match="unknown node.*ghost"):
            DAGCreateRequest(
                name="bad-edge",
                nodes=[_node("a")],
                edges=[DAGEdgeSpec(from_node="ghost", to_node="a")],
            )

    def test_unknown_edge_to_node(self):
        with pytest.raises(ValueError, match="unknown node.*ghost"):
            DAGCreateRequest(
                name="bad-edge",
                nodes=[_node("a")],
                edges=[DAGEdgeSpec(from_node="a", to_node="ghost")],
            )

    def test_self_loop(self):
        with pytest.raises(ValueError, match="[Ss]elf-loop"):
            DAGCreateRequest(
                name="self-loop",
                nodes=[_node("a")],
                edges=[DAGEdgeSpec(from_node="a", to_node="a")],
            )

    def test_wave_computation_parallel(self):
        """Nodes with no dependencies all get wave 0."""
        req = DAGCreateRequest(
            name="parallel",
            nodes=[_node("a"), _node("b"), _node("c")],
        )
        waves = req.compute_waves()
        assert waves == {"a": 0, "b": 0, "c": 0}

    def test_wave_computation_diamond(self):
        """Diamond pattern: a -> b, a -> c, b -> d, c -> d."""
        req = DAGCreateRequest(
            name="diamond",
            nodes=[_node("a"), _node("b"), _node("c"), _node("d")],
            edges=[
                DAGEdgeSpec(from_node="a", to_node="b"),
                DAGEdgeSpec(from_node="a", to_node="c"),
                DAGEdgeSpec(from_node="b", to_node="d"),
                DAGEdgeSpec(from_node="c", to_node="d"),
            ],
        )
        waves = req.compute_waves()
        assert waves["a"] == 0
        assert waves["b"] == 1
        assert waves["c"] == 1
        assert waves["d"] == 2

    def test_max_waves_exceeded(self):
        """Chain of 5 nodes = waves 0-4, exceeds max 4 waves (0-3)."""
        with pytest.raises(ValueError, match="exceeds maximum"):
            DAGCreateRequest(
                name="deep-chain",
                nodes=[_node(f"n{i}") for i in range(5)],
                edges=[
                    DAGEdgeSpec(from_node=f"n{i}", to_node=f"n{i + 1}")
                    for i in range(4)
                ],
            )

    def test_max_parallel_per_wave_exceeded(self):
        """5 independent nodes all in wave 0 exceeds max 4 parallel."""
        with pytest.raises(ValueError, match="parallel nodes"):
            DAGCreateRequest(
                name="wide",
                nodes=[_node(f"n{i}") for i in range(5)],
            )

    def test_non_dependency_edges_skip_wave_computation(self):
        """cancel_cascade and context_flow edges don't affect wave assignment."""
        req = DAGCreateRequest(
            name="mixed-edges",
            nodes=[_node("a"), _node("b")],
            edges=[
                DAGEdgeSpec(from_node="a", to_node="b", edge_type="cancel_cascade"),
            ],
        )
        waves = req.compute_waves()
        # Both in wave 0 because cancel_cascade is not a dependency
        assert waves["a"] == 0
        assert waves["b"] == 0

    def test_source_values(self):
        for source in ("conversation", "critic", "heartbeat", "schedule"):
            req = DAGCreateRequest(
                name="test",
                nodes=[_node("a")],
                source=source,
            )
            assert req.source == source

    def test_node_spec_fields(self):
        node = DAGNodeSpec(
            name="research",
            type=DAGNodeType.subtask,
            instructions="Search the web",
            description="Research step",
            tools=["web_search", "web_fetch"],
            frame_type="task",
            model="claude-sonnet-4-6",
            timeout_seconds=300,
            completion_condition="contains_url",
        )
        assert node.name == "research"
        assert node.type == DAGNodeType.subtask
        assert node.timeout_seconds == 300
        assert node.tools == ["web_search", "web_fetch"]

    def test_edge_spec_default_type(self):
        edge = DAGEdgeSpec(from_node="a", to_node="b")
        assert edge.edge_type == "dependency"

    def test_max_allowed_waves(self):
        """Chain of 4 nodes = waves 0-3, should be valid (exactly at limit)."""
        req = DAGCreateRequest(
            name="max-chain",
            nodes=[_node(f"n{i}") for i in range(4)],
            edges=[
                DAGEdgeSpec(from_node=f"n{i}", to_node=f"n{i + 1}")
                for i in range(3)
            ],
        )
        waves = req.compute_waves()
        assert waves["n0"] == 0
        assert waves["n3"] == 3

    def test_max_allowed_parallel(self):
        """4 independent nodes = exactly at limit, should be valid."""
        req = DAGCreateRequest(
            name="max-parallel",
            nodes=[_node(f"n{i}") for i in range(4)],
        )
        waves = req.compute_waves()
        assert all(w == 0 for w in waves.values())
