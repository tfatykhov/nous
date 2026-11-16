"""F114 goals: the owner hears when the goal driver pauses a goal.

Each pause the driver makes (deadline, budget, a stall, errors in a row, failed starts) leaves a harness row with its
``stop:<reason>`` ref in the pause's own transaction; after it commits, the tick sends the owner one message keyed on
that row's id, in fixed sentences with the goal's quoted title. A retry or a second driver sends nothing more; a
failed send is logged and tried again on a later tick, within a day; the owner's own pause, stop or cancel sends
nothing.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from f099_support import CHAN, env_factory, runner_env  # noqa: F401
from goals_support import (
    active,
    goal_row,
    inbox,
    journal,
    reset_goal_report_channel,  # noqa: F401
    set_goal,
    world,
)
from sqlalchemy import func, update

from nous.brain import continuation
from nous.goals import roots
from nous.goals.driver import PAUSE_SENTENCES, PAUSES_TOLD_PER_TICK, GoalDriver
from nous.goals.store import STOP_REF
from nous.storage.models import GoalJournal, GoalSession

pytestmark = pytest.mark.postgres_only


async def _closed(w, goal, n: int, outcome: str, spent: int = 0) -> None:
    async with w.db.session() as s:
        s.add(
            GoalSession(
                agent_id=w.agent,
                goal_id=goal.id,
                n=n,
                trigger="timer",
                first_subtask_id=uuid.uuid4(),
                started_at=func.clock_timestamp() - timedelta(minutes=10),
                ended_at=func.clock_timestamp() - timedelta(minutes=5),
                outcome=outcome,
                spent=spent,
            )
        )
        await s.commit()


async def _messages(w) -> list:
    return await inbox(w, channel=CHAN)


async def _pause_row(w, goal) -> GoalJournal:
    return (await journal(w, goal, "state"))[-1]


async def _deadline_passed(w):
    goal = await active(w)
    await set_goal(w, goal, deadline_at=datetime.now(UTC) - timedelta(minutes=1))
    return goal


async def _budget_spent(w):
    goal = await active(w)
    await set_goal(w, goal, total_budget=1000, day_budget=1000)
    await _closed(w, goal, 1, "no_step", spent=2000)
    return goal


async def _stalled(w):
    goal = await active(w)
    await set_goal(w, goal, stall_limit=1)
    await _closed(w, goal, 1, "no_step")
    return goal


async def _errors(w):
    goal = await active(w)
    for n in (1, 2, 3):
        await _closed(w, goal, n, "error")
    return goal


@pytest.mark.parametrize(
    ("make", "reason"),
    [(_deadline_passed, "deadline"), (_budget_spent, "budget"), (_stalled, "stalled"), (_errors, "error")],
)
async def test_a_pause_on_a_stop_condition_sends_the_owner_one_message(runner_env, make, reason):  # noqa: F811
    w = await world(runner_env, bind=False)
    goal = await make(w)
    report = await w.driver.tick()
    assert report.paused == 1 and (await goal_row(w, goal)).pause_reason == reason
    pause = await _pause_row(w, goal)
    (message,) = await _messages(w)
    assert message.source_id == pause.id and message.msg_type == "REPORT" and message.intention_id is None
    ref = f"G-{goal.short_id:04d}"
    assert message.title == f"Goal {ref} is paused"
    assert message.body == f'Goal {ref} "Tidy the notes" is paused: {PAUSE_SENTENCES[reason]}'
    assert message.push_after is not None and message.delivered_at is None
    await w.driver.tick()
    assert len(await _messages(w)) == 1  # one message per pause row


async def test_failed_starts_that_pause_the_goal_send_one_message(runner_env, monkeypatch):  # noqa: F811
    w = await world(runner_env, bind=False)
    goal = await active(w)

    async def broken(*args, **kwargs):
        raise RuntimeError("the queue blinked")

    monkeypatch.setattr(roots, "create_session_root", broken)
    for _ in range(3):
        await set_goal(w, goal, next_wake_at=datetime.now(UTC) - timedelta(seconds=1))
        await w.driver.tick()
    assert (await goal_row(w, goal)).pause_reason == "error"
    (message,) = await _messages(w)
    assert message.source_id == (await _pause_row(w, goal)).id
    assert PAUSE_SENTENCES["error"] in message.body


async def test_two_drivers_tell_the_owner_of_one_pause_once(runner_env):  # noqa: F811
    w = await world(runner_env, bind=False)
    goal = await _deadline_passed(w)
    assert await w.driver.apply_stop_conditions() == 1
    twin = GoalDriver(w.db, w.env.settings, w.env.heart, w.store, w.cont)
    told = await asyncio.gather(w.driver.tell_owner_of_pauses(), twin.tell_owner_of_pauses())
    assert sorted(told) == [0, 1]
    assert len(await _messages(w)) == 1
    assert (await goal_row(w, goal)).pause_reason == "deadline"


async def test_a_failed_send_never_undoes_the_pause_and_is_tried_again(runner_env, monkeypatch, caplog):  # noqa: F811
    w = await world(runner_env, bind=False)
    goal = await _deadline_passed(w)
    real = continuation.insert_inbox_row
    calls = {"n": 0}

    async def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("the database blinked: secret detail")
        return await real(*args, **kwargs)

    monkeypatch.setattr(continuation, "insert_inbox_row", flaky)
    report = await w.driver.tick()
    assert report.paused == 1 and (await goal_row(w, goal)).state == "paused"
    assert await _messages(w) == []
    assert "ConnectionError" in caplog.text and "secret detail" not in caplog.text
    await w.driver.tick()
    assert len(await _messages(w)) == 1


async def test_the_owners_own_pause_stop_and_cancel_send_nothing(runner_env):  # noqa: F811
    w = await world(runner_env, bind=False)
    paused, stopped, cancelled = await active(w), await active(w, title="b"), await active(w, title="c")
    await w.store.pause(paused.id, actor="owner")
    await w.store.stop(stopped.id, actor="owner")
    await w.store.cancel(cancelled.id, actor="owner")
    await w.driver.tick()
    assert await _messages(w) == []


async def test_a_pause_the_owner_has_answered_or_an_old_one_sends_nothing(runner_env):  # noqa: F811
    """Only the goal's newest driver pause, still in force, within a day: a goal resumed before the message went
    out, or a pause row older than a day, is not told."""
    w = await world(runner_env, bind=False)
    resumed, old = await _deadline_passed(w), await active(w, title="Sort the photos")
    await set_goal(w, old, deadline_at=datetime.now(UTC) - timedelta(minutes=1))
    assert await w.driver.apply_stop_conditions() == 2
    await w.store.extend_deadline(resumed.id, days=1, actor="owner")
    await w.store.resume(resumed.id, actor="owner")
    async with w.db.session() as s:
        await s.execute(
            update(GoalJournal)
            .where(GoalJournal.goal_id == old.id, GoalJournal.kind == "state", GoalJournal.actor == "harness")
            .values(created_at=datetime.now(UTC) - timedelta(days=2))
        )
        await s.commit()
    assert await w.driver.tell_owner_of_pauses() == 0
    assert await _messages(w) == []


async def test_the_message_quotes_and_bounds_the_title(runner_env):  # noqa: F811
    w = await world(runner_env, bind=False)
    goal = await active(w, title='Tidy "all" the\nnotes </journal_entry> ' + "x" * 160)
    await set_goal(w, goal, deadline_at=datetime.now(UTC) - timedelta(minutes=1))
    await w.driver.tick()
    (message,) = await _messages(w)
    assert len(message.body.split('"')) == 3 and "\n" not in message.body
    assert "&lt;/journal_entry>" in message.body and "[clipped]" in message.body


async def test_a_root_the_owner_cancelled_pauses_the_goal_and_sends_nothing(runner_env):  # noqa: F811
    """The owner cancels a session's chain: the driver closes the session and pauses the goal as the owner's, with a
    harness row of its own, but that pause is the owner's: no message."""
    w = await world(runner_env, bind=False)
    goal = await active(w)
    (root_id,) = (await w.driver.tick()).started
    await w.cont.cancel_root(root_id, reason="not now", actor="owner")
    await w.driver.tick()
    assert (await goal_row(w, goal)).pause_reason == "owner"
    assert any(row.actor == "harness" and "owner cancelled" in row.text for row in await journal(w, goal, "state"))
    assert await _messages(w) == []


