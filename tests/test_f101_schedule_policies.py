"""F101: schedule concurrency and catch-up policies (real Postgres)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select, update

from nous.config import Settings
from nous.handlers.task_scheduler import ActiveRun, TaskScheduler, find_active_run, run_schedule_now
from nous.heart.heart import Heart
from nous.storage.models import ExecutionDAG, Schedule, Subtask

pytestmark = pytest.mark.postgres_only


class _Bus:
    """Records emitted events (the scheduler's only use of the bus)."""

    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, event) -> None:
        self.events.append(event)


def _settings(agent_id: str | None = None, **kw) -> Settings:
    kw.setdefault("schedule_check_interval", 1)
    return Settings(agent_id=agent_id or f"test-f101-{uuid.uuid4().hex[:8]}", **kw)


@pytest.fixture
def settings():
    return _settings()


@pytest_asyncio.fixture
async def heart(db, settings):
    h = Heart(db, settings)
    yield h
    await h.close()


async def _due_schedule(heart: Heart, *, missed_slots: int = 1, interval: int = 3600, **kw) -> Schedule:
    """A recurring schedule whose next_fire_at lies ``missed_slots`` slots in the past (the oldest missed slot)."""
    schedule = await heart.schedules.create(
        task="daily job", schedule_type="recurring", interval_seconds=interval, **kw
    )
    due = datetime.now(UTC) - timedelta(seconds=interval * (missed_slots - 1) + 60)
    async with heart.db.session() as s:
        await s.execute(update(Schedule).where(Schedule.id == schedule.id).values(next_fire_at=due))
        await s.commit()
    return await heart.schedules.get(schedule.id)


async def _runs(heart: Heart, schedule: Schedule) -> list[Subtask]:
    async with heart.db.session() as s:
        rows = await s.execute(
            select(Subtask)
            .where(Subtask.agent_id == heart.agent_id)
            .where(Subtask.metadata_["schedule_id"].astext == schedule.id.hex)
            .order_by(Subtask.created_at)
        )
        return list(rows.scalars().all())


async def _active_subtask(
    heart: Heart, schedule: Schedule, *, status: str = "running", started_ago: int = 10
) -> Subtask:
    sub = await heart.subtasks.create(task="previous run", timeout=600, metadata={"schedule_id": schedule.id.hex})
    if status == "running":
        async with heart.db.session() as s:
            await s.execute(
                update(Subtask)
                .where(Subtask.id == sub.id)
                .values(status="running", started_at=datetime.now(UTC) - timedelta(seconds=started_ago))
            )
            await s.commit()
    return sub


async def _dag(heart: Heart, schedule: Schedule, *, status: str = "running", age: int = 60) -> ExecutionDAG:
    async with heart.db.session() as s:
        dag = ExecutionDAG(
            agent_id=heart.agent_id,
            name="daily dag",
            status=status,
            schedule_id=schedule.id,
            created_at=datetime.now(UTC) - timedelta(seconds=age),
        )
        s.add(dag)
        await s.commit()
        await s.refresh(dag)
        return dag


def _suppressed(bus: _Bus) -> list:
    return [e for e in bus.events if e.type == "schedule_fire_suppressed"]


# ---------------------------------------------------------------------------
# Defaults and storage
# ---------------------------------------------------------------------------


async def test_a_new_schedule_gets_the_safe_defaults(heart):
    schedule = await heart.schedules.create(task="t", schedule_type="recurring", interval_seconds=60)
    assert schedule.concurrency_policy == "coalesce_if_active"
    assert schedule.catch_up_policy == "skip_missed"
    assert schedule.catch_up_cap is None


async def test_create_refuses_an_unknown_policy(heart):
    with pytest.raises(ValueError, match="concurrency_policy"):
        await heart.schedules.create(
            task="t", schedule_type="recurring", interval_seconds=60, concurrency_policy="nope"
        )
    with pytest.raises(ValueError, match="catch_up_policy"):
        await heart.schedules.create(task="t", schedule_type="recurring", interval_seconds=60, catch_up_policy="nope")
    with pytest.raises(ValueError, match="catch_up_cap"):
        await heart.schedules.create(task="t", schedule_type="recurring", interval_seconds=60, catch_up_cap=0)


