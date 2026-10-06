"""F099 Phase 1: brain.intentions — schema, the flag, and nous/brain/intentions.py."""

from __future__ import annotations

import logging
import uuid

import pytest
from sqlalchemy import text

from nous.config import Settings

INTENTION_COLUMNS = {
    "id", "agent_id", "root_id", "parent_id", "depth", "source_kind", "source_id", "intent",
    "origin_kind", "origin_session_id", "origin_channel", "origin_decision_id", "wake_policy",
    "authority", "expected_result", "assumptions", "deadline", "state", "close_reason",
    "root_cancelled_at", "root_expired_at", "claimed_at", "claim_token", "attempts",
    "created_at", "result_at", "closed_at", "updated_at",
}  # fmt: skip


def _agent() -> str:
    return f"f099-int-{uuid.uuid4().hex[:8]}"


def test_the_flag_defaults_off():
    assert Settings(_env_file=None).intentions_enabled is False


def test_the_flag_needs_the_result_inbox(caplog):
    with caplog.at_level(logging.WARNING, logger="nous.config"):
        s = Settings(_env_file=None, intentions_enabled=True, result_inbox_enabled=False)
    assert s.intentions_enabled is False
    assert "NOUS_RESULT_INBOX_ENABLED" in caplog.text


def test_the_flag_stays_on_with_the_inbox():
    assert Settings(_env_file=None, intentions_enabled=True, result_inbox_enabled=True).intentions_enabled is True


@pytest.mark.postgres_only
async def test_the_migration_creates_the_table_and_the_inbox_column(db):
    async with db.engine.connect() as conn:
        cols = {
            r[0]
            for r in (
                await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'brain' AND table_name = 'intentions'"
                    )
                )
            ).all()
        }
        inbox = (
            await conn.execute(
                text(
                    "SELECT 1 FROM information_schema.columns WHERE table_schema = 'heart' "
                    "AND table_name = 'result_inbox' AND column_name = 'intention_id'"
                )
            )
        ).first()
    assert cols == INTENTION_COLUMNS
    assert inbox is not None


# ---------------------------------------------------------------------------
# Task 1.2: nous/brain/intentions.py
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402
from datetime import UTC, datetime  # noqa: E402

from sqlalchemy import select, update  # noqa: E402

from nous.brain import intentions  # noqa: E402
from nous.brain.intentions import IntentionSpec, ParentView  # noqa: E402
from nous.storage.models import Intention  # noqa: E402

OWNER_PARENT = ParentView(id=uuid.uuid4(), root_id=uuid.uuid4(), depth=0, authority="owner", wake_policy="remember")
NONE_PARENT = ParentView(id=uuid.uuid4(), root_id=uuid.uuid4(), depth=0, authority="owner", wake_policy="none")
REPORT_PARENT = ParentView(id=uuid.uuid4(), root_id=uuid.uuid4(), depth=0, authority="owner", wake_policy="report")
CONTINUE_PARENT = ParentView(id=uuid.uuid4(), root_id=uuid.uuid4(), depth=0, authority="owner", wake_policy="continue")
INTERNAL_PARENT = ParentView(
    id=uuid.uuid4(), root_id=uuid.uuid4(), depth=1, authority="internal_only", wake_policy="continue"
)


def _spec(origin_kind: str = "interactive", **over) -> IntentionSpec:
    return IntentionSpec(intent="Check the snow report", origin_kind=origin_kind, **over)