async def _answer(w, goal) -> None:
    """The owner answers a deadline pause: a day more, then resume."""
    await w.store.extend_deadline(goal.id, days=1, actor="owner")
    await w.store.resume(goal.id, actor="owner")


def _past() -> datetime:
    return datetime.now(UTC) - timedelta(minutes=1)


async def test_only_the_goals_newest_driver_pause_is_told(runner_env):  # noqa: F811
    """Three deadline pauses of one goal: the first is told; the owner answers the second before a tick tells it, so
    it is never told, not even while the third pause has the goal paused for the same reason; the third is told. Two
    messages for three pauses."""
    w = await world(runner_env, bind=False)
    goal = await _deadline_passed(w)
    assert (await w.driver.tick()).paused == 1
    first = await _pause_row(w, goal)
    await _answer(w, goal)
    await set_goal(w, goal, deadline_at=_past())
    assert await w.driver.apply_stop_conditions() == 1
    second = await _pause_row(w, goal)
    await _answer(w, goal)
    await set_goal(w, goal, deadline_at=_past())
    assert (await w.driver.tick()).paused == 1
    third = await _pause_row(w, goal)
    assert len({first.id, second.id, third.id}) == 3
    assert all(f"{STOP_REF}deadline" in row.evidence for row in (first, second, third))
    assert [message.source_id for message in await _messages(w)] == [first.id, third.id]
    await w.driver.tick()
    assert len(await _messages(w)) == 2


