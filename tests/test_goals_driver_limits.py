"""F114 goals: the goal driver's second half (PR 1b-ii): the owner's requests (run now, stop, cancel), stuck sessions,
stop conditions, budgets and pace, and what a closed session's resumed chain leaves in the journal.

Nothing builds the driver outside these tests yet: each test constructs it on an environment of its own agent.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from f099_dashboard_support import put_intention
from f099_support import (
    RESULT,
    add_arrival,
    env_factory,  # noqa: F401
    intention_of,
    make_child,
    make_dag,
    record,
    set_intention,
)
from sqlalchemy import event, func, select, update
from test_goals_driver import (
    FakeRunner,
    _active,
    _arrival,
    _db_now,
    _finish,
    _journal,
    _pending,
    _refuse,
    _running,
    _session,
    _sessions,
    _set_goal,
    _started,
    _until_waiters,
    _world,
)

from nous.brain import continuation
from nous.cognitive.ledger_store import detail_text
from nous.goals import driver as driver_module
from nous.goals import roots, spend
from nous.goals.driver import GoalDriver
from nous.goals.store import GoalRefused
from nous.handlers.continuation_runner import ContinuationRunner
from nous.storage.models import DAGNode, ExecutionDAG, Goal, GoalJournal, GoalSession, Intention, Subtask, ToolCallLog

pytestmark = pytest.mark.postgres_only


async def _cont_world(env_factory, **settings) -> SimpleNamespace:  # noqa: F811
    """A world whose driver ends chains through a real continuation runner's ``cancel_root``."""
    w = await _world(env_factory, **settings)
    cont = ContinuationRunner(
        database=w.db, settings=w.env.settings, runner=None, heart=w.env.heart, brain=None, bus=w.env.bus
    )
    w.runner = cont
    w.driver = GoalDriver(w.db, w.env.settings, w.env.heart, w.store, cont)
    return w


async def _goal(w: SimpleNamespace, goal: Goal) -> Goal:
    return await w.store.get(goal.id)


async def _harness(w: SimpleNamespace, goal: Goal) -> list[GoalJournal]:
    return [r for r in await _journal(w, goal, "state") if r.actor == "harness"]


async def _intention(w: SimpleNamespace, intention_id) -> Intention:
    async with w.db.session() as s:
        return await s.get(Intention, intention_id)


async def _ask(w: SimpleNamespace, goal: Goal, kind: str, *, at: datetime) -> None:
    """A request row as the host command leaves it, written by hand where the store would refuse it."""
    await _set_goal(w, goal, pending_request=kind, pending_request_at=at)


async def _closed(
    w: SimpleNamespace, goal: Goal, outcomes: list[str], *, spent: int = 0, started_at=None, ended_at=None
) -> list[GoalSession]:
    """Closed sessions of ``goal`` with these outcomes, numbered after its newest one (no chain behind them)."""
    rows = []
    async with w.db.session() as s:
        first = (
            await s.execute(select(func.coalesce(func.max(GoalSession.n), 0)).where(GoalSession.goal_id == goal.id))
        ).scalar_one()
        for i, outcome in enumerate(outcomes, start=first + 1):
            row = GoalSession(
                agent_id=w.agent,
                goal_id=goal.id,
                n=i,
                trigger="timer",
                first_subtask_id=uuid.uuid4(),
                started_at=started_at if started_at is not None else func.clock_timestamp(),
                ended_at=ended_at if ended_at is not None else func.clock_timestamp(),
                outcome=outcome,
                spent=spent,
            )
            s.add(row)
            rows.append(row)
        await s.commit()
    return rows


def _held_root(w: SimpleNamespace, subtask_id: uuid.UUID, *, past: int) -> object:
    """A worker took the subtask ``past`` seconds beyond its timeout plus the stuck grace (negative: still inside)."""
    timeout = w.env.settings.subtask_default_timeout
    return _running(
        w,
        subtask_id,
        timeout_seconds=timeout,
        started_at=func.clock_timestamp() - timedelta(seconds=timeout + driver_module.STUCK_GRACE_SECONDS + past),
    )


# ---- stop and cancel ---------------------------------------------------------------------------------------------


async def test_stop_cancels_the_root_and_pauses_the_goal(env_factory, monkeypatch):  # noqa: F811
    w = await _cont_world(env_factory)
    goal = await _active(w)
    root, session = await _started(w, goal)  # its first subtask is pending
    stopped = await w.store.stop(goal.id, actor="host")
    assert (stopped.state, stopped.pause_reason, stopped.pending_request) == ("paused", "owner", "stop")
    real_cancel, seen_at_cancel = w.runner.cancel_root, []

    async def cancel_root(root_id, **kwargs):  # the harness's row is committed before the root is cancelled
        seen_at_cancel.extend(r.evidence for r in await _harness(w, goal))
        return await real_cancel(root_id, **kwargs)

    monkeypatch.setattr(w.runner, "cancel_root", cancel_root)
    report = await w.driver.tick()
    assert report.stopped == 1
    request = f"request:stop@{stopped.pending_request_at.isoformat()}"
    assert [f"ending:{session.id}", request] in seen_at_cancel
    after_root = await _intention(w, root.id)
    assert after_root.root_cancelled_at is not None and after_root.state == "cancelled"
    async with w.db.session() as s:
        assert (await s.get(Subtask, session.first_subtask_id)).status == "cancelled"
    closed = await _session(w, session.id)
    assert closed.ended_at is not None and closed.outcome == "interrupted"
    after = await _goal(w, goal)
    assert (after.state, after.pause_reason, after.pending_request, after.pending_request_at) == (
        "paused",
        "owner",
        None,
        None,
    )
    harness = await _harness(w, goal)
    ending = [r for r in harness if f"ending:{session.id}" in r.evidence]
    assert [r.text for r in ending] == ["Session 1 is being stopped: the owner asked to stop it."]
    assert ending[0].seq < next(r.seq for r in harness if r.text.startswith("Session 1 ended: interrupted"))
    assert not [r for r in harness if "owner cancelled session" in r.text]  # the driver's own cancel
    assert (await w.driver.tick()).stopped == 0  # acted on once


async def test_stop_on_a_finished_root_still_closes_the_session(env_factory):  # noqa: F811
    """The chain ended before the stop was acted on: the cancel is refused (nothing running) and the session is
    closed by the close's rules all the same."""
    w = await _cont_world(env_factory)
    goal = await _active(w)
    root, session = await _started(w, goal)
    await _finish(w, root)
    await w.store.stop(goal.id, actor="host")
    assert (await w.driver.tick()).stopped == 1
    assert (await _session(w, session.id)).outcome == "no_step"
    assert (await _intention(w, root.id)).root_cancelled_at is None
    assert (await _goal(w, goal)).pending_request is None


async def test_cancel_request_cancels_the_goal_and_the_root(env_factory):  # noqa: F811
    w = await _cont_world(env_factory)
    goal = await _active(w)
    root, session = await _started(w, goal)
    await w.store.cancel(goal.id, actor="host")
    assert (await w.driver.tick()).stopped == 1
    assert (await _intention(w, root.id)).root_cancelled_at is not None
    assert (await _session(w, session.id)).outcome == "interrupted"
    after = await _goal(w, goal)
    assert (after.state, after.pending_request) == ("cancelled", None)
    assert "the owner cancelled the goal" in [r for r in await _harness(w, goal)][0].text