@pytest.mark.parametrize(
    ("spec", "parent", "expected"),
    [
        (_spec("interactive"), None, "continue"),
        (_spec("mcp"), None, "continue"),
        (_spec("heartbeat_check"), None, "continue"),
        (_spec("heartbeat_callback"), None, "continue"),
        (_spec("interactive", container=True), None, "container"),
        (_spec("scheduler", wake_policy="remember"), None, "remember"),
        (_spec("scheduler"), None, "none"),
        (_spec("work_queue"), None, "remember"),
        (_spec("app_act"), None, "none"),
        (_spec("rest", container=True), None, "container"),  # REST POST /schedules
        (_spec("dag_summary"), None, "none"),
        (_spec("interactive", inline=True), None, "none"),
        (_spec("subtask"), OWNER_PARENT, "remember"),
        (_spec("scheduled"), NONE_PARENT, "none"),
        (_spec("dag_node"), CONTINUE_PARENT, "continue"),
        (_spec("subtask"), None, "none"),
        (_spec("agent_action"), None, "none"),
        (_spec("heartbeat_triage"), None, "none"),
        (_spec("background"), None, "none"),
        (_spec("interactive", wake_policy="report"), None, "report"),
        (_spec("subtask", wake_policy="continue"), None, "none"),  # D7: a background turn may not widen
        (_spec("scheduled", wake_policy="continue"), NONE_PARENT, "none"),  # D7: nor under a none intention
        (_spec("dag_summary", wake_policy="continue"), None, "none"),  # D7
        (_spec("scheduler", wake_policy="continue"), None, "none"),  # D7: nor a code path
        (_spec("subtask", wake_policy="continue"), OWNER_PARENT, "remember"),  # D7: remember -> continue blocked too
        (_spec("subtask", wake_policy="continue"), REPORT_PARENT, "report"),  # D7: report -> continue blocked too
        (_spec("work_queue", wake_policy="continue"), None, "remember"),  # D7: a code path keeps its default
        (_spec("heartbeat_check", wake_policy="continue"), None, "continue"),  # its default is continue already
        (_spec("subtask", wake_policy="report"), None, "report"),
        (_spec("subtask", wake_policy="none"), INTERNAL_PARENT, "continue"),
        (_spec("subtask", inline=True), INTERNAL_PARENT, "none"),
        (
            _spec("subtask", wake_policy="report", container=True),
            INTERNAL_PARENT,
            "container",
        ),  # D3: whatever the parent
    ],
)
def test_default_wake_policy_for_every_origin(spec, parent, expected):
    """Spec section 4.1, "Default wake policy, by origin", row by row."""
    assert intentions.resolve_wake_policy(spec, parent) == expected


def test_a_blocked_continue_request_is_logged(caplog):
    """D7: the fallback is visible, so the rollout can count blocked requests."""
    with caplog.at_level(logging.INFO, logger="nous.brain.intentions"):
        assert intentions.resolve_wake_policy(_spec("scheduled", wake_policy="continue"), OWNER_PARENT) == "remember"
    assert "wake_policy=continue" in caplog.text and "(D7)" in caplog.text


def test_intent_line_is_one_capped_line():
    assert intentions.intent_line("  Check\n the   snow ") == "Check the snow"
    assert len(intentions.intent_line("x" * 900)) == intentions.INTENT_MAX_CHARS
    assert intentions.intent_line(None) == ""


@pytest.mark.parametrize("kind", ["interactive", "mcp", "continuation"])
@pytest.mark.parametrize("intent", [None, "", "   \n "])
def test_a_foreground_tool_call_without_an_intent_is_refused(intent, kind, caplog):
    with caplog.at_level(logging.INFO, logger="nous.brain.intentions"):
        with pytest.raises(intentions.IntentArgumentError) as exc:
            intentions.spec_from_tool_call(intent=intent, wake_policy=None, origin_kind=kind, fallback_text="t")
    assert str(exc.value) == intentions.INTENT_REQUIRED_ERROR
    assert intentions.INTENT_HELP in str(exc.value)
    assert intentions.intent_refused(intent, kind)
    assert "intent is required" in caplog.text  # the rollout grep (Review Focus 1)


@pytest.mark.parametrize("kind", ["dag_summary", "scheduled", "subtask", "heartbeat_callback", "dag_node", None])
def test_a_background_tool_call_without_an_intent_gets_one_generated(kind):
    """I2: background prompts predate intent; refusing them (the F087 summary
    turn's email subtask) would silently break delivery."""
    spec = intentions.spec_from_tool_call(
        intent="  ", wake_policy=None, origin_kind=kind, fallback_text="\n  Email the user the result  \nsecond line"
    )
    assert spec.intent == f"{kind or 'background'}: Email the user the result"
    assert not intentions.intent_refused("  ", kind)