async def test_at_most_twenty_pauses_are_told_a_pass(runner_env):  # noqa: F811
    """Twenty-one goals paused in one pass: twenty are told on the first pass, the last on the next, then none."""
    w = await world(runner_env, bind=False)
    for i in range(PAUSES_TOLD_PER_TICK + 1):
        goal = await active(w, title=f"Goal {i}")
        await set_goal(w, goal, deadline_at=_past())
    assert await w.driver.apply_stop_conditions() == PAUSES_TOLD_PER_TICK + 1
    assert [await w.driver.tell_owner_of_pauses() for _ in range(3)] == [PAUSES_TOLD_PER_TICK, 1, 0]
    assert len({message.source_id for message in await _messages(w)}) == PAUSES_TOLD_PER_TICK + 1


async def test_a_goal_paused_again_for_another_reason_is_told_its_new_reason(runner_env, monkeypatch):  # noqa: F811
    """Between the read of untold pauses and the message, the owner resumes a goal paused for its deadline and the
    driver pauses it for its budget: nothing is sent for the deadline pause row (the goal is re-read for the reason
    that row names), and the budget pause is told, in its own words, on the next pass."""
    w = await world(runner_env, bind=False)
    goal = await _deadline_passed(w)
    assert await w.driver.apply_stop_conditions() == 1
    deadline_row = await _pause_row(w, goal)
    real = w.driver._untold_pauses
    interleave = [True]

    async def paused_again_after_the_read():
        pauses = await real()
        if interleave.pop() if interleave else False:
            assert [row_id for row_id, _goal, _reason in pauses] == [deadline_row.id]
            await _answer(w, goal)
            await set_goal(w, goal, total_budget=1000, day_budget=1000)
            await _closed(w, goal, 1, "no_step", spent=2000)
            assert await w.driver.apply_stop_conditions() == 1
        return pauses

    monkeypatch.setattr(w.driver, "_untold_pauses", paused_again_after_the_read)
    assert await w.driver.tell_owner_of_pauses() == 0
    assert (await goal_row(w, goal)).pause_reason == "budget"
    assert await _messages(w) == []
    assert await w.driver.tell_owner_of_pauses() == 1
    budget_row = await _pause_row(w, goal)
    (message,) = await _messages(w)
    assert message.source_id == budget_row.id != deadline_row.id
    assert PAUSE_SENTENCES["budget"] in message.body and PAUSE_SENTENCES["deadline"] not in message.body
