"""F114 goals: the goal driver (PR 1b-i): it starts a session, closes finished ones and claims the goal channel.

Nothing builds the driver outside these tests yet: each test constructs it on an environment of its own agent.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from f099_support import (
    CHAN,
    CONT,
    env_factory,  # noqa: F401
)
from sqlalchemy import func, select, text, update

from nous.brain import intentions
from nous.goals import charter
from nous.goals.driver import GoalDriver
from nous.goals.store import GoalStore
from nous.storage.models import Goal, GoalJournal, GoalSession, Subtask

pytestmark = pytest.mark.postgres_only


class FakeRunner:
    """The continuation runner as the start path uses it: ``wake()``."""

    def __init__(self) -> None:
        self.wakes = 0

    def wake(self) -> None:
        self.wakes += 1


async def _world(env_factory, **settings) -> SimpleNamespace:  # noqa: F811
    env = await env_factory(**{**CONT, **settings})
    store = GoalStore(env.db, env.agent, env.settings)
    runner = FakeRunner()
    driver = GoalDriver(env.db, env.settings, env.heart, store, runner)
    return SimpleNamespace(env=env, db=env.db, agent=env.agent, store=store, runner=runner, driver=driver)


async def _active(w: SimpleNamespace, **over) -> Goal:
    values = {
        "title": "Tidy the notes",
        "objective": "Merge the duplicate notes in the notes folder",
        "done_when": "No two notes say the same thing",
        "proposed_by": "host",
        "origin_channel": CHAN,
        **over,
    }
    goal = await w.store.create(**values)
    return await w.store.approve(goal.id, actor="host")


async def _sessions(w: SimpleNamespace, goal: Goal | None = None) -> list[GoalSession]:
    query = select(GoalSession).where(GoalSession.agent_id == w.agent)
    if goal is not None:
        query = query.where(GoalSession.goal_id == goal.id)
    async with w.db.session() as s:
        return list((await s.execute(query.order_by(GoalSession.n))).scalars())


async def _journal(w: SimpleNamespace, goal: Goal, kind: str | None = None) -> list[GoalJournal]:
    query = select(GoalJournal).where(GoalJournal.goal_id == goal.id)
    if kind is not None:
        query = query.where(GoalJournal.kind == kind)
    async with w.db.session() as s:
        return list((await s.execute(query.order_by(GoalJournal.seq))).scalars())


async def _subtasks(w: SimpleNamespace) -> list[Subtask]:
    async with w.db.session() as s:
        return list((await s.execute(select(Subtask).where(Subtask.agent_id == w.agent))).scalars())


async def _db_now(w: SimpleNamespace):
    async with w.db.session() as s:
        return (await s.execute(select(func.clock_timestamp()))).scalar_one()


async def _pending(w: SimpleNamespace, count: int) -> list[Subtask]:
    return [await w.env.heart.subtasks.create(task=f"unrelated work {i}") for i in range(count)]


async def _until_waiters(w: SimpleNamespace, table: str, at_least: int = 1, timeout: float = 10.0) -> None:
    """Until ``at_least`` other connections wait on a lock in a statement naming ``table``."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    query = text(
        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
        "AND wait_event_type = 'Lock' AND query ILIKE :pattern"
    )
    while True:
        async with w.db.session() as s:
            if (await s.execute(query, {"pattern": f"%{table}%"})).scalar_one() >= at_least:
                return
        assert loop.time() < deadline, f"no statement waited on {table}"
        await asyncio.sleep(0.02)


def _refuse(reason: str = "no work on the archive"):
    async def check_censors(text: str, *args, **kwargs):
        return [
            SimpleNamespace(
                action="refuse",
                trigger_action=None,
                unblock_pattern=None,
                reason=reason,
                trigger_pattern="archive",
                action_instruction=None,
            )
        ]

    return check_censors


# ---- the start path (Races 1 to 3) ------------------------------------------------------------------------------


async def test_a_started_session_has_a_root_that_is_continue_and_internal_only(env_factory):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    root_id = await w.driver.start_session(goal.id, trigger="timer")
    (session,) = await _sessions(w, goal)
    root = await w.env.heart.intentions.get_for_source(intentions.SOURCE_SUBTASK, session.first_subtask_id)
    assert root.id == root_id
    assert (root.wake_policy, root.authority, root.origin_kind) == ("continue", "internal_only", "goal")
    assert root.root_id == root.id and root.origin_channel == CHAN and root.state == "pending"
    assert (session.n, session.trigger, session.root_id, session.ended_at) == (1, "timer", root_id, None)
    (subtask,) = await _subtasks(w)
    assert subtask.id == session.first_subtask_id and subtask.status == "pending"
    assert charter.FRAMING in subtask.task and "Goal G-" in subtask.task and "Tidy the notes" in subtask.task
    assert w.runner.wakes == 1
    assert w.driver._failures == {}


