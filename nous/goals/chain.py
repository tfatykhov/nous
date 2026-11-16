"""F114: what a goal session's chain knows about its goal, and where its reports go.

A goal session's root is its first subtask's intention (``nous.goals.roots``), so a root belongs to a goal exactly
when its source is the ``first_subtask_id`` of a goal session (the key the start writes before the subtask exists,
so the lookup holds before the session's ``root_id`` is written). ``report_channel`` is the hook
``continuation.set_goal_report_channel`` binds: a REPORT of such a root goes to the goal's channel (``goal:<id>``),
which the driver claims into the journal, instead of the owner's chat.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Text, and_, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain import intentions
from nous.storage.models import GoalSession, Intention


def _session_of_root(agent_id: str, root_id: UUID) -> Any:
    """The goal session whose first subtask is the source of the root ``root_id`` (a root: its own root)."""
    root = Intention
    return (
        select(GoalSession)
        .join(
            root,
            and_(
                root.agent_id == agent_id,
                root.id == root_id,
                root.root_id == root.id,
                root.source_kind == intentions.SOURCE_SUBTASK,
                root.source_id == cast(GoalSession.first_subtask_id, Text),
            ),
        )
        .where(GoalSession.agent_id == agent_id)
    )


async def report_channel(session: AsyncSession, agent_id: str, root_id: UUID) -> str | None:
    """``goal:<id>`` when ``root_id`` is the root of a session of that goal, else None. One read, in the caller's
    transaction (``continuation._insert_report_row``)."""
    row = (await session.execute(_session_of_root(agent_id, root_id))).scalar_one_or_none()
    return f"goal:{row.goal_id}" if row is not None else None