async def test_a_crash_between_the_ending_row_and_the_cancel_writes_the_row_once(env_factory, monkeypatch):  # noqa: F811
    w = await _cont_world(env_factory)
    goal = await _active(w)
    root, session = await _started(w, goal)
    await w.store.stop(goal.id, actor="host")
    real_cancel = w.runner.cancel_root

    async def crash(*args, **kwargs):
        raise RuntimeError("the process stopped")

    monkeypatch.setattr(w.runner, "cancel_root", crash)
    assert (await w.driver.tick()).stopped == 0
    assert (await _goal(w, goal)).pending_request == "stop"  # kept: acted on at the next tick
    assert (await _session(w, session.id)).ended_at is None
    monkeypatch.setattr(w.runner, "cancel_root", real_cancel)
    assert (await w.driver.tick()).stopped == 1
    ending = [r for r in await _harness(w, goal) if f"ending:{session.id}" in r.evidence]
    assert len(ending) == 1
    assert (await _goal(w, goal)).pause_reason == "owner"
    assert not [r for r in await _harness(w, goal) if "owner cancelled session" in r.text]


async def _stop_cut_short_then_resumed(w: SimpleNamespace, goal: Goal, monkeypatch) -> GoalSession:
    """A stop acted on up to its ending row, the process stopping before the cancel, then the owner's resume (which
    clears the request). Returns the session, still open with its chain running."""
    _root, session = await _started(w, goal)
    await w.store.stop(goal.id, actor="host")
    real_cancel = w.runner.cancel_root

    async def crash(*args, **kwargs):
        raise RuntimeError("the process stopped")

    monkeypatch.setattr(w.runner, "cancel_root", crash)
    assert (await w.driver.tick()).stopped == 0
    assert [r for r in await _harness(w, goal) if f"ending:{session.id}" in r.evidence]
    monkeypatch.setattr(w.runner, "cancel_root", real_cancel)
    resumed = await w.store.resume(goal.id, actor="host")
    assert (resumed.state, resumed.pending_request) == ("active", None)
    return session


async def test_an_ending_row_of_a_stop_the_owner_resumed_does_not_hide_a_later_owner_cancel(env_factory, monkeypatch):  # noqa: F811
    """The ending row was written for the stop, which the resume cleared: the owner's later cancel of the chain's
    root (the continuation's own cancel, as the routes call it) is read as the owner's, and pauses the goal."""
    w = await _cont_world(env_factory)
    goal = await _active(w)
    session = await _stop_cut_short_then_resumed(w, goal, monkeypatch)
    await w.runner.cancel_root(session.root_id, reason="cancelled by the owner", actor="owner")
    assert (await w.driver.tick()).closed == 1
    closed = await _session(w, session.id)
    assert (closed.ended_at is not None, closed.outcome) == (True, "interrupted")
    after = await _goal(w, goal)
    assert (after.state, after.pause_reason, after.next_wake_at) == ("paused", "owner", None)
    assert (await _harness(w, goal))[-1].text == "Paused: the owner cancelled session 1's chain."


async def test_a_new_stop_after_a_resume_writes_its_own_ending_row(env_factory, monkeypatch):  # noqa: F811
    """The old row speaks for the cleared stop only, so the new stop writes a row of its own before its cancel, and
    the close takes that cancel for the driver's."""
    w = await _cont_world(env_factory)
    goal = await _active(w)
    session = await _stop_cut_short_then_resumed(w, goal, monkeypatch)
    stopped = await w.store.stop(goal.id, actor="host")
    assert (await w.driver.tick()).stopped == 1
    ending = [r for r in await _harness(w, goal) if f"ending:{session.id}" in r.evidence]
    assert len(ending) == 2
    assert ending[-1].evidence == [f"ending:{session.id}", f"request:stop@{stopped.pending_request_at.isoformat()}"]
    assert not [r for r in await _harness(w, goal) if "owner cancelled session" in r.text]
    assert (await _goal(w, goal)).pending_request is None


async def test_a_stop_the_owner_takes_back_after_the_tick_read_it_ends_nothing(env_factory, monkeypatch):  # noqa: F811
    """The tick reads a stop; before it acts, the owner resumes the goal (which clears the request). The request is
    read again under the goal's lock when the ending row would be written: nothing is ended."""
    w = await _cont_world(env_factory)
    goal = await _active(w)
    root, session = await _started(w, goal)
    await w.store.stop(goal.id, actor="host")
    end_chain = w.driver._end_chain

    async def resumed_first(*args, **kwargs):
        await w.store.resume(goal.id, actor="host")
        return await end_chain(*args, **kwargs)

    monkeypatch.setattr(w.driver, "_end_chain", resumed_first)
    assert (await w.driver.tick()).stopped == 0
    assert (await _intention(w, root.id)).root_cancelled_at is None
    assert (await _session(w, session.id)).ended_at is None
    assert not [r for r in await _harness(w, goal) if f"ending:{session.id}" in r.evidence]
    assert (await _goal(w, goal)).state == "active"


async def test_a_cancel_that_replaces_a_stop_after_the_read_is_acted_on_as_a_cancel(env_factory, monkeypatch):  # noqa: F811
    w = await _cont_world(env_factory)
    goal = await _active(w)
    root, session = await _started(w, goal)
    await w.store.stop(goal.id, actor="host")
    end_chain, fired = w.driver._end_chain, []

    async def cancelled_first(*args, **kwargs):
        if not fired:
            fired.append(True)
            await w.store.cancel(goal.id, actor="host")
        return await end_chain(*args, **kwargs)

    monkeypatch.setattr(w.driver, "_end_chain", cancelled_first)
    assert (await w.driver.tick()).stopped == 0  # the stop it read is gone
    assert (await _goal(w, goal)).pending_request == "cancel"
    assert (await w.driver.tick()).stopped == 1
    ending = [r for r in await _harness(w, goal) if f"ending:{session.id}" in r.evidence]
    assert [r.text for r in ending] == ["Session 1 is being stopped: the owner cancelled the goal."]
    after = await _goal(w, goal)
    assert (after.state, after.pending_request) == ("cancelled", None)
    assert (await _intention(w, root.id)).root_cancelled_at is not None


async def test_an_owner_cancel_through_the_route_pauses_the_goal(env_factory):  # noqa: F811
    w = await _cont_world(env_factory)
    goal = await _active(w)
    root, session = await _started(w, goal)
    await w.runner.cancel_root(root.id, reason="no longer needed", actor="rest")  # what the owner's route calls
    assert (await w.driver.tick()).closed == 1
    assert (await _session(w, session.id)).outcome == "interrupted"
    after = await _goal(w, goal)
    assert (after.state, after.pause_reason, after.next_wake_at) == ("paused", "owner", None)
    assert (await _harness(w, goal))[-1].text == "Paused: the owner cancelled session 1's chain."


async def test_an_expired_root_never_pauses_the_goal_as_owner(env_factory):  # noqa: F811
    w = await _cont_world(env_factory)
    goal = await _active(w)
    root, session = await _started(w, goal)
    await set_intention(w.env, root.id, deadline=datetime.now(UTC) - timedelta(hours=1))
    async with w.db.session() as s:
        assert await continuation.expire_roots(s, w.agent, ttl_hours=72.0, settings=w.env.settings) == [root.id]
        await s.commit()
    assert (await w.driver.tick()).closed == 1
    assert (await _session(w, session.id)).outcome == "interrupted"
    after = await _goal(w, goal)
    assert after.state == "active" and after.next_wake_at is not None


async def test_a_stale_stop_does_not_kill_a_later_session(env_factory):  # noqa: F811
    w = await _cont_world(env_factory)
    goal = await _active(w)
    assert (await w.store.stop(goal.id, actor="host")).pending_request is None  # no session: paused, nothing asked
    with pytest.raises(GoalRefused, match="nothing to stop"):  # paused with no session: refused
        await w.store.stop(goal.id, actor="host")
    await w.store.resume(goal.id, actor="host")
    before = await _db_now(w)
    root, session = await _started(w, goal)
    await _ask(w, goal, "stop", at=before - timedelta(seconds=1))  # made before this session started
    assert (await w.driver.tick()).stopped == 0
    assert (await _intention(w, root.id)).root_cancelled_at is None
    assert (await _session(w, session.id)).ended_at is None
    after = await _goal(w, goal)
    assert (after.state, after.pending_request) == ("active", None)
    assert (await _harness(w, goal))[-1].text == "The stop request was cleared: it was made before session 1 started."