# ---------------------------------------------------------------------------
# Concurrency policies
# ---------------------------------------------------------------------------


async def test_coalesce_spawns_nothing_records_the_fire_and_advances(heart, settings):
    schedule = await _due_schedule(heart)
    active = await _active_subtask(heart, schedule)
    bus = _Bus()

    fired = await TaskScheduler(heart, settings, bus=bus)._fire_due_tasks()

    assert fired == 0
    assert [r.id for r in await _runs(heart, schedule)] == [active.id]
    after = await heart.schedules.get(schedule.id)
    assert after.next_fire_at > datetime.now(UTC)  # the slot is consumed, not retried
    assert after.fire_count == 0
    [event] = _suppressed(bus)
    assert event.data["outcome"] == "coalesced"
    assert event.data["schedule_id"] == schedule.id.hex
    assert event.data["active_run"] == {"kind": "subtask", "id": str(active.id)}


async def test_skip_if_active_records_skipped(heart, settings):
    schedule = await _due_schedule(heart, concurrency_policy="skip_if_active")
    active = await _active_subtask(heart, schedule, status="pending")
    bus = _Bus()

    assert await TaskScheduler(heart, settings, bus=bus)._fire_due_tasks() == 0

    assert len(await _runs(heart, schedule)) == 1
    [event] = _suppressed(bus)
    assert event.data["outcome"] == "skipped"
    assert event.data["active_run"]["id"] == str(active.id)


async def test_always_enqueue_spawns_beside_the_active_run(heart, settings):
    schedule = await _due_schedule(heart, concurrency_policy="always_enqueue")
    await _active_subtask(heart, schedule)
    bus = _Bus()

    assert await TaskScheduler(heart, settings, bus=bus)._fire_due_tasks() == 1

    assert len(await _runs(heart, schedule)) == 2
    assert (await heart.schedules.get(schedule.id)).fire_count == 1
    assert _suppressed(bus) == []


async def test_a_live_dag_the_run_created_is_the_active_run(heart, settings):
    schedule = await _due_schedule(heart)
    dag = await _dag(heart, schedule)
    bus = _Bus()

    assert await TaskScheduler(heart, settings, bus=bus)._fire_due_tasks() == 0

    assert await _runs(heart, schedule) == []
    [event] = _suppressed(bus)
    assert event.data["active_run"] == {"kind": "dag", "id": str(dag.id)}


async def test_a_finished_dag_is_not_active(heart, settings):
    schedule = await _due_schedule(heart)
    await _dag(heart, schedule, status="completed")

    assert await TaskScheduler(heart, settings)._fire_due_tasks() == 1


async def test_another_schedules_run_does_not_count(heart, settings):
    schedule = await _due_schedule(heart)
    other = await heart.schedules.create(task="other", schedule_type="recurring", interval_seconds=3600)
    await _active_subtask(heart, other)
    await _dag(heart, other)

    assert await find_active_run(heart, settings, schedule.id, datetime.now(UTC)) is None


# ---------------------------------------------------------------------------
# Stale runs never block a schedule
# ---------------------------------------------------------------------------


async def test_a_running_subtask_past_its_timeout_is_not_active(heart, settings):
    schedule = await _due_schedule(heart)
    await _active_subtask(heart, schedule, started_ago=600 + 301)  # timeout 600 + 300s grace

    assert await find_active_run(heart, settings, schedule.id, datetime.now(UTC)) is None
    assert await TaskScheduler(heart, settings)._fire_due_tasks() == 1


async def test_a_running_subtask_within_its_timeout_is_active(heart, settings):
    schedule = await _due_schedule(heart)
    sub = await _active_subtask(heart, schedule, started_ago=600 + 200)

    assert await find_active_run(heart, settings, schedule.id, datetime.now(UTC)) == ActiveRun("subtask", sub.id)


