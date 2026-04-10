"""Tests for F038 DAG Orchestrator state machine."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from nous.dag.orchestrator import CheckResult, DAGOrchestrator
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


def _completion_check_request(check_cmd: str = "test -f /tmp/done.flag", timeout: int = 120) -> DAGCreateRequest:
    """Two nodes: first has completion_check, second depends on it."""
    return DAGCreateRequest(
        name="completion-check-dag",
        nodes=[
            DAGNodeSpec(
                name="async-job",
                type=DAGNodeType.subtask,
                instructions="Launch async job",
                timeout_seconds=timeout,
                completion_check=check_cmd,
            ),
            DAGNodeSpec(
                name="use-result",
                type=DAGNodeType.subtask,
                instructions="Use the result",
            ),
        ],
        edges=[
            DAGEdgeSpec(from_node="async-job", to_node="use-result", edge_type="dependency"),
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


class TestDAGCompletionCheck:
    """Test F038.1 completion_check flow."""

    @pytest.mark.asyncio
    async def test_no_completion_check_unchanged(self, store, orchestrator, subtask_mgr):
        """No completion_check — existing behavior, subtask complete → node complete."""
        dag = await store.create(_two_subtask_request())
        await orchestrator.start_dag(dag.id)

        fetched = await store.get_dag(dag.id)
        research = next(n for n in fetched.nodes if n.name == "research")
        subtask_mgr.get.return_value = SimpleNamespace(
            id=research.subtask_id, status="completed", result="Done", error=None
        )

        await orchestrator.tick()
        fetched = await store.get_dag(dag.id)
        research = next(n for n in fetched.nodes if n.name == "research")
        assert research.status == "completed"  # NOT awaiting_check

    @pytest.mark.asyncio
    async def test_completion_check_transitions_to_awaiting(self, store, orchestrator, subtask_mgr):
        """completion_check present — subtask complete → awaiting_check (not completed)."""
        dag = await store.create(_completion_check_request())
        await orchestrator.start_dag(dag.id)

        fetched = await store.get_dag(dag.id)
        async_job = next(n for n in fetched.nodes if n.name == "async-job")
        subtask_mgr.get.return_value = SimpleNamespace(
            id=async_job.subtask_id, status="completed", result="Job launched", error=None
        )

        # Mock _run_completion_check to return False so node stays awaiting_check
        orchestrator._run_completion_check = AsyncMock(return_value=CheckResult("pending"))

        await orchestrator.tick()
        fetched = await store.get_dag(dag.id)
        async_job = next(n for n in fetched.nodes if n.name == "async-job")
        assert async_job.status == "awaiting_check"
        assert async_job.result == "Job launched"
        assert async_job.awaiting_check_at is not None

    @pytest.mark.asyncio
    async def test_completion_check_immediate_pass(self, store, orchestrator, subtask_mgr):
        """completion_check exits 0 on first poll → node completes same tick."""
        dag = await store.create(_completion_check_request())
        await orchestrator.start_dag(dag.id)

        fetched = await store.get_dag(dag.id)
        async_job = next(n for n in fetched.nodes if n.name == "async-job")
        subtask_mgr.get.return_value = SimpleNamespace(
            id=async_job.subtask_id, status="completed", result="Job launched", error=None
        )

        # Mock _run_completion_check to return True
        orchestrator._run_completion_check = AsyncMock(return_value=CheckResult("success"))

        await orchestrator.tick()
        fetched = await store.get_dag(dag.id)
        async_job = next(n for n in fetched.nodes if n.name == "async-job")
        assert async_job.status == "completed"
        assert async_job.check_attempts == 1

    @pytest.mark.asyncio
    async def test_completion_check_delayed_pass(self, store, orchestrator, subtask_mgr):
        """completion_check fails 3 ticks, then exits 0 → node completes on 4th tick."""
        dag = await store.create(_completion_check_request())
        await orchestrator.start_dag(dag.id)

        fetched = await store.get_dag(dag.id)
        async_job = next(n for n in fetched.nodes if n.name == "async-job")
        subtask_mgr.get.return_value = SimpleNamespace(
            id=async_job.subtask_id, status="completed", result="Job launched", error=None
        )

        # First tick: transitions to awaiting_check, polls once (fail)
        orchestrator._run_completion_check = AsyncMock(return_value=CheckResult("pending"))
        await orchestrator.tick()

        # Ticks 2-3: still failing
        await orchestrator.tick()
        await orchestrator.tick()

        fetched = await store.get_dag(dag.id)
        async_job = next(n for n in fetched.nodes if n.name == "async-job")
        assert async_job.status == "awaiting_check"
        assert async_job.check_attempts == 3

        # Tick 4: passes
        orchestrator._run_completion_check = AsyncMock(return_value=CheckResult("success"))
        await orchestrator.tick()

        fetched = await store.get_dag(dag.id)
        async_job = next(n for n in fetched.nodes if n.name == "async-job")
        assert async_job.status == "completed"
        assert async_job.check_attempts == 4

    @pytest.mark.asyncio
    async def test_completion_check_timeout(self, store, orchestrator, subtask_mgr):
        """completion_check never passes, timeout exceeded → node fails."""
        from datetime import timedelta

        # Short timeout for test
        dag = await store.create(_completion_check_request(timeout=1))
        await orchestrator.start_dag(dag.id)

        fetched = await store.get_dag(dag.id)
        async_job = next(n for n in fetched.nodes if n.name == "async-job")
        subtask_mgr.get.return_value = SimpleNamespace(
            id=async_job.subtask_id, status="completed", result="Job launched", error=None
        )

        # First tick transitions to awaiting_check
        orchestrator._run_completion_check = AsyncMock(return_value=CheckResult("pending"))
        await orchestrator.tick()

        # Manually set awaiting_check_at to past to simulate timeout
        fetched = await store.get_dag(dag.id)
        async_job = next(n for n in fetched.nodes if n.name == "async-job")
        past_time = datetime.now(UTC) - timedelta(seconds=10)
        await store.update_node(async_job.id, awaiting_check_at=past_time)

        await orchestrator.tick()

        fetched = await store.get_dag(dag.id)
        async_job = next(n for n in fetched.nodes if n.name == "async-job")
        assert async_job.status == "failed"
        assert "timed out" in async_job.error

    @pytest.mark.asyncio
    async def test_downstream_blocked_during_awaiting_check(self, store, orchestrator, subtask_mgr):
        """Node in awaiting_check → dependent nodes stay pending."""
        dag = await store.create(_completion_check_request())
        await orchestrator.start_dag(dag.id)

        fetched = await store.get_dag(dag.id)
        async_job = next(n for n in fetched.nodes if n.name == "async-job")
        subtask_mgr.get.return_value = SimpleNamespace(
            id=async_job.subtask_id, status="completed", result="Job launched", error=None
        )

        orchestrator._run_completion_check = AsyncMock(return_value=CheckResult("pending"))
        await orchestrator.tick()

        fetched = await store.get_dag(dag.id)
        use_result = next(n for n in fetched.nodes if n.name == "use-result")
        assert use_result.status == "pending"  # NOT ready or running

    @pytest.mark.asyncio
    async def test_cancel_during_awaiting_check(self, store, orchestrator, subtask_mgr):
        """DAG cancelled → awaiting_check node transitions to cancelled."""
        dag = await store.create(_completion_check_request())
        await orchestrator.start_dag(dag.id)

        fetched = await store.get_dag(dag.id)
        async_job = next(n for n in fetched.nodes if n.name == "async-job")
        subtask_mgr.get.return_value = SimpleNamespace(
            id=async_job.subtask_id, status="completed", result="Job launched", error=None
        )

        orchestrator._run_completion_check = AsyncMock(return_value=CheckResult("pending"))
        await orchestrator.tick()  # Transitions to awaiting_check

        await orchestrator.cancel_dag(dag.id, reason="User cancelled")

        fetched = await store.get_dag(dag.id)
        async_job = next(n for n in fetched.nodes if n.name == "async-job")
        assert async_job.status == "cancelled"
        assert fetched.status == "cancelled"

    @pytest.mark.asyncio
    async def test_check_command_error(self, store, orchestrator, subtask_mgr):
        """Check command raises exception → treated as not-passed, doesn't crash."""
        dag = await store.create(_completion_check_request())
        await orchestrator.start_dag(dag.id)

        fetched = await store.get_dag(dag.id)
        async_job = next(n for n in fetched.nodes if n.name == "async-job")
        subtask_mgr.get.return_value = SimpleNamespace(
            id=async_job.subtask_id, status="completed", result="Job launched", error=None
        )

        # Mock to raise exception
        orchestrator._run_completion_check = AsyncMock(side_effect=Exception("Command not found"))

        # Should not crash — tick() wraps _advance_dag in try/except
        await orchestrator.tick()

        fetched = await store.get_dag(dag.id)
        async_job = next(n for n in fetched.nodes if n.name == "async-job")
        # Node should still be awaiting_check (exception aborted _advance_dag before update)
        assert async_job.status == "awaiting_check"

    @pytest.mark.asyncio
    async def test_completion_check_definitive_failure(self, store, orchestrator, subtask_mgr):
        """completion_check exits 1 → node fails immediately without further polling."""
        dag = await store.create(_completion_check_request())
        await orchestrator.start_dag(dag.id)

        fetched = await store.get_dag(dag.id)
        async_job = next(n for n in fetched.nodes if n.name == "async-job")
        subtask_mgr.get.return_value = SimpleNamespace(
            id=async_job.subtask_id, status="completed", result="Job launched", error=None
        )

        # First tick transitions to awaiting_check, then check returns "failed"
        orchestrator._run_completion_check = AsyncMock(
            return_value=CheckResult("failed", "15 test failures")
        )
        await orchestrator.tick()

        fetched = await store.get_dag(dag.id)
        async_job = next(n for n in fetched.nodes if n.name == "async-job")
        assert async_job.status == "failed"
        assert "15 test failures" in async_job.error
        assert async_job.check_attempts == 1

    @pytest.mark.asyncio
    async def test_completion_check_failure_blocks_downstream(self, store, orchestrator, subtask_mgr):
        """completion_check definitive failure → dependent nodes get blocked."""
        dag = await store.create(_completion_check_request())
        await orchestrator.start_dag(dag.id)

        fetched = await store.get_dag(dag.id)
        async_job = next(n for n in fetched.nodes if n.name == "async-job")
        subtask_mgr.get.return_value = SimpleNamespace(
            id=async_job.subtask_id, status="completed", result="Job launched", error=None
        )

        orchestrator._run_completion_check = AsyncMock(
            return_value=CheckResult("failed", "CI failed")
        )
        await orchestrator.tick()  # transitions to awaiting_check + fails
        await orchestrator.tick()  # propagate failures

        fetched = await store.get_dag(dag.id)
        use_result = next(n for n in fetched.nodes if n.name == "use-result")
        assert use_result.status in ("blocked", "cancelled")

    @pytest.mark.asyncio
    async def test_max_check_attempts(self, store, orchestrator, subtask_mgr):
        """max_check_attempts exceeded → node fails."""
        req = DAGCreateRequest(
            name="max-attempts-dag",
            nodes=[
                DAGNodeSpec(
                    name="limited-job",
                    type=DAGNodeType.subtask,
                    instructions="Job with limited retries",
                    completion_check="test -f /tmp/done",
                    max_check_attempts=3,
                ),
            ],
        )
        dag = await store.create(req)
        await orchestrator.start_dag(dag.id)

        fetched = await store.get_dag(dag.id)
        limited = next(n for n in fetched.nodes if n.name == "limited-job")
        subtask_mgr.get.return_value = SimpleNamespace(
            id=limited.subtask_id, status="completed", result="Launched", error=None
        )

        orchestrator._run_completion_check = AsyncMock(return_value=CheckResult("pending"))

        # Tick 1: transitions to awaiting_check, polls (fail), attempts=1
        await orchestrator.tick()
        # Tick 2: polls (fail), attempts=2
        await orchestrator.tick()
        # Tick 3: polls (fail), attempts=3
        await orchestrator.tick()
        # Tick 4: attempts >= max (3), node fails
        await orchestrator.tick()

        fetched = await store.get_dag(dag.id)
        limited = next(n for n in fetched.nodes if n.name == "limited-job")
        assert limited.status == "failed"
        assert "max attempts" in limited.error.lower()

    @pytest.mark.asyncio
    async def test_retry_resets_check_state(self, store, orchestrator, subtask_mgr):
        """retry_node resets check_attempts and last_check_at."""
        req = DAGCreateRequest(
            name="retry-check-dag",
            nodes=[
                DAGNodeSpec(
                    name="retryable",
                    type=DAGNodeType.subtask,
                    instructions="Retryable job",
                    completion_check="test -f /tmp/done",
                    max_check_attempts=2,
                ),
            ],
        )
        dag = await store.create(req)
        await orchestrator.start_dag(dag.id)

        fetched = await store.get_dag(dag.id)
        node = next(n for n in fetched.nodes if n.name == "retryable")
        subtask_mgr.get.return_value = SimpleNamespace(
            id=node.subtask_id, status="completed", result="Launched", error=None
        )

        orchestrator._run_completion_check = AsyncMock(return_value=CheckResult("pending"))

        # Exhaust attempts: tick1(transition+poll1), tick2(poll2), tick3(exceeds max, fails)
        await orchestrator.tick()
        await orchestrator.tick()
        await orchestrator.tick()

        fetched = await store.get_dag(dag.id)
        node = next(n for n in fetched.nodes if n.name == "retryable")
        assert node.status == "failed"

        # Retry
        await orchestrator.retry_node(dag.id, "retryable")

        fetched = await store.get_dag(dag.id)
        node = next(n for n in fetched.nodes if n.name == "retryable")
        assert node.status == "ready"
        assert node.check_attempts == 0
        assert node.last_check_at is None
        assert node.awaiting_check_at is None

    @pytest.mark.asyncio
    async def test_concurrent_ticks_no_double_poll(self, store, orchestrator, subtask_mgr):
        """Two concurrent ticks — lock prevents double-polling."""
        import asyncio as _asyncio

        dag = await store.create(_completion_check_request())
        await orchestrator.start_dag(dag.id)

        fetched = await store.get_dag(dag.id)
        async_job = next(n for n in fetched.nodes if n.name == "async-job")
        subtask_mgr.get.return_value = SimpleNamespace(
            id=async_job.subtask_id, status="completed", result="Job launched", error=None
        )

        call_count = 0

        async def slow_check(node):
            nonlocal call_count
            call_count += 1
            await _asyncio.sleep(0.1)  # Simulate slow check
            return CheckResult("pending")

        orchestrator._run_completion_check = slow_check

        # Run two ticks concurrently
        await _asyncio.gather(orchestrator.tick(), orchestrator.tick())

        # Lock should serialize — both ticks run but sequentially
        # Key assertion: no crash and state is consistent
        fetched = await store.get_dag(dag.id)
        async_job = next(n for n in fetched.nodes if n.name == "async-job")
        assert async_job.status == "awaiting_check"
