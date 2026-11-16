"""Goals PR 0c: no background loop claims, fires, ticks or delivers while halted."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from f099_support import RecordingBus, env_factory, inbox_rows, runner_env, say  # noqa: F401
from sqlalchemy import select, update
from test_f099_phase0b import _callback_check
from test_f099_phase0b import _runner as _hb_runner

from nous import background_halt
from nous.background_halt import BackgroundHalt
from nous.dag.schemas import DAGCreateRequest
from nous.dag.store import DAGStore
from nous.handlers.continuation_runner import ContinuationRunner
from nous.handlers.subtask_worker import SubtaskWorkerPool
from nous.heart.subtasks import BackgroundHalted, SubtaskQueueFull
from nous.heartbeat.checks import EmailCheck, HealthCheck, SelfInitiatedCheck
from nous.heartbeat.finding_store import FindingStore
from nous.heartbeat.registry import CheckRegistry
from nous.heartbeat.schemas import CheckResult, Finding
from nous.storage.models import ExecutionDAG, Intention, Schedule, Subtask

GOOD_REPORT = {
    "summary": "Found 3 candidates matching the search criteria. Each was reviewed for fitness.",
    "confidence": 0.9,
    "findings": ["a", "b", "c"],
}


def _install() -> BackgroundHalt:
    halt = BackgroundHalt(host_hold=False)
    background_halt.install(halt)
    return halt


async def _row(env, subtask_id) -> Subtask:
    async with env.db.session() as s:
        return (await s.execute(select(Subtask).where(Subtask.id == subtask_id))).scalar_one()


async def _make(env, *, notify: bool = True) -> Subtask:
    return await env.heart.subtasks.create(task="Check the snow report", parent_session_id="S1", notify=notify)


# ---- the worker ---------------------------------------------------------------------------------------------


async def test_workers_claim_nothing_while_halted_and_a_dequeued_row_is_cancelled(env_factory):  # noqa: F811
    env = await env_factory(subtask_poll_interval=0.01)
    pending = await _make(env)
    halt = _install()
    await halt.halt(actor="t", surface="dashboard")
    pool = SubtaskWorkerPool(MagicMock(), env.heart, env.settings, http_client=env.http)
    pool._running = True
    pool._process_subtask = AsyncMock()
    dequeue = env.heart.subtasks.dequeue
    env.heart.subtasks.dequeue = AsyncMock(side_effect=dequeue)
    worker = asyncio.create_task(pool._worker_loop("worker-0"))
    await asyncio.sleep(0.1)
    env.heart.subtasks.dequeue.assert_not_awaited()  # nothing is claimed while halted
    assert (await _row(env, pending.id)).status == "pending"

    # The race: the claim succeeds, and the halt lands as it returns. The row is cancelled and never processed.
    assert (await halt.resume(actor="t", via="dashboard")).changed

    async def claim_then_halt(worker_id):
        claimed = await dequeue(worker_id)
        if claimed is not None:
            await halt.halt(actor="t", surface="dashboard")
        return claimed

    env.heart.subtasks.dequeue = AsyncMock(side_effect=claim_then_halt)
    for _ in range(100):
        if (await _row(env, pending.id)).status == "cancelled":
            break
        await asyncio.sleep(0.02)
    assert (await _row(env, pending.id)).status == "cancelled"
    pool._process_subtask.assert_not_awaited()
    assert not worker.done()  # the worker is alive
    pool._running = False
    await asyncio.wait_for(worker, timeout=2)


def _fake_runner(halt: BackgroundHalt, *, report: bool) -> MagicMock:
    """A runner whose turn is running when the halt lands, and returns its text (and a report) afterwards."""
    runner = MagicMock()
    runner.end_conversation = AsyncMock()
    runner.root_cancelled = MagicMock(return_value=False)

    async def run_turn(**kwargs):
        await halt.halt(actor="t", surface="dashboard")  # the flag flips while the model call is out
        extra = kwargs.get("extra_tools") or {}
        if report and "submit_final_report" in extra:
            await extra["submit_final_report"][1](**GOOD_REPORT)
        return "Snow: 40 cm", MagicMock(), {"input_tokens": 10, "output_tokens": 5, "tool_calls": 0}

    runner.run_turn = AsyncMock(side_effect=run_turn)
    return runner


@pytest.mark.parametrize("hardened", [False, True], ids=["legacy", "hardened"])
async def test_a_subtask_whose_model_call_returns_between_the_flag_and_the_cancel_leaves_no_inbox_row_and_no_completed_row(  # noqa: E501
    env_factory,  # noqa: F811
    hardened,
):
    env = await env_factory(
        result_inbox_enabled=True,
        result_memory_enabled=True,
        subtask_hardening_enabled=hardened,
        telegram_bot_token="token",
        telegram_chat_id="1",
    )
    subtask = await _make(env, notify=True)
    halt = _install()
    bus = RecordingBus()
    pool = SubtaskWorkerPool(
        _fake_runner(halt, report=hardened), env.heart, env.settings, bus=bus, http_client=env.http
    )
    env.heart.result_memory.record = AsyncMock()
    claimed = await env.heart.subtasks.dequeue("worker-0")
    assert claimed.id == subtask.id
    with background_halt.epoch_scope(halt.epoch):  # as the worker loop does around a claimed run
        await pool._process_subtask(claimed)
    await env.heart.subtasks.cancel_all_active()  # the sweep's UPDATE, after the turn returned
    row = await _row(env, subtask.id)
    assert row.status == "cancelled" and row.result is None
    assert await inbox_rows(env, subtask.id) == []
    assert [e.type for e in bus.events if e.type in ("subtask_completed", "subtask_failed", "subtask_outcome")] == []
    env.http.post.assert_not_called()  # no Telegram push
    env.heart.result_memory.record.assert_not_awaited()


# ---- creation ---------------------------------------------------------------------------------------------------


async def test_create_refuses_while_halted_for_every_caller(env_factory):  # noqa: F811
    env = await env_factory()
    halt = _install()
    await halt.halt(actor="t", surface="dashboard")
    with pytest.raises(BackgroundHalted) as caught:
        await env.heart.subtasks.create(task="anything")
    assert isinstance(caught.value, SubtaskQueueFull)  # every caller that maps a full queue refuses it the same way
    # The companion tap's answer is "could not queue the action: " + this text.
    assert str(caught.value) == "background work is halted by the owner; resume it to run this"
    store = DAGStore(env.db, env.settings.agent_id, env.settings)
    request = DAGCreateRequest(name="d", nodes=[{"name": "a", "type": "subtask", "instructions": "do a"}], edges=[])
    with pytest.raises(BackgroundHalted):
        await store.create(request)
    async with env.db.session() as s:
        dags = (await s.execute(select(ExecutionDAG).where(ExecutionDAG.agent_id == env.settings.agent_id))).all()
        subtasks = (await s.execute(select(Subtask).where(Subtask.agent_id == env.agent))).all()
    assert dags == [] and subtasks == []  # no pending DAG for the tick's repair to start, no row at all


# ---- the scheduler -----------------------------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_schedule_due_while_halted_and_after_resume_does_not_burst(env_factory):  # noqa: F811
    from nous.handlers.task_scheduler import TaskScheduler

    env = await env_factory(schedule_check_interval=1, schedule_policies_enabled=True)
    interval = 3600
    recurring = []
    for policy in ("skip_missed", "skip_missed", "enqueue_missed_with_cap"):
        schedule = await env.heart.schedules.create(
            task=f"job {len(recurring)}",
            schedule_type="recurring",
            interval_seconds=interval,
            catch_up_policy=policy,
            **({"catch_up_cap": 5} if policy == "enqueue_missed_with_cap" else {}),
        )
        recurring.append(schedule)
    once = [
        await env.heart.schedules.create(
            task=f"once {i}", schedule_type="once", fire_at=datetime.now(UTC) + timedelta(hours=1)
        )
        for i in range(2)
    ]
    past = datetime.now(UTC) - timedelta(seconds=interval * 5 + 60)  # five missed slots
    async with env.db.session() as s:
        for schedule in [*recurring, *once]:
            await s.execute(update(Schedule).where(Schedule.id == schedule.id).values(next_fire_at=past))
        await s.commit()
    bus = RecordingBus()
    scheduler = TaskScheduler(env.heart, env.settings, bus=bus)
    halt = _install()
    await halt.halt(actor="t", surface="dashboard")

    assert await scheduler._fire_due_tasks() == 0
    assert await _subtasks(env) == []
    now = datetime.now(UTC)
    for schedule in recurring:
        row = await env.heart.schedules.get(schedule.id)
        assert row.next_fire_at > now and row.fire_count == 0 and row.active
    for schedule in once:
        row = await env.heart.schedules.get(schedule.id)
        assert row.active and row.next_fire_at <= now  # left due: it fires at the resume, late
    assert {e.data["outcome"] for e in bus.events if e.type == "schedule_fire_suppressed"} == {"halted"}

    assert (await halt.resume(actor="t", via="dashboard")).changed
    fired = await scheduler._fire_due_tasks()
    assert fired == 2  # only the two one-time schedules, together; no recurring burst
    assert sorted(st.task for st in await _subtasks(env)) == ["once 0", "once 1"]
    # Past the next slot: exactly one run per recurring schedule.
    async with env.db.session() as s:
        for schedule in recurring:
            await s.execute(
                update(Schedule)
                .where(Schedule.id == schedule.id)
                .values(next_fire_at=datetime.now(UTC) - timedelta(seconds=30))
            )
        await s.execute(update(Subtask).where(Subtask.agent_id == env.agent).values(status="completed"))
        await s.commit()
    assert await scheduler._fire_due_tasks() == 3


async def _subtasks(env) -> list[Subtask]:
    async with env.db.session() as s:
        return list((await s.execute(select(Subtask).where(Subtask.agent_id == env.agent))).scalars().all())


# ---- the continuation runner -----------------------------------------------------------------------------------


class _NoDatabase:
    def __init__(self) -> None:
        self.sessions = 0

    def session(self):
        self.sessions += 1
        raise AssertionError("no database read while halted")


async def test_a_halted_continuation_runner_launches_claims_and_resumes_nothing():
    from nous.config import Settings

    db = _NoDatabase()
    runner = ContinuationRunner(
        database=db,
        settings=Settings(_env_file=None, telegram_bot_token="t"),
        runner=MagicMock(),
        heart=MagicMock(),
        brain=MagicMock(),
    )
    halt = _install()
    await halt.halt(actor="t", surface="dashboard")
    assert await runner._launch() == ([], None)
    assert await runner.run_arrival(uuid.uuid4()) is None
    assert await runner._resume_approved() == 0
    assert db.sessions == 0


# ---- the DAG orchestrator ----------------------------------------------------------------------------------------


async def test_a_halted_dag_tick_advances_nothing_and_start_and_retry_are_refused():
    from nous.dag.orchestrator import DAGOrchestrator

    store = MagicMock()
    store.get_active_dags = AsyncMock(return_value=[])
    orchestrator = DAGOrchestrator.__new__(DAGOrchestrator)
    orchestrator._store = store
    orchestrator._lock = asyncio.Lock()
    halt = _install()
    await halt.halt(actor="t", surface="dashboard")
    with pytest.raises(BackgroundHalted):
        await orchestrator.start_dag(uuid.uuid4())
    with pytest.raises(BackgroundHalted):
        await orchestrator.retry_node(uuid.uuid4(), "a")
    store.get_dag.assert_not_called()


# ---- the heartbeat -----------------------------------------------------------------------------------------------


def _health_check(finding: Finding) -> HealthCheck:
    check = HealthCheck(MagicMock(), MagicMock(), MagicMock())
    check.run = AsyncMock(return_value=CheckResult(has_updates=True, findings=[finding]))
    return check


async def test_a_halted_tick_runs_only_code_only_checks_and_a_health_finding_still_reaches_telegram():
    registry = CheckRegistry()
    urgent = Finding(source="health", summary="The database is slow", urgency="high", needs_action=True)
    health = _health_check(urgent)
    registry.register(health, permanent=True)
    agent_check = _callback_check("disk_watch")
    agent_check.run = AsyncMock(return_value=CheckResult(has_updates=False, findings=[]))
    registry.register(agent_check)
    initiated = SelfInitiatedCheck(MagicMock(), MagicMock(), MagicMock(), embeddings=None)
    initiated.run = AsyncMock(return_value=CheckResult(has_updates=False, findings=[]))
    registry.register(initiated)
    hb, triage = _hb_runner(registry)
    del hb._triage  # the real triage: the urgent send must still happen
    hb._send_telegram = AsyncMock()
    hb._finding_store = None
    halt = _install()
    await halt.halt(actor="t", surface="dashboard")
    await hb._tick()
    health.run.assert_awaited_once()
    agent_check.run.assert_not_awaited()
    initiated.run.assert_not_awaited()
    hb._send_telegram.assert_awaited_once()
    assert "The database is slow" in hb._send_telegram.await_args.args[0]
    triage.run_turn.assert_not_awaited()  # no cognitive triage, no callback


def _triage_messages(triage: MagicMock) -> list[str]:
    return [c.args[1] for c in triage.run_turn.call_args_list if c.args[1].startswith("[Heartbeat] The following")]


@pytest.mark.parametrize("urgency", ["normal", "high"])
@pytest.mark.parametrize("with_store", [True, False], ids=["finding-store", "no-store"])
async def test_a_finding_kept_while_a_halt_landed_is_triaged_once_after_the_resume(with_store, urgency):
    """An email check reports a finding once (its cursor moves) while a halt lands: no triage turn while halted, one
    at the first tick after the resume, none at the next. A high-urgency one reaches Telegram once, while halted."""
    registry = CheckRegistry()
    halt = _install()
    finding = Finding(source="email", summary="The bank asks for a signed form", urgency=urgency, needs_action=True)
    reports: list[int] = []

    async def report_once_while_the_halt_lands():
        if reports:
            return CheckResult(has_updates=False, findings=[])
        reports.append(1)
        await halt.halt(actor="t", surface="dashboard")
        return CheckResult(has_updates=True, findings=[finding])

    email = EmailCheck(
        SimpleNamespace(heartbeat_email_interval=3600, heartbeat_email_imap_host="", email_user="", email_password="")
    )
    email.run = AsyncMock(side_effect=report_once_while_the_halt_lands)
    registry.register(email)
    hb, triage = _hb_runner(registry)
    del hb._triage  # the real triage
    hb._send_telegram = AsyncMock()
    if with_store:
        hb._finding_store = FindingStore()
        hb._finding_store._startup_suppression_seconds = 0
    await hb._tick()  # the halt lands during the check; its result is kept
    assert _triage_messages(triage) == []
    assert hb._send_telegram.await_count == (1 if urgency == "high" else 0)
    await hb._tick()  # still halted
    assert _triage_messages(triage) == [] and email.run.await_count == 1
    await halt.resume(actor="t", via="dashboard")
    await hb._tick()
    (message,) = _triage_messages(triage)
    assert "The bank asks for a signed form" in message
    await hb._tick()
    await hb._tick()
    assert len(_triage_messages(triage)) == 1
    assert hb._send_telegram.await_count == (1 if urgency == "high" else 0)  # not sent again at the resume


async def test_findings_held_while_halted_keep_the_newest_and_a_resolved_one_is_not_triaged():
    from nous.heartbeat import runner as hb_runner_module
    from nous.heartbeat.schemas import FindingState

    hb, triage = _hb_runner(CheckRegistry())
    hb._finding_store = FindingStore()
    hb._finding_store._startup_suppression_seconds = 0
    halt = _install()
    await halt.halt(actor="t", surface="dashboard")
    total = hb_runner_module._UNTRIAGED_MAX + 3
    names = [f"item {'abcdefghij'[i // 10]}{'abcdefghij'[i % 10]}" for i in range(total)]  # digits fingerprint alike
    many = [Finding(source="x", summary=name, needs_action=True) for name in names]
    for f in many:
        hb._finding_store.ingest(f)
    await hb._cognitive_triage(many)
    assert list(hb._untriaged) == [f.fingerprint() for f in many[3:]]
    hb._finding_store.get_tracked(many[-1].fingerprint()).state = FindingState.RESOLVED
    await halt.resume(actor="t", via="dashboard")
    await hb._tick()
    (message,) = _triage_messages(triage)
    assert [line for line in message.splitlines() if line.startswith("- [")] == [f"- [x] {n}" for n in names[3:-1]]
    assert hb._untriaged == {}


async def test_a_mid_run_agent_check_is_discarded_not_failed():
    registry = CheckRegistry()
    check = _callback_check("disk_watch")
    halt = _install()

    async def run_while_the_halt_lands():
        await halt.halt(actor="t", surface="dashboard")
        return CheckResult(has_updates=True, findings=[Finding(source="x", summary="y")], self_disabled=True)

    check.run = AsyncMock(side_effect=run_while_the_halt_lands)
    registry.register(check)
    hb, triage = _hb_runner(registry)
    hb._record_run_stats = AsyncMock()
    findings = await hb._tick()
    assert findings == []
    hb._record_run_stats.assert_not_awaited()  # neither a success nor a failure is recorded
    assert check._consecutive_failures == 0 if hasattr(check, "_consecutive_failures") else True
    triage.run_turn.assert_not_awaited()  # its callback does not run


async def test_trigger_check_skips_an_agent_run_check_while_halted():
    registry = CheckRegistry()
    check = _callback_check("disk_watch")
    check.run = AsyncMock()
    registry.register(check)
    hb, _triage = _hb_runner(registry)
    halt = _install()
    await halt.halt(actor="t", surface="dashboard")
    result = await hb.trigger_check("disk_watch")
    assert result.skipped and not check.run.await_count


async def test_missed_check_alarm_and_tuning_do_not_run_while_halted():
    hb, _triage = _hb_runner(CheckRegistry())
    hb._bus = MagicMock()
    hb._bus.emit = AsyncMock()
    hb._settings = SimpleNamespace(heartbeat_tuning_enabled=True)
    hb._finding_store = MagicMock()
    hb._last_tune = None
    halt = _install()
    await halt.halt(actor="t", surface="dashboard")
    await hb._detect_missed_checks()
    await hb._maybe_tune()
    hb._bus.emit.assert_not_awaited()
    assert hb._last_tune is None


# ---- the companion's agent_script source -------------------------------------------------------------------------


async def test_agent_script_source_returns_halted():
    from nous.a2ui.sources import build_default_registry

    ran = []

    async def run_script(*args, **kwargs):
        ran.append(args)
        return {"result": []}

    registry = build_default_registry(run_script=run_script)
    halt = _install()
    await halt.halt(actor="t", surface="dashboard")
    source = registry._fetchers["agent_script"]
    value = await source({"code": "print(1)", "shape": "series"})
    assert ran == [] and "halted" in str(value)


# ---- every consumer of a background turn's text ------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_a_continuation_turn_the_halt_ended_commits_no_report_and_charges_no_attempt(runner_env):  # noqa: F811
    from test_f099_phase2c_arrival import _arrivals, _cont, _owner_rows, _ready_root

    halt = _install()

    async def the_halt_lands_during_the_model_call(kwargs):
        await halt.halt(actor="t", surface="dashboard")
        return [say("I was going to report this.")]

    env = await runner_env(the_halt_lands_during_the_model_call)
    root = await _ready_root(env)
    assert await _cont(env).run_arrival(root.id) is None
    assert len(env.model.calls) == 1  # the follow-up turn made no model call
    assert await _owner_rows(env) == []  # no fallback report of the fixed text
    assert all(a.outcome is None for a in await _arrivals(env, root.id))
    async with env.db.session() as s:
        row = (await s.execute(select(Intention).where(Intention.id == root.id))).scalar_one()
    assert row.attempts == 0 and row.claim_token is None  # released, as for a stop
    assert row.failed_tokens == 110  # what the turn spent still counts against its root


@pytest.mark.postgres_only
async def test_a_continuation_turn_whose_root_the_sweep_cancelled_first_still_books_its_spend(runner_env):  # noqa: F811
    """The halt's sweep cancels the root (and clears the live claim) before the turn's model call returns: the
    spend is booked on the cancelled row, and no attempt is counted."""
    from test_f099_phase2c_arrival import _cont, _owner_rows, _ready_root

    from nous.handlers.halt_sweep import HALT_REASON_TEXT

    halt = _install()
    cont: list[ContinuationRunner] = []

    async def the_sweep_cancels_the_root_during_the_model_call(kwargs):
        await halt.halt(actor="t", surface="dashboard")
        await cont[0].cancel_root(root.id, reason=HALT_REASON_TEXT, actor="halt")
        return [say("I was going to report this.")]

    env = await runner_env(the_sweep_cancels_the_root_during_the_model_call)
    root = await _ready_root(env)
    cont.append(_cont(env))
    assert await cont[0].run_arrival(root.id) is None
    assert await _owner_rows(env) == []
    async with env.db.session() as s:
        row = (await s.execute(select(Intention).where(Intention.id == root.id))).scalar_one()
    assert row.state == "cancelled" and row.claim_token is None and row.attempts == 0
    assert row.failed_tokens == 110  # booked on the cancelled row, with no fence left to pass


async def test_a_dag_summary_the_halt_cut_short_is_not_used():
    from nous.dag.delivery import DAGResultDelivery

    halt = _install()
    delivery = DAGResultDelivery.__new__(DAGResultDelivery)
    delivery._settings = SimpleNamespace(dag_delivery_agent_summary_timeout_seconds=5)
    delivery._build_summary_prompt = MagicMock(return_value="summarise")
    delivery._extract_text = MagicMock(side_effect=lambda result: result[0])

    async def run_turn(**kwargs):
        await halt.halt(actor="t", surface="dashboard")
        return background_halt.HALT_TURN_TEXT, MagicMock(), {}

    delivery._runner = SimpleNamespace(run_turn=run_turn)
    dag = SimpleNamespace(id=uuid.uuid4(), delivery_generation=1)
    leg, text = await delivery._leg_agent_summary(dag, "the template")
    assert text is None and leg.detail == "halted"


async def test_trigger_check_discards_a_run_the_halt_reached():
    registry = CheckRegistry()
    check = _callback_check("disk_watch")
    halt = _install()

    async def run_while_the_halt_lands():
        await halt.halt(actor="t", surface="dashboard")
        return CheckResult(has_updates=True, findings=[Finding(source="x", summary="y")])

    check.run = AsyncMock(side_effect=run_while_the_halt_lands)
    registry.register(check)
    hb, _triage = _hb_runner(registry)
    hb._record_run_stats = AsyncMock()
    result = await hb.trigger_check("disk_watch")
    assert result.skipped
    hb._record_run_stats.assert_not_awaited()


class _DagTickOrchestrator:
    """A DAGOrchestrator with only what tick() touches stubbed."""

    @staticmethod
    def build(dags):
        from nous.dag.orchestrator import DAGOrchestrator

        orchestrator = DAGOrchestrator.__new__(DAGOrchestrator)
        orchestrator._store = MagicMock()
        orchestrator._store.get_active_dags = AsyncMock(return_value=dags)
        orchestrator._lock = asyncio.Lock()
        orchestrator._delivery_lock = MagicMock(locked=MagicMock(return_value=True))  # no delivery sweep task
        orchestrator._sweep_leaked_approval_cards = AsyncMock()
        orchestrator._sweep_leaked_heartbeat_checks = AsyncMock()
        orchestrator._sweep_unclaimed_tokens = AsyncMock()
        orchestrator._advance_dag = AsyncMock()
        return orchestrator


async def test_a_halted_dag_tick_reads_no_dag_and_advances_none():
    dag = SimpleNamespace(id=uuid.uuid4(), nodes=[])
    orchestrator = _DagTickOrchestrator.build([dag])
    halt = _install()
    await halt.halt(actor="t", surface="dashboard")
    assert await orchestrator.tick() == 0
    orchestrator._store.get_active_dags.assert_not_awaited()
    orchestrator._advance_dag.assert_not_awaited()
    orchestrator._sweep_leaked_heartbeat_checks.assert_awaited_once()  # the rest of the tick still runs
    assert (await halt.resume(actor="t", via="dashboard")).changed
    assert await orchestrator.tick() == 1
    orchestrator._advance_dag.assert_awaited_once()


async def test_a_halt_that_lands_during_a_dag_tick_stops_it_at_the_next_dag():
    dags = [SimpleNamespace(id=uuid.uuid4(), nodes=[]) for _ in range(3)]
    orchestrator = _DagTickOrchestrator.build(dags)
    halt = _install()

    async def advance(dag, may_start=True):
        await halt.halt(actor="t", surface="dashboard")

    orchestrator._advance_dag = AsyncMock(side_effect=advance)
    await orchestrator.tick()
    assert orchestrator._advance_dag.await_count == 1


@pytest.mark.postgres_only  # the cancelled row's usage is written with GREATEST, which SQLite lacks
async def test_a_legacy_inline_run_spanning_a_halt_and_a_resume_is_cancelled_not_completed(runner_env):  # noqa: F811
    from nous.api.execution_context import ExecutionContext

    halt = _install()

    async def the_inline_turn_spans_a_halt(kwargs):
        await halt.halt(actor="t", surface="dashboard")
        assert (await halt.resume(actor="t", via="dashboard")).changed
        return [say("Snow: 40 cm")]

    env = await runner_env(the_inline_turn_spans_a_halt, subtask_hardening_enabled=False)
    chat = ExecutionContext(kind="interactive", session_id="s1")
    text, is_error = await env.dispatcher.dispatch(
        "spawn_task",
        {"task": "Check the snow report", "await_result": True, "intent": "Tell the user about the snow"},
        session_id="s1",
        context=chat,
    )
    assert is_error and background_halt.HALT_REASON in text and not halt.halted
    (row,) = await _subtasks(env)
    assert row.status == "cancelled" and row.result is None


async def test_a_hardened_run_saved_before_the_halt_landed_is_delivered_as_usual(env_factory):  # noqa: F811
    env = await env_factory(
        result_inbox_enabled=True,
        subtask_hardening_enabled=True,
        telegram_bot_token="token",
        telegram_chat_id="1",
    )
    subtask = await _make(env, notify=True)
    halt = _install()
    runner = MagicMock()
    runner.end_conversation = AsyncMock()
    runner.root_cancelled = MagicMock(return_value=False)

    async def run_turn(**kwargs):
        await kwargs["extra_tools"]["submit_final_report"][1](**GOOD_REPORT)
        return "Report submitted.", MagicMock(), {"input_tokens": 10, "output_tokens": 5, "tool_calls": 1}

    runner.run_turn = AsyncMock(side_effect=run_turn)
    real_complete = env.heart.subtasks.complete

    async def complete_then_the_halt_lands(*args, **kwargs):
        await real_complete(*args, **kwargs)
        await halt.halt(actor="t", surface="dashboard")

    env.heart.subtasks.complete = complete_then_the_halt_lands
    bus = RecordingBus()
    pool = SubtaskWorkerPool(runner, env.heart, env.settings, bus=bus, http_client=env.http)
    claimed = await env.heart.subtasks.dequeue("worker-0")
    with background_halt.epoch_scope(halt.epoch):
        await pool._process_subtask(claimed)
    assert halt.halted
    row = await _row(env, subtask.id)
    assert row.status == "completed"
    assert "subtask_completed" in [e.type for e in bus.events]
    env.http.post.assert_called_once()  # its push
    assert len(await inbox_rows(env, subtask.id)) == 1  # its inbox row


@pytest.mark.postgres_only  # complete() on a terminal row books the run's usage with GREATEST
async def test_a_legacy_run_whose_row_the_sweep_cancelled_first_announces_nothing(env_factory):  # noqa: F811
    env = await env_factory(
        result_inbox_enabled=True, subtask_hardening_enabled=False, telegram_bot_token="token", telegram_chat_id="1"
    )
    subtask = await _make(env, notify=True)
    halt = _install()
    runner = MagicMock()
    runner.end_conversation = AsyncMock()
    runner.root_cancelled = MagicMock(return_value=False)
    runner.run_turn = AsyncMock(return_value=("Snow: 40 cm", MagicMock(), {"input_tokens": 1, "output_tokens": 1}))
    real_complete = env.heart.subtasks.complete

    async def the_sweep_lands_first(*args, **kwargs):
        await halt.halt(actor="t", surface="dashboard")
        await env.heart.subtasks.cancel_all_active()  # the sweep's first step, before this write
        return await real_complete(*args, **kwargs)

    env.heart.subtasks.complete = the_sweep_lands_first
    bus = RecordingBus()
    pool = SubtaskWorkerPool(runner, env.heart, env.settings, bus=bus, http_client=env.http)
    claimed = await env.heart.subtasks.dequeue("worker-0")
    with background_halt.epoch_scope(halt.epoch):
        await pool._process_subtask(claimed)
    assert (await _row(env, subtask.id)).status == "cancelled"
    assert "subtask_completed" not in [e.type for e in bus.events]
    env.http.post.assert_not_called()
    assert await inbox_rows(env, subtask.id) == []


# ---- a halt that lands during a tick, and the usage of a run the halt ended ---------------------------------------


async def test_a_halt_landing_during_a_tick_stops_the_remaining_agent_run_checks():
    registry = CheckRegistry()
    halt = _install()
    first = _callback_check("first")

    async def run_while_the_halt_lands():
        await halt.halt(actor="t", surface="dashboard")
        return CheckResult(has_updates=False, findings=[])

    first.run = AsyncMock(side_effect=run_while_the_halt_lands)
    registry.register(first)
    second = SelfInitiatedCheck(MagicMock(), MagicMock(), MagicMock(), embeddings=None)
    second.run = AsyncMock(return_value=CheckResult(has_updates=False, findings=[]))
    registry.register(second)
    hb, _triage = _hb_runner(registry)
    await hb._tick()
    first.run.assert_awaited_once()
    second.run.assert_not_awaited()  # due at tick start, not started once the halt was on


async def test_a_check_that_runs_no_turn_keeps_its_result_when_the_halt_lands_during_it():
    registry = CheckRegistry()
    halt = _install()
    found = Finding(source="email", summary="A new invoice arrived", urgency="normal", needs_action=False)
    email_like = SelfInitiatedCheck(MagicMock(), MagicMock(), MagicMock(), embeddings=None)

    async def run_while_the_halt_lands():
        await halt.halt(actor="t", surface="dashboard")  # its own cursor has already moved
        return CheckResult(has_updates=True, findings=[found])

    email_like.run = AsyncMock(side_effect=run_while_the_halt_lands)
    registry.register(email_like)
    hb, _triage = _hb_runner(registry)
    hb._record_run_stats = AsyncMock()
    findings = await hb._tick()
    assert [f.summary for f in findings] == ["A new invoice arrived"]  # kept, not discarded
    hb._record_run_stats.assert_awaited_once()


async def test_a_halted_hardened_run_records_its_usage_on_the_cancelled_row():
    from test_f061_subtask_executor import _make_heart_mock, _make_settings, _make_subtask, _scripted_runner

    from nous.handlers.subtask_executor import execute_hardened

    halt = _install()
    runner = _scripted_runner(
        scripted_payloads=[GOOD_REPORT], scripted_usages=[{"input_tokens": 300, "output_tokens": 120, "tool_calls": 4}]
    )
    inner = runner.run_turn.side_effect

    async def halt_then_return(**kwargs):
        await halt.halt(actor="t", surface="dashboard")
        return await inner(**kwargs)

    runner.run_turn.side_effect = halt_then_return
    heart = _make_heart_mock()
    heart.subtasks.cancel = AsyncMock(return_value=True)
    heart.subtasks.record_cancelled_usage = AsyncMock(return_value=True)
    subtask = _make_subtask()
    await execute_hardened(subtask, "subtask-1", runner=runner, heart=heart, settings=_make_settings())
    heart.subtasks.record_cancelled_usage.assert_awaited_once_with(
        subtask.id, attempts=1, tokens_in=300, tokens_out=120, tool_calls_made=4
    )


@pytest.mark.postgres_only  # the usage write uses GREATEST
async def test_a_halted_dag_node_run_keeps_its_tokens_on_its_row_and_in_its_dags_total(db):
    from test_fix_586_cancelled_subtask_usage import REPORT, _dag_with_running_node

    from nous import cancel_provenance

    store, manager, orch, dag_id, row = await _dag_with_running_node(db)
    halt = _install()

    class _Runner:
        async def run_turn(self, **kwargs):
            await halt.halt(actor="t", surface="dashboard")  # lands while the model call is out
            _schema, submit = kwargs["extra_tools"]["submit_final_report"]
            await submit(**REPORT)
            return "Report submitted.", None, {"input_tokens": 300, "output_tokens": 120, "tool_calls": 4}

        async def end_conversation(self, session_id, **_):
            return True

        def root_cancelled(self, _root):
            return False

    from test_fix_586_cancelled_subtask_usage import _settings as _586_settings

    pool = SubtaskWorkerPool(runner=_Runner(), heart=SimpleNamespace(subtasks=manager), settings=_586_settings())
    with background_halt.epoch_scope(halt.epoch):
        await pool._process_subtask(row)
    stored = await manager.get(row.id)
    assert stored.status == "cancelled" and stored.result is None
    assert (stored.tokens_in, stored.tokens_out, stored.tool_calls_made) == (300, 120, 4)
    await orch.cancel_dag(dag_id, "halted by the owner", cancel_provenance.halt_record())  # the sweep's DAG step
    await orch.tick()
    await orch.wait_for_delivery()
    await orch.tick()
    assert (await store.get_dag(dag_id)).tokens_consumed == 420


@pytest.mark.postgres_only  # the usage write uses GREATEST
async def test_a_halted_legacy_run_records_its_usage_on_the_cancelled_row(env_factory):  # noqa: F811
    env = await env_factory(subtask_hardening_enabled=False)
    subtask = await _make(env, notify=False)
    halt = _install()
    runner = MagicMock()
    runner.end_conversation = AsyncMock()
    runner.root_cancelled = MagicMock(return_value=False)

    async def run_turn(**kwargs):
        await halt.halt(actor="t", surface="dashboard")
        return "late", MagicMock(), {"input_tokens": 70, "output_tokens": 30, "tool_calls": 2}

    runner.run_turn = AsyncMock(side_effect=run_turn)
    pool = SubtaskWorkerPool(runner, env.heart, env.settings, http_client=env.http)
    claimed = await env.heart.subtasks.dequeue("worker-0")
    with background_halt.epoch_scope(halt.epoch):
        await pool._process_subtask(claimed)
    row = await _row(env, subtask.id)
    assert row.status == "cancelled" and (row.tokens_in, row.tokens_out, row.tool_calls_made, row.attempts) == (
        70,
        30,
        2,
        1,
    )