def test_a_written_intent_wins_over_the_generated_one():
    spec = intentions.spec_from_tool_call(intent="Why", wake_policy=None, origin_kind="dag_summary", fallback_text="t")
    assert spec.intent == "Why"


def test_generated_intent_is_the_first_line_capped():
    assert intentions.generated_intent("rest", "\n  Check the snow\nand the wind") == "rest: Check the snow"
    assert len(intentions.generated_intent("rest", "x" * 900)) == intentions.INTENT_MAX_CHARS


def test_a_tool_call_with_an_unknown_wake_policy_is_refused():
    with pytest.raises(intentions.IntentArgumentError):
        intentions.spec_from_tool_call(intent="x", wake_policy="always", origin_kind="interactive")


def test_an_empty_wake_policy_means_the_default():
    spec = intentions.spec_from_tool_call(intent="x", wake_policy="", origin_kind="interactive")
    assert spec.wake_policy is None


def test_an_unreadable_parent_id_is_refused_not_made_a_root():
    with pytest.raises(intentions.IntentionParentMissing):
        intentions.spec_from_tool_call(intent="x", wake_policy=None, origin_kind="subtask", intention_id="not-a-uuid")


def test_terminal_dag_statuses_match_the_dag_store():
    from nous.dag.store import TERMINAL_DAG_STATUSES

    assert set(intentions.TERMINAL_DAG_STATUSES) == set(TERMINAL_DAG_STATUSES)


async def _root(
    db,
    agent,
    *,
    authority="owner",
    wake_policy="continue",
    cancelled=False,
    expired=False,
    source_kind="subtask",
    source_id=None,
) -> Intention:
    rid = uuid.uuid4()
    now = datetime.now(UTC)
    row = Intention(
        id=rid,
        agent_id=agent,
        root_id=rid,
        depth=0,
        source_kind=source_kind,
        source_id=str(source_id or uuid.uuid4()),
        intent="root",
        origin_kind="interactive",
        wake_policy=wake_policy,
        authority=authority,
        state="pending",
        root_cancelled_at=now if cancelled else None,
        root_expired_at=now if expired else None,
    )
    async with db.session() as s:
        s.add(row)
        await s.commit()
    return row


async def _record(db, agent, spec, *, source_kind="subtask", source_id=None) -> Intention:
    async with db.session() as s:
        prepared = await intentions.prepare_intention(s, agent, spec)
        await intentions.insert_prepared(
            s, agent, prepared, source_kind=source_kind, source_id=source_id or uuid.uuid4()
        )
        await s.commit()
    async with db.session() as s:
        return await s.get(Intention, prepared.id)


async def test_a_root_is_its_own_root(db):
    agent = _agent()
    row = await _record(db, agent, _spec("interactive", origin_session_id="S1", origin_channel="telegram:1"))
    assert (row.root_id, row.parent_id, row.depth) == (row.id, None, 0)
    assert (row.authority, row.wake_policy, row.state) == ("owner", "continue", "pending")
    assert (row.origin_kind, row.origin_session_id, row.origin_channel) == ("interactive", "S1", "telegram:1")
    assert row.deadline is None  # D2: Phase 1 writes no deadline


async def test_a_child_joins_its_parents_lineage(db):
    agent = _agent()
    root = await _root(db, agent, wake_policy="remember")
    row = await _record(db, agent, _spec("subtask", parent_id=root.id))
    assert (row.root_id, row.parent_id, row.depth) == (root.id, root.id, 1)
    assert (row.authority, row.wake_policy) == ("owner", "remember")


async def test_authority_only_narrows_down_a_lineage(db):
    agent = _agent()
    root = await _root(db, agent, authority="internal_only")
    row = await _record(db, agent, _spec("subtask", parent_id=root.id, wake_policy="none"))
    assert (row.authority, row.wake_policy) == ("internal_only", "continue")