async def test_two_starts_at_once_make_one_session_and_one_root(env_factory):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    async with w.db.session() as holding:
        await holding.execute(select(Goal).where(Goal.id == goal.id).with_for_update())
        first = asyncio.create_task(w.driver.start_session(goal.id, trigger="timer"))
        second = asyncio.create_task(w.driver.start_session(goal.id, trigger="run_now"))
        await _until_waiters(w, "brain.goals", at_least=2)
        await holding.commit()
    results = await asyncio.gather(first, second)
    assert sorted(r is None for r in results) == [False, True]
    (session,) = await _sessions(w, goal)
    (subtask,) = await _subtasks(w)
    root = await w.env.heart.intentions.get_for_source(intentions.SOURCE_SUBTASK, subtask.id)
    assert root.origin_kind == "goal" and session.root_id == root.id
    assert [r for r in results if r is not None] == [root.id]


async def test_start_rechecks_state_under_the_goal_lock(env_factory):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    async with w.db.session() as pausing:
        await pausing.execute(select(Goal).where(Goal.id == goal.id).with_for_update())
        await pausing.execute(
            update(Goal).where(Goal.id == goal.id).values(state="paused", pause_reason="owner", next_wake_at=None)
        )
        starting = asyncio.create_task(w.driver.start_session(goal.id, trigger="timer"))
        await _until_waiters(w, "brain.goals")
        await pausing.commit()
    assert await starting is None
    assert await _sessions(w, goal) == [] and await _subtasks(w) == []
    assert w.runner.wakes == 0


async def test_a_full_queue_starts_nothing_and_leaves_no_session_row(env_factory):  # noqa: F811
    """Race 3, second guard: the headroom count is unlocked, so the queue can fill between it and the create. With
    no headroom the start reaches the create, which refuses; the session row is undone and the goal waits 60 s."""
    w = await _world(env_factory, goals_queue_headroom=0)
    goal = await _active(w)
    await _pending(w, 5)
    before = await _db_now(w)
    assert await w.driver.start_session(goal.id, trigger="timer") is None
    assert await _sessions(w, goal) == []
    after = await w.store.get(goal.id)
    assert (after.state, after.pause_reason) == ("active", None)
    assert after.next_wake_at >= before + timedelta(seconds=55)  # delayed by 60 s, from the database's clock
    assert len(await _journal(w, goal, "state")) == 2  # created and approved: nothing about the full queue
    assert w.driver._failures == {}  # a full queue is not a failed start
    assert len(await _subtasks(w)) == 5


async def test_headroom_holds_goal_sessions_back_before_the_queue_is_full(env_factory):  # noqa: F811
    w = await _world(env_factory)  # headroom 2: a session starts only while at most 3 subtasks are pending
    goal = await _active(w)
    work = await _pending(w, 4)
    held = await w.driver.tick()
    assert held.started == () and held.held == "queue"
    assert await _sessions(w, goal) == [] and len(await _subtasks(w)) == 4
    assert (await w.store.get(goal.id)).next_wake_at == goal.next_wake_at  # still due: the next tick tries again
    await w.env.heart.subtasks.complete(work[0].id, "done")
    report = await w.driver.tick()
    assert len(report.started) == 1 and report.held is None
    assert [s.root_id for s in await _sessions(w, goal)] == list(report.started)


async def test_a_halted_background_starts_nothing(env_factory, monkeypatch):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    assert w.driver.background_halted() is False  # the stub: nothing halts goal work yet
    monkeypatch.setattr(w.driver, "background_halted", lambda: True)
    report = await w.driver.tick()
    assert report.started == () and report.held == "halted"
    assert await _sessions(w, goal) == [] and await _subtasks(w) == []
    monkeypatch.undo()
    assert len((await w.driver.tick()).started) == 1


async def test_a_goal_that_is_not_due_is_not_started(env_factory):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    async with w.db.session() as s:
        await s.execute(
            update(Goal).where(Goal.id == goal.id).values(next_wake_at=func.clock_timestamp() + timedelta(minutes=5))
        )
        await s.commit()
    assert (await w.driver.tick()).started == ()
    assert await _sessions(w, goal) == []