async def test_a_stop_with_no_session_open_is_cleared(env_factory):  # noqa: F811
    w = await _cont_world(env_factory)
    goal = await _active(w)
    await w.store.pause(goal.id, actor="host")
    await _ask(w, goal, "stop", at=await _db_now(w))
    await w.driver.tick()
    assert (await _goal(w, goal)).pending_request is None
    assert (await _harness(w, goal))[-1].text == "The stop request was cleared: no session is open."


async def test_a_request_made_while_the_driver_acts_is_kept(env_factory):  # noqa: F811
    """A request is cleared only while it is still the one read: a newer one is kept."""
    w = await _cont_world(env_factory)
    goal = await _active(w)
    await w.store.pause(goal.id, actor="host")
    read_at = await _db_now(w)
    await _ask(w, goal, "stop", at=read_at)
    newer = read_at + timedelta(seconds=5)
    await _ask(w, goal, "cancel", at=newer)  # replaced after the read
    assert await w.driver._drop_request(goal.id, "stop", read_at, why="no session is open") is False
    after = await _goal(w, goal)
    assert (after.pending_request, after.pending_request_at) == ("cancel", newer)


# ---- run now -----------------------------------------------------------------------------------------------------


async def _later_wake(w: SimpleNamespace, goal: Goal, hours: int = 2):
    await _set_goal(w, goal, next_wake_at=func.clock_timestamp() + timedelta(hours=hours))
    return (await _goal(w, goal)).next_wake_at


async def test_run_now_starts_a_session_and_clears_the_request(env_factory):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    wake = await _later_wake(w, goal)
    await w.store.request_run_now(goal.id, actor="host")
    report = await w.driver.tick()
    assert len(report.started) == 1
    (session,) = await _sessions(w, goal)
    assert (session.trigger, session.root_id) == ("run_now", report.started[0])
    after = await _goal(w, goal)
    assert (after.pending_request, after.pending_request_at, after.next_wake_at) == (None, None, wake)
    assert (await w.driver.tick()).started == ()  # acted on once


async def test_run_now_skips_the_interval_and_the_days_sessions_but_no_other_limit(env_factory):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    await _set_goal(w, goal, sessions_per_day=1, day_budget=100)
    await _closed(w, goal, ["no_step"])  # today's one session
    await _later_wake(w, goal)
    await w.store.request_run_now(goal.id, actor="host")
    assert len((await w.driver.tick()).started) == 1  # neither the wake nor the day's count holds it
    (_first, second) = await _sessions(w, goal)
    async with w.db.session() as s:
        await s.execute(update(GoalSession).where(GoalSession.id == second.id).values(spent=100))
        await s.commit()
    await _finish(w, await w.env.heart.intentions.get_for_source("subtask", second.first_subtask_id))
    await w.driver.tick()
    await w.store.request_run_now(goal.id, actor="host")
    assert (await w.driver.tick()).started == ()
    assert (await _harness(w, goal))[-1].text == "Run now was not started: today's budget is spent."
    assert (await _goal(w, goal)).pending_request is None


async def test_run_now_is_held_by_a_halted_background_and_cleared(env_factory, monkeypatch):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    wake = await _later_wake(w, goal)
    await w.store.request_run_now(goal.id, actor="host")
    monkeypatch.setattr(w.driver, "background_halted", lambda: True)
    report = await w.driver.tick()
    assert report.started == () and await _sessions(w, goal) == []
    after = await _goal(w, goal)
    assert (after.pending_request, after.next_wake_at) == (None, wake)
    assert (await _harness(w, goal))[-1].text == "Run now was not started: background work is halted."


async def test_run_now_on_a_paused_goal_is_ignored_and_cleared(env_factory):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    await w.store.pause(goal.id, actor="host")
    await _ask(w, goal, "run_now", at=await _db_now(w))
    assert (await w.driver.tick()).started == ()
    assert await _sessions(w, goal) == []
    after = await _goal(w, goal)
    assert (after.state, after.pending_request) == ("paused", None)
    assert (await _harness(w, goal))[-1].text == "The run-now request was cleared: the goal is not active."


async def test_a_failed_run_now_never_brings_the_wake_forward(env_factory, monkeypatch):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    wake = await _later_wake(w, goal)

    async def broken(*args, **kwargs):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(roots, "create_session_root", broken)
    await w.store.request_run_now(goal.id, actor="host")
    assert (await w.driver.tick()).started == ()
    after = await _goal(w, goal)
    assert (after.next_wake_at, after.pending_request) == (wake, None)  # five minutes would have been earlier
    assert w.driver._failures == {goal.id: 1}
    assert (await _harness(w, goal))[-1].text == "Session 1 could not start: RuntimeError."
    assert (await w.driver.tick()).started == ()  # the request was acted on once: no second failure
    assert w.driver._failures == {goal.id: 1}


NOT_STARTED = "Run now was not started"


async def test_a_run_now_that_finds_the_queue_full_at_its_publish_leaves_one_row(env_factory, monkeypatch):  # noqa: F811
    """The queue fills after the tick's headroom check: the publish is refused, the session row undone, and the
    request (cleared with that row) leaves one harness row saying the run-now was not started."""
    w = await _world(env_factory)
    goal = await _active(w)
    await _later_wake(w, goal)
    gated_task = w.driver._gated_task

    async def queue_fills(goal_row):
        await _pending(w, 5)
        return await gated_task(goal_row)

    monkeypatch.setattr(w.driver, "_gated_task", queue_fills)
    await w.store.request_run_now(goal.id, actor="host")
    assert (await w.driver.tick()).started == ()
    assert await _sessions(w, goal) == []
    assert (await _goal(w, goal)).pending_request is None
    rows = [r for r in await _harness(w, goal) if r.text.startswith(NOT_STARTED)]
    assert [(r.text, r.session_n) for r in rows] == [
        ("Run now was not started: the subtask queue has too few free places.", None)
    ]
    assert w.driver._failures == {}
    assert (await w.driver.tick()).started == ()  # the request was acted on once: no second row
    assert len([r for r in await _harness(w, goal) if r.text.startswith(NOT_STARTED)]) == 1


async def test_a_run_now_whose_session_recovery_closed_before_its_publish_leaves_one_row(
    env_factory,  # noqa: F811
    monkeypatch,
):
    """The start is held between its two transactions past the window, and another tick's close finds no first task
    and closes the session: the publish finds it closed and publishes nothing, and the run-now leaves one row."""
    w = await _world(env_factory)
    goal = await _active(w)
    reached, release = asyncio.Event(), asyncio.Event()
    gated_task = w.driver._gated_task

    async def held(goal_row):
        reached.set()
        await release.wait()
        return await gated_task(goal_row)

    monkeypatch.setattr(w.driver, "_gated_task", held)
    await w.store.request_run_now(goal.id, actor="host")
    ticking = asyncio.create_task(w.driver.tick())
    await asyncio.wait_for(reached.wait(), timeout=10)
    (session,) = await _sessions(w, goal)
    async with w.db.session() as s:
        await s.execute(
            update(GoalSession)
            .where(GoalSession.id == session.id)
            .values(started_at=GoalSession.started_at - timedelta(seconds=600))
        )
        await s.commit()
    recovery = GoalDriver(w.db, w.env.settings, w.env.heart, w.store, FakeRunner())
    assert await recovery.close_finished() == 1
    release.set()
    assert (await ticking).started == ()
    closed = await _session(w, session.id)
    assert (closed.trigger, closed.outcome, closed.root_id) == ("run_now", "interrupted", None)
    rows = [r for r in await _harness(w, goal) if r.text.startswith(NOT_STARTED)]
    assert [(r.text, r.session_n) for r in rows] == [
        ("Run now was not started: its session was closed before its first task was published.", 1)
    ]
    assert w.driver._failures == {} and (await _goal(w, goal)).pending_request is None