async def test_an_internal_only_turn_with_no_parent_records_internal_only():  # PIN
    """F099 Phase 2: the turn's authority narrows a root too, not only a child.

    With no parent_id and no parent_source the session is never touched, so None stands in for it."""
    spec = _spec("subtask", origin_authority="internal_only")
    prepared = await intentions.prepare_intention(None, _agent(), spec)
    assert (prepared.parent_id, prepared.authority) == (None, "internal_only")


def test_an_internal_only_turn_with_no_parent_wakes_the_continuation():  # PIN
    """C9 with no parent. origin_kind "subtask" because its own default is none, so only the
    authority clause can make it continue."""
    spec = _spec("subtask", origin_authority="internal_only")
    assert intentions.resolve_wake_policy(spec, None) == "continue"


async def _live_schedule(db, agent):
    from nous.heart.schedules import ScheduleManager

    return await ScheduleManager(db, agent).create(task="t", schedule_type="recurring", interval_seconds=1800)


async def test_a_schedule_fire_is_a_new_root_under_its_container(db):
    agent = _agent()
    schedule = await _live_schedule(db, agent)
    container = await _root(db, agent, wake_policy="container", source_kind="schedule", source_id=schedule.id)
    row = await _record(
        db, agent, _spec("scheduler", wake_policy="remember", parent_source=("schedule", str(schedule.id)))
    )
    assert (row.root_id, row.parent_id, row.depth) == (row.id, container.id, 0)
    assert row.wake_policy == "remember"


async def test_a_fire_of_a_schedule_with_no_container_has_no_parent(db):
    row = await _record(db, _agent(), _spec("scheduler", parent_source=("schedule", str(uuid.uuid4()))))
    assert (row.root_id, row.parent_id) == (row.id, None)


@pytest.mark.parametrize("stop", ["closed", "cancelled", "inactive", "deleted"])
async def test_a_fire_whose_container_or_schedule_stopped_is_refused(db, stop):
    """Codex: a tick that loaded a due schedule before the owner stopped it
    must not commit the fire as a fresh root outside the cancel. Each case
    fails alone: the container check (closed, cancelled) and the schedule
    check (inactive, deleted) are separate conditions."""
    from nous.storage.models import Schedule

    agent = _agent()
    schedule = await _live_schedule(db, agent)
    source_id = uuid.uuid4() if stop == "deleted" else schedule.id
    container = await _root(
        db, agent, wake_policy="container", source_kind="schedule", source_id=source_id, cancelled=stop == "cancelled"
    )
    async with db.session() as s:
        if stop == "closed":
            await s.execute(
                update(Intention).where(Intention.id == container.id).values(state="closed", close_reason="legacy")
            )
        if stop == "inactive":
            await s.execute(update(Schedule).where(Schedule.id == schedule.id).values(active=False))
        await s.commit()
    with pytest.raises(intentions.IntentionRootClosed):
        await _record(db, agent, _spec("scheduler", parent_source=("schedule", str(source_id))))


@pytest.mark.postgres_only
@pytest.mark.parametrize("held", ["container", "schedule"])
async def test_a_stop_in_flight_on_the_container_or_its_schedule_stops_a_fire(db, held):
    """The fire reads its container and its schedule FOR SHARE. A cancel
    (which writes the container) or a deactivation (which writes the
    schedule) that holds its row wins, and the fire sees it after the commit."""
    from nous.storage.models import Schedule

    agent = _agent()
    schedule = await _live_schedule(db, agent)
    container = await _root(db, agent, wake_policy="container", source_kind="schedule", source_id=schedule.id)
    model, row_id = (Intention, container.id) if held == "container" else (Schedule, schedule.id)
    async with db.session() as stopper:
        await stopper.execute(select(model).where(model.id == row_id).with_for_update())
        fire = asyncio.create_task(_record(db, agent, _spec("scheduler", parent_source=("schedule", str(schedule.id)))))
        await asyncio.sleep(0.3)
        assert not fire.done(), f"the fire must wait for the {held} row"
        if held == "container":
            await stopper.execute(
                update(Intention).where(Intention.id == container.id).values(root_cancelled_at=datetime.now(UTC))
            )
        else:
            await stopper.execute(update(Schedule).where(Schedule.id == schedule.id).values(active=False))
        await stopper.commit()
    with pytest.raises(intentions.IntentionRootClosed):
        await asyncio.wait_for(fire, timeout=10)