async def test_a_dag_older_than_the_max_age_is_not_active(heart):
    settings = _settings(heart.agent_id, schedule_active_run_max_age_seconds=3600)
    schedule = await _due_schedule(heart)
    await _dag(heart, schedule, age=3601)

    assert await find_active_run(heart, settings, schedule.id, datetime.now(UTC)) is None


# ---------------------------------------------------------------------------
# Catch-up after downtime
# ---------------------------------------------------------------------------


async def test_skip_missed_fires_once_for_many_missed_slots(heart, settings):
    schedule = await _due_schedule(heart, missed_slots=6)

    assert await TaskScheduler(heart, settings)._fire_due_tasks() == 1

    assert len(await _runs(heart, schedule)) == 1
    after = await heart.schedules.get(schedule.id)
    assert after.fire_count == 1
    assert datetime.now(UTC) < after.next_fire_at <= datetime.now(UTC) + timedelta(seconds=3600)


async def test_enqueue_missed_with_cap_starts_one_run_per_missed_slot_up_to_the_cap(heart, settings):
    schedule = await _due_schedule(
        heart,
        missed_slots=6,
        concurrency_policy="always_enqueue",
        catch_up_policy="enqueue_missed_with_cap",
        catch_up_cap=3,
    )

    assert await TaskScheduler(heart, settings)._fire_due_tasks() == 3

    assert len(await _runs(heart, schedule)) == 3
    after = await heart.schedules.get(schedule.id)
    assert after.fire_count == 3
    assert after.next_fire_at > datetime.now(UTC)


async def test_enqueue_missed_with_fewer_missed_slots_than_the_cap(heart, settings):
    schedule = await _due_schedule(
        heart,
        missed_slots=2,
        concurrency_policy="always_enqueue",
        catch_up_policy="enqueue_missed_with_cap",
        catch_up_cap=5,
    )

    assert await TaskScheduler(heart, settings)._fire_due_tasks() == 2
    assert len(await _runs(heart, schedule)) == 2


async def test_enqueue_missed_without_a_cap_uses_the_setting(heart):
    settings = _settings(heart.agent_id, schedule_catch_up_cap=2)
    schedule = await _due_schedule(
        heart, missed_slots=5, concurrency_policy="always_enqueue", catch_up_policy="enqueue_missed_with_cap"
    )

    assert await TaskScheduler(heart, settings)._fire_due_tasks() == 2
    assert len(await _runs(heart, schedule)) == 2


async def test_enqueue_missed_under_coalesce_folds_the_later_slots_into_the_first_run(heart, settings):
    schedule = await _due_schedule(heart, missed_slots=3, catch_up_policy="enqueue_missed_with_cap", catch_up_cap=3)
    bus = _Bus()

    assert await TaskScheduler(heart, settings, bus=bus)._fire_due_tasks() == 1

    [run] = await _runs(heart, schedule)
    events = _suppressed(bus)
    assert [e.data["outcome"] for e in events] == ["coalesced", "coalesced"]
    assert all(e.data["active_run"] == {"kind": "subtask", "id": str(run.id)} for e in events)


async def test_catch_up_counts_cron_slots(heart, settings):
    schedule = await heart.schedules.create(
        task="hourly",
        schedule_type="recurring",
        cron_expr="0 * * * *",
        concurrency_policy="always_enqueue",
        catch_up_policy="enqueue_missed_with_cap",
        catch_up_cap=10,
    )
    top_of_hour = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    async with heart.db.session() as s:
        await s.execute(
            update(Schedule).where(Schedule.id == schedule.id).values(next_fire_at=top_of_hour - timedelta(hours=3))
        )
        await s.commit()

    # Slots at -3h, -2h, -1h and the top of this hour.
    assert await TaskScheduler(heart, settings)._fire_due_tasks() == 4


async def test_catch_up_stops_at_max_fires(heart, settings):
    schedule = await _due_schedule(
        heart,
        missed_slots=5,
        concurrency_policy="always_enqueue",
        catch_up_policy="enqueue_missed_with_cap",
        catch_up_cap=5,
        max_fires=2,
    )

    assert await TaskScheduler(heart, settings)._fire_due_tasks() == 2
    after = await heart.schedules.get(schedule.id)
    assert after.active is False
    assert after.fire_count == 2


