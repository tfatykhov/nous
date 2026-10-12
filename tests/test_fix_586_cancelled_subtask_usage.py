"""#586: a subtask cancelled while it runs keeps the usage of that run.

cancel() makes a running row terminal without stopping its worker, and the
terminal-state guard in complete()/fail() then dropped the worker's whole
write, counters included: every cancelled subtask showed 0 tokens. The DAG
roll-up claimed that 0 as soon as the row read 'cancelled', and its claim is
one-shot, so a DAG never counted the work of a node it cancelled.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import update

from nous.config import Settings
from nous.dag.orchestrator import DAGOrchestrator
from nous.dag.schemas import DAGCreateRequest, DAGNodeSpec, DAGNodeType
from nous.dag.store import DAGStore
from nous.handlers.subtask_worker import SubtaskWorkerPool
from nous.heart.subtasks import SubtaskManager
from nous.storage.models import Subtask

REPORT = {
    "summary": "Found 3 candidates matching the search criteria. Each was reviewed for fitness.",
    "confidence": 0.9,
    "findings": ["a", "b", "c"],
}


def _settings(**overrides) -> Settings:
    base = dict(
        _env_file=None,
        subtask_hardening_enabled=True,
        subtask_max_attempts=1,
        subtask_workers=1,
        subtask_cleanup_timeout_seconds=5,
        result_inbox_enabled=False,
        result_memory_enabled=False,
        telegram_bot_token="",
        telegram_chat_id="",
        dag_node_default_timeout=120,
        dag_node_max_timeout=3600,
        dag_node_timeout_grace_seconds=300,
    )
    base.update(overrides)
    return Settings(**base)


def _manager(db) -> SubtaskManager:
    return SubtaskManager(db, f"test-fix-586-{uuid.uuid4().hex[:8]}")


class _CancelledMidRun:
    """Stands in for AgentRunner. The subtask is cancelled while its turn runs
    (a DAG cancel, the REST cancel, a companion tap), and the turn goes on,
    as it does in production: cancel() does not stop the worker."""

    def __init__(self, subtasks: SubtaskManager, *, hang_after: int | None = None) -> None:
        self._subtasks = subtasks
        self._hang_after = hang_after
        self.calls = 0

    async def run_turn(self, **kwargs: object) -> tuple[str, None, dict]:
        self.calls += 1
        context = kwargs["context"]
        if self._hang_after is not None and self.calls > self._hang_after:
            await asyncio.Event().wait()  # until the worker's timeout
        _schema, submit = kwargs["extra_tools"]["submit_final_report"]
        if self._hang_after is None:
            await submit(**REPORT)
        await self._subtasks.cancel(context.subtask_id)
        return "Report submitted.", None, {"input_tokens": 300, "output_tokens": 120, "tool_calls": 4}

    async def end_conversation(self, session_id: str, **_: object) -> bool:
        return True


async def _running_row(manager: SubtaskManager, **create_kwargs) -> Subtask:
    await manager.create(task="Research the options", **create_kwargs)
    row = await manager.dequeue("worker-0")
    assert row is not None
    return row


# ---------------------------------------------------------------------------
# (a) The worker's write reaches the cancelled row
# ---------------------------------------------------------------------------


async def test_a_worker_whose_subtask_was_cancelled_mid_run_stores_its_usage(db):
    manager = _manager(db)
    row = await _running_row(manager)
    pool = SubtaskWorkerPool(
        runner=_CancelledMidRun(manager), heart=SimpleNamespace(subtasks=manager), settings=_settings()
    )

    await pool._process_subtask(row)

    stored = await manager.get(row.id)
    assert (stored.status, stored.final_outcome) == ("cancelled", "cancelled")
    assert stored.result is None, "the late worker overwrote the cancelled row's result"
    assert stored.report_jsonb is None
    assert (stored.tokens_in, stored.tokens_out, stored.tool_calls_made, stored.attempts) == (300, 120, 4, 1), (
        "the usage of the run that was cancelled was dropped"
    )


async def test_a_worker_that_times_out_after_its_subtask_was_cancelled_stores_its_usage(db):
    """The worker's timeout branch is the other write a cancelled run reaches."""
    manager = _manager(db)
    row = await _running_row(manager, timeout=1)
    pool = SubtaskWorkerPool(
        runner=_CancelledMidRun(manager, hang_after=1),
        heart=SimpleNamespace(subtasks=manager),
        settings=_settings(subtask_max_attempts=2),
    )

    await pool._process_subtask(row)

    stored = await manager.get(row.id)
    assert (stored.status, stored.final_outcome, stored.error) == ("cancelled", "cancelled", None)
    assert (stored.tokens_in, stored.tokens_out, stored.tool_calls_made, stored.attempts) == (300, 120, 4, 2)