async def test_a_censor_refusal_is_a_failed_start_and_is_journalled(env_factory, monkeypatch):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    monkeypatch.setattr(w.env.heart, "check_censors", _refuse())
    before = await _db_now(w)
    assert await w.driver.start_session(goal.id, trigger="timer") is None
    assert await _sessions(w, goal) == [] and await _subtasks(w) == []
    row = (await _journal(w, goal, "state"))[-1]
    assert row.actor == "harness" and row.session_n == 1
    assert "refused by the censor gate" in row.text and "no work on the archive" in row.text
    after = await w.store.get(goal.id)
    assert after.state == "active" and after.next_wake_at >= before + timedelta(seconds=295)
    assert w.driver._failures == {goal.id: 1}
    assert w.runner.wakes == 0


async def test_the_third_failed_start_in_a_row_pauses_the_goal_as_error(env_factory, monkeypatch):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    monkeypatch.setattr(w.env.heart, "check_censors", _refuse())
    for attempt in (1, 2):
        assert await w.driver.start_session(goal.id, trigger="timer") is None
        assert (await w.store.get(goal.id)).state == "active" and w.driver._failures == {goal.id: attempt}
    assert await w.driver.start_session(goal.id, trigger="timer") is None
    after = await w.store.get(goal.id)
    assert (after.state, after.pause_reason, after.next_wake_at) == ("paused", "error", None)
    rows = await _journal(w, goal, "state")
    assert [r.actor for r in rows[-3:]] == ["harness"] * 3
    assert "3 times in a row" in rows[-1].text and "censor gate" in rows[-1].text
    assert w.driver._failures == {}
    assert await _sessions(w, goal) == []


async def test_a_successful_start_resets_the_failure_count(env_factory, monkeypatch):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    monkeypatch.setattr(w.env.heart, "check_censors", _refuse())
    assert await w.driver.start_session(goal.id, trigger="timer") is None
    assert w.driver._failures == {goal.id: 1}
    monkeypatch.undo()
    assert await w.driver.start_session(goal.id, trigger="timer") is not None
    assert w.driver._failures == {}


async def test_any_other_failure_of_the_start_is_undone_journalled_and_counted(env_factory, monkeypatch):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    from nous.goals import roots

    async def broken(*args, **kwargs):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(roots, "create_session_root", broken)
    assert await w.driver.start_session(goal.id, trigger="timer") is None
    assert await _sessions(w, goal) == []
    row = (await _journal(w, goal, "state"))[-1]
    assert row.actor == "harness" and "could not start" in row.text and "RuntimeError" in row.text
    assert w.driver._failures == {goal.id: 1}


async def test_undo_never_deletes_a_session_whose_subtask_exists(env_factory):  # noqa: F811
    """The one DELETE: a session row whose first subtask was never created. A create that committed and then
    raised (its refresh failed) left a live root: the row stays, and the close repairs its root id."""
    w = await _world(env_factory)
    goal = await _active(w)
    subtask = await w.env.heart.subtasks.create(task="the session's first task")
    async with w.db.session() as s:
        row = GoalSession(agent_id=w.agent, goal_id=goal.id, n=1, trigger="timer", first_subtask_id=subtask.id)
        s.add(row)
        await s.commit()
    assert await w.driver._undo_start(row.id, subtask.id) is False
    assert [s.id for s in await _sessions(w, goal)] == [row.id]
    ghost = uuid.uuid4()
    async with w.db.session() as s:
        await s.execute(update(GoalSession).where(GoalSession.id == row.id).values(first_subtask_id=ghost))
        await s.commit()
    assert await w.driver._undo_start(row.id, ghost) is True
    assert await _sessions(w, goal) == []


async def test_the_driver_loop_runs_only_with_goals_on(env_factory):  # noqa: F811
    off = await _world(env_factory)
    await off.driver.start()
    assert off.driver._task is None
    await off.driver.stop()
    on = await _world(
        env_factory, goals_enabled=True, subtask_enabled=True, result_inbox_enabled=True, intentions_enabled=True
    )
    assert on.env.settings.goals_enabled is True
    await on.driver.start()
    task = on.driver._task
    assert task is not None and not task.done()
    await on.driver.stop()
    assert on.driver._task is None and task.done()