async def test_a_full_queue_on_the_first_run_leaves_the_schedule_unadvanced(heart, settings):
    schedule = await _due_schedule(heart)
    before = schedule.next_fire_at
    for i in range(5):
        await heart.subtasks.create(task=f"filler {i}")

    assert await TaskScheduler(heart, settings)._fire_due_tasks() == 0
    assert (await heart.schedules.get(schedule.id)).next_fire_at == before


# ---------------------------------------------------------------------------
# Flag off: today's behaviour
# ---------------------------------------------------------------------------


async def test_flag_off_defers_without_advancing_and_records_nothing(heart):
    settings = _settings(heart.agent_id, schedule_policies_enabled=False)
    schedule = await _due_schedule(heart, concurrency_policy="always_enqueue")
    await _active_subtask(heart, schedule)
    bus = _Bus()

    assert await TaskScheduler(heart, settings, bus=bus)._fire_due_tasks() == 0

    assert len(await _runs(heart, schedule)) == 1
    assert (await heart.schedules.get(schedule.id)).next_fire_at == schedule.next_fire_at
    assert bus.events == []


async def test_flag_off_ignores_enqueue_missed(heart):
    settings = _settings(heart.agent_id, schedule_policies_enabled=False)
    schedule = await _due_schedule(
        heart, missed_slots=4, concurrency_policy="always_enqueue", catch_up_policy="enqueue_missed_with_cap"
    )

    assert await TaskScheduler(heart, settings)._fire_due_tasks() == 1
    assert len(await _runs(heart, schedule)) == 1


# ---------------------------------------------------------------------------
# Manual run
# ---------------------------------------------------------------------------


async def test_manual_run_starts_a_run_without_moving_the_schedule(heart, settings):
    schedule = await heart.schedules.create(task="t", schedule_type="recurring", interval_seconds=3600)

    result = await run_schedule_now(heart, settings, schedule.id)

    assert result.outcome == "started"
    [run] = await _runs(heart, schedule)
    assert result.run_id == run.id
    after = await heart.schedules.get(schedule.id)
    assert after.next_fire_at == schedule.next_fire_at
    assert after.fire_count == 0


async def test_manual_run_obeys_the_concurrency_policy(heart, settings):
    schedule = await heart.schedules.create(task="t", schedule_type="recurring", interval_seconds=3600)
    active = await _active_subtask(heart, schedule)
    bus = _Bus()

    result = await run_schedule_now(heart, settings, schedule.id, bus=bus)

    assert result.outcome == "coalesced"
    assert result.run_id == active.id
    assert len(await _runs(heart, schedule)) == 1
    [event] = _suppressed(bus)
    assert event.data["trigger"] == "manual"


async def test_manual_run_with_always_enqueue_starts_beside_the_active_run(heart, settings):
    schedule = await heart.schedules.create(
        task="t", schedule_type="recurring", interval_seconds=3600, concurrency_policy="always_enqueue"
    )
    await _active_subtask(heart, schedule)

    assert (await run_schedule_now(heart, settings, schedule.id)).outcome == "started"
    assert len(await _runs(heart, schedule)) == 2


async def test_manual_run_of_an_inactive_or_unknown_schedule_is_refused(heart, settings):
    schedule = await heart.schedules.create(task="t", schedule_type="recurring", interval_seconds=3600)
    await heart.schedules.deactivate(schedule.id)
    with pytest.raises(LookupError):
        await run_schedule_now(heart, settings, schedule.id)
    with pytest.raises(LookupError):
        await run_schedule_now(heart, settings, uuid.uuid4())


# ---------------------------------------------------------------------------
# F064.5 continuation under the policies
# ---------------------------------------------------------------------------


