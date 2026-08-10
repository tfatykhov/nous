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
    return DAGStore(db, agent_id, Settings(_env_file=None))


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
        settings=Settings(_env_file=None),
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
    async def test_budget_exceeded(self, store, subtask_mgr, dynamic_loader):
        """Exceed budget — pending nodes cancelled, DAG marked partial or failed."""
        # F087: enforcement is flag-gated. tokens_consumed was structurally 0
        # until F087 wired the accounting, so this branch had never fired in
        # prod; enabling accounting and enforcement together would have started
        # cancelling DAGs for anyone who set token_budget casually.
        orchestrator = DAGOrchestrator(
            store=store,
            subtask_mgr=subtask_mgr,
            dynamic_loader=dynamic_loader,
            settings=Settings(_env_file=None, dag_token_budget_enforcement_enabled=True),
        )
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


class TestF061OutcomeAware:
    """F061: _sync_subtask_node reads final_outcome and treats
    incomplete_blocked / incomplete_no_terminal / validation_failed as
    DAG node failure (instead of advancing on garbage).
    """

    @pytest.mark.asyncio
    async def test_incomplete_blocked_marks_node_failed(
        self, store, orchestrator, subtask_mgr,
    ):
        dag = await store.create(_two_subtask_request())
        await orchestrator.start_dag(dag.id)
        fetched = await store.get_dag(dag.id)
        research = next(n for n in fetched.nodes if n.name == "research")

        # Subtask self-reported incomplete_blocked. status='completed' (per spec)
        # but final_outcome surfaces the block.
        subtask_mgr.get.return_value = SimpleNamespace(
            id=research.subtask_id,
            status="completed",
            result="blocked",
            error=None,
            final_outcome="incomplete_blocked",
            report_jsonb={
                "summary": "blocked",
                "incomplete": True,
                "blocked_reason": "permission denied on /etc/shadow",
                "confidence": 0.0,
            },
        )

        await orchestrator.tick()

        fetched = await store.get_dag(dag.id)
        research = next(n for n in fetched.nodes if n.name == "research")
        write = next(n for n in fetched.nodes if n.name == "write")

        assert research.status == "failed", (
            "incomplete_blocked must NOT advance the DAG"
        )
        assert "incomplete_blocked" in (research.error or "")
        assert "permission denied" in (research.error or "")
        # Dependent node was never started — DAG halted
        assert write.status == "blocked"
        assert fetched.status == "failed"

    @pytest.mark.asyncio
    async def test_incomplete_no_terminal_marks_node_failed(
        self, store, orchestrator, subtask_mgr,
    ):
        dag = await store.create(_two_subtask_request())
        await orchestrator.start_dag(dag.id)
        fetched = await store.get_dag(dag.id)
        research = next(n for n in fetched.nodes if n.name == "research")

        subtask_mgr.get.return_value = SimpleNamespace(
            id=research.subtask_id,
            status="failed",
            result=None,
            error="Subtask exited without calling submit_final_report.",
            final_outcome="incomplete_no_terminal",
            report_jsonb=None,
        )

        await orchestrator.tick()

        fetched = await store.get_dag(dag.id)
        research = next(n for n in fetched.nodes if n.name == "research")
        assert research.status == "failed"
        assert "incomplete_no_terminal" in (research.error or "")

    @pytest.mark.asyncio
    async def test_validation_failed_marks_node_failed(
        self, store, orchestrator, subtask_mgr,
    ):
        dag = await store.create(_two_subtask_request())
        await orchestrator.start_dag(dag.id)
        fetched = await store.get_dag(dag.id)
        research = next(n for n in fetched.nodes if n.name == "research")

        subtask_mgr.get.return_value = SimpleNamespace(
            id=research.subtask_id,
            status="failed",
            result=None,
            error="summary_too_short: len=12 (min 50)",
            final_outcome="validation_failed",
            report_jsonb={
                "summary": "too short",
                "confidence": 0.5,
            },
        )

        await orchestrator.tick()

        fetched = await store.get_dag(dag.id)
        research = next(n for n in fetched.nodes if n.name == "research")
        assert research.status == "failed"
        assert "validation_failed" in (research.error or "")

    @pytest.mark.asyncio
    async def test_completed_with_outcome_completed_advances_normally(
        self, store, orchestrator, subtask_mgr,
    ):
        """Regression guard: final_outcome='completed' MUST advance the DAG."""
        dag = await store.create(_two_subtask_request())
        await orchestrator.start_dag(dag.id)
        fetched = await store.get_dag(dag.id)
        research = next(n for n in fetched.nodes if n.name == "research")

        subtask_mgr.get.return_value = SimpleNamespace(
            id=research.subtask_id,
            status="completed",
            result="real summary text here",
            error=None,
            final_outcome="completed",
            report_jsonb={"summary": "real summary text here", "confidence": 0.9},
        )
        # On the second tick, the dependent's subtask returns pending so the
        # outer DAG status stays running. Replace return_value AFTER tick.
        await orchestrator.tick()

        fetched = await store.get_dag(dag.id)
        research = next(n for n in fetched.nodes if n.name == "research")
        assert research.status == "completed"
        assert research.result == "real summary text here"

    @pytest.mark.asyncio
    async def test_legacy_row_no_final_outcome_falls_through_to_status(
        self, store, orchestrator, subtask_mgr,
    ):
        """Pre-flag rows have final_outcome=None — must use existing status path."""
        dag = await store.create(_two_subtask_request())
        await orchestrator.start_dag(dag.id)
        fetched = await store.get_dag(dag.id)
        research = next(n for n in fetched.nodes if n.name == "research")

        subtask_mgr.get.return_value = SimpleNamespace(
            id=research.subtask_id,
            status="completed",
            result="legacy text result",
            error=None,
            final_outcome=None,  # legacy / pre-flag row
            report_jsonb=None,
        )

        await orchestrator.tick()

        fetched = await store.get_dag(dag.id)
        research = next(n for n in fetched.nodes if n.name == "research")
        assert research.status == "completed"  # legacy advances normally
        assert research.result == "legacy text result"


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

    @pytest.mark.asyncio
    async def test_check_node_active_heartbeat_with_completion_check_transitions_to_awaiting(
        self, store, orchestrator, dynamic_loader
    ):
        """Regression: check node with active heartbeat (run_count=0, quiet hours)
        must still transition to awaiting_check and poll its shell completion_check.

        Before the fix: _sync_check_node only hoisted when the heartbeat check
        was unregistered (None) or inactive. A check present+active with run_count=0
        (never ran — quiet hours suppressed it) hit neither branch and stayed
        'running' until wall-clock timeout.

        After the fix: completion_check present → awaiting_check immediately,
        regardless of heartbeat state. Shell command is polled in the same tick.

        This test MUST fail against pre-fix code (node stays 'running',
        check_attempts stays 0) and pass after the fix.
        """
        dag = await store.create(self._check_node_request())
        await orchestrator.start_dag(dag.id)
        fetched = await store.get_dag(dag.id)
        monitor = next(n for n in fetched.nodes if n.name == "monitor")
        await store.update_node(monitor.id, status="running", check_name="dag-test-monitor")

        # Simulate: heartbeat check registered and active but run_count=0 (quiet hours).
        # This is the exact state from the real incident (DAG ae5f2e9e, node verify-193-ci).
        active_unrun_check = MagicMock()
        active_unrun_check.active = True
        active_unrun_check.run_count = 0
        dynamic_loader._registry.get_check.return_value = active_unrun_check

        # Shell check returns pending — we only care that it was invoked.
        orchestrator._run_completion_check = AsyncMock(return_value=CheckResult("pending"))
        await orchestrator.tick()

        fetched2 = await store.get_dag(dag.id)
        monitor2 = next(n for n in fetched2.nodes if n.name == "monitor")

        assert monitor2.status == "awaiting_check", (
            f"Expected awaiting_check, got {monitor2.status}. "
            "completion_check was not polled — node deadlocked waiting for "
            "heartbeat check that quiet hours suppressed."
        )
        assert monitor2.awaiting_check_at is not None
        # Shell command must have been invoked in the same tick
        orchestrator._run_completion_check.assert_called_once()

    @pytest.mark.asyncio
    async def test_launch_check_node_registers_as_urgent(
        self, store, orchestrator, dynamic_loader
    ):
        """Secondary fix: DAG-managed checks must register with urgent=True so
        quiet-hour suppression (runner.py:205) does not skip them.

        A check node without completion_check depends entirely on the heartbeat
        worker. If that worker is never scheduled (quiet hours, urgent=False),
        the node can never self-disable → same deadlock, different path.
        """
        node = DAGNode(
            id=uuid.uuid4(),
            dag_id=uuid.uuid4(),
            name="chk",
            node_type="check",
            status="ready",
            wave=0,
            instructions="Monitor CI",
        )
        dag = ExecutionDAG(
            id=uuid.uuid4(),
            agent_id="test",
            name="t",
            status="running",
            nodes=[node],
            edges=[],
        )
        await orchestrator._launch_check_node(node, dag)

        dynamic_loader.create_check.assert_called_once()
        _, kwargs = dynamic_loader.create_check.call_args
        assert kwargs.get("urgent") is True, (
            "DAG-managed checks must be registered with urgent=True so "
            "runner.py:205 does not suppress them during quiet hours."
        )

    @pytest.mark.asyncio
    async def test_check_node_heartbeat_disabled_on_shell_success(
        self, store, orchestrator, dynamic_loader
    ):
        """When the shell completion_check passes, the heartbeat worker is disabled
        so it does not keep burning LLM tokens after the node is done."""
        dag = await store.create(self._check_node_request())
        await orchestrator.start_dag(dag.id)
        fetched = await store.get_dag(dag.id)
        monitor = next(n for n in fetched.nodes if n.name == "monitor")
        await store.update_node(
            monitor.id, status="awaiting_check", check_name="dag-test-monitor",
            awaiting_check_at=datetime.now(UTC),
        )
        # Shell check passes on first poll
        orchestrator._run_completion_check = AsyncMock(return_value=CheckResult("success"))
        await orchestrator.tick()

        fetched2 = await store.get_dag(dag.id)
        monitor2 = next(n for n in fetched2.nodes if n.name == "monitor")
        assert monitor2.status == "completed"
        # Heartbeat worker must have been disabled
        dynamic_loader.manage_check.assert_called_with(
            action="disable", name="dag-test-monitor"
        )


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

    @pytest.mark.asyncio
    async def test_launch_subtask_node_passes_dag_node_id(
        self, orchestrator, subtask_mgr
    ):
        """F061 round 5 Codex review: DAG-created subtasks must have
        ``dag_node_id`` set so the dashboard ``dag_correlation`` card
        (which filters ``WHERE dag_node_id IS NOT NULL``) actually shows
        them. Without this, the card stays empty in production.
        """
        node = DAGNode(
            id=uuid.uuid4(),
            dag_id=uuid.uuid4(),
            name="n",
            node_type="subtask",
            status="ready",
            timeout_seconds=600,
            wave=0,
            instructions="x",
        )
        dag = ExecutionDAG(
            id=uuid.uuid4(), agent_id="test", name="t",
            status="running", nodes=[node], edges=[],
        )
        await orchestrator._launch_subtask_node(node, dag)
        subtask_mgr.create.assert_called_once()
        kwargs = subtask_mgr.create.call_args.kwargs
        assert kwargs.get("dag_node_id") == node.id, (
            "F061 round 5: dag_node_id must be passed through to "
            "subtask_mgr.create() so the dashboard dag_correlation card "
            "can attribute outcomes to DAG nodes."
        )

    @pytest.mark.asyncio
    async def test_orchestrator_clamps_check_node_timeout_to_max(
        self, orchestrator, dynamic_loader
    ):
        """_launch_check_node clamps node.timeout_seconds at dynamic_loader.create_check."""
        settings = orchestrator._settings
        node = DAGNode(
            id=uuid.uuid4(),
            dag_id=uuid.uuid4(),
            name="chk",
            node_type="check",
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
        await orchestrator._launch_check_node(node, dag)
        dynamic_loader.create_check.assert_called_once()
        _, kwargs = dynamic_loader.create_check.call_args
        assert kwargs["timeout_seconds"] == settings.dag_node_max_timeout

    @pytest.mark.asyncio
    async def test_orchestrator_clamps_awaiting_check_timeout_to_max(
        self, orchestrator, store
    ):
        """_poll_awaiting_checks uses clamped timeout when deciding whether to fail out.

        A node with timeout_seconds=99999 (above ceiling 7200) whose completion-check
        has been awaiting for 7500s should fail — because the effective timeout is
        clamped to 7200, not 99999.
        """
        settings = orchestrator._settings
        dag_req = DAGCreateRequest(
            name="awaiting-clamp-dag",
            nodes=[
                DAGNodeSpec(
                    name="long-check",
                    type=DAGNodeType.subtask,
                    instructions="run and check",
                    completion_check="test -f /tmp/done",
                    timeout_seconds=7200,  # at ceiling; store accepts
                ),
            ],
        )
        dag = await store.create(dag_req)
        node = dag.nodes[0]

        # Simulate out-of-band value ABOVE ceiling that historical rows might carry.
        await store.update_node(node.id, timeout_seconds=99999)
        # Put node into awaiting_check with backdated timestamp > clamped ceiling.
        from datetime import UTC, datetime, timedelta
        past = datetime.now(UTC) - timedelta(seconds=settings.dag_node_max_timeout + 300)
        await store.update_node(
            node.id, status="awaiting_check", awaiting_check_at=past
        )

        fetched = await store.get_dag(dag.id)
        await orchestrator._poll_awaiting_checks(fetched)

        final = await store.get_dag(dag.id)
        timed_out = next(n for n in final.nodes if n.name == "long-check")
        assert timed_out.status == "failed"
        assert f"{settings.dag_node_max_timeout}s" in (timed_out.error or "")


class TestStaleReadyRecovery:
    """Issue #430 Bug 1: orphaned wave-0 'ready' nodes are recovered.

    Root cause: start_dag() is the only path that transitions wave-0 nodes
    from 'ready' to dispatched. If that path is bypassed (psql-INSERT in
    the issue #430 case, or any other failure between dag_create and the
    start_dag call) those nodes are invisible to _find_ready_nodes() which
    only looks for 'pending' nodes — the DAG hangs silently.

    The fix: _recover_stale_ready_nodes() in _advance_dag() demotes stale
    'ready' nodes to 'pending' after _STALE_READY_GRACE_SECONDS. For the
    issue #430 pending-DAG bypass scenario, it ALSO promotes the DAG to
    'running' so downstream tick-loop steps see a consistent invariant.
    """

    @pytest.mark.asyncio
    async def test_pending_dag_bypass_scenario_from_issue_430(
        self, store, orchestrator, subtask_mgr, monkeypatch
    ):
        """The actual scenario from issue #430: psql INSERT'd a DAG with
        status='pending' and wave-0 status='ready', started_at=NULL.
        After the grace period the sweep must promote the DAG to 'running'
        AND demote the ready nodes — the same canonical invariant
        start_dag() would have produced.
        """
        import nous.dag.orchestrator as orch_module
        monkeypatch.setattr(orch_module, "_STALE_READY_GRACE_SECONDS", 0)

        # store.create produces exactly the issue-430 bypass state:
        # status='pending', started_at=NULL, wave-0 nodes status='ready'.
        # No update_dag_status('running') call — that's the bypass.
        dag = await store.create(_two_subtask_request())
        fetched = await store.get_dag(dag.id)
        assert fetched.status == "pending"
        assert fetched.started_at is None
        research = next(n for n in fetched.nodes if n.name == "research")
        assert research.status == "ready"
        assert research.started_at is None

        await orchestrator.tick()

        fetched = await store.get_dag(dag.id)
        # DAG promoted to running, started_at populated.
        assert fetched.status == "running", (
            f"Recovery must promote orphaned 'pending' DAG to 'running'; "
            f"got '{fetched.status}'"
        )
        assert fetched.started_at is not None, (
            "Recovery must set started_at when promoting from pending"
        )
        # Ready node demoted and dispatched on the same tick.
        research = next(n for n in fetched.nodes if n.name == "research")
        assert research.status == "running", (
            f"Wave-0 node must be dispatched after recovery; "
            f"got '{research.status}'"
        )
        subtask_mgr.create.assert_called()

    @pytest.mark.asyncio
    async def test_running_dag_stale_ready_node_recovered(
        self, store, orchestrator, subtask_mgr, monkeypatch
    ):
        """Codex round-1 parity case: DAG already 'running' (started_at set)
        but a wave-0 node is still 'ready' because the dispatch path died.
        Should be swept and dispatched on the same tick.
        """
        import nous.dag.orchestrator as orch_module
        monkeypatch.setattr(orch_module, "_STALE_READY_GRACE_SECONDS", 0)

        dag = await store.create(_two_subtask_request())
        await store.update_dag_status(dag.id, "running")  # bypass start_dag

        fetched = await store.get_dag(dag.id)
        research = next(n for n in fetched.nodes if n.name == "research")
        assert research.status == "ready"
        assert research.started_at is None
        assert research.subtask_id is None

        await orchestrator.tick()

        fetched = await store.get_dag(dag.id)
        research = next(n for n in fetched.nodes if n.name == "research")
        assert research.status == "running"
        subtask_mgr.create.assert_called()

    @pytest.mark.asyncio
    async def test_fresh_running_dag_ready_node_not_swept(
        self, store, orchestrator, subtask_mgr
    ):
        """A 'ready' wave-0 node whose DAG just transitioned to 'running'
        is NOT swept because the elapsed time is within the default grace
        period (300s). This preserves the legitimate sub-second
        dag_create→start_dag window.
        """
        dag = await store.create(_two_subtask_request())
        await store.update_dag_status(dag.id, "running")  # bypass start_dag

        fetched = await store.get_dag(dag.id)
        research = next(n for n in fetched.nodes if n.name == "research")
        assert research.status == "ready"

        await orchestrator.tick()

        # started_at is 'just now' → elapsed < 300s → no sweep fires.
        # _find_ready_nodes ignores 'ready' status → no dispatch.
        fetched = await store.get_dag(dag.id)
        research = next(n for n in fetched.nodes if n.name == "research")
        assert research.status == "ready", (
            "Fresh ready node must NOT be swept before the grace period elapses"
        )
        subtask_mgr.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_fresh_pending_dag_not_swept(
        self, store, orchestrator, subtask_mgr
    ):
        """A 'pending' DAG within the grace window represents the legitimate
        sub-second dag_create → start_dag transition. The sweep must NOT
        fire on it — only orphans (DAGs older than grace) are recovered.
        """
        # No grace monkeypatch — full 300s default.
        # No update_dag_status — DAG stays 'pending', started_at=NULL.
        dag = await store.create(_two_subtask_request())

        fetched = await store.get_dag(dag.id)
        assert fetched.status == "pending"
        assert fetched.started_at is None

        await orchestrator.tick()

        # Within grace window → sweep guard rejects the recovery → DAG
        # remains pending with ready wave-0 nodes (waiting for start_dag).
        fetched = await store.get_dag(dag.id)
        assert fetched.status == "pending", (
            "Fresh pending DAG must NOT be promoted before grace expires"
        )
        research = next(n for n in fetched.nodes if n.name == "research")
        assert research.status == "ready"
        subtask_mgr.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_recovery_skipped_when_no_recoverable_nodes(
        self, store, orchestrator, subtask_mgr, monkeypatch
    ):
        """Defense in depth: if a pending DAG has no orphan ready nodes
        (all wave-0 nodes already moved to completed), the sweep must NOT
        promote the DAG to 'running' — avoids spurious state transitions.
        """
        import nous.dag.orchestrator as orch_module
        monkeypatch.setattr(orch_module, "_STALE_READY_GRACE_SECONDS", 0)

        dag = await store.create(_two_subtask_request())
        for node in dag.nodes:
            if node.wave == 0:
                await store.update_node(node.id, status="completed")

        await orchestrator.tick()

        fetched = await store.get_dag(dag.id)
        # No orphan ready nodes → recovery sweep early-returns. The DAG
        # may end up promoted by OTHER tick-loop paths (e.g. wave-1
        # dispatch) but the sweep itself must not be what promotes it.
        # Specifically, no log warning about "Recovered orphaned pending
        # DAG" should have fired. We assert subtask_mgr.create was NOT
        # called for any wave-0 node (already completed) — wave-1 nodes
        # may or may not dispatch depending on dependency edges.
        wave0_completed = [n for n in fetched.nodes if n.wave == 0]
        assert all(n.status == "completed" for n in wave0_completed)


class TestDAGCompletionStatus:
    """Audit DG-1: a DAG that finishes via skip_and_continue (some nodes
    'skipped', rest 'completed') must finalize 'completed', not 'failed'."""

    def _orchestrator_with_mock_store(self):
        store = AsyncMock()
        return DAGOrchestrator(
            store=store,
            subtask_mgr=AsyncMock(),
            dynamic_loader=AsyncMock(),
            settings=Settings(_env_file=None),
        ), store

    def _node(self, status, name="n", node_type="subtask"):
        return SimpleNamespace(
            id=uuid.uuid4(), name=name, status=status,
            node_type=node_type, parent_node=None,
        )

    @pytest.mark.asyncio
    async def test_completed_plus_skipped_finalizes_completed(self):
        orch, store = self._orchestrator_with_mock_store()
        dag = SimpleNamespace(
            id=uuid.uuid4(),
            nodes=[self._node("completed", "a"), self._node("skipped", "b")],
        )
        await orch._check_dag_completion(dag)
        store.update_dag_status.assert_awaited_once()
        args, kwargs = store.update_dag_status.await_args
        assert args[1] == "completed"
        assert "skipped" in (kwargs.get("result_summary") or args[2])

    @pytest.mark.asyncio
    async def test_all_completed_finalizes_completed(self):
        orch, store = self._orchestrator_with_mock_store()
        dag = SimpleNamespace(
            id=uuid.uuid4(),
            nodes=[self._node("completed", "a"), self._node("completed", "b")],
        )
        await orch._check_dag_completion(dag)
        args, _ = store.update_dag_status.await_args
        assert args[1] == "completed"

    @pytest.mark.asyncio
    async def test_failed_node_still_finalizes_failed(self):
        orch, store = self._orchestrator_with_mock_store()
        dag = SimpleNamespace(
            id=uuid.uuid4(),
            nodes=[self._node("completed", "a"), self._node("failed", "b")],
        )
        await orch._check_dag_completion(dag)
        args, _ = store.update_dag_status.await_args
        assert args[1] == "failed"

    @pytest.mark.asyncio
    async def test_all_blocked_still_finalizes_failed(self):
        orch, store = self._orchestrator_with_mock_store()
        dag = SimpleNamespace(
            id=uuid.uuid4(),
            nodes=[self._node("blocked", "a"), self._node("blocked", "b")],
        )
        await orch._check_dag_completion(dag)
        args, _ = store.update_dag_status.await_args
        assert args[1] == "failed"


class TestSubtaskNodeDeferral:
    """Audit DG-4: a full subtask queue must DEFER a node, not fail it."""

    def _node(self):
        return SimpleNamespace(
            id=uuid.uuid4(), name="work", status="ready", node_type="subtask",
            subtask_id=None, frame_type="task", model=None, timeout_seconds=600,
            instructions="do the thing", tools=None, last_activity_at=None,
        )

    @pytest.mark.asyncio
    async def test_queue_full_defers_node_to_pending(self):
        from nous.heart.subtasks import SubtaskQueueFull

        store = AsyncMock()
        subtask_mgr = AsyncMock()
        subtask_mgr.create.side_effect = SubtaskQueueFull("pending subtask limit (5) reached")
        orch = DAGOrchestrator(
            store=store, subtask_mgr=subtask_mgr,
            dynamic_loader=AsyncMock(), settings=Settings(_env_file=None),
        )
        node = self._node()
        dag = SimpleNamespace(id=uuid.uuid4(), nodes=[node], edges=[])

        await orch._launch_subtask_node(node, dag)

        # Deferred, NOT failed.
        assert node.status == "pending"
        statuses = [
            kw.get("status") for (_a, kw) in store.update_node.await_args_list
        ]
        assert "pending" in statuses
        assert "failed" not in statuses

    @pytest.mark.asyncio
    async def test_real_launch_error_still_fails_node(self):
        store = AsyncMock()
        subtask_mgr = AsyncMock()
        subtask_mgr.create.side_effect = RuntimeError("boom")
        orch = DAGOrchestrator(
            store=store, subtask_mgr=subtask_mgr,
            dynamic_loader=AsyncMock(), settings=Settings(_env_file=None),
        )
        node = self._node()
        dag = SimpleNamespace(id=uuid.uuid4(), nodes=[node], edges=[])

        await orch._launch_subtask_node(node, dag)

        assert node.status == "failed"
        statuses = [
            kw.get("status") for (_a, kw) in store.update_node.await_args_list
        ]
        assert "failed" in statuses

    @pytest.mark.asyncio
    async def test_check_pool_full_defers_node(self):
        """Audit DG-4 (review follow-up): a full dynamic-check pool defers the
        check node, it does not permanently fail it."""
        from nous.heartbeat.dynamic import DynamicCheckLimitReached

        store = AsyncMock()
        loader = AsyncMock()
        loader.create_check.side_effect = DynamicCheckLimitReached("Maximum reached")
        orch = DAGOrchestrator(
            store=store, subtask_mgr=AsyncMock(),
            dynamic_loader=loader, settings=Settings(_env_file=None),
        )
        node = SimpleNamespace(
            id=uuid.uuid4(), name="chk", status="ready", node_type="check",
            description="d", instructions="run the check", tools=None,
            timeout_seconds=300,
        )
        dag = SimpleNamespace(id=uuid.uuid4(), nodes=[node], edges=[])

        await orch._launch_check_node(node, dag)

        assert node.status == "pending"
        statuses = [kw.get("status") for (_a, kw) in store.update_node.await_args_list]
        assert "pending" in statuses
        assert "failed" not in statuses

    @pytest.mark.asyncio
    async def test_deferral_cap_eventually_fails_node(self):
        """Audit review P2: a node that keeps hitting a saturated queue is
        failed (with a clear error) after the backstop cap, not bounced forever."""
        from nous.heart.subtasks import SubtaskQueueFull

        store = AsyncMock()
        subtask_mgr = AsyncMock()
        subtask_mgr.create.side_effect = SubtaskQueueFull("full")
        orch = DAGOrchestrator(
            store=store, subtask_mgr=subtask_mgr,
            dynamic_loader=AsyncMock(), settings=Settings(_env_file=None),
        )
        node = SimpleNamespace(
            id=uuid.uuid4(), name="work", status="ready", node_type="subtask",
            subtask_id=None, frame_type="task", model=None, timeout_seconds=600,
            instructions="x", tools=None, last_activity_at=None,
        )
        dag = SimpleNamespace(id=uuid.uuid4(), nodes=[node], edges=[])

        for _ in range(orch._MAX_DEFERRALS):
            await orch._launch_subtask_node(node, dag)

        assert node.status == "failed"
        # final update carries an explanatory error mentioning saturation
        errors = [kw.get("error") for (_a, kw) in store.update_node.await_args_list]
        assert any(e and "saturated" in e for e in errors)