# ---------------------------------------------------------------------------
# (b) A repeated write does not add the usage twice
# ---------------------------------------------------------------------------


async def test_writing_a_cancelled_runs_usage_twice_does_not_double_it(db):
    manager = _manager(db)
    row = await _running_row(manager)
    assert await manager.cancel(row.id) is True
    usage = dict(attempts=1, tokens_in=300, tokens_out=120, tool_calls_made=4)

    await manager.complete(row.id, "late result", final_outcome="completed", **usage)
    await manager.fail(row.id, "late error", final_outcome="errored", **usage)
    stored = await manager.get(row.id)
    assert (stored.tokens_in, stored.tokens_out, stored.tool_calls_made, stored.attempts) == (300, 120, 4, 1)

    assert await manager.record_cancelled_usage(row.id, **usage) is True
    stored = await manager.get(row.id)
    assert (stored.tokens_in, stored.tokens_out, stored.tool_calls_made, stored.attempts) == (300, 120, 4, 1)
    assert (stored.status, stored.final_outcome, stored.result, stored.error) == ("cancelled", "cancelled", None, None)


async def test_a_smaller_usage_never_lowers_a_cancelled_rows_counters(db):
    manager = _manager(db)
    row = await _running_row(manager)
    await manager.cancel(row.id)

    await manager.record_cancelled_usage(row.id, attempts=2, tokens_in=300, tokens_out=120)
    await manager.record_cancelled_usage(row.id, attempts=1, tokens_in=10, tokens_out=500)

    stored = await manager.get(row.id)
    assert (stored.attempts, stored.tokens_in, stored.tokens_out) == (2, 300, 500)


async def test_the_usage_write_touches_only_cancelled_rows(db):
    manager = _manager(db)
    row = await _running_row(manager)
    await manager.complete(row.id, "done", attempts=1, tokens_in=50, tokens_out=5)

    assert await manager.record_cancelled_usage(row.id, attempts=3, tokens_in=999, tokens_out=999) is False

    stored = await manager.get(row.id)
    assert (stored.attempts, stored.tokens_in, stored.tokens_out) == (1, 50, 5)


# ---------------------------------------------------------------------------
# (c) The DAG roll-up counts a node it cancelled
# ---------------------------------------------------------------------------


async def _dag_with_running_node(db, *, subtask_timeout: int = 120):
    agent_id = f"test-fix-586-{uuid.uuid4().hex[:8]}"
    settings = _settings()
    store = DAGStore(db, agent_id, settings)
    manager = SubtaskManager(db, agent_id)
    loader = AsyncMock()
    loader._registry = MagicMock()
    loader._registry.get_check.return_value = None
    delivery = AsyncMock()
    delivery.deliver.return_value = SimpleNamespace(delivered=True, legs=(), summary="ok", summary_authored=False)
    orch = DAGOrchestrator(
        store=store, subtask_mgr=manager, dynamic_loader=loader, settings=settings, delivery=delivery
    )
    orch.clock_wired = True

    dag = await store.create(
        DAGCreateRequest(
            name="cancelled-node-tokens",
            nodes=[DAGNodeSpec(name="work", type=DAGNodeType.subtask, instructions="Do the work", timeout_seconds=120)],
        )
    )
    await store.update_dag_status(dag.id, "running")
    node = dag.nodes[0]
    row = await _running_row(manager, dag_node_id=node.id, timeout=subtask_timeout)
    await store.update_node(node.id, status="running", subtask_id=row.id, started_at=datetime.now(UTC))
    return store, manager, orch, dag.id, row