async def test_a_full_queue_never_brings_a_later_wake_forward(env_factory):  # noqa: F811
    """``delay_wake`` moves a wake to a time from now only when that is later than the wake it has."""
    w = await _world(env_factory)
    goal = await _active(w)
    wake = await _later_wake(w, goal)
    assert await w.store.delay_wake(goal.id, seconds=60, seen=wake) is True
    assert (await _goal(w, goal)).next_wake_at == wake
    due = await _later_wake(w, goal, hours=0)
    before = await _db_now(w)
    assert await w.store.delay_wake(goal.id, seconds=60, seen=due) is True
    assert (await _goal(w, goal)).next_wake_at >= before + timedelta(seconds=60)


async def test_stop_conditions_run_before_a_run_now(env_factory):  # noqa: F811
    """A goal over its budget with a run-now pending is paused, never started."""
    w = await _world(env_factory)
    goal = await _active(w)
    await w.store.request_run_now(goal.id, actor="host")
    await _closed(w, goal, ["no_step"], spent=goal.total_budget)
    report = await w.driver.tick()
    assert report.paused == 1 and report.started == ()
    after = await _goal(w, goal)
    assert (after.state, after.pause_reason, after.pending_request) == ("paused", "budget", None)


async def test_a_start_is_held_by_the_total_budget_under_the_goal_lock(env_factory):  # noqa: F811
    """A start called directly (no stop-condition step before it) reads the total budget under the goal lock: spent to
    the token holds it, one token under starts."""
    w = await _world(env_factory)
    goal = await _active(w)
    (session,) = await _closed(w, goal, ["step_closed"], spent=goal.total_budget)
    await _set_goal(w, goal, day_budget=1, sessions_per_day=24)  # the day's budget is not what holds it
    async with w.db.session() as s:  # yesterday's session: today's budget is free
        await s.execute(
            update(GoalSession)
            .where(GoalSession.id == session.id)
            .values(started_at=func.clock_timestamp() - timedelta(days=1))
        )
        await s.commit()
    assert await w.driver.start_session(goal.id, trigger="run_now") is None
    assert len(await _sessions(w, goal)) == 1
    async with w.db.session() as s:
        await s.execute(update(GoalSession).where(GoalSession.id == session.id).values(spent=goal.total_budget - 1))
        await s.commit()
    assert await w.driver.start_session(goal.id, trigger="run_now") is not None


async def test_a_start_is_held_by_the_deadline_under_the_goal_lock(env_factory):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    await _set_goal(w, goal, deadline_at=func.clock_timestamp() - timedelta(milliseconds=1))
    assert await w.driver.start_session(goal.id, trigger="run_now") is None
    assert await _sessions(w, goal) == []
    await _set_goal(w, goal, deadline_at=func.clock_timestamp() + timedelta(minutes=5))
    assert await w.driver.start_session(goal.id, trigger="run_now") is not None


# ---- stuck sessions ----------------------------------------------------------------------------------------------


async def test_a_running_subtask_past_its_timeout_and_grace_cancels_the_root_and_closes_the_session_interrupted(
    env_factory,  # noqa: F811
):
    w = await _cont_world(env_factory)
    goal = await _active(w)
    root, session = await _started(w, goal)
    await _held_root(w, session.first_subtask_id, past=5)
    assert (await w.driver.tick()).stopped == 1
    after_root = await _intention(w, root.id)
    assert after_root.root_cancelled_at is not None
    assert (after_root.state, after_root.close_reason) == ("cancelled", "cancelled")  # not a legacy close
    assert (await _session(w, session.id)).outcome == "interrupted"
    after = await _goal(w, goal)
    assert (after.state, after.pause_reason) == ("active", None)  # the harness's cancel is not the owner's
    texts = [r.text for r in await _harness(w, goal)]
    assert "Session 1 is being stopped: a task of its chain ran past its time limit." in texts


async def test_a_running_subtask_inside_its_timeout_is_left_alone(env_factory):  # noqa: F811
    w = await _cont_world(env_factory)
    goal = await _active(w)
    root, session = await _started(w, goal)
    await _held_root(w, session.first_subtask_id, past=-5)  # five seconds inside its limit
    assert (await w.driver.tick()).stopped == 0
    assert (await _intention(w, root.id)).root_cancelled_at is None
    assert (await _session(w, session.id)).ended_at is None


async def test_a_stuck_session_is_interrupted_not_an_error_and_does_not_count_toward_a_stall(env_factory):  # noqa: F811
    w = await _cont_world(env_factory)
    goal = await _active(w)
    await _set_goal(w, goal, stall_limit=3, sessions_per_day=24)
    await _closed(w, goal, ["no_step", "no_step"])
    await _set_goal(w, goal, next_wake_at=func.clock_timestamp())
    _root, session = await _started(w, goal)
    await _held_root(w, session.first_subtask_id, past=5)
    await w.driver.tick()
    assert (await _session(w, session.id)).outcome == "interrupted"
    assert (await _goal(w, goal)).state == "active"  # two no_step and an interrupted: no stall


async def _resumed_child(w: SimpleNamespace, root: Intention) -> Intention:
    """A late result reopens the closed root, and the decision turn spawns a step whose subtask a worker takes."""
    await record(w.env, root)
    await set_intention(w.env, root.id, state="closed", close_reason="resolved")
    return await make_child(w.env, root)


async def test_a_stuck_chain_of_a_closed_session_that_holds_the_goal_is_ended(env_factory):  # noqa: F811
    """A closed session's root that a late result reopened holds the goal's next session until its deadline. A task
    of it stuck past its limit ends that chain; the session row keeps what its close wrote."""
    w = await _cont_world(env_factory)
    goal = await _active(w)
    root, session = await _started(w, goal)
    await _finish(w, root)
    assert (await w.driver.tick()).closed == 1
    closed = await _session(w, session.id)
    child = await _resumed_child(w, root)
    await _held_root(w, uuid.UUID(child.source_id), past=-30)  # still inside its limit
    await _set_goal(w, goal, next_wake_at=func.clock_timestamp())
    held = await w.driver.tick()
    assert (held.stopped, held.started) == (0, ())  # the resumed chain holds the next session
    await _held_root(w, uuid.UUID(child.source_id), past=5)
    await _set_goal(w, goal, next_wake_at=func.clock_timestamp())
    report = await w.driver.tick()
    assert report.stopped == 1 and len(report.started) == 1  # ended, and no longer holding the goal
    assert (await _intention(w, root.id)).root_cancelled_at is not None
    again = await _session(w, session.id)
    assert (again.outcome, again.ended_at, again.summary) == (closed.outcome, closed.ended_at, closed.summary)
    texts = [r.text for r in await _harness(w, goal)]
    assert "Session 1's chain, resumed after the session closed, is being stopped: a task ran past its limit." in texts
    assert (await _goal(w, goal)).state == "active"


async def test_a_stuck_task_under_a_root_past_its_deadline_is_left_alone(env_factory):  # noqa: F811
    """A closed session's root past its deadline holds nothing (the TTL sweep ends it): not looked at."""
    w = await _cont_world(env_factory)
    goal = await _active(w)
    root, _session_row = await _started(w, goal)
    await _finish(w, root)
    await w.driver.tick()
    child = await _resumed_child(w, root)
    await _held_root(w, uuid.UUID(child.source_id), past=5)
    await set_intention(w.env, root.id, deadline=datetime.now(UTC) - timedelta(minutes=1))
    assert (await w.driver.tick()).stopped == 0
    assert (await _intention(w, root.id)).root_cancelled_at is None


