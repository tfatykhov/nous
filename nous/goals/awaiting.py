"""F114 goals: what a goal's sessions wait on the owner for, for the Goals screen and the dashboard's badges.

A goal's session is a continuation chain (``nous.goals.roots``); what a chain waits on the owner for is the
continuation's own list (``continuation.awaiting_ids``, the one the Intentions card shows): the pending approval
requests and the open questions of its latest arrivals. The roots read are those of the goal's sessions that have an
intention ``awaiting_owner`` now, whatever the session's age and whether it is still open (an ask leaves its chain
waiting after the session closed). Workflow approval steps are not part of it: they are answered on the Workflows
screen.

Read-only, in the caller's transaction: one query for the roots, then the continuation's three, then (for one goal in
full) one for each list that has ids. A goal that is not draft, active or paused is not read.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain import continuation
from nous.goals.store import LIVE_STATES
from nous.storage.models import Goal, GoalSession, Intention


async def waiting_ids(
    session: AsyncSession, agent_id: str, goal_ids: Sequence[UUID], *, settings: Any, now: datetime
) -> dict[UUID, tuple[list[str], list[str]]]:
    """Per goal of ``goal_ids``, the ids of the approval requests and the questions its sessions wait on the owner
    for, as ``(proposal_ids, question_ids)``; a goal with none is not in the answer."""
    if not goal_ids:
        return {}
    pairs = (
        await session.execute(
            select(GoalSession.goal_id, GoalSession.root_id)
            .where(
                GoalSession.agent_id == agent_id,
                GoalSession.goal_id.in_(list(goal_ids)),
                GoalSession.root_id.is_not(None),
                exists().where(
                    Intention.agent_id == agent_id,
                    Intention.root_id == GoalSession.root_id,
                    Intention.state == continuation.STATE_AWAITING_OWNER,
                ),
            )
            .order_by(GoalSession.goal_id, GoalSession.n)
        )
    ).all()
    if not pairs:
        return {}
    by_root = await continuation.awaiting_ids(
        session, agent_id, list({root_id for _goal_id, root_id in pairs}), settings=settings, now=now
    )
    out: dict[UUID, tuple[list[str], list[str]]] = {}
    for goal_id, root_id in pairs:
        proposal_ids, question_ids = by_root.get(root_id, ([], []))
        if proposal_ids or question_ids:
            mine = out.setdefault(goal_id, ([], []))
            mine[0].extend(proposal_ids)
            mine[1].extend(question_ids)
    return out


async def waiting_counts(
    session: AsyncSession, agent_id: str, goal_ids: Sequence[UUID], *, settings: Any, now: datetime
) -> dict[UUID, int]:
    """How many approval requests and questions each goal's sessions wait on the owner for (0 for none)."""
    found = await waiting_ids(session, agent_id, goal_ids, settings=settings, now=now)
    return {goal_id: sum(map(len, found.get(goal_id, ([], [])))) for goal_id in goal_ids}


async def waiting_items(
    session: AsyncSession, agent_id: str, goal: Goal, *, settings: Any, now: datetime
) -> dict[str, list[dict[str, Any]]]:
    """What ``goal``'s sessions wait on the owner for, in full (``continuation.awaiting_items``): ``proposals`` and
    ``questions``, each oldest first. Both empty, with no read, for a goal that is not draft, active or paused."""
    if goal.state not in LIVE_STATES:
        return {"proposals": [], "questions": []}
    found = await waiting_ids(session, agent_id, [goal.id], settings=settings, now=now)
    proposal_ids, question_ids = found.get(goal.id, ([], []))
    return await continuation.awaiting_items(session, agent_id, proposal_ids, question_ids, settings=settings)


async def waiting_total(session: AsyncSession, agent_id: str, *, settings: Any) -> int:
    """How many approval requests and questions the sessions of all the agent's draft, active and paused goals wait
    on the owner for, by the database's clock: the Goals badge of the dashboard's navigation."""
    now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
    goal_ids = list(
        (await session.execute(select(Goal.id).where(Goal.agent_id == agent_id, Goal.state.in_(LIVE_STATES)))).scalars()
    )
    counts = await waiting_counts(session, agent_id, goal_ids, settings=settings, now=now)
    return sum(counts.values())