@pytest.mark.parametrize("closed", ["cancelled", "expired"])
async def test_a_child_of_a_closed_root_is_refused(db, closed):
    agent = _agent()
    root = await _root(db, agent, **{closed: True})
    with pytest.raises(intentions.IntentionRootClosed):
        await _record(db, agent, _spec("subtask", parent_id=root.id))


async def test_a_child_of_a_missing_parent_is_refused(db):
    with pytest.raises(intentions.IntentionParentMissing):
        await _record(db, _agent(), _spec("subtask", parent_id=uuid.uuid4()))


async def test_another_agents_parent_counts_as_missing(db):
    root = await _root(db, _agent())
    with pytest.raises(intentions.IntentionParentMissing):
        await _record(db, _agent(), _spec("subtask", parent_id=root.id))


@pytest.mark.postgres_only
async def test_a_cancel_holding_the_root_row_stops_a_spawn(db):
    """I1: the child insert reads the root FOR SHARE, so a cancel that locks
    the root and writes root_cancelled_at wins, and the spawn sees it."""
    agent = _agent()
    root = await _root(db, agent)
    async with db.session() as canceller:
        await canceller.execute(select(Intention).where(Intention.id == root.id).with_for_update())
        spawn = asyncio.create_task(_record(db, agent, _spec("subtask", parent_id=root.id)))
        await asyncio.sleep(0.3)
        assert not spawn.done(), "the child insert must wait for the root row"
        await canceller.execute(
            update(Intention).where(Intention.id == root.id).values(root_cancelled_at=datetime.now(UTC))
        )
        await canceller.commit()
    with pytest.raises(intentions.IntentionRootClosed):
        await asyncio.wait_for(spawn, timeout=10)


async def test_close_for_source_closes_once_and_always_returns_the_id(db):
    agent = _agent()
    source = uuid.uuid4()
    row = await _record(db, agent, _spec(), source_kind="dag", source_id=source)
    async with db.session() as s:
        first = await intentions.close_for_source(s, agent, "dag", source)
        await s.commit()
    async with db.session() as s:
        closed = await s.get(Intention, row.id)
        second = await intentions.close_for_source(s, agent, "dag", source, reason="delivered")
        await s.commit()
    async with db.session() as s:
        again = await s.get(Intention, row.id)
        missing = await intentions.close_for_source(s, agent, "dag", uuid.uuid4())
    assert first == second == row.id and missing is None
    assert (closed.state, closed.close_reason) == ("closed", "legacy")
    assert closed.result_at is not None and closed.closed_at is not None
    assert again.close_reason == "legacy"  # a closed row is not re-closed


async def test_a_container_closes_without_a_result(db):
    agent = _agent()
    schedule_id = uuid.uuid4()
    await _record(db, agent, _spec(container=True), source_kind="schedule", source_id=schedule_id)
    async with db.session() as s:
        found = await intentions.close_for_source(s, agent, "schedule", schedule_id, with_result=False)
        await s.commit()
    async with db.session() as s:
        row = await s.get(Intention, found)
    assert row.state == "closed" and row.result_at is None


async def test_lineage_for_source_is_the_stamp(db):
    agent = _agent()
    dag_id = uuid.uuid4()
    row = await _record(db, agent, _spec(), source_kind="dag", source_id=dag_id)
    async with db.session() as s:
        stamp = await intentions.lineage_for_source(s, agent, "dag", dag_id)
        none = await intentions.lineage_for_source(s, agent, "dag", uuid.uuid4())
    assert stamp == {"id": str(row.id), "root_id": str(row.root_id), "authority": "owner"}
    assert none is None