async def test_catch_up_runs_carry_the_continuation_cycle_forward(heart):
    settings = _settings(heart.agent_id, schedule_continuation_enabled=True)
    schedule = await _due_schedule(
        heart,
        missed_slots=3,
        concurrency_policy="always_enqueue",
        catch_up_policy="enqueue_missed_with_cap",
        continuation_turns=2,
    )

    assert await TaskScheduler(heart, settings)._fire_due_tasks() == 3

    sessions = [r.metadata_["session_id"] for r in await _runs(heart, schedule)]
    prefix = f"schedule-{schedule.id.hex[:8]}"
    # Run 1 starts a cycle, run 2 reuses it and ends it (2 turns), run 3 starts the next.
    assert sessions == [f"{prefix}-1", f"{prefix}-1", f"{prefix}-3"]
    after = await heart.schedules.get(schedule.id)
    assert after.continuation_session_id == f"{prefix}-3"
    assert after.continuation_count == 1


# ---------------------------------------------------------------------------
# The DAG a scheduled run creates is linked to its schedule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model_sent", [False, True])
async def test_the_dispatcher_tells_dag_create_which_schedule_ran_the_turn(model_sent):
    from nous.api.execution_context import ExecutionContext
    from nous.api.tools import ToolDispatcher

    seen: dict = {}

    async def handler(**kwargs):
        seen.update(kwargs)
        return {"content": [{"type": "text", "text": "ok"}]}

    d = ToolDispatcher()
    d.register("dag_create", handler, {"name": "dag_create", "input_schema": {"type": "object", "properties": {}}})
    schedule_hex = uuid.uuid4().hex
    forged = {"_schedule_id": uuid.uuid4().hex} if model_sent else {}

    scheduled = ExecutionContext(kind="scheduled", session_id="subtask-1", schedule_id=schedule_hex)
    await d.dispatch("dag_create", {"name": "d", **forged}, session_id="subtask-1", context=scheduled)
    assert seen["_schedule_id"] == schedule_hex

    seen.clear()
    plain = ExecutionContext(kind="subtask", session_id="subtask-2")
    await d.dispatch("dag_create", {"name": "d", **forged}, session_id="subtask-2", context=plain)
    assert "_schedule_id" not in seen


async def test_the_dag_store_records_the_schedule(db, heart):
    from nous.dag.schemas import DAGCreateRequest, DAGNodeSpec, DAGNodeType
    from nous.dag.store import DAGStore

    schedule = await heart.schedules.create(task="t", schedule_type="recurring", interval_seconds=3600)
    store = DAGStore(db, heart.agent_id, Settings(agent_id=heart.agent_id))
    dag = await store.create(
        DAGCreateRequest(
            name="daily",
            nodes=[DAGNodeSpec(name="n", type=DAGNodeType.callback, instructions="x")],
            schedule_id=schedule.id,
        )
    )

    assert (await store.get_dag(dag.id)).schedule_id == schedule.id


# ---------------------------------------------------------------------------
# REST: POST /schedules/{id}/run
# ---------------------------------------------------------------------------


def _client(heart, settings, bus=None):
    from types import SimpleNamespace

    from httpx import ASGITransport, AsyncClient

    from nous.api.rest import create_app

    app = create_app(SimpleNamespace(), SimpleNamespace(), heart, SimpleNamespace(), heart.db, settings, bus=bus)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_rest_run_starts_then_coalesces(heart, settings):
    schedule = await heart.schedules.create(task="t", schedule_type="recurring", interval_seconds=3600)
    async with _client(heart, settings) as client:
        first = await client.post(f"/schedules/{schedule.id}/run")
        second = await client.post(f"/schedules/{schedule.id}/run")

    assert first.status_code == 202, first.text
    [run] = await _runs(heart, schedule)
    assert first.json() == {"status": "started", "run_id": str(run.id), "schedule_id": str(schedule.id)}
    assert second.status_code == 200, second.text
    assert second.json() == {"status": "coalesced", "run_id": str(run.id), "schedule_id": str(schedule.id)}


async def test_rest_run_refusals(heart, settings):
    schedule = await heart.schedules.create(task="t", schedule_type="recurring", interval_seconds=3600)
    async with _client(heart, settings) as client:
        assert (await client.post("/schedules/not-a-uuid/run")).status_code == 400
        assert (await client.post(f"/schedules/{uuid.uuid4()}/run")).status_code == 404
    async with _client(heart, _settings(heart.agent_id, schedule_policies_enabled=False)) as client:
        off = await client.post(f"/schedules/{schedule.id}/run")
    assert off.status_code == 409
    assert await _runs(heart, schedule) == []


