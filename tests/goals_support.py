"""Shared builders for the F114 goal tests of the chain (PR 1c).

``world`` builds a goal store, a driver and a real continuation runner on one fresh agent of ``runner_env`` (a
scripted model).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from f099_support import CHAN, CONT, RESULT, record
from sqlalchemy import select, update

from nous.brain import continuation, intentions
from nous.goals.driver import GoalDriver
from nous.goals.store import GoalStore
from nous.handlers.continuation_runner import ContinuationRunner
from nous.storage.models import Goal, GoalJournal, GoalSession, Intention, ResultInbox

GOALS = {**CONT, "subtask_enabled": True, "goals_enabled": True}  # the flag and every setting it needs


async def world(runner_env, *steps, bind: bool = True, **settings) -> SimpleNamespace:
    """A goal world on a ``runner_env`` agent: ``store``, ``driver`` and ``cont`` (a real ContinuationRunner).
    With ``bind`` the chain's hooks are bound as ``main.py`` binds them (the runner's goal turns and the report
    channel), and ``chain`` is the bound ``GoalChain``."""
    env = await runner_env(*steps, **{**GOALS, **settings})
    cont = ContinuationRunner(
        database=env.db,
        settings=env.settings,
        runner=env.runner,
        heart=env.heart,
        brain=env.brain,
        bus=env.bus,
        dispatcher=env.dispatcher,
    )
    store = GoalStore(env.db, env.agent, env.settings)
    driver = GoalDriver(env.db, env.settings, env.heart, store, cont)
    chain = None
    if bind:
        from nous.goals.chain import GoalChain, report_channel

        chain = GoalChain(env.db, env.settings, store)
        cont.set_goal_turns(chain.turn_context)
        continuation.set_goal_report_channel(report_channel)
    return SimpleNamespace(
        env=env, db=env.db, agent=env.agent, store=store, driver=driver, cont=cont, chain=chain, model=env.model
    )


async def active(w: SimpleNamespace, **over) -> Goal:
    """A goal created and approved through the store (its owner channel is ``CHAN``)."""
    values = {
        "title": "Tidy the notes",
        "objective": "Merge the duplicate notes in the notes folder",
        "done_when": "No two notes say the same thing",
        "proposed_by": "owner",
        "origin_channel": CHAN,
        **over,
    }
    goal = await w.store.create(**values)
    return await w.store.approve(goal.id, actor="owner")


async def started(w: SimpleNamespace, goal: Goal) -> tuple[Intention, GoalSession]:
    """Start a session of ``goal`` through the driver: its root intention and its session row."""
    root_id = await w.driver.start_session(goal.id, trigger="timer")
    assert root_id is not None
    async with w.db.session() as s:
        row = (await s.execute(select(GoalSession).where(GoalSession.root_id == root_id))).scalar_one()
        root = (await s.execute(select(Intention).where(Intention.id == root_id))).scalar_one()
    return root, row


def goal_tools(w: SimpleNamespace, goal: Goal, row: GoalSession) -> tuple:
    """``(goal_note, goal_plan)``: the executors a decision turn of ``row``'s root is given."""
    from nous.goals.tools import GoalSessionRef, make_goal_tools

    ref = GoalSessionRef(
        goal_id=goal.id, session_id=row.id, session_n=row.n, root_id=row.root_id, started_at=row.started_at
    )
    tools = make_goal_tools(w.db, w.env.settings, w.store, ref)
    return tools["goal_note"][1], tools["goal_plan"][1]


async def first_result(w: SimpleNamespace, root: Intention, body: str = RESULT) -> uuid.UUID:
    """The session's first subtask completes and its result is recorded for the root, as the worker does. Returns
    the subtask's id."""
    subtask_id = uuid.UUID(root.source_id)
    await w.env.heart.subtasks.complete(subtask_id, body, final_outcome="completed")
    await record(w.env, root, body=body)
    return subtask_id


async def journal(w: SimpleNamespace, goal: Goal, kind: str | None = None) -> list[GoalJournal]:
    query = select(GoalJournal).where(GoalJournal.goal_id == goal.id)
    if kind is not None:
        query = query.where(GoalJournal.kind == kind)
    async with w.db.session() as s:
        return list((await s.execute(query.order_by(GoalJournal.seq))).scalars())


async def goal_row(w: SimpleNamespace, goal: Goal) -> Goal:
    async with w.db.session() as s:
        return (await s.execute(select(Goal).where(Goal.id == goal.id))).scalar_one()


async def session_row(w: SimpleNamespace, session_id: uuid.UUID) -> GoalSession:
    async with w.db.session() as s:
        return (await s.execute(select(GoalSession).where(GoalSession.id == session_id))).scalar_one()


async def set_goal(w: SimpleNamespace, goal: Goal, **values) -> None:
    async with w.db.session() as s:
        await s.execute(update(Goal).where(Goal.id == goal.id).values(**values))
        await s.commit()


async def inbox(w: SimpleNamespace, **where) -> list[ResultInbox]:
    query = select(ResultInbox).where(ResultInbox.agent_id == w.agent)
    for name, value in where.items():
        query = query.where(getattr(ResultInbox, name) == value)
    async with w.db.session() as s:
        return list((await s.execute(query.order_by(ResultInbox.created_at, ResultInbox.id))).scalars())


async def root_of(w: SimpleNamespace, session: GoalSession) -> Intention:
    return await w.env.heart.intentions.get_for_source(intentions.SOURCE_SUBTASK, session.first_subtask_id)