@pytest.mark.postgres_only  # CAST(text AS uuid) join
async def test_close_finished_sources_closes_finished_work_only(db):
    from nous.dag.schemas import DAGCreateRequest, DAGNodeSpec, DAGNodeType
    from nous.dag.store import DAGStore
    from nous.heart.subtasks import SubtaskManager

    agent = _agent()
    subtasks = SubtaskManager(db, agent)
    done = await subtasks.create(task="done")
    await subtasks.complete(done.id, "ok")
    failed = await subtasks.create(task="failed")
    await subtasks.fail(failed.id, "boom")
    cancelled = await subtasks.create(task="cancelled")
    await subtasks.cancel(cancelled.id)
    running = await subtasks.create(task="running")
    dags = DAGStore(db, agent, Settings(_env_file=None))
    dag = await dags.create(
        DAGCreateRequest(name="d", nodes=[DAGNodeSpec(name="n", type=DAGNodeType.subtask, instructions="x")])
    )
    await dags.update_dag_status(dag.id, "completed")
    rows = {
        "done": await _record(db, agent, _spec(), source_id=done.id),
        "failed": await _record(db, agent, _spec(), source_id=failed.id),
        "cancelled": await _record(db, agent, _spec(), source_id=cancelled.id),
        "running": await _record(db, agent, _spec(), source_id=running.id),
        "dag": await _record(db, agent, _spec(), source_kind="dag", source_id=dag.id),
        "container": await _record(db, agent, _spec(container=True), source_kind="schedule", source_id=uuid.uuid4()),
    }
    async with db.session() as s:
        closed = await intentions.close_finished_sources(s, agent, limit=10)
        await s.commit()
    assert set(closed) == {rows[k].id for k in ("done", "failed", "cancelled", "dag")}


@pytest.mark.postgres_only  # CAST(text AS uuid) join
async def test_close_finished_containers_closes_only_schedules_that_stopped(db):
    """D4's repair: a container whose schedule is inactive or gone closes as
    legacy, with no result; one whose schedule still fires stays open."""
    from nous.heart.schedules import ScheduleManager
    from nous.storage.models import Schedule

    agent = _agent()
    schedules = ScheduleManager(db, agent)
    live = await schedules.create(task="live", schedule_type="recurring", interval_seconds=1800)
    stopped = await schedules.create(task="stopped", schedule_type="recurring", interval_seconds=1800)
    async with db.session() as s:
        await s.execute(update(Schedule).where(Schedule.id == stopped.id).values(active=False))
        await s.commit()
    rows = {
        "live": await _record(db, agent, _spec(container=True), source_kind="schedule", source_id=live.id),
        "stopped": await _record(db, agent, _spec(container=True), source_kind="schedule", source_id=stopped.id),
        "deleted": await _record(db, agent, _spec(container=True), source_kind="schedule", source_id=uuid.uuid4()),
        "subtask": await _record(db, agent, _spec(), source_id=uuid.uuid4()),
    }
    async with db.session() as s:
        closed = await intentions.close_finished_containers(s, agent, limit=10)
        await s.commit()
    assert set(closed) == {rows["stopped"].id, rows["deleted"].id}
    async with db.session() as s:
        row = await s.get(Intention, rows["stopped"].id)
    assert (row.state, row.close_reason, row.result_at) == ("closed", "legacy", None)


async def test_heart_has_an_intention_store(db, mock_embeddings):
    from nous.heart import Heart

    heart = Heart(db, Settings(_env_file=None), embedding_provider=mock_embeddings)
    try:
        assert isinstance(heart.intentions, intentions.IntentionStore)
    finally:
        await heart.close()


# ---------------------------------------------------------------------------
# Task 1.3: every store writes its row and the intention atomically (I1)
# ---------------------------------------------------------------------------

from nous.dag.schemas import DAGCreateRequest, DAGNodeSpec, DAGNodeType  # noqa: E402
from nous.dag.store import DAGStore  # noqa: E402
from nous.heart.schedules import ScheduleManager  # noqa: E402
from nous.heart.subtasks import SubtaskManager  # noqa: E402
from nous.storage.models import ExecutionDAG, Schedule, Subtask  # noqa: E402

_WORK = {"subtask": Subtask, "schedule": Schedule, "dag": ExecutionDAG}
STORES = pytest.mark.parametrize("store", ["subtask", "schedule", "dag"])