async def test_a_session_stopped_before_its_first_task_was_published_closes_interrupted(env_factory):  # noqa: F811
    w = await _cont_world(env_factory)
    goal = await _active(w)
    async with w.db.session() as s:
        s.add(GoalSession(agent_id=w.agent, goal_id=goal.id, n=1, trigger="timer", first_subtask_id=uuid.uuid4()))
        await s.commit()
    await w.store.stop(goal.id, actor="host")
    assert (await w.driver.tick()).stopped == 1
    (session,) = await _sessions(w, goal)
    assert (session.outcome, session.root_id) == ("interrupted", None)
    assert "stopped before its first task was created" in (await _harness(w, goal))[-1].text


# ---- a closed session's chain that resumes -------------------------------------------------------------------------


async def _closed_with_arrival(w: SimpleNamespace, goal: Goal) -> tuple[Intention, GoalSession]:
    root, session = await _started(w, goal)
    await _arrival(w, root, 1, decision="report", note="Listed the notes.")
    await _finish(w, root)
    assert (await w.driver.tick()).closed == 1
    return root, await _session(w, session.id)


async def test_a_resumed_chain_that_ends_again_leaves_one_row_and_the_close_stands(env_factory):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    root, closed = await _closed_with_arrival(w, goal)
    assert closed.outcome == "no_step" and closed.summary == "Listed the notes."
    await record(w.env, root)  # a late result reopens the root
    await _arrival(w, root, 2, decision="report", note="Merged two more.")
    await w.driver.tick()  # still open: nothing to say yet
    assert not [r for r in await _harness(w, goal) if "resumed" in r.text]
    await set_intention(w.env, root.id, state="closed", close_reason="resolved")
    await w.driver.tick()
    await w.driver.tick()
    rows = [r for r in await _harness(w, goal) if "resumed" in r.text]
    assert [r.text for r in rows] == ["Session 1's chain resumed after the session closed and ended again: no_step."]
    assert rows[0].session_n == 1 and rows[0].evidence[0].startswith("resumed:")
    again = await _session(w, session_id := closed.id)
    assert (again.outcome, again.summary, again.steps_closed, again.ended_at) == (
        closed.outcome,
        closed.summary,
        closed.steps_closed,
        closed.ended_at,
    )
    assert session_id == closed.id


@pytest.mark.parametrize(
    ("arrivals", "plan_step", "outcome"),
    [
        ([("ask", "resolved"), ("report", "resolved")], False, "asked"),
        ([("report", "failed_report")], False, "error"),
        ([("report", "resolved")], True, "step_closed"),
    ],
)
async def test_a_resumed_chains_ending_is_named_by_the_close_rules(env_factory, arrivals, plan_step, outcome):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    root, closed_row = await _closed_with_arrival(w, goal)
    await record(w.env, root)
    for n, (decision, how) in enumerate(arrivals, start=2):
        await add_arrival(w.env, root.id, n, decision=decision, outcome=how)
    if plan_step:
        plan = [{"id": "s1", "text": "Merge", "status": "closed", "closed_session": 1, "evidence": ["x"]}]
        await _set_goal(w, goal, plan=plan)
    await set_intention(w.env, root.id, state="closed", close_reason="resolved")
    await w.driver.tick()
    (row,) = [r for r in await _harness(w, goal) if "resumed" in r.text]
    assert row.text.endswith(f"ended again: {outcome}.")
    again = await _session(w, closed_row.id)
    assert (again.outcome, again.steps_closed, again.summary) == ("no_step", 0, "Listed the notes.")  # never rewritten


async def test_a_chain_that_resumes_twice_leaves_a_row_each_time(env_factory):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    root, _closed_row = await _closed_with_arrival(w, goal)
    for n in (2, 3):
        await record(w.env, root, generation=n)
        await add_arrival(w.env, root.id, n, decision="report")
        await set_intention(w.env, root.id, state="closed", close_reason="resolved")
        await w.driver.tick()
    assert len([r for r in await _harness(w, goal) if "resumed" in r.text]) == 2


async def test_a_resumed_chain_never_counts_toward_a_stall(env_factory):  # noqa: F811
    """The stall is read from the sessions' closing outcomes: a resumed chain that ended without a step adds none."""
    w = await _world(env_factory)
    goal = await _active(w)
    await _set_goal(w, goal, stall_limit=2)
    root, closed = await _closed_with_arrival(w, goal)
    assert closed.outcome == "no_step"
    await record(w.env, root)
    await add_arrival(w.env, root.id, 2, decision="report")
    await set_intention(w.env, root.id, state="closed", close_reason="resolved")
    report = await w.driver.tick()
    assert [r for r in await _harness(w, goal) if "resumed" in r.text]
    assert report.paused == 0 and (await _goal(w, goal)).state == "active"


async def test_two_resumed_reconciles_at_once_write_the_row_once(env_factory):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    root, closed = await _closed_with_arrival(w, goal)
    await record(w.env, root)
    await add_arrival(w.env, root.id, 2, decision="report")
    await set_intention(w.env, root.id, state="closed", close_reason="resolved")
    twin = GoalDriver(w.db, w.env.settings, w.env.heart, w.store, FakeRunner())
    async with w.db.session() as holding:
        await holding.execute(select(GoalSession).where(GoalSession.id == closed.id).with_for_update())
        both = [
            asyncio.create_task(w.driver._reconcile_resumed(closed.id)),
            asyncio.create_task(twin._reconcile_resumed(closed.id)),
        ]
        await _until_waiters(w, "brain.goal_sessions", at_least=2)
        await holding.commit()
    assert sorted(await asyncio.gather(*both)) == [0, 1]


# ---- stop conditions ---------------------------------------------------------------------------------------------


async def test_deadline_pauses_the_goal(env_factory):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    deadline = (await _goal(w, goal)).deadline_at
    assert await w.driver.apply_stop_conditions(now=deadline - timedelta(microseconds=1)) == 0
    assert await w.driver.apply_stop_conditions(now=deadline) == 1  # at the deadline, it has passed
    after = await _goal(w, goal)
    assert (after.state, after.pause_reason, after.next_wake_at) == ("paused", "deadline", None)
    assert await w.driver.apply_stop_conditions(now=deadline) == 0  # never twice
    rows = [r for r in await _harness(w, goal) if r.text.startswith("Paused")]
    assert [(r.text, r.evidence) for r in rows] == [("Paused: the deadline has passed.", ["stop:deadline"])]
    await _set_goal(w, goal, deadline_at=func.clock_timestamp() - timedelta(minutes=1))
    with pytest.raises(GoalRefused, match="deadline has passed"):
        await w.store.resume(goal.id, actor="host")
    await w.store.extend_deadline(goal.id, days=1, actor="host")
    assert (await w.store.resume(goal.id, actor="host")).state == "active"
    assert (await w.driver.tick()).paused == 0


async def test_total_budget_pauses_the_goal_and_names_the_reason(env_factory):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    (session,) = await _closed(w, goal, ["step_closed"], spent=goal.total_budget - 1)
    assert await w.driver.apply_stop_conditions() == 0
    async with w.db.session() as s:
        await s.execute(update(GoalSession).where(GoalSession.id == session.id).values(spent=goal.total_budget))
        await s.commit()
    assert await w.driver.apply_stop_conditions() == 1
    after = await _goal(w, goal)
    assert (after.state, after.pause_reason, after.next_wake_at) == ("paused", "budget", None)
    total = goal.total_budget
    assert (await _harness(w, goal))[-1].text == f"Paused: the total budget is spent ({total} of {total} tokens)."
    with pytest.raises(GoalRefused, match="total budget is spent"):
        await w.store.resume(goal.id, actor="host")
    await w.store.raise_budget(goal.id, total=total + 1, actor="host")
    assert (await w.store.resume(goal.id, actor="host")).state == "active"
    assert await w.driver.apply_stop_conditions() == 0


