"""F114 goals: a session's spend is its root's token budget as root_limits counts it."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from f099_support import (
    CONT,
    add_arrival,
    env_factory,  # noqa: F401
    make_child,
    make_dag,
    make_root,
    set_intention,
)
from sqlalchemy import func, select, update

from nous.brain import continuation
from nous.config import Settings
from nous.goals import spend
from nous.goals.store import GoalStore
from nous.storage.models import ExecutionDAG, GoalSession, Subtask

pytestmark = pytest.mark.postgres_only


async def test_spend_equals_root_limits_tokens(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root = await make_root(env)
    child = await make_child(env, root)
    await set_intention(env, root.id, failed_tokens=300)
    async with env.db.session() as s:
        await s.execute(
            update(Subtask).where(Subtask.id == uuid.UUID(child.source_id)).values(tokens_in=100, tokens_out=50)
        )
        await s.commit()
    dag, _store = await make_dag(env, parent=root)
    async with env.db.session() as s:
        await s.execute(update(ExecutionDAG).where(ExecutionDAG.id == dag.id).values(tokens_consumed=70))
        await s.commit()
    await add_arrival(env, root.id, 1, tokens=(20, 10))
    async with env.db.session() as s:
        spent = await spend.session_spent(s, env.agent, [root.id])
        limits = await continuation.root_limits(s, env.agent, root.id, settings=env.settings)
    assert spent == {root.id: limits.tokens} == {root.id: 300 + 150 + 70 + 30}


async def test_spend_of_an_unknown_root_is_zero(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    ghost = uuid.uuid4()
    async with env.db.session() as s:
        assert await spend.session_spent(s, env.agent, [ghost]) == {ghost: 0}
        assert await spend.session_spent(s, env.agent, []) == {}


async def test_goal_spent_is_the_sum_of_the_sessions_and_today_counts_from_the_utc_day_start(db):
    agent = f"goals-spend-{uuid.uuid4().hex[:8]}"
    store = GoalStore(db, agent, Settings(_env_file=None, agent_id=agent, telegram_chat_id=""))
    goal = await store.create(
        title="Tidy", objective="Tidy the notes", done_when="Tidy", proposed_by="host", origin_channel="telegram:1"
    )
    async with db.session() as s:
        midnight = (await s.execute(select(func.date_trunc("day", func.clock_timestamp(), "UTC")))).scalar_one()
        for n, (started, spent) in enumerate(
            [(midnight - timedelta(seconds=1), 700), (midnight, 40), (midnight + timedelta(hours=1), 2)], start=1
        ):
            s.add(
                GoalSession(
                    agent_id=agent,
                    goal_id=goal.id,
                    n=n,
                    trigger="timer",
                    first_subtask_id=uuid.uuid4(),
                    started_at=started,
                    ended_at=started,
                    outcome="no_step",
                    spent=spent,
                )
            )
        await s.commit()
    async with db.session() as s:
        assert await spend.goal_spent(s, agent, goal.id) == (742, 42)
        assert await spend.goal_spent(s, agent, uuid.uuid4()) == (0, 0)
        assert await spend.goal_spent(s, "another-agent", goal.id) == (0, 0)
