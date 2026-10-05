"""F099 section 4.2: spawns that no model turn made record their intention too.

A scheduler fire is a new root under its schedule's container; both
work-queue DAG sites record the item; companion app.act records the tapped
action (tests/test_a2ui_agent_actions.py). A schedule that stops firing closes
its container.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select, update

from nous.api.execution_context import ExecutionContext
from nous.api.tools import ToolDispatcher, register_subtask_tools
from nous.brain import intentions
from nous.brain.intentions import IntentionSpec
from nous.config import Settings
from nous.handlers.task_scheduler import TaskScheduler
from nous.storage.models import Intention, Schedule, Subtask

ON = {"result_inbox_enabled": True, "intentions_enabled": True}
INTERACTIVE = ExecutionContext(kind="interactive", session_id="S-sched", channel="telegram:42")


@pytest.fixture
async def sched_env(db, mock_embeddings):
    from nous.heart import Heart

    hearts = []

    async def build(**over):
        agent = f"f099-cp-{uuid.uuid4().hex[:8]}"
        settings = Settings(_env_file=None, agent_id=agent, telegram_bot_token="", telegram_chat_id="", **over)
        heart = Heart(db, settings, embedding_provider=mock_embeddings)
        hearts.append(heart)
        d = ToolDispatcher()
        register_subtask_tools(d, heart, settings)
        return SimpleNamespace(agent=agent, settings=settings, heart=heart, d=d, db=db)

    yield build
    for heart in hearts:
        await heart.close()


async def _schedule(env, args) -> Schedule:
    text, is_error = await env.d.dispatch("schedule_task", args, session_id=INTERACTIVE.session_id, context=INTERACTIVE)
    assert not is_error, text
    async with env.db.session() as s:
        return (await s.execute(select(Schedule).where(Schedule.agent_id == env.agent))).scalar_one()


async def _fire(env, schedule_id) -> Subtask:
    async with env.db.session() as s:
        await s.execute(
            update(Schedule)
            .where(Schedule.id == schedule_id)
            .values(next_fire_at=datetime.now(UTC) - timedelta(minutes=1))
        )
        await s.commit()
    assert await TaskScheduler(env.heart, env.settings)._fire_due_tasks() == 1
    async with env.db.session() as s:
        return (await s.execute(select(Subtask).where(Subtask.agent_id == env.agent))).scalar_one()


async def _of(env, kind, source_id) -> Intention:
    return await env.heart.intentions.get_for_source(kind, source_id)


@pytest.mark.parametrize(("notify", "policy"), [(True, "remember"), (False, "none")])
async def test_a_fire_is_a_new_root_under_its_schedules_container(sched_env, notify, policy):
    env = await sched_env(**ON)
    schedule = await _schedule(
        env,
        {"task": "Check the snow report", "every": "30 minutes", "notify": notify, "intent": "Keep the user posted"},
    )
    container = await _of(env, "schedule", schedule.id)
    assert (container.wake_policy, container.intent, container.origin_session_id) == (
        "container",
        "Keep the user posted",
        "S-sched",
    )
    assert schedule.created_by_session is None  # I5
    row = await _fire(env, schedule.id)
    fire = await _of(env, "subtask", row.id)
    assert (fire.root_id, fire.parent_id, fire.depth) == (fire.id, container.id, 0)
    assert (fire.wake_policy, fire.origin_kind, fire.intent) == (policy, "scheduler", "Check the snow report")
    assert row.metadata_["intention"] == {"id": str(fire.id), "root_id": str(fire.id), "authority": "owner"}
    assert row.metadata_["schedule_id"] == schedule.id.hex
    assert (row.parent_session_id, row.parent_channel) == (None, None)  # I5


async def test_a_one_shot_schedule_closes_its_container_when_it_fires(sched_env):
    env = await sched_env(**ON)
    schedule = await _schedule(
        env, {"task": "Remind the user", "when": "in 2 hours", "intent": "Remind the user of the dentist"}
    )
    await _fire(env, schedule.id)
    container = await _of(env, "schedule", schedule.id)
    assert (container.state, container.close_reason, container.result_at) == ("closed", "legacy", None)


async def test_reaching_max_fires_closes_the_container(sched_env):
    env = await sched_env(**ON)
    spec = IntentionSpec(intent="Check once more", origin_kind="interactive", container=True)
    schedule = await env.heart.schedules.create(
        task="Check", schedule_type="recurring", interval_seconds=1800, max_fires=1, intention=spec
    )
    await _fire(env, schedule.id)
    assert (await _of(env, "schedule", schedule.id)).state == "closed"


async def test_a_recurring_schedule_keeps_its_container_open(sched_env):
    env = await sched_env(**ON)
    schedule = await _schedule(env, {"task": "t", "every": "30 minutes", "intent": "x"})
    await _fire(env, schedule.id)
    assert (await _of(env, "schedule", schedule.id)).state == "pending"


async def test_cancel_task_on_a_schedule_closes_its_container(sched_env):
    env = await sched_env(**ON)
    schedule = await _schedule(env, {"task": "t", "every": "30 minutes", "intent": "x"})
    text, is_error = await env.d.dispatch(
        "cancel_task", {"task_id": str(schedule.id)}, session_id="S-sched", context=INTERACTIVE
    )
    assert not is_error, text
    assert (await _of(env, "schedule", schedule.id)).state == "closed"


@pytest.mark.parametrize("stop", ["cancel_task", "deactivated_before_its_container_closed"])
async def test_a_schedule_stopped_after_the_tick_loaded_it_fires_nothing(sched_env, monkeypatch, stop):
    """Codex: the tick loads a due schedule, the owner stops it, then the tick
    creates the fire. The fire's transaction reads the container and its
    schedule FOR SHARE and sees the stop, so no subtask and no intention
    escape it. The second case is D4's window: the deactivation committed and
    the container is still pending, so only the schedule check catches it."""
    env = await sched_env(**ON)
    schedule = await _schedule(env, {"task": "t", "every": "30 minutes", "intent": "x"})
    async with env.db.session() as s:
        await s.execute(
            update(Schedule)
            .where(Schedule.id == schedule.id)
            .values(next_fire_at=datetime.now(UTC) - timedelta(minutes=1))
        )
        await s.commit()
    real_get_due = env.heart.schedules.get_due

    async def load_then_stop(now):
        due = await real_get_due(now)  # the tick holds these rows, unlocked
        if stop == "cancel_task":
            text, is_error = await env.d.dispatch(
                "cancel_task", {"task_id": str(schedule.id)}, session_id="S-sched", context=INTERACTIVE
            )
            assert not is_error, text
        else:
            async with env.db.session() as s:
                await s.execute(update(Schedule).where(Schedule.id == schedule.id).values(active=False))
                await s.commit()
        return due

    monkeypatch.setattr(env.heart.schedules, "get_due", load_then_stop)
    assert await TaskScheduler(env.heart, env.settings)._fire_due_tasks() == 0
    async with env.db.session() as s:
        subtasks = (await s.execute(select(Subtask).where(Subtask.agent_id == env.agent))).scalars().all()
        rows = (await s.execute(select(Intention).where(Intention.agent_id == env.agent))).scalars().all()
    assert subtasks == []
    assert [r.source_kind for r in rows] == ["schedule"]  # the container alone: no fire intention


async def test_a_failed_container_close_never_keeps_a_schedule_active(sched_env, monkeypatch):
    env = await sched_env(**ON)
    schedule = await _schedule(env, {"task": "t", "when": "in 2 hours", "intent": "x"})

    async def failing_sql(session, *args, **kwargs):
        # A real database error inside the close's own session: a close placed
        # INSIDE the deactivate transaction would roll the deactivation back.
        from sqlalchemy import text

        await session.execute(text("SELECT 1/0"))

    monkeypatch.setattr(intentions, "close_for_source", failing_sql)
    await _fire(env, schedule.id)
    assert (await env.heart.schedules.get(schedule.id)).active is False


async def test_a_schedule_from_before_the_flag_fires_without_a_parent(sched_env):
    env = await sched_env(**ON)
    schedule = await env.heart.schedules.create(task="Old monitor", schedule_type="recurring", interval_seconds=1800)
    row = await _fire(env, schedule.id)
    fire = await _of(env, "subtask", row.id)
    assert (fire.root_id, fire.parent_id, fire.wake_policy) == (fire.id, None, "none")


async def test_with_the_flag_off_a_fire_records_nothing(sched_env):
    env = await sched_env(result_inbox_enabled=True)
    schedule = await _schedule(env, {"task": "t", "every": "30 minutes"})
    row = await _fire(env, schedule.id)
    assert "intention" not in row.metadata_
    async with env.db.session() as s:
        assert (await s.execute(select(Intention).where(Intention.agent_id == env.agent))).scalars().all() == []


# ---------------------------------------------------------------------------
# Work queue: both DAG creation sites
# ---------------------------------------------------------------------------


def _wq(db, agent, path, **over):
    from nous.dag.orchestrator import DAGOrchestrator
    from nous.dag.store import DAGStore
    from nous.heart.work_queue import WorkQueueItemManager
    from nous.heartbeat.work_queue import FileJsonlAdapter, WorkQueueCheck

    settings = Settings(_env_file=None, agent_id=agent, work_queue_enabled=True, work_queue_source="file_jsonl", **over)
    store = DAGStore(db, agent, settings)
    orch = DAGOrchestrator(store=store, subtask_mgr=AsyncMock(), dynamic_loader=MagicMock(), settings=settings)
    items = WorkQueueItemManager(db, agent)
    check = WorkQueueCheck(
        adapter=FileJsonlAdapter(str(path)), items_mgr=items, dag_store=store, orchestrator=orch, settings=settings
    )
    return check, items


async def _dag_intentions(db, agent) -> list[Intention]:
    async with db.session() as s:
        return list(
            (await s.execute(select(Intention).where(Intention.agent_id == agent, Intention.source_kind == "dag")))
            .scalars()
            .all()
        )


async def test_both_work_queue_sites_record_the_item(tmp_path, db):
    from nous.heartbeat.work_queue import WorkItem

    path = tmp_path / "queue.jsonl"
    path.write_text(json.dumps({"external_id": "a1", "title": "Fix the flaky login test", "body": "do it"}))
    agent = f"f099-wq-{uuid.uuid4().hex[:8]}"
    check, items = _wq(db, agent, path, **ON)
    await check.run()
    row = await items.claim_for_dispatch(source="file_jsonl", external_id="o1", payload={"title": "Renew the TLS cert"})
    orphan = WorkItem(
        external_id="o1", title="Renew the TLS cert", body="do it", state="open", terminal=False, payload={}
    )
    assert await check._reconcile_orphan(row.id, orphan, []) is True
    found = await _dag_intentions(db, agent)
    assert {i.intent for i in found} == {"Fix the flaky login test", "Renew the TLS cert"}
    assert {(i.wake_policy, i.origin_kind, i.root_id == i.id) for i in found} == {("remember", "work_queue", True)}


async def test_work_queue_with_the_flag_off_records_nothing(tmp_path, db):
    path = tmp_path / "queue.jsonl"
    path.write_text(json.dumps({"external_id": "a1", "title": "t", "body": "b"}))
    agent = f"f099-wq-{uuid.uuid4().hex[:8]}"
    check, _ = _wq(db, agent, path, result_inbox_enabled=True)
    await check.run()
    assert await _dag_intentions(db, agent) == []


async def test_channel_of_session_finds_the_channel_whose_latest_session_it_is(db):
    from nous.heart.result_inbox import ResultInboxStore

    store = ResultInboxStore(db, f"f099-ch-{uuid.uuid4().hex[:8]}")
    await store.touch_channel("telegram:7", "chat-7")
    assert await store.channel_of_session("chat-7") == "telegram:7"
    assert await store.channel_of_session("chat-other") is None


# ---------------------------------------------------------------------------
# REST POST /schedules: a spawn path too (I1)
# ---------------------------------------------------------------------------


def _rest_client(env):
    """The REST app over the env's real Heart. The schedule routes use only
    heart and settings; the other create_app arguments are inert stand-ins
    (copy tests/test_rest.py's fixtures if create_app ever needs more)."""
    from httpx import ASGITransport, AsyncClient

    from nous.api.rest import create_app

    app = create_app(MagicMock(), MagicMock(), env.heart, MagicMock(), env.db, env.settings)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _post_schedule(env, body: dict):
    async with _rest_client(env) as client:
        return await client.post("/schedules", json=body)


async def test_post_schedules_records_a_container_and_its_fires_join_it(sched_env):
    env = await sched_env(**ON)
    resp = await _post_schedule(env, {"task": "Check the snow report\nand the wind", "every": "30 minutes"})
    assert resp.status_code == 200, resp.text
    schedule_id = uuid.UUID(resp.json()["id"])
    container = await _of(env, "schedule", schedule_id)
    assert (container.wake_policy, container.origin_kind, container.authority) == ("container", "rest", "owner")
    assert container.intent == "rest: Check the snow report"
    row = await _fire(env, schedule_id)
    fire = await _of(env, "subtask", row.id)
    assert (fire.parent_id, fire.root_id) == (container.id, fire.id)
    assert fire.wake_policy == "remember"  # the route's notify defaults to true


async def test_post_schedules_writes_the_schedule_and_its_container_in_one_transaction(sched_env, monkeypatch):
    env = await sched_env(**ON)

    async def faulty(*args, **kwargs):
        raise RuntimeError("injected fault")

    monkeypatch.setattr(intentions, "insert_prepared", faulty)
    resp = await _post_schedule(env, {"task": "t", "every": "30 minutes"})
    assert resp.status_code == 500
    async with env.db.session() as s:
        assert (await s.execute(select(Schedule).where(Schedule.agent_id == env.agent))).scalars().all() == []
        assert (await s.execute(select(Intention).where(Intention.agent_id == env.agent))).scalars().all() == []


async def test_post_schedules_with_the_flag_off_records_nothing(sched_env):
    env = await sched_env(result_inbox_enabled=True)
    resp = await _post_schedule(env, {"task": "t", "every": "30 minutes"})
    assert resp.status_code == 200, resp.text
    async with env.db.session() as s:
        assert len((await s.execute(select(Schedule).where(Schedule.agent_id == env.agent))).scalars().all()) == 1
        assert (await s.execute(select(Intention).where(Intention.agent_id == env.agent))).scalars().all() == []
