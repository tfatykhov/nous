"""F114: what goal sessions spend, read from rows.

A session's spend is its root's token budget exactly as the continuation counts it (``continuation.root_limits``):
the root's arrivals (input plus output), what its failed attempts spent, its lineage's subtasks outside a DAG and
its lineage's DAGs. Not counted: cache reads and writes, jobs launched from the chain, embeddings and helper calls
such as compaction, and a model call that failed before it returned its usage.
"""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain import continuation
from nous.storage.models import GoalSession


async def session_spent(session: AsyncSession, agent_id: str, root_ids: Iterable[UUID]) -> dict[UUID, int]:
    """Tokens each root's chain has spent (the five terms ``root_limits`` adds); 0 for a root with no rows."""
    parts = await continuation.root_token_parts(session, agent_id, root_ids)
    return {root_id: tally.total for root_id, tally in parts.items()}


async def goal_spent(session: AsyncSession, agent_id: str, goal_id: UUID) -> tuple[int, int]:
    """``(total, today)``: what the goal's sessions have spent in all, and what those started since the start of
    the current UTC day (by the database's clock) have spent. A session counts for the day it started."""
    day_start = func.date_trunc("day", func.clock_timestamp(), "UTC")
    total, today = (
        await session.execute(
            select(
                func.coalesce(func.sum(GoalSession.spent), 0),
                func.coalesce(func.sum(GoalSession.spent).filter(GoalSession.started_at >= day_start), 0),
            ).where(GoalSession.agent_id == agent_id, GoalSession.goal_id == goal_id)
        )
    ).one()
    return int(total), int(today)