async def test_a_pause_waits_for_its_condition_in_the_row(env_factory, monkeypatch):  # noqa: F811
    """The owner raises the budget between the driver's read and its pause: the pause finds the condition gone in
    the row and changes nothing."""
    w = await _world(env_factory)
    goal = await _active(w)
    await _closed(w, goal, ["step_closed"], spent=goal.total_budget)
    real_pause = w.driver._pause

    async def owner_first(*args, **kwargs):
        await w.store.raise_budget(goal.id, total=goal.total_budget * 2, actor="host")
        return await real_pause(*args, **kwargs)

    monkeypatch.setattr(w.driver, "_pause", owner_first)
    assert await w.driver.apply_stop_conditions() == 0
    assert (await _goal(w, goal)).state == "active"


async def test_three_no_step_sessions_pause_the_goal_as_stalled(env_factory):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    assert goal.stall_limit == 3
    await _closed(w, goal, ["no_step", "no_step"])
    assert await w.driver.apply_stop_conditions() == 0
    await _closed(w, goal, ["no_step"])
    assert await w.driver.apply_stop_conditions() == 1
    after = await _goal(w, goal)
    assert (after.state, after.pause_reason, after.next_wake_at) == ("paused", "stalled", None)
    pause = (await _harness(w, goal))[-1]
    assert (pause.text, pause.evidence, pause.session_n) == (
        "Paused: 3 sessions in a row took no step.",
        ["stop:stalled"],
        3,
    )
    assert (await w.store.resume(goal.id, actor="host")).state == "active"
    assert await w.driver.apply_stop_conditions() == 0  # the run restarts after the pause
    await _closed(w, goal, ["no_step", "no_step"])
    assert await w.driver.apply_stop_conditions() == 0
    await _closed(w, goal, ["no_step"])
    assert await w.driver.apply_stop_conditions() == 1


async def test_asked_and_interrupted_sessions_neither_count_nor_break_a_stall(env_factory):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    await _closed(w, goal, ["no_step", "asked", "no_step", "interrupted"])
    assert await w.driver.apply_stop_conditions() == 0
    await _closed(w, goal, ["no_step"])
    assert await w.driver.apply_stop_conditions() == 1
    assert (await _goal(w, goal)).pause_reason == "stalled"


@pytest.mark.parametrize("breaker", ["step_closed", "error"])
async def test_a_step_or_an_error_breaks_a_stall(env_factory, breaker):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    await _closed(w, goal, ["no_step", "no_step", breaker, "no_step", "no_step"])
    assert await w.driver.apply_stop_conditions() == 0


async def test_three_errors_pause_the_goal(env_factory):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    await _closed(w, goal, ["error", "interrupted", "error"])
    assert await w.driver.apply_stop_conditions() == 0
    await _closed(w, goal, ["error"])
    assert await w.driver.apply_stop_conditions() == 1
    after = await _goal(w, goal)
    assert (after.state, after.pause_reason) == ("paused", "error")
    assert (await _harness(w, goal))[-1].text == "Paused: 3 sessions in a row ended in an error."
    await w.store.resume(goal.id, actor="host")
    assert await w.driver.apply_stop_conditions() == 0


@pytest.mark.parametrize("breaker", ["step_closed", "asked", "no_step"])
async def test_anything_but_interrupted_breaks_an_error_run(env_factory, breaker):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    await _closed(w, goal, ["error", "error", breaker, "error"])
    assert await w.driver.apply_stop_conditions() == 0


async def test_a_pause_for_one_reason_never_restarts_another_reasons_run(env_factory):  # noqa: F811
    """A run restarts only after a pause for its own reason: a deadline or budget pause in the middle of a stall,
    or a stall pause in the middle of an error run, leaves that run counting."""
    w = await _world(env_factory)
    stalling = await _active(w)
    await _closed(w, stalling, ["no_step", "no_step"])
    deadline = (await _goal(w, stalling)).deadline_at
    assert await w.driver.apply_stop_conditions(now=deadline) == 1  # paused for the deadline, the stall at 2
    await w.store.extend_deadline(stalling.id, days=1, actor="host")
    await w.store.resume(stalling.id, actor="host")
    await _closed(w, stalling, ["no_step"])
    assert await w.driver.apply_stop_conditions() == 1
    assert (await _goal(w, stalling)).pause_reason == "stalled"

    failing = await _active(w, title="Sort the photos")
    await _closed(w, failing, ["error", "error"])
    assert await w.driver._pause(  # a stall pause between two errors and a third one
        failing.id,
        "stalled",
        "Paused: 3 sessions in a row took no step.",
        evidence=["stop:stalled"],
        newest_session=True,
    )
    await w.store.resume(failing.id, actor="host")
    await _closed(w, failing, ["error"])
    assert await w.driver.apply_stop_conditions() == 1
    assert (await _goal(w, failing)).pause_reason == "error"


async def test_a_failed_start_pause_names_its_reason_and_leaves_the_error_run_as_it_was(env_factory, monkeypatch):  # noqa: F811
    """The pause after three failed starts carries the pause's evidence ref (the Goals screen selects its sentence by
    it) and no session: the start made none, and the number it tried is the next session's. Two error sessions before
    it and one after are still three in a row."""
    w = await _world(env_factory)
    goal = await _active(w)
    await _set_goal(w, goal, sessions_per_day=24)
    await _closed(w, goal, ["error", "error"])
    check_censors = w.env.heart.check_censors
    monkeypatch.setattr(w.env.heart, "check_censors", _refuse())
    for _attempt in range(driver_module.FAILED_STARTS_BEFORE_PAUSE):
        await _set_goal(w, goal, next_wake_at=func.clock_timestamp())
        assert await w.driver.start_session(goal.id, trigger="timer") is None
    assert ((await _goal(w, goal)).state, (await _goal(w, goal)).pause_reason) == ("paused", "error")
    pause = (await _harness(w, goal))[-1]
    assert (pause.evidence, pause.session_n) == (["stop:error"], None)
    assert pause.text.endswith("Paused: the start failed 3 times in a row.")
    monkeypatch.setattr(w.env.heart, "check_censors", check_censors)
    await w.store.resume(goal.id, actor="host")
    (third,) = await _closed(w, goal, ["error"])
    assert third.n == 3
    assert await w.driver.apply_stop_conditions() == 1
    after = await _goal(w, goal)
    assert (after.state, after.pause_reason) == ("paused", "error")
    assert (await _harness(w, goal))[-1].text == "Paused: 3 sessions in a row ended in an error."


async def test_owner_actions_and_other_rows_never_restart_or_skip_a_run(env_factory):  # noqa: F811
    """Only the driver's own pause row moves where a run starts. The owner's pause, resume, stop request and note,
    an owner cancel through the route (a pause of its own, no stop reason), and a row naming a stop reason under
    any other actor leave the run as it is; the store will not write a harness row at all."""
    w = await _cont_world(env_factory)
    goal = await _active(w)
    await _set_goal(w, goal, sessions_per_day=24)
    await _closed(w, goal, ["no_step", "no_step"])
    await w.store.pause(goal.id, actor="host")
    await w.store.resume(goal.id, actor="host")
    await w.store.add_note(goal.id, "Try the other folder.", actor="owner")
    root, session = await _started(w, goal)
    await w.runner.cancel_root(root.id, reason="not now", actor="rest")  # the owner, through the route
    await w.driver.tick()
    assert ((await _session(w, session.id)).outcome, (await _goal(w, goal)).pause_reason) == ("interrupted", "owner")
    await w.store.resume(goal.id, actor="host")
    await w.store.append_journal(goal.id, "state", "Paused.", actor="host", session_n=99, evidence=["stop:stalled"])
    with pytest.raises(GoalRefused, match="goal driver only"):
        await w.store.append_journal(
            goal.id, "state", "Paused.", actor="harness", session_n=99, evidence=["stop:stalled"]
        )
    assert await w.driver.apply_stop_conditions() == 0  # two no_step and an interrupted
    await _closed(w, goal, ["no_step"])
    assert await w.driver.apply_stop_conditions() == 1
    pause = (await _harness(w, goal))[-1]
    assert (pause.text, pause.session_n) == ("Paused: 3 sessions in a row took no step.", 4)