async def _create(store: str, db, agent, spec):
    if store == "subtask":
        return await SubtaskManager(db, agent).create(task="t", **intentions.intention_kwargs(spec))
    if store == "schedule":
        return await ScheduleManager(db, agent).create(
            task="t", schedule_type="recurring", interval_seconds=1800, **intentions.intention_kwargs(spec)
        )
    request = DAGCreateRequest(name="d", nodes=[DAGNodeSpec(name="n", type=DAGNodeType.subtask, instructions="x")])
    return await DAGStore(db, agent, Settings(_env_file=None)).create(request, **intentions.intention_kwargs(spec))


def _store_spec(store: str, **over) -> IntentionSpec:
    return _spec(container=(store == "schedule"), **over)


async def _count(db, model, agent) -> int:
    async with db.session() as s:
        return len((await s.execute(select(model).where(model.agent_id == agent))).scalars().all())


@STORES
async def test_a_store_writes_its_row_and_one_intention(db, store):
    agent = _agent()
    row = await _create(store, db, agent, _store_spec(store))
    found = await intentions.IntentionStore(db, agent).get_for_source(store, row.id)
    assert found is not None and found.intent == "Check the snow report"
    assert await _count(db, Intention, agent) == 1


async def test_a_subtask_row_carries_its_lineage_stamp(db):
    agent = _agent()
    row = await SubtaskManager(db, agent).create(task="t", metadata={"k": "v"}, intention=_spec())
    found = await intentions.IntentionStore(db, agent).get_for_source("subtask", row.id)
    assert row.metadata_ == {
        "k": "v",
        "intention": {"id": str(found.id), "root_id": str(found.root_id), "authority": "owner"},
    }


@STORES
async def test_no_spec_writes_no_intention(db, store):
    agent = _agent()
    await _create(store, db, agent, None)
    assert await _count(db, Intention, agent) == 0


@STORES
@pytest.mark.parametrize("order", ["after_the_work_row", "after_the_intention_row"])
async def test_a_fault_after_either_insert_leaves_neither_row(db, monkeypatch, store, order):
    real = intentions.insert_prepared

    async def faulty(session, *args, **kwargs):
        # Both rows must be in THIS session's transaction: a store that wrote
        # either one through a second session would leave it behind.
        assert await session.get(_WORK[store], kwargs["source_id"]) is not None
        if order == "after_the_intention_row":
            await real(session, *args, **kwargs)
            prepared = args[1]
            assert await session.get(Intention, prepared.id) is not None
        raise RuntimeError("injected fault")

    monkeypatch.setattr(intentions, "insert_prepared", faulty)
    agent = _agent()
    with pytest.raises(RuntimeError, match="injected fault"):
        await _create(store, db, agent, _store_spec(store))
    assert await _count(db, _WORK[store], agent) == 0
    assert await _count(db, Intention, agent) == 0


@STORES
async def test_a_spawn_under_a_closed_root_leaves_no_row(db, store):
    agent = _agent()
    root = await _root(db, agent, cancelled=True)
    with pytest.raises(intentions.IntentionRootClosed):
        await _create(store, db, agent, _store_spec(store, origin_kind="subtask", parent_id=root.id))
    assert await _count(db, _WORK[store], agent) == 0
    assert await _count(db, Intention, agent) == 1  # the root alone


def test_enabled_reads_only_a_real_true():
    from unittest.mock import MagicMock

    assert intentions.enabled(Settings(_env_file=None, result_inbox_enabled=True, intentions_enabled=True))
    assert not intentions.enabled(Settings(_env_file=None))
    assert not intentions.enabled(MagicMock())  # a mocked Settings is not "on"


def test_an_unreadable_lineage_is_refused_with_a_message_the_model_can_act_on():
    with pytest.raises(intentions.IntentionParentMissing) as err:
        intentions.spec_from_tool_call(
            intent="x", wake_policy=None, origin_kind="subtask", intention_id=intentions.UNREADABLE_LINEAGE
        )
    assert "lineage could not be read" in str(err.value)
    assert "unreadable-lineage" not in str(err.value)
