"""F114 goals: the store under real concurrency (separate connections), and the guard behind the row lock."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select, text

from nous.config import Settings
from nous.goals.store import GoalRefused, GoalStore
from nous.storage.models import Goal, GoalJournal, GoalSession

pytestmark = pytest.mark.postgres_only


def _store(db) -> GoalStore:
    agent = f"goals-conc-{uuid.uuid4().hex[:8]}"
    return GoalStore(db, agent, Settings(_env_file=None, agent_id=agent, telegram_chat_id=""))


async def _draft(store: GoalStore) -> Goal:
    return await store.create(
        title="Tidy the notes",
        objective="Merge the duplicate notes",
        done_when="None are left",
        proposed_by="host",
        origin_channel="telegram:4242",
    )


async def _active(store: GoalStore) -> Goal:
    return await store.approve((await _draft(store)).id, actor="host")


async def _state_rows(db, goal: Goal) -> list[GoalJournal]:
    async with db.session() as s:
        query = select(GoalJournal).where(GoalJournal.goal_id == goal.id, GoalJournal.kind == "state")
        return list((await s.execute(query.order_by(GoalJournal.seq))).scalars())


async def _wait_for_a_waiter_on_goals(db, timeout: float = 10.0) -> None:
    """Until another connection is blocked on a lock in a statement on brain.goals."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    query = text(
        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
        "AND wait_event_type = 'Lock' AND query ILIKE '%brain.goals%'"
    )
    while True:
        async with db.session() as s:
            if (await s.execute(query)).scalar_one():
                return
        assert loop.time() < deadline, "no statement waited on the goal row"
        await asyncio.sleep(0.02)


async def test_two_approvals_at_once_make_one_approval(db):
    store = _store(db)
    goal = await _draft(store)
    results = await asyncio.gather(
        store.approve(goal.id, actor="host"), store.approve(goal.id, actor="owner"), return_exceptions=True
    )
    assert sorted(type(r).__name__ for r in results) == ["Goal", "GoalRefused"]
    assert [row.text.startswith("Approved") for row in await _state_rows(db, goal)] == [False, True]


async def test_pause_against_cancel_ends_cancelled_with_one_row_per_change_made(db):
    store = _store(db)
    goal = await _active(store)
    paused, cancelled = await asyncio.gather(
        store.pause(goal.id, actor="host"), store.cancel(goal.id, actor="host"), return_exceptions=True
    )
    assert not isinstance(cancelled, Exception)  # a cancel takes an active or a paused goal
    assert isinstance(paused, (Goal, GoalRefused))  # a pause after the cancel is refused, nothing else
    after = await store.get(goal.id)
    assert (after.state, after.pause_reason, after.next_wake_at) == ("cancelled", None, None)
    made = 2 + (1 if isinstance(paused, Goal) else 0)  # created and approved, the cancel, the pause if it ran
    assert len(await _state_rows(db, goal)) == made + 1


@pytest.mark.parametrize(("change", "asked"), [("stop", "stop"), ("pause", None)])
async def test_a_change_waiting_behind_a_session_start_sees_the_session(db, change, asked):
    """The driver's order: the goal row FOR UPDATE, then the session row, then commit. A stop that waited on that
    lock must see the session and ask for it to be stopped; it never pauses with the session left running
    unasked. A pause lets it run."""
    store = _store(db)
    goal = await _active(store)
    async with db.session() as starting:
        await starting.execute(select(Goal).where(Goal.id == goal.id).with_for_update())
        starting.add(
            GoalSession(agent_id=store.agent_id, goal_id=goal.id, n=1, trigger="timer", first_subtask_id=uuid.uuid4())
        )
        await starting.flush()
        waiting = asyncio.create_task(getattr(store, change)(goal.id, actor="host"))
        await _wait_for_a_waiter_on_goals(db)
        await starting.commit()
    after = await waiting
    assert (after.state, after.pause_reason, after.pending_request) == ("paused", "owner", asked)
    assert (await _state_rows(db, goal))[-1].session_n == (1 if asked else None)


async def test_a_wake_delay_waiting_behind_a_pause_changes_nothing(db):
    store = _store(db)
    goal = await _active(store)
    async with db.session() as pausing:
        await pausing.execute(select(Goal).where(Goal.id == goal.id).with_for_update())
        await pausing.execute(
            text("UPDATE brain.goals SET state = 'paused', pause_reason = 'owner', next_wake_at = NULL WHERE id = :i"),
            {"i": goal.id},
        )
        waiting = asyncio.create_task(store.delay_wake(goal.id, seconds=300, seen=goal.next_wake_at))
        await _wait_for_a_waiter_on_goals(db)
        await pausing.commit()
    assert await waiting is False
    after = await store.get(goal.id)
    assert (after.state, after.pause_reason, after.next_wake_at) == ("paused", "owner", None)


async def test_a_state_change_between_the_read_and_the_update_is_refused(db, monkeypatch):
    """The state guard and the row count check stand behind the row lock. Nothing else can change a locked row,
    so this changes it inside the store's own transaction, between its read and its update."""
    store = _store(db)
    goal = await _active(store)
    count = len(await _state_rows(db, goal))
    real_open_session = store._open_session

    async def change_then_look(s, goal_id):
        await s.execute(
            text("UPDATE brain.goals SET state = 'paused', pause_reason = 'owner', next_wake_at = NULL WHERE id = :i"),
            {"i": goal_id},
        )
        return await real_open_session(s, goal_id)

    monkeypatch.setattr(store, "_open_session", change_then_look)
    with pytest.raises(GoalRefused, match="changed meanwhile"):
        await store.stop(goal.id, actor="host")
    after = await store.get(goal.id)
    assert (after.state, after.pending_request) == ("active", None)  # rolled back, the injected change too
    assert len(await _state_rows(db, goal)) == count