async def test_a_goal_waiting_for_the_owner_is_never_resumed_by_the_driver(env_factory):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    await _set_goal(w, goal, state="paused", pause_reason="waiting_for_owner", next_wake_at=func.clock_timestamp())
    await _closed(w, goal, ["no_step"] * 3, spent=goal.total_budget)
    report = await w.driver.tick()
    assert report.started == () and report.paused == 0
    after = await _goal(w, goal)
    assert (after.state, after.pause_reason) == ("paused", "waiting_for_owner")


async def test_a_paused_goal_is_not_started_even_when_next_wake_is_due(env_factory):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    await _set_goal(w, goal, state="paused", pause_reason="owner", next_wake_at=func.clock_timestamp())
    assert (await w.driver.tick()).started == ()
    assert await _sessions(w, goal) == []


# ---- budgets and pace at the start -------------------------------------------------------------------------------


async def _next_utc_midnight(w: SimpleNamespace) -> datetime:
    now = await _db_now(w)
    return datetime(now.year, now.month, now.day, tzinfo=UTC) + timedelta(days=1)


async def test_the_day_budget_holds_the_start_until_the_next_utc_day(env_factory):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    (session,) = await _closed(w, goal, ["step_closed"], spent=goal.day_budget - 1)
    await _set_goal(w, goal, next_wake_at=func.clock_timestamp())
    assert len((await w.driver.tick()).started) == 1  # one token under: it starts
    await _finish(
        w, await w.env.heart.intentions.get_for_source("subtask", (await _sessions(w, goal))[-1].first_subtask_id)
    )
    await w.driver.tick()
    async with w.db.session() as s:
        await s.execute(update(GoalSession).where(GoalSession.id == session.id).values(spent=goal.day_budget))
        await s.commit()
    await _set_goal(w, goal, next_wake_at=func.clock_timestamp())
    assert (await w.driver.tick()).started == ()
    after = await _goal(w, goal)
    assert after.state == "active" and after.next_wake_at == await _next_utc_midnight(w)


async def test_the_day_budget_counts_a_session_on_the_day_it_started(env_factory):  # noqa: F811
    """A session's spend counts for the UTC day it started, whenever its tokens came: one started yesterday and
    closed today, over today's budget, does not hold today's start."""
    w = await _world(env_factory)
    goal = await _active(w)
    midnight = await _next_utc_midnight(w) - timedelta(days=1)
    await _closed(
        w,
        goal,
        ["step_closed"],
        spent=goal.day_budget,
        started_at=midnight - timedelta(minutes=5),
        ended_at=func.clock_timestamp(),
    )
    await _set_goal(w, goal, next_wake_at=func.clock_timestamp())
    assert len((await w.driver.tick()).started) == 1


async def test_sessions_per_day_holds_the_start(env_factory):  # noqa: F811
    w = await _world(env_factory)
    goal = await _active(w)
    assert goal.sessions_per_day == 2
    yesterday = await _next_utc_midnight(w) - timedelta(days=1, minutes=5)
    await _closed(w, goal, ["step_closed"], started_at=yesterday, ended_at=yesterday)
    await _closed(w, goal, ["step_closed"])
    await _set_goal(w, goal, next_wake_at=func.clock_timestamp())
    assert len((await w.driver.tick()).started) == 1  # one today before this one
    await _finish(
        w, await w.env.heart.intentions.get_for_source("subtask", (await _sessions(w, goal))[-1].first_subtask_id)
    )
    await w.driver.tick()
    await _set_goal(w, goal, next_wake_at=func.clock_timestamp())
    assert (await w.driver.tick()).started == ()
    assert (await _goal(w, goal)).next_wake_at == await _next_utc_midnight(w)


async def _quiet_now(w: SimpleNamespace) -> dict:
    hour = (await _db_now(w)).hour
    return {"heartbeat_quiet_start": hour, "heartbeat_quiet_end": (hour + 2) % 24}


async def test_quiet_hours_hold_a_goal_that_may_not_run_then(env_factory):  # noqa: F811
    probe = await _world(env_factory)
    w = await _world(env_factory, **await _quiet_now(probe))
    quiet = await _active(w)
    wake = (await _goal(w, quiet)).next_wake_at
    assert (await w.driver.tick()).started == ()
    assert (await _goal(w, quiet)).next_wake_at == wake  # due again at the next tick
    await _set_goal(w, quiet, quiet_hours_ok=True)
    assert len((await w.driver.tick()).started) == 1


async def test_run_now_starts_inside_quiet_hours(env_factory):  # noqa: F811
    """Quiet hours are kept for the owner: the owner's run-now is not held by them, the timer is."""
    probe = await _world(env_factory)
    w = await _world(env_factory, **await _quiet_now(probe))
    goal = await _active(w)
    assert not goal.quiet_hours_ok
    assert (await w.driver.tick()).started == ()  # the timer waits
    await w.store.request_run_now(goal.id, actor="host")
    report = await w.driver.tick()
    assert len(report.started) == 1
    (session,) = await _sessions(w, goal)
    assert session.trigger == "run_now" and (await _goal(w, goal)).pending_request is None


async def test_max_active_sessions_holds_a_second_goal(env_factory):  # noqa: F811
    w = await _world(env_factory)
    first, second = await _active(w), await _active(w, title="Sort the photos")
    report = await w.driver.tick()
    assert len(report.started) == 1 and report.held == "sessions"
    assert len(await _sessions(w)) == 1
    for goal in (first, second):
        assert (await _goal(w, goal)).state == "active"


async def test_max_active_sessions_holds_across_two_drivers(env_factory):  # noqa: F811
    """Two driver objects (two processes) start two different goals at once: every active goal row is locked
    before the open sessions are counted, so only one starts."""
    w = await _world(env_factory)
    first, second = await _active(w), await _active(w, title="Sort the photos")
    twin = GoalDriver(w.db, w.env.settings, w.env.heart, w.store, FakeRunner())
    async with w.db.session() as holding:
        await holding.execute(select(Goal).where(Goal.id.in_([first.id, second.id])).with_for_update())
        starts = [
            asyncio.create_task(w.driver.start_session(first.id, trigger="timer")),
            asyncio.create_task(twin.start_session(second.id, trigger="timer")),
        ]
        await _until_waiters(w, "brain.goals", at_least=2)
        await holding.commit()
    results = await asyncio.gather(*starts)
    assert sorted(r is None for r in results) == [False, True]
    assert len(await _sessions(w)) == 1


async def _launch_logged(w: SimpleNamespace, *, session_id=None, subtask_id=None, outcome="ran", command=None):
    async with w.db.session() as s:
        s.add(
            ToolCallLog(
                agent_id=w.agent,
                session_id=session_id,
                subtask_id=subtask_id,
                call_index=0,
                work_kind="intention",
                tool_name="bash",
                tool_name_called="bash",
                input_text=command or '{"command": "bash claude-jobs/runner.sh launch nous prompt.md"}',
                outcome=outcome,
            )
        )
        await s.commit()