# ---------------------------------------------------------------------------
# Tools: schedule_task / list_tasks
# ---------------------------------------------------------------------------


def _tools(heart, settings):
    from nous.api.tools import create_subtask_tools

    return create_subtask_tools(heart, settings, runner=None)


def _text(result: dict) -> str:
    return result["content"][0]["text"]


async def test_schedule_task_stores_the_policies(heart, settings):
    result = await _tools(heart, settings)["schedule_task"](
        task="nightly dag",
        every="1 hour",
        concurrency_policy="skip_if_active",
        catch_up_policy="enqueue_missed_with_cap",
        catch_up_cap=2,
    )
    assert "is_error" not in result, result
    [schedule] = await heart.schedules.list()
    assert (schedule.concurrency_policy, schedule.catch_up_policy, schedule.catch_up_cap) == (
        "skip_if_active",
        "enqueue_missed_with_cap",
        2,
    )


async def test_schedule_task_defaults_the_policies(heart, settings):
    await _tools(heart, settings)["schedule_task"](task="t", every="1 hour")
    [schedule] = await heart.schedules.list()
    assert (schedule.concurrency_policy, schedule.catch_up_policy, schedule.catch_up_cap) == (
        "coalesce_if_active",
        "skip_missed",
        None,
    )


@pytest.mark.parametrize(
    "bad",
    [{"concurrency_policy": "queue"}, {"catch_up_policy": "all"}, {"catch_up_cap": 26}, {"catch_up_cap": 0}],
)
async def test_schedule_task_refuses_a_bad_policy(heart, settings, bad):
    result = await _tools(heart, settings)["schedule_task"](task="t", every="1 hour", **bad)
    assert result.get("is_error") is True
    assert next(iter(bad)) in _text(result)
    assert await heart.schedules.list() == []


async def test_list_tasks_shows_the_policies(heart, settings):
    await heart.schedules.create(
        task="nightly dag",
        schedule_type="recurring",
        interval_seconds=3600,
        concurrency_policy="always_enqueue",
        catch_up_policy="enqueue_missed_with_cap",
        catch_up_cap=4,
    )
    await heart.schedules.create(task="plain", schedule_type="recurring", interval_seconds=3600)

    text_out = _text(await _tools(heart, settings)["list_tasks"]())

    assert "concurrency: always_enqueue, catch-up: enqueue_missed_with_cap (cap 4)" in text_out
    assert "concurrency: coalesce_if_active, catch-up: skip_missed" in text_out


# ---------------------------------------------------------------------------
# Review follow-ups: the fire lock, partial catch-up, late checks, scope
# ---------------------------------------------------------------------------