async def test_a_dag_counts_the_tokens_of_a_node_it_cancelled_mid_run(db):
    store, manager, orch, dag_id, row = await _dag_with_running_node(db)

    await orch.cancel_dag(dag_id, reason="cancelled by user")
    assert (await manager.get(row.id)).status == "cancelled"
    await orch.tick()
    await orch.wait_for_delivery()

    # The cancellation is announced at once, but the worker is still running:
    # its counters are not final, so the node is not claimed yet.
    dag = await store.get_dag(dag_id)
    assert dag.delivered_at is not None and dag.delivery_error is None
    assert dag.nodes[0].tokens_counted is False, "the roll-up froze a cancelled run's usage before it reported"

    # The worker unwinds and writes its outcome, as it does in production.
    await manager.complete(
        row.id, "late result", final_outcome="completed", attempts=1, tokens_in=300, tokens_out=120, tool_calls_made=4
    )
    await orch.tick()

    dag = await store.get_dag(dag_id)
    assert dag.status == "cancelled"
    assert (dag.nodes[0].tokens_counted, dag.nodes[0].tokens_used) == (True, 420)
    assert dag.tokens_consumed == 420, "the DAG total missed the node it cancelled"

    # Counted once: another tick adds nothing.
    await orch.tick()
    assert (await store.get_dag(dag_id)).tokens_consumed == 420


async def test_a_dag_cancelled_mid_run_is_announced_without_waiting_for_the_run(db):
    """Parity pin (green before #586 too): the deferred claim must not hold
    the cancellation notice back while the run takes its timeout to unwind."""
    store, manager, orch, dag_id, row = await _dag_with_running_node(db)

    await orch.cancel_dag(dag_id)
    await orch.tick()
    await orch.wait_for_delivery()

    dag = await store.get_dag(dag_id)
    assert (dag.delivery_attempts, dag.delivery_error) == (0, None)
    assert dag.delivered_at is not None
    orch._delivery.deliver.assert_awaited_once()


async def test_a_node_cancelled_before_its_subtask_started_is_claimed_at_once(db):
    """Parity pin (green before #586 too): no run, nothing to wait for."""
    store, manager, orch, dag_id, row = await _dag_with_running_node(db)
    # Back to pending, as a node whose subtask no worker had taken yet.
    async with db.session() as session:
        await session.execute(
            update(Subtask).where(Subtask.id == row.id).values(status="pending", worker_id=None, started_at=None)
        )
        await session.commit()

    await orch.cancel_dag(dag_id)

    assert await orch._reconcile_token_accounting(await store.get_dag(dag_id)) is True
    dag = await store.get_dag(dag_id)
    assert (dag.nodes[0].tokens_counted, dag.tokens_consumed) == (True, 0)


async def test_a_cancelled_run_that_never_reports_stops_blocking_the_claim(db):
    """A worker that died never writes; the wait ends at its timeout plus grace.
    Parity pin (green before #586 too, which never waited)."""
    store, manager, orch, dag_id, row = await _dag_with_running_node(db, subtask_timeout=60)
    await orch.cancel_dag(dag_id)
    long_ago = datetime.now(UTC) - timedelta(seconds=60 + 300 + 5)
    async with db.session() as session:
        await session.execute(update(Subtask).where(Subtask.id == row.id).values(started_at=long_ago))
        await session.commit()

    assert await orch._reconcile_token_accounting(await store.get_dag(dag_id)) is True
    assert (await store.get_dag(dag_id)).nodes[0].tokens_counted is True