async def test_job_launches_over_the_cap_hold_the_start_when_the_call_log_is_on(env_factory):  # noqa: F811
    w = await _world(env_factory, tool_call_log_enabled=True)
    goal = await _active(w)
    assert goal.jobs_per_day == 1
    await _set_goal(w, goal, sessions_per_day=24)  # only the job cap can hold the third start
    root, session = await _started(w, goal)
    await _launch_logged(w, session_id=f"intent-{root.id}", outcome="refused")  # never ran
    await _launch_logged(w, session_id="intent-" + str(uuid.uuid4()))  # another chain's
    await _launch_logged(w, session_id=f"intent-{root.id}", command='{"command": "ls claude-jobs"}')
    await _finish(w, root)
    await w.driver.tick()
    await _set_goal(w, goal, next_wake_at=func.clock_timestamp())
    assert len((await w.driver.tick()).started) == 1  # nothing counted yet: the cap is one a day
    second = (await _sessions(w, goal))[-1]
    await _launch_logged(w, subtask_id=second.first_subtask_id)  # a launch by the session's first task
    await _finish(w, await w.env.heart.intentions.get_for_source("subtask", second.first_subtask_id))
    await w.driver.tick()
    await _set_goal(w, goal, next_wake_at=func.clock_timestamp())
    assert (await w.driver.tick()).started == ()  # one launch counted: the cap of one a day holds the start
    await _launch_logged(w, session_id=f"intent-{second.root_id}")  # one by the session's decision turn
    async with w.db.session() as s:
        assert await spend.jobs_launched_today(s, w.agent, goal.id, w.env.settings, now=await _db_now(w)) == 2


RUNNER = "/workspace/claude-jobs/runner.sh"
# Launches the lineage shell's policy accepts (it compares shell words), however the words are spaced or quoted.
LAUNCH_SPELLINGS = [
    f"{RUNNER}  launch nous --prompt-file /workspace/notes/p.md",  # two spaces
    f"{RUNNER}\tlaunch nous --prompt-file /workspace/notes/p.md",  # a tab
    f'{RUNNER} "launch" nous --prompt-file /workspace/notes/p.md',  # the verb in quotes
]
# Commands that name runner.sh and launch without launching a job.
NOT_LAUNCHES = [
    f"{RUNNER} status job-20261115-101500-0a1b2c3d",
    f"echo {RUNNER} launch nous",
]


def _logged_input(command: str) -> str:
    """The tool-call log's text of a bash call: its input as JSON (the runner's ``json.dumps``)."""
    return json.dumps({"command": command}, ensure_ascii=False)


@pytest.mark.parametrize("command", LAUNCH_SPELLINGS)
def test_a_launch_counts_however_its_words_are_spaced_or_quoted(command):
    assert spend.launches_job(_logged_input(command)) is True


@pytest.mark.parametrize("command", NOT_LAUNCHES)
def test_a_command_that_only_names_the_runner_and_launch_is_not_a_launch(command):
    assert spend.launches_job(_logged_input(command)) is False


def test_a_launch_whose_logged_text_was_clipped_still_counts():
    """A long inline prompt: the log keeps the head and the tail of the JSON, which no longer parses."""
    command = f"{RUNNER}\tlaunch nous " + '"' + "fix the notes " * 400 + '"'
    text = detail_text(_logged_input(command), 2000)
    with pytest.raises(ValueError):
        json.loads(text)
    assert spend.launches_job(text) is True


async def test_the_days_job_count_reads_the_shell_words(env_factory):  # noqa: F811
    """Three launches spelled three ways count; a status call and an echo that names the words do not."""
    w = await _world(env_factory, tool_call_log_enabled=True)
    goal = await _active(w)
    root, _session_row = await _started(w, goal)
    for command in LAUNCH_SPELLINGS + NOT_LAUNCHES:
        await _launch_logged(w, session_id=f"intent-{root.id}", command=_logged_input(command))
    async with w.db.session() as s:
        assert await spend.jobs_launched_today(s, w.agent, goal.id, w.env.settings, now=await _db_now(w)) == 3


async def test_job_launches_are_not_counted_when_the_call_log_is_off(env_factory):  # noqa: F811
    w = await _world(env_factory, tool_call_log_enabled=False)
    goal = await _active(w)
    root, _session_row = await _started(w, goal)
    for _ in range(3):
        await _launch_logged(w, session_id=f"intent-{root.id}")
    await _finish(w, root)
    await w.driver.tick()
    await _set_goal(w, goal, next_wake_at=func.clock_timestamp())
    assert len((await w.driver.tick()).started) == 1


async def test_the_refresh_updates_its_session_rows_in_id_order(env_factory):  # noqa: F811
    """Two processes refreshing the same sessions take the rows' locks in one order: every UPDATE of a session's spend
    is issued in id order. The restart catch-up reads the closed sessions newest close first; the ids here run the
    other way, so an update in the order read is not in id order."""
    w = await _world(env_factory)
    goal = await _active(w)
    await w.store.pause(goal.id, actor="host")  # no session starts
    ids = sorted((uuid.uuid4() for _ in range(5)), reverse=True)  # the newest close has the highest id
    now = await _db_now(w)
    long_ago = now - timedelta(hours=w.env.settings.intention_root_ttl_hours + 24)
    for n, session_id in enumerate(ids, start=1):
        root = await put_intention(w.env, state="closed", deadline=now - timedelta(days=5))
        await add_arrival(w.env, root.id, 1, tokens=(10 * n, n))
        async with w.db.session() as s:
            s.add(
                GoalSession(
                    id=session_id,
                    agent_id=w.agent,
                    goal_id=goal.id,
                    n=n,
                    trigger="timer",
                    first_subtask_id=uuid.uuid4(),
                    root_id=root.id,
                    started_at=long_ago - timedelta(hours=1),
                    ended_at=long_ago - timedelta(minutes=n),
                    outcome="no_step",
                    spent=0,
                )
            )
            await s.commit()
    updated: list[uuid.UUID] = []

    def seen(conn, cursor, statement, parameters, context, executemany):
        if " ".join(statement.split()).lower().startswith("update brain.goal_sessions set spent"):
            for value in parameters or ():
                try:
                    updated.append(uuid.UUID(str(value)))
                except ValueError:
                    pass

    engine = w.db.engine.sync_engine
    event.listen(engine, "before_cursor_execute", seen)
    try:
        assert await w.driver.refresh_sessions() == 5
    finally:
        event.remove(engine, "before_cursor_execute", seen)
    assert updated == sorted(ids)


# ---- a reactivated lineage DAG -----------------------------------------------------------------------------------


async def test_a_lineage_dag_that_leaves_a_terminal_state_is_followed_again(env_factory):  # noqa: F811
    """A failed DAG of a closed session's lineage is retried (``retry_node`` sets it running again) after the
    session stopped being read and after its root's deadline: the session is read again until the DAG settles."""
    w = await _world(env_factory)
    goal = await _active(w)
    root, session = await _started(w, goal)
    await w.env.heart.subtasks.complete(uuid.UUID(root.source_id), RESULT)
    dag, _store = await make_dag(w.env, parent=root, status="failed")
    async with w.db.session() as s:
        await s.execute(update(DAGNode).where(DAGNode.dag_id == dag.id).values(status="failed", tokens_counted=True))
        await s.execute(update(ExecutionDAG).where(ExecutionDAG.id == dag.id).values(tokens_consumed=40))
        await s.commit()
    dag_intention = await intention_of(w.env, "dag", dag.id)
    await set_intention(w.env, dag_intention.id, state="closed", close_reason="resolved")
    await _finish(w, root, deadline=datetime.now(UTC) - timedelta(minutes=1))
    assert (await w.driver.tick()).closed == 1
    await w.driver.tick()
    assert session.id not in w.driver._following and (await _session(w, session.id)).spent == 40
    async with w.db.session() as s:  # the retry: the DAG runs again and adds to its total
        await s.execute(
            update(ExecutionDAG).where(ExecutionDAG.id == dag.id).values(status="running", tokens_consumed=90)
        )
        await s.commit()
    await w.driver.tick()
    assert (await _session(w, session.id)).spent == 90
    assert session.id in w.driver._following
    async with w.db.session() as s:
        await s.execute(
            update(ExecutionDAG)
            .where(ExecutionDAG.id == dag.id)
            .values(status="completed", completed_at=func.clock_timestamp() - timedelta(days=1), tokens_consumed=95)
        )
        await s.commit()
    await w.driver.tick()
    assert (await _session(w, session.id)).spent == 95
    assert session.id not in w.driver._following
