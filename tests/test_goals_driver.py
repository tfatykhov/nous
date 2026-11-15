"""F114 goals: the goal driver (PR 1b-i): it starts a session, closes finished ones and claims the goal channel.

Nothing builds the driver outside these tests yet: each test constructs it on an environment of its own agent.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from f099_support import (
    CHAN,
    CONT,
    RESULT,
    add_arrival,
    claim,
    commit_ask,
    env_factory,  # noqa: F401
    make_child,
    record,
    runner_env,  # noqa: F401
    set_intention,
    use,
)
from sqlalchemy import func, select, text, update

from nous.brain import continuation, intentions
from nous.goals import charter
from nous.goals.driver import GoalDriver
from nous.goals.store import GoalStore
from nous.handlers.continuation_runner import ContinuationRunner
from nous.storage.models import (
    Goal,
    GoalJournal,
    GoalSession,
    Intention,
    IntentionArrival,
    ResultInbox,
    Subtask,
)

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
    """Race 3, second guard: the tick's headroom count is not locked, so the queue can fill between it and the
    create. Called directly (as after such a race), the start reaches the create, which refuses; the session row is
    undone and the goal waits 60 s."""
    w = await _world(env_factory)
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


# ---- closing a session (Races 2, 4 and 5) -----------------------------------------------------------------------


async def _started(w: SimpleNamespace, goal: Goal) -> tuple[Intention, GoalSession]:
    root_id = await w.driver.start_session(goal.id, trigger="timer")
    assert root_id is not None
    return await _started_view(w, goal, root_id)


async def _started_view(w: SimpleNamespace, goal: Goal, root_id: uuid.UUID) -> tuple[Intention, GoalSession]:
    session = next(s for s in await _sessions(w, goal) if s.root_id == root_id)
    return await w.env.heart.intentions.get_for_source(intentions.SOURCE_SUBTASK, session.first_subtask_id), session


async def _session(w: SimpleNamespace, session_id: uuid.UUID) -> GoalSession:
    async with w.db.session() as s:
        return await s.get(GoalSession, session_id)


async def _finish(w: SimpleNamespace, root: Intention, **values) -> None:
    """End the chain by hand, as a decision that closed the root would."""
    await set_intention(w.env, root.id, **{"state": "closed", "close_reason": "resolved", **values})
    await w.env.heart.subtasks.complete(uuid.UUID(root.source_id), RESULT)  # out of the pending queue


async def _arrival(w: SimpleNamespace, root: Intention, n: int, *, decision: str, note: str | None = None, **kw):
    arrival_id = await add_arrival(w.env, root.id, n, decision=decision, **kw)
    if note is not None:
        async with w.db.session() as s:
            await s.execute(update(IntentionArrival).where(IntentionArrival.id == arrival_id).values(note=note))
            await s.commit()
    return arrival_id


async def _set_goal(w: SimpleNamespace, goal: Goal, **values) -> None:
    async with w.db.session() as s:
        await s.execute(update(Goal).where(Goal.id == goal.id).values(**values))
        await s.commit()


async def test_a_session_is_closed_when_no_intention_of_its_root_is_open(env_factory):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    root, session = await _started(w, goal)
    await _arrival(w, root, 1, decision="report", note="Listed 40 notes.", tokens=(300, 40))
    await _finish(w, root)
    report = await w.driver.tick()
    assert report.closed == 1 and report.started == ()
    closed = await _session(w, session.id)
    assert closed.ended_at is not None and (closed.outcome, closed.spent, closed.steps_closed) == ("no_step", 340, 0)
    assert closed.summary == "Listed 40 notes."
    row = (await _journal(w, goal, "state"))[-1]
    assert (row.actor, row.session_n) == ("harness", 1)
    assert row.text == "Session 1 ended: no_step, 340 tokens.\nListed 40 notes."
    assert (await w.driver.tick()).closed == 0  # closed once


async def test_an_open_session_is_left_alone(env_factory):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    root, session = await _started(w, goal)
    assert (await w.driver.tick()).closed == 0  # the root is pending
    await set_intention(w.env, root.id, state="closed", close_reason="resolved")
    await make_child(w.env, root)  # a step the chain spawned is still open
    assert (await w.driver.tick()).closed == 0
    assert (await _session(w, session.id)).ended_at is None


@pytest.mark.parametrize(
    ("setup", "outcome"),
    [
        ("failed_report", "error"),
        ("failed_report_and_step", "error"),
        ("step", "step_closed"),
        ("step_and_ask", "step_closed"),
        ("ask", "asked"),
        ("ask_and_cancelled", "asked"),
        ("cancelled", "interrupted"),
        ("expired", "interrupted"),
        ("nothing", "no_step"),
    ],
)
async def test_the_outcome_is_the_first_rule_that_holds(env_factory, setup, outcome):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    root, session = await _started(w, goal)
    end = {"state": "closed", "close_reason": "resolved"}
    if "failed_report" in setup:
        end = {"state": "closed", "close_reason": "failed_report"}
    if "step" in setup:
        plan = [{"id": "s1", "text": "List the notes", "status": "closed", "closed_session": 1, "evidence": ["x"]}]
        await _set_goal(w, goal, plan=plan)
    if "ask" in setup:
        await _arrival(w, root, 1, decision="ask", note="Which folder?")
    if "cancelled" in setup:
        end = {"state": "cancelled", "close_reason": "cancelled", "root_cancelled_at": datetime.now(UTC)}
    if setup == "expired":
        end = {"state": "expired", "close_reason": "expired", "root_expired_at": datetime.now(UTC)}
    await _finish(w, root, **end)
    assert (await w.driver.tick()).closed == 1
    closed = await _session(w, session.id)
    assert closed.outcome == outcome
    assert closed.steps_closed == (1 if "step" in setup else 0)


async def test_a_root_marker_ends_the_session_even_with_work_still_open(env_factory):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    root, session = await _started(w, goal)
    await make_child(w.env, root)
    await set_intention(w.env, root.id, root_expired_at=datetime.now(UTC))
    assert (await w.driver.tick()).closed == 1
    assert (await _session(w, session.id)).outcome == "interrupted"


async def test_next_wake_is_the_interval_after_a_step_and_twice_that_after_none(env_factory):  # noqa: F811
    w = await _world(env_factory)
    stepped, idle = await _active(w), await _active(w, title="Sort the photos")
    plan = [{"id": "s1", "text": "List the notes", "status": "closed", "closed_session": 1, "evidence": ["x"]}]
    await _set_goal(w, stepped, plan=plan)
    for goal in (stepped, idle):
        root, _session_row = await _started(w, goal)
        await _finish(w, root)
    assert (await w.driver.tick()).closed == 2
    for goal, times in ((stepped, 1), (idle, 2)):
        (session,) = await _sessions(w, goal)
        after = await w.store.get(goal.id)
        assert after.next_wake_at - session.ended_at == timedelta(seconds=times * goal.min_interval_seconds)


async def test_a_session_that_ends_in_drop_leaves_its_summary_in_a_state_journal_row_the_next_task_shows(
    env_factory,  # noqa: F811
):
    w = await _world(env_factory)
    goal = await _active(w)
    root, _session_row = await _started(w, goal)
    await _arrival(w, root, 1, decision="drop", note="The notes folder is empty: nothing to merge yet.")
    await _finish(w, root)
    assert (await w.driver.tick()).closed == 1
    row = (await _journal(w, goal, "state"))[-1]
    assert "Session 1 ended: no_step" in row.text and "The notes folder is empty" in row.text
    await _set_goal(w, goal, next_wake_at=func.clock_timestamp())
    (started,) = (await w.driver.tick()).started
    _root, second = await _started_view(w, goal, started)
    subtask = await w.env.heart.subtasks.get(second.first_subtask_id)
    entry = subtask.task[subtask.task.index("## Journal") :]
    assert "The notes folder is empty: nothing to merge yet." in entry
    assert entry.index('<journal_entry kind="state" actor="harness" session="1">') < entry.index("notes folder")


async def test_a_late_result_after_the_close_does_not_start_a_second_chain(env_factory):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    root, session = await _started(w, goal)
    await _finish(w, root)
    assert (await w.driver.tick()).closed == 1
    spent_at_close = (await _session(w, session.id)).spent
    await _set_goal(w, goal, next_wake_at=func.clock_timestamp())  # due again
    await record(w.env, root)  # a late result reopens the closed root intention
    assert (await w.env.heart.intentions.get_for_source("subtask", root.source_id)).state == "result_ready"
    await add_arrival(w.env, root.id, 1, tokens=(50, 5))
    before = await _db_now(w)
    report = await w.driver.tick()
    assert report.started == () and len(await _sessions(w, goal)) == 1
    assert (await w.store.get(goal.id)).next_wake_at >= before + timedelta(seconds=55)
    assert (await _session(w, session.id)).spent == spent_at_close + 55  # re-read: the root's deadline has not passed


async def test_the_close_rechecks_open_work_inside_its_transaction(env_factory, monkeypatch):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    _root, session = await _started(w, goal)

    async def every_open_session():  # the first, unlocked read says the chain has ended
        async with w.db.session() as s:
            query = select(GoalSession.id).where(GoalSession.agent_id == w.agent, GoalSession.ended_at.is_(None))
            return list((await s.execute(query)).scalars())

    monkeypatch.setattr(w.driver, "_sessions_to_close", every_open_session)
    assert (await w.driver.tick()).closed == 0
    assert (await _session(w, session.id)).ended_at is None


async def test_a_close_waiting_behind_another_close_writes_nothing_twice(env_factory):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    root, session = await _started(w, goal)
    await _finish(w, root)
    async with w.db.session() as other:
        await other.execute(select(GoalSession).where(GoalSession.id == session.id).with_for_update())
        await other.execute(
            update(GoalSession)
            .where(GoalSession.id == session.id)
            .values(ended_at=func.clock_timestamp(), outcome="interrupted")
        )
        closing = asyncio.create_task(w.driver.close_finished())
        await _until_waiters(w, "brain.goal_sessions")
        await other.commit()
    assert await closing == 0
    assert (await _session(w, session.id)).outcome == "interrupted"
    assert not [r for r in await _journal(w, goal, "state") if "ended" in r.text]


async def test_a_pause_during_a_session_lets_it_finish_and_starts_nothing_more(env_factory):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    root, session = await _started(w, goal)
    await w.store.pause(goal.id, actor="host")
    await _finish(w, root)
    report = await w.driver.tick()
    assert report.closed == 1 and report.started == ()
    assert (await _session(w, session.id)).outcome == "no_step"
    after = await w.store.get(goal.id)
    assert (after.state, after.pause_reason, after.next_wake_at) == ("paused", "owner", None)
    assert len(await _sessions(w, goal)) == 1


async def test_a_session_whose_subtask_never_got_created_is_closed_interrupted(env_factory):  # noqa: F811
    """Race 2: the start stopped between its two transactions. After 120 s with no subtask, the session is closed
    interrupted (not an error); a younger one is left for the start still in flight."""
    w = await _world(env_factory)
    old, young = await _active(w), await _active(w, title="Sort the photos")
    async with w.db.session() as s:
        now = (await s.execute(select(func.clock_timestamp()))).scalar_one()
        for goal, age in ((old, 200), (young, 10)):
            s.add(
                GoalSession(
                    agent_id=w.agent,
                    goal_id=goal.id,
                    n=1,
                    trigger="timer",
                    first_subtask_id=uuid.uuid4(),
                    started_at=now - timedelta(seconds=age),
                )
            )
        await s.commit()
    assert (await w.driver.tick()).closed == 1
    (gone,) = await _sessions(w, old)
    assert (gone.outcome, gone.spent, gone.root_id) == ("interrupted", 0, None)
    row = (await _journal(w, old, "state"))[-1]
    assert row.text.startswith("Session 1 ended: interrupted, 0 tokens.") and "never created" in row.text
    (waiting,) = await _sessions(w, young)
    assert waiting.ended_at is None
    assert (await w.store.get(old.id)).state == "active"


async def test_a_session_whose_root_id_was_not_written_is_repaired(env_factory, monkeypatch):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)

    async def lost(session_id, root_id):  # the process stopped before the root id was written
        return None

    monkeypatch.setattr(w.driver, "_set_root", lost)
    root_id = await w.driver.start_session(goal.id, trigger="timer")
    (session,) = await _sessions(w, goal)
    assert session.root_id is None
    monkeypatch.undo()
    assert (await w.driver.tick()).closed == 0
    repaired = await _session(w, session.id)
    assert repaired.root_id == root_id and repaired.ended_at is None


async def test_spent_of_a_recently_closed_session_is_re_read(env_factory):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    root, session = await _started(w, goal)
    await add_arrival(w.env, root.id, 1, tokens=(100, 10))
    assert (await w.driver.tick()).closed == 0
    assert (await _session(w, session.id)).spent == 110  # an open session's spend is read every tick
    await _finish(w, root)
    await w.driver.tick()
    await add_arrival(w.env, root.id, 2, tokens=(7, 3))  # a late arrival on the closed session's root
    await w.driver.tick()
    assert (await _session(w, session.id)).spent == 120
    await set_intention(w.env, root.id, deadline=datetime.now(UTC) - timedelta(minutes=1))
    await add_arrival(w.env, root.id, 3, tokens=(1000, 0))
    await w.driver.tick()
    assert (await _session(w, session.id)).spent == 120  # its root's deadline has passed: frozen


# ---- the goal channel: reports become journal rows ---------------------------------------------------------------


async def _goal_report(w: SimpleNamespace, goal: Goal, *, body: str = "Merged 3 pairs.", intention_id=None, **kw):
    async with w.db.session() as s:
        row_id = await continuation.insert_inbox_row(
            s,
            w.agent,
            source_kind=continuation.SOURCE_INTENTION_REPORT,
            source_id=uuid.uuid4(),
            msg_type="REPORT",
            title="Update",
            body=body,
            channel=f"goal:{goal.id}",
            intention_id=intention_id,
            **kw,
        )
        await s.commit()
    return row_id


async def _inbox(w: SimpleNamespace, row_id) -> ResultInbox:
    async with w.db.session() as s:
        return await s.get(ResultInbox, row_id)


async def test_report_rows_on_the_goal_channel_become_journal_rows_once(env_factory):  # noqa: F811
    w = await _world(env_factory)
    goal, other = await _active(w), await _active(w, title="Sort the photos")
    root, _session_row = await _started(w, goal)
    child = await make_child(w.env, root)
    long = await _goal_report(w, goal, body="z" * 5000, intention_id=child.id)
    short = await _goal_report(w, goal, intention_id=uuid.uuid4())  # an intention nothing knows: the newest session
    elsewhere = await _goal_report(w, other)
    report = await w.driver.tick()
    assert report.claimed == 3
    rows = await _journal(w, goal, "report")
    assert [(r.actor, r.session_n) for r in rows] == [("chain", 1), ("chain", 1)]
    assert len(rows[0].text) == 4000 and rows[0].text.endswith("[clipped]") and rows[0].evidence == [f"row:{long}"]
    assert rows[1].text == "Merged 3 pairs." and rows[1].evidence == [f"row:{short}"]
    (other_row,) = await _journal(w, other, "report")
    assert other_row.session_n is None and other_row.evidence == [f"row:{elsewhere}"]
    for row_id, channel in ((long, f"goal:{goal.id}"), (elsewhere, f"goal:{other.id}")):
        stamped = await _inbox(w, row_id)
        assert stamped.delivered_at is not None and stamped.delivered_session_id == channel
    assert (await w.driver.tick()).claimed == 0
    assert len(await _journal(w, goal, "report")) == 2


async def test_a_report_is_attributed_to_its_session_by_the_first_subtask_before_the_root_id_is_written(
    env_factory,  # noqa: F811
):
    w = await _world(env_factory)
    goal = await _active(w)
    root, session = await _started(w, goal)
    async with w.db.session() as s:
        await s.execute(update(GoalSession).where(GoalSession.id == session.id).values(n=4, root_id=None))
        s.add(
            GoalSession(
                agent_id=w.agent,
                goal_id=goal.id,
                n=5,
                trigger="timer",
                first_subtask_id=uuid.uuid4(),
                ended_at=func.clock_timestamp(),
                outcome="interrupted",
            )
        )
        await s.commit()
    await _goal_report(w, goal, intention_id=root.id)
    assert await w.driver.claim_reports() == 1
    (row,) = await _journal(w, goal, "report")
    assert row.session_n == 4  # not the newest session (5): the root's first subtask names it


async def test_a_report_older_than_the_age_window_is_still_claimed(env_factory):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    old = datetime.now(UTC) - timedelta(hours=w.env.settings.result_inbox_max_age_hours + 5)
    row_id = await _goal_report(w, goal, created_at=old)
    assert (await w.driver.tick()).claimed == 1
    assert (await _inbox(w, row_id)).delivered_at is not None
    assert len(await _journal(w, goal, "report")) == 1


async def test_more_than_fifty_reports_are_all_journalled(env_factory):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    for i in range(60):
        await _goal_report(w, goal, body=f"report {i}")
    assert (await w.driver.tick()).claimed == 60
    assert sorted(r.text for r in await _journal(w, goal, "report")) == sorted(f"report {i}" for i in range(60))


async def test_a_crash_between_claim_and_journal_loses_no_report(env_factory, monkeypatch):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    row_id = await _goal_report(w, goal)

    def crash(*args, **kwargs):
        raise RuntimeError("the process stopped")

    monkeypatch.setattr(w.driver, "_journal", crash)
    assert (await w.driver.tick()).claimed == 0
    assert (await _inbox(w, row_id)).delivered_at is None  # the claim rolled back with the journal insert
    assert await _journal(w, goal, "report") == []
    monkeypatch.undo()
    assert (await w.driver.tick()).claimed == 1


async def test_two_claims_at_once_journal_each_report_once(env_factory):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    ids = [await _goal_report(w, goal, body=f"report {i}") for i in range(3)]
    twin = GoalDriver(w.db, w.env.settings, w.env.heart, w.store, FakeRunner())
    async with w.db.session() as holding:
        await holding.execute(select(ResultInbox).where(ResultInbox.id.in_(ids)).with_for_update())
        claims = [asyncio.create_task(w.driver.claim_reports()), asyncio.create_task(twin.claim_reports())]
        await _until_waiters(w, "heart.result_inbox", at_least=2)
        await holding.commit()
    assert sorted(await asyncio.gather(*claims)) == [0, 3]
    assert sorted(r.text for r in await _journal(w, goal, "report")) == ["report 0", "report 1", "report 2"]


# ---- questions and answers, and a real ask ----------------------------------------------------------------------


async def _question_id(w: SimpleNamespace, arrival_id) -> uuid.UUID:
    async with w.db.session() as s:
        query = select(ResultInbox.source_id).where(
            ResultInbox.agent_id == w.agent, ResultInbox.arrival_id == arrival_id, ResultInbox.msg_type == "QUESTION"
        )
        return (await s.execute(query)).scalar_one()


async def test_questions_and_answers_of_a_session_are_journalled_once(env_factory):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    root, session = await _started(w, goal)
    child = await make_child(w.env, root)
    await record(w.env, root)
    await record(w.env, child)
    got = await claim(w.env, root.id)
    asked = await commit_ask(w.env, got, "Which folder holds the notes?")
    async with w.db.session() as s:
        answer = await continuation.record_answer(
            s,
            w.agent,
            await _question_id(w, asked.arrival_id),
            text="The notes folder.",
            actor="telegram:42",
            settings=w.env.settings,
        )
        await s.commit()
    assert len(answer.intention_ids) == 2  # one answer row per waiting intention
    for intention in (root, child):
        await set_intention(w.env, intention.id, state="closed", close_reason="resolved")
    assert (await w.driver.tick()).closed == 1
    async with w.db.session() as s:  # the close's step again, on the closed session: each row is journalled once
        assert await w.driver._journal_questions_and_answers(s, goal.id, await s.get(GoalSession, session.id)) == 0
        await s.commit()
    questions, answers = await _journal(w, goal, "question"), await _journal(w, goal, "answer")
    assert [(q.text, q.actor, q.session_n) for q in questions] == [("Which folder holds the notes?", "chain", 1)]
    assert [(a.text, a.actor, a.session_n) for a in answers] == [("The notes folder.", "owner", 1)]
    assert questions[0].evidence[0].startswith("row:") and answers[0].evidence[1] == f"arrival:{asked.arrival_id}"
    ended = (await _journal(w, goal))[-1]
    assert ended.kind == "state" and ended.seq > answers[0].seq > questions[0].seq
    assert (await _session(w, session.id)).outcome == "asked"


async def test_asked_comes_from_a_real_ask_then_an_expiry_and_a_report(runner_env):  # noqa: F811
    """An ask leaves the chain waiting (``awaiting_owner`` is an open state), so the session closes only after the
    question is answered or expires and the chain ends: the outcome is ``asked`` from the ask in its history."""
    note = "Which folder holds the notes?"
    ask = use("resolve_intention", decision="ask", note=note, progress=False, confidence=0.7)
    done = "Nobody answered, so I merged the notes folder only."
    report = use("resolve_intention", decision="report", note=done, progress=True, confidence=0.7)
    env = await runner_env([ask], [report])
    cont = ContinuationRunner(
        database=env.db, settings=env.settings, runner=env.runner, heart=env.heart, brain=env.brain, bus=env.bus
    )
    store = GoalStore(env.db, env.agent, env.settings)
    driver = GoalDriver(env.db, env.settings, env.heart, store, cont)
    w = SimpleNamespace(env=env, db=env.db, agent=env.agent, store=store, runner=cont, driver=driver)
    goal = await _active(w)
    root, session = await _started(w, goal)
    await env.heart.subtasks.complete(uuid.UUID(root.source_id), RESULT)
    await record(env, root)  # the first subtask's result, as the worker's hook writes it
    asked = await cont.run_arrival(root.id)
    assert asked is not None and asked.next_states == {root.id: "awaiting_owner"}
    assert (await driver.tick()).closed == 0  # waiting for the owner is open work
    async with env.db.session() as s:  # nobody answers: the question's window passes
        await s.execute(
            update(ResultInbox)
            .where(ResultInbox.agent_id == env.agent, ResultInbox.arrival_id == asked.arrival_id)
            .values(created_at=datetime.now(UTC) - timedelta(hours=25), push_after=None)
        )
        await s.commit()
    async with env.db.session() as s:
        assert await continuation.wake_terminal_arrivals(s, env.agent, settings=env.settings) == [root.id]
        await s.commit()
    assert await cont.run_arrival(root.id) is not None
    assert (await driver.tick()).closed == 1
    assert (await _session(w, session.id)).outcome == "asked"
    (question,) = await _journal(w, goal, "question")
    assert (question.text, question.session_n) == (note, 1)
    assert await _journal(w, goal, "answer") == []  # the "did not answer" row is not the owner's answer
    ended = (await _journal(w, goal, "state"))[-1]
    assert ended.text.startswith("Session 1 ended: asked") and done in ended.text
