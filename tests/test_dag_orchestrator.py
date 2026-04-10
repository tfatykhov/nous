"""Tests for F038 DAG Orchestrator state machine."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from nous.dag.orchestrator import DAGOrchestrator
from nous.dag.schemas import (
    DAGCreateRequest,
    DAGEdgeSpec,
    DAGNodeSpec,
    DAGNodeType,
)
from nous.dag.store import DAGStore

@pytest_asyncio.fixture
async def store(db):
    """DAGStore instance with unique agent_id per test."""
    agent_id = f"test-dag-orch-{uuid.uuid4().hex[:8]}"
    return DAGStore(db, agent_id)


@pytest.fixture
def subtask_mgr():
    """Mock SubtaskManager."""
    mgr = AsyncMock()
    # create() returns a mock subtask with id
    mgr.create.return_value = SimpleNamespace(id=uuid.uuid4(), status="pending")
    return mgr


@pytest.fixture
def dynamic_loader():
    """Mock DynamicCheckLoader."""
    loader = AsyncMock()
    loader.create_check = AsyncMock(return_value={"name": "test-check"})
    loader._registry = MagicMock()
    loader._registry.get_check.return_value = None
    return loader


@pytest.fixture
def orchestrator(store, subtask_mgr, dynamic_loader):
    """DAGOrchestrator with mocked dependencies."""
    return DAGOrchestrator(
        store=store,
        subtask_mgr=subtask_mgr,
        dynamic_loader=dynamic_loader,
    )


def _single_callback_request() -> DAGCreateRequest:
    """Single callback node — completes immediately."""
    return DAGCreateRequest(
        name="callback-dag",
        nodes=[
            DAGNodeSpec(
                name="notify",
                type=DAGNodeType.callback,
                instructions="Send notification",
            ),
        ],
    )


def _two_subtask_request() -> DAGCreateRequest:
    """Two subtask nodes with dependency."""
    return DAGCreateRequest(
        name="two-subtask-dag",
        nodes=[
            DAGNodeSpec(name="research", type=DAGNodeType.subtask, instructions="Research the topic"),
            DAGNodeSpec(name="write", type=DAGNodeType.subtask, instructions="Write the report"),
        ],
        edges=[
            DAGEdgeSpec(from_node="research", to_node="write", edge_type="dependency"),
        ],
    )


def _parallel_request() -> DAGCreateRequest:
    """Two independent wave-0 subtask nodes."""
    return DAGCreateRequest(
        name="parallel-dag",
        nodes=[
            DAGNodeSpec(name="task-a", type=DAGNodeType.subtask, instructions="Task A"),
            DAGNodeSpec(name="task-b", type=DAGNodeType.subtask, instructions="Task B"),
        ],
    )


def _budget_request() -> DAGCreateRequest:
    """DAG with tight token budget."""
    return DAGCreateRequest(
        name="budget-dag",
        token_budget=100,
        nodes=[
            DAGNodeSpec(name="step-1", type=DAGNodeType.subtask, instructions="Step 1"),
            DAGNodeSpec(name="step-2", type=DAGNodeType.subtask, instructions="Step 2"),
        ],
        edges=[
            DAGEdgeSpec(from_node="step-1", to_node="step-2", edge_type="dependency"),
        ],
    )


class TestDAGOrchestratorStart:
    """Test starting a DAG."""

    @pytest.mark.asyncio
    async def test_create_and_start_dag(self, store, orchestrator, subtask_mgr):
        """Start a DAG — wave-0 subtask nodes become running."""
        dag = await store.create(_two_subtask_request())
        await orchestrator.start_dag(dag.id)

        # DAG should be running
        fetched = await store.get_dag(dag.id)
        assert fetched.status == "running"

        # Wave-0 node (research) should have spawned a subtask
        research = next(n for n in fetched.nodes if n.name == "research")
        assert research.status == "running"
        assert research.subtask_id is not None
        subtask_mgr.create.assert_called_once()

        # Wave-1 node (write) should still be pending
        write = next(n for n in fetched.nodes if n.name == "write")
        assert write.status == "pending"

    @pytest.mark.asyncio
    async def test_parallel_wave_launch(self, store, orchestrator, subtask_mgr):
        """Two wave-0 nodes both start running."""
        dag = await store.create(_parallel_request())
        await orchestrator.start_dag(dag.id)

        fetched = await store.get_dag(dag.id)
        task_a = next(n for n in fetched.nodes if n.name == "task-a")
        task_b = next(n for n in fetched.nodes if n.name == "task-b")

        assert task_a.status == "running"
        assert task_b.status == "running"
        assert subtask_mgr.create.call_count == 2


class TestDAGOrchestratorTick:
    """Test tick-based advancement."""

    @pytest.mark.asyncio
    async def test_tick_advances_ready_nodes(self, store, orchestrator, subtask_mgr):
        """Complete a node, tick, dependents advance."""
        dag = await store.create(_two_subtask_request())
        await orchestrator.start_dag(dag.id)

        # Simulate: research subtask completed
        fetched = await store.get_dag(dag.id)
        research = next(n for n in fetched.nodes if n.name == "research")
        subtask_id = research.subtask_id

        # Mock subtask_mgr.get to return completed subtask
        subtask_mgr.get.return_value = SimpleNamespace(
            id=subtask_id, status="completed", result="Research done", error=None
        )

        # Tick should sync status + launch next node
        subtask_mgr.create.reset_mock()
        count = await orchestrator.tick()
        assert count >= 1

        # Write node should now be running
        fetched = await store.get_dag(dag.id)
        write = next(n for n in fetched.nodes if n.name == "write")
        assert write.status == "running"
        subtask_mgr.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_dag_completes_when_all_nodes_done(self, store, orchestrator):
        """Single callback node — tick marks DAG completed."""
        dag = await store.create(_single_callback_request())
        await orchestrator.start_dag(dag.id)

        # Callback auto-completes, so tick should finalize DAG
        await orchestrator.tick()

        fetched = await store.get_dag(dag.id)
        assert fetched.status == "completed"


class TestDAGOrchestratorFailure:
    """Test failure propagation."""

    @pytest.mark.asyncio
    async def test_cascade_failure(self, store, orchestrator, subtask_mgr):
        """Fail a node, tick, dependents blocked, DAG fails."""
        dag = await store.create(_two_subtask_request())
        await orchestrator.start_dag(dag.id)

        fetched = await store.get_dag(dag.id)
        research = next(n for n in fetched.nodes if n.name == "research")

        # Mock subtask_mgr.get to return failed subtask
        subtask_mgr.get.return_value = SimpleNamespace(
            id=research.subtask_id, status="failed", result=None, error="Out of memory"
        )

        await orchestrator.tick()

        fetched = await store.get_dag(dag.id)
        research = next(n for n in fetched.nodes if n.name == "research")
        write = next(n for n in fetched.nodes if n.name == "write")

        assert research.status == "failed"
        assert write.status == "blocked"
        assert fetched.status == "failed"


class TestDAGOrchestratorCancel:
    """Test cancellation."""

    @pytest.mark.asyncio
    async def test_cancel_dag(self, store, orchestrator, subtask_mgr):
        """Cancel — all non-terminal nodes cancelled."""
        dag = await store.create(_two_subtask_request())
        await orchestrator.start_dag(dag.id)

        await orchestrator.cancel_dag(dag.id, reason="User requested")

        fetched = await store.get_dag(dag.id)
        assert fetched.status == "cancelled"
        for node in fetched.nodes:
            assert node.status == "cancelled"


class TestDAGOrchestratorBudget:
    """Test token budget enforcement."""

    @pytest.mark.asyncio
    async def test_budget_exceeded(self, store, orchestrator, subtask_mgr):
        """Exceed budget — pending nodes cancelled, DAG marked partial or failed."""
        dag = await store.create(_budget_request())
        await orchestrator.start_dag(dag.id)

        # Simulate: step-1 completed with tokens
        fetched = await store.get_dag(dag.id)
        step1 = next(n for n in fetched.nodes if n.name == "step-1")

        subtask_mgr.get.return_value = SimpleNamespace(
            id=step1.subtask_id, status="completed", result="Done", error=None
        )

        # Push tokens over budget
        await store.update_dag_tokens(dag.id, 150)  # 150 > budget of 100

        await orchestrator.tick()

        fetched = await store.get_dag(dag.id)
        # step-2 should be cancelled due to budget
        step2 = next(n for n in fetched.nodes if n.name == "step-2")
        assert step2.status == "cancelled"

        # DAG should be partial (since step-1 completed)
        assert fetched.status == "partial"
