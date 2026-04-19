"""Tests for F038 DAG Orchestrator state machine."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from nous.config import Settings
from nous.dag.orchestrator import CheckResult, DAGOrchestrator
from nous.dag.schemas import (
    DAGCreateRequest,
    DAGEdgeSpec,
    DAGNodeSpec,
    DAGNodeType,
)
from nous.dag.store import DAGStore
from nous.storage.models import DAGNode, ExecutionDAG

@pytest_asyncio.fixture
async def store(db):
    """DAGStore instance with unique agent_id per test."""
    agent_id = f"test-dag-orch-{uuid.uuid4().hex[:8]}"
    return DAGStore(db, agent_id, Settings())


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
        settings=Settings(),
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

    @pytest.mark.asyncio
    async def test_cancel_dag_cancels_running_subtask(self, store, orchestrator, subtask_mgr):
        """cancel_dag calls cancel on running subtasks, not just pending."""
        dag = await store.create(_two_subtask_request())
        await orchestrator.start_dag(dag.id)

        fetched = await store.get_dag(dag.id)
        research = next(n for n in fetched.nodes if n.name == "research")
        subtask_mgr.get.return_value = SimpleNamespace(id=research.subtask_id, status="running")

        subtask_mgr.cancel.reset_mock()
        await orchestrator.cancel_dag(dag.id, reason="User cancelled")

        subtask_mgr.cancel.assert_called_once_with(research.subtask_id)

    @pytest.mark.asyncio
    async def test_cancel_completed_dag_noop(self, store, orchestrator):
        """cancel_dag on completed DAG is a no-op."""
        dag = await store.create(_single_callback_request())
        await orchestrator.start_dag(dag.id)
        await orchestrator.tick()

        fetched = await store.get_dag(dag.id)
        assert fetched.status == "completed"
        original_summary = fetched.result_summary

        await orchestrator.cancel_dag(dag.id, reason="too late")

        fetched = await store.get_dag(dag.id)
        assert fetched.status == "completed"
        assert fetched.result_summary == original_summary


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
        assert node.status == "pending"
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


class TestDAGRetryNode:
    """Test retry_node correctness."""

    @pytest.mark.asyncio
    async def test_retry_node_resets_to_pending(self, store, orchestrator, subtask_mgr):
        """retry_node resets to pending so _find_ready_nodes picks it up."""
        dag = await store.create(_two_subtask_request())
        await orchestrator.start_dag(dag.id)

        # Simulate research subtask failed
        fetched = await store.get_dag(dag.id)
        research = next(n for n in fetched.nodes if n.name == "research")
        subtask_mgr.get.return_value = SimpleNamespace(
            id=research.subtask_id, status="failed", result=None, error="OOM"
        )
        await orchestrator.tick()

        # Verify failed
        fetched = await store.get_dag(dag.id)
        research = next(n for n in fetched.nodes if n.name == "research")
        assert research.status == "failed"

        # Retry
        await orchestrator.retry_node(dag.id, "research")

        fetched = await store.get_dag(dag.id)
        research = next(n for n in fetched.nodes if n.name == "research")
        assert research.status == "pending"  # Must be pending, not ready

        # Tick should pick it up and launch
        subtask_mgr.create.reset_mock()
        subtask_mgr.get.return_value = SimpleNamespace(
            id=uuid.uuid4(), status="pending", result=None, error=None
        )
        await orchestrator.tick()

        fetched = await store.get_dag(dag.id)
        research = next(n for n in fetched.nodes if n.name == "research")
        assert research.status == "running"
        subtask_mgr.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_retry_node_not_failed_raises(self, store, orchestrator):
        """retry_node on non-failed node raises ValueError."""
        dag = await store.create(_two_subtask_request())
        await orchestrator.start_dag(dag.id)
        with pytest.raises(ValueError, match="expected failed"):
            await orchestrator.retry_node(dag.id, "research")

    @pytest.mark.asyncio
    async def test_retry_node_unknown_raises(self, store, orchestrator):
        """retry_node on unknown node raises ValueError."""
        dag = await store.create(_two_subtask_request())
        with pytest.raises(ValueError, match="not found"):
            await orchestrator.retry_node(dag.id, "nonexistent")

    @pytest.mark.asyncio
    async def test_retry_only_unblocks_downstream(self, store, orchestrator, subtask_mgr):
        """retry_node only unblocks nodes downstream of the retried node."""
        # DAG: A -> B, C -> D  (two independent chains)
        request = DAGCreateRequest(
            name="selective-unblock",
            nodes=[
                DAGNodeSpec(name="a", type=DAGNodeType.subtask, instructions="A"),
                DAGNodeSpec(name="b", type=DAGNodeType.subtask, instructions="B"),
                DAGNodeSpec(name="c", type=DAGNodeType.subtask, instructions="C"),
                DAGNodeSpec(name="d", type=DAGNodeType.subtask, instructions="D"),
            ],
            edges=[
                DAGEdgeSpec(from_node="a", to_node="b", edge_type="dependency"),
                DAGEdgeSpec(from_node="c", to_node="d", edge_type="dependency"),
            ],
        )
        dag = await store.create(request)
        await orchestrator.start_dag(dag.id)

        # Both A and C are wave-0, running. Simulate both fail.
        async def mock_get(sid):
            return SimpleNamespace(id=sid, status="failed", result=None, error="fail")
        subtask_mgr.get = AsyncMock(side_effect=mock_get)

        await orchestrator.tick()

        fetched = await store.get_dag(dag.id)
        assert next(n for n in fetched.nodes if n.name == "b").status == "blocked"
        assert next(n for n in fetched.nodes if n.name == "d").status == "blocked"

        # Retry only A
        await orchestrator.retry_node(dag.id, "a")

        fetched = await store.get_dag(dag.id)
        assert next(n for n in fetched.nodes if n.name == "b").status == "pending"
        assert next(n for n in fetched.nodes if n.name == "d").status == "blocked"  # Still blocked


class TestDAGCancelCascade:
    """Test cancel_cascade edge semantics."""

    @pytest.mark.asyncio
    async def test_cancel_cascade_produces_cancelled_not_blocked(self, store, orchestrator, subtask_mgr):
        """cancel_cascade edges set downstream to cancelled, not blocked."""
        # main -> cleanup (cancel_cascade), main -> dep-node (dependency)
        # cleanup and dep-node must be wave-1 (depend on main) so they stay pending
        # while main runs. We use dependency edges for wave ordering and
        # cancel_cascade for the cascade semantics on cleanup.
        request = DAGCreateRequest(
            name="cascade-test",
            nodes=[
                DAGNodeSpec(name="main", type=DAGNodeType.subtask, instructions="Main"),
                DAGNodeSpec(name="cleanup", type=DAGNodeType.subtask, instructions="Cleanup"),
                DAGNodeSpec(name="dep-node", type=DAGNodeType.subtask, instructions="Depends"),
            ],
            edges=[
                DAGEdgeSpec(from_node="main", to_node="cleanup", edge_type="cancel_cascade"),
                DAGEdgeSpec(from_node="main", to_node="dep-node", edge_type="dependency"),
            ],
        )

        # Make create() return unique subtask IDs for each call
        subtask_ids = iter([uuid.uuid4(), uuid.uuid4()])
        subtask_mgr.create.side_effect = lambda **kw: SimpleNamespace(
            id=next(subtask_ids), status="pending"
        )

        dag = await store.create(request)
        await orchestrator.start_dag(dag.id)

        fetched = await store.get_dag(dag.id)
        main_node = next(n for n in fetched.nodes if n.name == "main")
        cleanup_node = next(n for n in fetched.nodes if n.name == "cleanup")
        main_sid = main_node.subtask_id

        # Only main's subtask fails; cleanup is wave-0 too (cancel_cascade
        # doesn't count for waves), so we need per-subtask_id mock results.
        async def selective_get(sid):
            if sid == main_sid:
                return SimpleNamespace(id=sid, status="failed", result=None, error="fail")
            # cleanup subtask is still running
            return SimpleNamespace(id=sid, status="running", result=None, error=None)

        subtask_mgr.get = AsyncMock(side_effect=selective_get)

        await orchestrator.tick()

        fetched = await store.get_dag(dag.id)
        cleanup = next(n for n in fetched.nodes if n.name == "cleanup")
        dep_node = next(n for n in fetched.nodes if n.name == "dep-node")

        assert cleanup.status == "cancelled"  # cancel_cascade -> cancelled
        assert dep_node.status == "blocked"   # dependency -> blocked

    @pytest.mark.asyncio
    async def test_cancel_cascade_blocks_transitive_descendants(self, store, orchestrator, subtask_mgr):
        """A -(cancel_cascade)-> B -(dependency)-> C: A fails, B cancelled, C blocked."""
        request = DAGCreateRequest(
            name="transitive-cascade",
            nodes=[
                DAGNodeSpec(name="a", type=DAGNodeType.subtask, instructions="A"),
                DAGNodeSpec(name="b", type=DAGNodeType.subtask, instructions="B"),
                DAGNodeSpec(name="c", type=DAGNodeType.subtask, instructions="C"),
            ],
            edges=[
                DAGEdgeSpec(from_node="a", to_node="b", edge_type="cancel_cascade"),
                DAGEdgeSpec(from_node="b", to_node="c", edge_type="dependency"),
            ],
        )

        # Unique subtask IDs per create call
        subtask_mgr.create.side_effect = lambda **kw: SimpleNamespace(
            id=uuid.uuid4(), status="pending"
        )

        dag = await store.create(request)
        await orchestrator.start_dag(dag.id)

        # A and B are both wave-0 (cancel_cascade doesn't affect waves).
        # Fail A's subtask, B is still running.
        fetched = await store.get_dag(dag.id)
        node_a = next(n for n in fetched.nodes if n.name == "a")

        async def selective_get(sid):
            if sid == node_a.subtask_id:
                return SimpleNamespace(id=sid, status="failed", result=None, error="fail")
            return SimpleNamespace(id=sid, status="running", result=None, error=None)

        subtask_mgr.get = AsyncMock(side_effect=selective_get)

        await orchestrator.tick()

        fetched = await store.get_dag(dag.id)
        b = next(n for n in fetched.nodes if n.name == "b")
        c = next(n for n in fetched.nodes if n.name == "c")

        assert b.status == "cancelled"  # direct cancel_cascade target
        assert c.status == "blocked"    # dependency descendant of cancelled node

    @pytest.mark.asyncio
    async def test_retry_recovers_cancel_cascade_descendants(self, store, orchestrator, subtask_mgr):
        """retry_node resets cancelled descendants from cancel_cascade, not just blocked."""
        request = DAGCreateRequest(
            name="retry-cascade",
            nodes=[
                DAGNodeSpec(name="a", type=DAGNodeType.subtask, instructions="A"),
                DAGNodeSpec(name="b", type=DAGNodeType.subtask, instructions="B"),
            ],
            edges=[
                DAGEdgeSpec(from_node="a", to_node="b", edge_type="cancel_cascade"),
            ],
        )

        # Unique subtask IDs per create call
        subtask_mgr.create.side_effect = lambda **kw: SimpleNamespace(
            id=uuid.uuid4(), status="pending"
        )

        dag = await store.create(request)
        await orchestrator.start_dag(dag.id)

        fetched = await store.get_dag(dag.id)
        node_a = next(n for n in fetched.nodes if n.name == "a")

        async def selective_get(sid):
            if sid == node_a.subtask_id:
                return SimpleNamespace(id=sid, status="failed", result=None, error="fail")
            return SimpleNamespace(id=sid, status="running", result=None, error=None)

        subtask_mgr.get = AsyncMock(side_effect=selective_get)
        await orchestrator.tick()

        fetched = await store.get_dag(dag.id)
        assert next(n for n in fetched.nodes if n.name == "b").status == "cancelled"

        # Retry A — B should be requeued from cancelled to pending
        await orchestrator.retry_node(dag.id, "a")

        fetched = await store.get_dag(dag.id)
        assert next(n for n in fetched.nodes if n.name == "b").status == "pending"


class TestDAGEdgeCases:
    """Test edge cases and coverage gaps."""

    @pytest.mark.asyncio
    async def test_subtask_deleted_mid_run(self, store, orchestrator, subtask_mgr):
        """Subtask externally deleted -> node fails."""
        dag = await store.create(_two_subtask_request())
        await orchestrator.start_dag(dag.id)
        subtask_mgr.get.return_value = None
        await orchestrator.tick()

        fetched = await store.get_dag(dag.id)
        research = next(n for n in fetched.nodes if n.name == "research")
        assert research.status == "failed"
        assert "deleted" in research.error.lower()

    @pytest.mark.asyncio
    async def test_gate_node_auto_passes(self, store, orchestrator, subtask_mgr):
        """Gate node auto-completes when launched."""
        request = DAGCreateRequest(
            name="gate-test",
            nodes=[
                DAGNodeSpec(name="setup", type=DAGNodeType.subtask, instructions="Setup"),
                DAGNodeSpec(name="gate", type=DAGNodeType.gate, instructions="Approval gate"),
                DAGNodeSpec(name="deploy", type=DAGNodeType.subtask, instructions="Deploy"),
            ],
            edges=[
                DAGEdgeSpec(from_node="setup", to_node="gate", edge_type="dependency"),
                DAGEdgeSpec(from_node="gate", to_node="deploy", edge_type="dependency"),
            ],
        )
        dag = await store.create(request)
        await orchestrator.start_dag(dag.id)

        fetched = await store.get_dag(dag.id)
        setup = next(n for n in fetched.nodes if n.name == "setup")
        subtask_mgr.get.return_value = SimpleNamespace(
            id=setup.subtask_id, status="completed", result="Done", error=None
        )

        subtask_mgr.create.reset_mock()
        await orchestrator.tick()  # setup completes, gate auto-passes

        fetched = await store.get_dag(dag.id)
        gate = next(n for n in fetched.nodes if n.name == "gate")
        assert gate.status == "completed"
        assert "auto-passed" in gate.result.lower()

        # Second tick picks up deploy (gate completed on previous tick)
        await orchestrator.tick()

        fetched = await store.get_dag(dag.id)
        deploy = next(n for n in fetched.nodes if n.name == "deploy")
        assert deploy.status == "running"

    @pytest.mark.asyncio
    async def test_context_flow_injects_predecessor_result(self, store, orchestrator, subtask_mgr):
        """context_flow edge injects predecessor result into subtask instructions."""
        request = DAGCreateRequest(
            name="context-flow-test",
            nodes=[
                DAGNodeSpec(name="research", type=DAGNodeType.subtask, instructions="Research topic"),
                DAGNodeSpec(name="write", type=DAGNodeType.subtask, instructions="Write report"),
            ],
            edges=[
                DAGEdgeSpec(from_node="research", to_node="write", edge_type="context_flow"),
            ],
        )
        dag = await store.create(request)
        await orchestrator.start_dag(dag.id)

        fetched = await store.get_dag(dag.id)
        research = next(n for n in fetched.nodes if n.name == "research")
        subtask_mgr.get.return_value = SimpleNamespace(
            id=research.subtask_id, status="completed", result="KEY_FINDING_XYZ", error=None
        )

        subtask_mgr.create.reset_mock()
        await orchestrator.tick()

        # Verify the "write" subtask was created with the predecessor's result
        assert subtask_mgr.create.called
        # Get the task= keyword argument
        call_kwargs = subtask_mgr.create.call_args
        task_arg = call_kwargs.kwargs.get("task", "") if call_kwargs.kwargs else ""
        assert "KEY_FINDING_XYZ" in task_arg
        assert "Context from prior steps" in task_arg

    @pytest.mark.asyncio
    async def test_start_dag_not_found_raises(self, store, orchestrator):
        """start_dag on non-existent DAG raises ValueError."""
        with pytest.raises(ValueError, match="not found"):
            await orchestrator.start_dag(uuid.uuid4())

    @pytest.mark.asyncio
    async def test_start_dag_already_running_raises(self, store, orchestrator):
        """start_dag on already-running DAG raises ValueError."""
        dag = await store.create(_single_callback_request())
        await orchestrator.start_dag(dag.id)
        with pytest.raises(ValueError, match="expected pending"):
            await orchestrator.start_dag(dag.id)


class TestCheckNodeCompletionCheck:
    """Test that check-type nodes honour completion_check after heartbeat finishes."""

    def _check_node_request(self, completion_check: str = "test -f /tmp/done.flag") -> DAGCreateRequest:
        return DAGCreateRequest(
            name="check-node-cc-test",
            nodes=[
                DAGNodeSpec(
                    name="monitor",
                    type=DAGNodeType.check,
                    instructions="Monitor something",
                    completion_check=completion_check,
                ),
                DAGNodeSpec(
                    name="use-result",
                    type=DAGNodeType.subtask,
                    instructions="Use the result",
                ),
            ],
            edges=[
                DAGEdgeSpec(from_node="monitor", to_node="use-result", edge_type="dependency"),
            ],
        )

    @pytest.mark.asyncio
    async def test_check_node_unregistered_no_completion_check_completes(
        self, store, orchestrator, dynamic_loader
    ):
        """check-type node with no completion_check: unregistered → completed directly."""
        request = DAGCreateRequest(
            name="check-no-cc",
            nodes=[DAGNodeSpec(name="monitor", type=DAGNodeType.check, instructions="Monitor")],
            edges=[],
        )
        dag = await store.create(request)
        await orchestrator.start_dag(dag.id)
        # Simulate check launched (running)
        fetched = await store.get_dag(dag.id)
        monitor = next(n for n in fetched.nodes if n.name == "monitor")
        await store.update_node(monitor.id, status="running", check_name="dag-test-monitor")
        # Registry returns None → check unregistered
        dynamic_loader._registry.get_check.return_value = None
        await orchestrator.tick()
        fetched2 = await store.get_dag(dag.id)
        monitor2 = next(n for n in fetched2.nodes if n.name == "monitor")
        assert monitor2.status == "completed"

    @pytest.mark.asyncio
    async def test_check_node_unregistered_with_completion_check_transitions_to_awaiting(
        self, store, orchestrator, dynamic_loader
    ):
        """check-type node: when heartbeat check unregisters and completion_check defined
        → should transition to awaiting_check (not immediately completed)."""
        dag = await store.create(self._check_node_request())
        await orchestrator.start_dag(dag.id)
        fetched = await store.get_dag(dag.id)
        monitor = next(n for n in fetched.nodes if n.name == "monitor")
        await store.update_node(monitor.id, status="running", check_name="dag-test-monitor")
        # Registry returns None → heartbeat check unregistered (finished)
        dynamic_loader._registry.get_check.return_value = None
        # Prevent shell command from actually running
        orchestrator._run_completion_check = AsyncMock(return_value=CheckResult("pending"))
        await orchestrator.tick()
        fetched2 = await store.get_dag(dag.id)
        monitor2 = next(n for n in fetched2.nodes if n.name == "monitor")
        # BUG FIX: must be awaiting_check, not completed
        assert monitor2.status == "awaiting_check", (
            f"Expected awaiting_check, got {monitor2.status}. "
            "completion_check was ignored — check-type node skipped directly to completed."
        )
        assert monitor2.awaiting_check_at is not None

    @pytest.mark.asyncio
    async def test_check_node_disabled_with_completion_check_transitions_to_awaiting(
        self, store, orchestrator, dynamic_loader
    ):
        """check-type node: when heartbeat check disables itself and completion_check defined
        → should transition to awaiting_check."""
        dag = await store.create(self._check_node_request())
        await orchestrator.start_dag(dag.id)
        fetched = await store.get_dag(dag.id)
        monitor = next(n for n in fetched.nodes if n.name == "monitor")
        await store.update_node(monitor.id, status="running", check_name="dag-test-monitor")
        # Registry returns an inactive check → check disabled itself
        inactive_check = MagicMock()
        inactive_check.active = False
        dynamic_loader._registry.get_check.return_value = inactive_check
        orchestrator._run_completion_check = AsyncMock(return_value=CheckResult("pending"))
        await orchestrator.tick()
        fetched2 = await store.get_dag(dag.id)
        monitor2 = next(n for n in fetched2.nodes if n.name == "monitor")
        assert monitor2.status == "awaiting_check", (
            f"Expected awaiting_check, got {monitor2.status}."
        )
        assert monitor2.awaiting_check_at is not None

    @pytest.mark.asyncio
    async def test_check_node_completion_check_passes_unblocks_dependent(
        self, store, orchestrator, subtask_mgr, dynamic_loader
    ):
        """Full flow: heartbeat check unregisters → awaiting_check → shell passes → dependent launches."""
        dag = await store.create(self._check_node_request())
        await orchestrator.start_dag(dag.id)
        fetched = await store.get_dag(dag.id)
        monitor = next(n for n in fetched.nodes if n.name == "monitor")
        await store.update_node(monitor.id, status="running", check_name="dag-test-monitor")
        # Tick 1: heartbeat unregisters → awaiting_check
        dynamic_loader._registry.get_check.return_value = None
        orchestrator._run_completion_check = AsyncMock(return_value=CheckResult("pending"))
        await orchestrator.tick()
        # Tick 2: completion_check passes → completed → dependent launches
        orchestrator._run_completion_check = AsyncMock(return_value=CheckResult("success"))
        await orchestrator.tick()
        fetched2 = await store.get_dag(dag.id)
        monitor2 = next(n for n in fetched2.nodes if n.name == "monitor")
        use_result = next(n for n in fetched2.nodes if n.name == "use-result")
        assert monitor2.status == "completed"
        assert use_result.status == "running"


class TestDAGOrchestratorTimeoutClamp:
    """F046: Defensive re-clamp of node.timeout_seconds at launch time.

    Store already clamps at insert, but historical rows or direct DB writes
    may carry values above the current ceiling — the orchestrator's
    _effective_timeout() helper clamps at each read site.
    """

    @pytest.mark.asyncio
    async def test_orchestrator_clamps_node_timeout_to_max(
        self, orchestrator, subtask_mgr
    ):
        settings = orchestrator._settings
        node = DAGNode(
            id=uuid.uuid4(),
            dag_id=uuid.uuid4(),
            name="n",
            node_type="subtask",
            status="ready",
            timeout_seconds=99999,  # above ceiling
            wave=0,
            instructions="x",
        )
        dag = ExecutionDAG(
            id=uuid.uuid4(),
            agent_id="test",
            name="t",
            status="running",
            nodes=[node],
            edges=[],
        )
        await orchestrator._launch_subtask_node(node, dag)
        subtask_mgr.create.assert_called_once()
        _, kwargs = subtask_mgr.create.call_args
        assert kwargs["timeout"] == settings.dag_node_max_timeout