async def test_a_held_fire_lock_stops_both_the_check_and_a_manual_run(heart, settings):
    from sqlalchemy import text

    schedule = await _due_schedule(heart)
    async with heart.db.session() as holder:
        assert await holder.scalar(
            text("SELECT pg_try_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"schedule_fire:{schedule.id.hex}"}
        )
        assert await TaskScheduler(heart, settings)._fire_due_tasks() == 0
        from nous.handlers.task_scheduler import ScheduleBusy

        with pytest.raises(ScheduleBusy):
            await run_schedule_now(heart, settings, schedule.id)
        async with _client(heart, settings) as client:
            assert (await client.post(f"/schedules/{schedule.id}/run")).status_code == 409
    assert await _runs(heart, schedule) == []
    assert (await heart.schedules.get(schedule.id)).next_fire_at == schedule.next_fire_at
    # Released with the holder's transaction: the next check fires.
    assert await TaskScheduler(heart, settings)._fire_due_tasks() == 1


async def test_a_catch_up_cut_short_counts_its_runs_and_advances(heart, settings):
    for i in range(3):  # the pending limit is 5: room for two runs
        await heart.subtasks.create(task=f"filler {i}")
    schedule = await _due_schedule(
        heart,
        missed_slots=3,
        concurrency_policy="always_enqueue",
        catch_up_policy="enqueue_missed_with_cap",
        catch_up_cap=3,
    )

    assert await TaskScheduler(heart, settings)._fire_due_tasks() == 2

    after = await heart.schedules.get(schedule.id)
    assert after.fire_count == 2
    assert after.next_fire_at > datetime.now(UTC)


async def test_a_late_check_is_not_a_missed_slot(heart):
    settings = _settings(heart.agent_id, schedule_check_interval=300)
    schedule = await heart.schedules.create(
        task="t",
        schedule_type="recurring",
        interval_seconds=60,
        concurrency_policy="always_enqueue",
        catch_up_policy="enqueue_missed_with_cap",
    )
    async with heart.db.session() as s:
        await s.execute(
            update(Schedule)
            .where(Schedule.id == schedule.id)
            .values(next_fire_at=datetime.now(UTC) - timedelta(seconds=90))
        )
        await s.commit()

    assert await TaskScheduler(heart, settings)._fire_due_tasks() == 1


async def test_a_pending_subtask_older_than_the_max_age_is_not_active(heart):
    settings = _settings(heart.agent_id, schedule_active_run_max_age_seconds=3600)
    schedule = await _due_schedule(heart)
    sub = await _active_subtask(heart, schedule, status="pending")
    async with heart.db.session() as s:
        await s.execute(
            update(Subtask).where(Subtask.id == sub.id).values(created_at=datetime.now(UTC) - timedelta(seconds=3601))
        )
        await s.commit()

    assert await find_active_run(heart, settings, schedule.id, datetime.now(UTC)) is None


async def test_a_running_subtask_with_no_timeout_uses_the_default(heart):
    settings = _settings(heart.agent_id, subtask_default_timeout=600)
    schedule = await _due_schedule(heart)
    sub = await _active_subtask(heart, schedule, started_ago=600 + 200)
    async with heart.db.session() as s:
        await s.execute(update(Subtask).where(Subtask.id == sub.id).values(timeout_seconds=0))
        await s.commit()

    assert await find_active_run(heart, settings, schedule.id, datetime.now(UTC)) == ActiveRun("subtask", sub.id)


async def test_manual_run_of_another_agents_schedule_is_refused(heart, settings):
    schedule = await heart.schedules.create(task="t", schedule_type="recurring", interval_seconds=3600)

    with pytest.raises(LookupError):
        await run_schedule_now(heart, _settings(), schedule.id)
    assert await _runs(heart, schedule) == []


async def test_a_once_schedule_keeps_the_legacy_path_with_the_flag_on(heart, settings):
    schedule = await heart.schedules.create(
        task="t", schedule_type="once", fire_at=datetime.now(UTC) - timedelta(minutes=1)
    )
    await _active_subtask(heart, schedule)
    bus = _Bus()

    assert await TaskScheduler(heart, settings, bus=bus)._fire_due_tasks() == 0

    after = await heart.schedules.get(schedule.id)
    assert after.active is True  # deferred, not consumed
    assert after.next_fire_at == schedule.next_fire_at
    assert bus.events == []


async def test_manual_run_of_a_once_schedule_is_refused(heart, settings):
    schedule = await heart.schedules.create(
        task="t", schedule_type="once", fire_at=datetime.now(UTC) + timedelta(hours=1)
    )

    with pytest.raises(LookupError):
        await run_schedule_now(heart, settings, schedule.id)
    async with _client(heart, settings) as client:
        assert (await client.post(f"/schedules/{schedule.id}/run")).status_code == 404
    assert await _runs(heart, schedule) == []


async def test_rest_run_with_a_full_queue_is_a_conflict(heart, settings):
    schedule = await heart.schedules.create(task="t", schedule_type="recurring", interval_seconds=3600)
    for i in range(5):
        await heart.subtasks.create(task=f"filler {i}")
    async with _client(heart, settings) as client:
        assert (await client.post(f"/schedules/{schedule.id}/run")).status_code == 409
