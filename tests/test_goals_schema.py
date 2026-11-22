"""F114 goals: what migration 113 holds by itself (CHECKs, the one-open-session index, the short id)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from nous.storage.models import Goal

pytestmark = pytest.mark.postgres_only

_GOAL = {
    "title": "Tidy the notes",
    "objective": "Merge the duplicate notes",
    "done_when": "No duplicates are left",
    "preset": "small",
    "total_budget": 2_000_000,
    "day_budget": 500_000,
    "deadline_days": 3,
    "sessions_per_day": 2,
    "min_interval_seconds": 7200,
    "jobs_per_day": 1,
    "proposed_by": "owner",
}


def _agent() -> str:
    return f"goals-schema-{uuid.uuid4().hex[:8]}"


async def _insert_goal(db, agent: str, **over) -> uuid.UUID:
    values = {**_GOAL, **over, "agent_id": agent}
    columns = ", ".join(values)
    params = ", ".join(f":{name}" for name in values)
    async with db.session() as s:
        goal_id = (
            await s.execute(text(f"INSERT INTO brain.goals ({columns}) VALUES ({params}) RETURNING id"), values)
        ).scalar_one()
        await s.commit()
    return goal_id


async def _refused(db, agent: str, **over) -> None:
    with pytest.raises(IntegrityError):
        await _insert_goal(db, agent, **over)


async def _insert_session(db, agent: str, goal_id: uuid.UUID, n: int, *, ended: bool = False) -> None:
    async with db.session() as s:
        await s.execute(
            text(
                "INSERT INTO brain.goal_sessions (agent_id, goal_id, n, trigger, first_subtask_id, ended_at, outcome) "
                "VALUES (:a, :g, :n, 'timer', :f, :ended_at, :outcome)"
            ),
            {
                "a": agent,
                "g": goal_id,
                "n": n,
                "f": uuid.uuid4(),
                "ended_at": datetime.now(UTC) if ended else None,
                "outcome": "no_step" if ended else None,
            },
        )
        await s.commit()


async def _insert_journal(db, agent: str, goal_id: uuid.UUID, body: str, evidence: str = "[]") -> None:
    async with db.session() as s:
        await s.execute(
            text(
                "INSERT INTO brain.goal_journal (agent_id, goal_id, kind, actor, text, evidence) "
                "VALUES (:a, :g, 'progress', 'chain', :t, CAST(:e AS JSONB))"
            ),
            {"a": agent, "g": goal_id, "t": body, "e": evidence},
        )
        await s.commit()


async def test_the_open_session_index_allows_one_open_session_per_goal(db):
    agent = _agent()
    goal_id = await _insert_goal(db, agent)
    await _insert_session(db, agent, goal_id, 1, ended=True)
    await _insert_session(db, agent, goal_id, 2)  # one open and one closed: fine
    with pytest.raises(IntegrityError):
        await _insert_session(db, agent, goal_id, 3)  # a second open session of the same goal


async def test_state_and_pause_reason_move_together(db):
    agent = _agent()
    deadline = datetime.now(UTC) + timedelta(days=3)
    await _refused(db, agent, state="paused", deadline_at=deadline)
    await _refused(db, agent, state="active", pause_reason="owner", deadline_at=deadline)
    await _insert_goal(db, agent, state="paused", pause_reason="owner", deadline_at=deadline)


async def test_a_goal_needs_total_budget_and_deadline(db):
    agent = _agent()
    await _refused(db, agent, total_budget=0, day_budget=0)
    await _refused(db, agent, deadline_days=0)
    await _refused(db, agent, state="active")  # active with no deadline_at
    await _insert_goal(db, agent, state="cancelled")  # a cancelled draft never had one


async def test_day_budget_cannot_exceed_total(db):
    agent = _agent()
    await _refused(db, agent, total_budget=1000, day_budget=1001)
    await _insert_goal(db, agent, total_budget=1000, day_budget=1000)


async def test_journal_text_is_bounded(db):
    agent = _agent()
    goal_id = await _insert_goal(db, agent)
    await _insert_journal(db, agent, goal_id, "x" * 4000)
    with pytest.raises(IntegrityError):
        await _insert_journal(db, agent, goal_id, "x" * 4001)
    with pytest.raises(IntegrityError):
        await _insert_journal(db, agent, goal_id, "ok", evidence="[" + ",".join(['"r"'] * 11) + "]")


async def test_pending_request_and_its_time_move_together(db):
    agent = _agent()
    await _refused(db, agent, pending_request="stop")
    await _refused(db, agent, pending_request_at=datetime.now(UTC))
    await _insert_goal(db, agent, pending_request="stop", pending_request_at=datetime.now(UTC))


async def test_mode_defaults_to_contained_and_commands_needs_its_hours(db):
    agent = _agent()
    goal_id = await _insert_goal(db, agent)
    async with db.session() as s:
        assert (await s.execute(text("SELECT mode FROM brain.goals WHERE id = :i"), {"i": goal_id})).scalar_one() == (
            "contained"
        )
    await _refused(db, agent, mode="full_autonomy", mode_until=datetime.now(UTC) + timedelta(hours=1))
    await _refused(db, agent, mode="commands")
    await _insert_goal(db, agent, mode="commands", mode_hours=1)


async def test_daily_at_minute_defaults_to_null_and_holds_a_minute_of_the_day(db):
    """Goal pacing (migration 120): NULL runs as often as the limits allow; a number is a minute of the day."""
    agent = _agent()
    goal_id = await _insert_goal(db, agent)
    async with db.session() as s:
        found = await s.execute(text("SELECT daily_at_minute FROM brain.goals WHERE id = :i"), {"i": goal_id})
        assert found.scalar_one() is None
    for minute in (0, 1439):
        await _insert_goal(db, agent, daily_at_minute=minute)
    for minute in (-1, 1440):
        await _refused(db, agent, daily_at_minute=minute)


async def test_short_id_is_assigned_on_insert(db):
    agent = _agent()
    values = {key: value for key, value in _GOAL.items()}
    async with db.session() as s:
        goal = Goal(agent_id=agent, **values)
        s.add(goal)
        await s.commit()
        first = goal.short_id
        second = Goal(agent_id=agent, **values)
        s.add(second)
        await s.commit()
    assert isinstance(first, int) and first >= 1
    assert second.short_id > first
