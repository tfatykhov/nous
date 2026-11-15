"""F114: what goal sessions spend, read from rows.

A session's spend is its root's token budget exactly as the continuation counts it (``continuation.root_limits``):
the root's arrivals (input plus output), what its failed attempts spent, its lineage's subtasks outside a DAG and
its lineage's DAGs. Not counted: cache reads and writes, jobs launched from the chain, embeddings and helper calls
such as compaction, and a model call that failed before it returned its usage.

A session's spend belongs to the UTC day it started, however late its tokens come: two of the five sources (a
failed attempt's tokens and a DAG's total) are running totals with no time of their own, so the spend cannot be
split by the day it was spent without a second way to count it.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, Text, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain import continuation, intentions
from nous.storage.models import GoalSession, Intention, ToolCallLog

# A job launch in a lineage's shell, as the tool-call log records the command (redacted and clipped, head kept).
JOB_LAUNCH_PATTERN = "%runner.sh launch%"
# The tool-call log's outcomes of a call that reached the shell (refused, gated, suppressed calls never ran).
RAN_OUTCOMES: tuple[str, ...] = ("ran", "error")


def day_start(now: datetime | None = None) -> ColumnElement[datetime]:
    """The start of the UTC day ``now`` is in (by default the database's clock)."""
    return func.date_trunc("day", func.clock_timestamp() if now is None else now, "UTC")


async def session_spent(session: AsyncSession, agent_id: str, root_ids: Iterable[UUID]) -> dict[UUID, int]:
    """Tokens each root's chain has spent (the five terms ``root_limits`` adds); 0 for a root with no rows."""
    parts = await continuation.root_token_parts(session, agent_id, root_ids)
    return {root_id: tally.total for root_id, tally in parts.items()}


async def goal_spent(
    session: AsyncSession, agent_id: str, goal_id: UUID, *, now: datetime | None = None
) -> tuple[int, int]:
    """``(total, today)``: what the goal's sessions have spent in all, and what those started since the start of
    the UTC day of ``now`` (by default the database's clock) have spent. A session counts for the day it started."""
    since = day_start(now)
    total, today = (
        await session.execute(
            select(
                func.coalesce(func.sum(GoalSession.spent), 0),
                func.coalesce(func.sum(GoalSession.spent).filter(GoalSession.started_at >= since), 0),
            ).where(GoalSession.agent_id == agent_id, GoalSession.goal_id == goal_id)
        )
    ).one()
    return int(total), int(today)


async def sessions_started_today(session: AsyncSession, agent_id: str, goal_id: UUID, *, now: datetime) -> int:
    """How many of the goal's sessions started since the start of the UTC day of ``now``."""
    return int(
        await session.scalar(
            select(func.count()).where(
                GoalSession.agent_id == agent_id,
                GoalSession.goal_id == goal_id,
                GoalSession.started_at >= day_start(now),
            )
        )
        or 0
    )


async def jobs_launched_today(
    session: AsyncSession, agent_id: str, goal_id: UUID, settings: Any, *, now: datetime
) -> int | None:
    """Job launches (``runner.sh launch`` in a shell call that ran) of the goal's lineages since the start of the
    UTC day of ``now``, read from the tool-call log: a call of a session's decision turns (their session id), of a
    lineage subtask or of a lineage DAG. None (unknown) when the tool-call log is off; it is best-effort even when
    on (a write dropped under load is a launch not counted)."""
    if getattr(settings, "tool_call_log_enabled", False) is not True:
        return None
    goal_roots = select(GoalSession.root_id).where(
        GoalSession.agent_id == agent_id, GoalSession.goal_id == goal_id, GoalSession.root_id.is_not(None)
    )
    turn_sessions = select(func.concat(continuation.INTENT_SESSION_PREFIX, cast(GoalSession.root_id, Text))).where(
        GoalSession.agent_id == agent_id, GoalSession.goal_id == goal_id, GoalSession.root_id.is_not(None)
    )

    def lineage(kind: str):
        return select(Intention.source_id).where(
            Intention.agent_id == agent_id, Intention.root_id.in_(goal_roots), Intention.source_kind == kind
        )

    count = await session.scalar(
        select(func.count()).where(
            ToolCallLog.agent_id == agent_id,
            ToolCallLog.tool_name == "bash",
            ToolCallLog.started_at >= day_start(now),
            ToolCallLog.outcome.in_(RAN_OUTCOMES),
            ToolCallLog.input_text.like(JOB_LAUNCH_PATTERN),
            or_(
                ToolCallLog.session_id.in_(turn_sessions),
                cast(ToolCallLog.subtask_id, Text).in_(lineage(intentions.SOURCE_SUBTASK)),
                cast(ToolCallLog.dag_id, Text).in_(lineage(intentions.SOURCE_DAG)),
            ),
        )
    )
    return int(count or 0)
