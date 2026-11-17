"""The end of a goal's mode by time, by a finished goal, and at a start of the server (``ModeSweeper``).

``expire`` (first in each driver tick, with ``NOUS_GOAL_COMMANDS_ENABLED`` on) returns to contained every goal of the
agent whose mode is not ``contained`` and whose end has passed by the database's clock (``expired``), whose state is
finished (``finished``), or which is past its draft with no end (``cleared``): each under the goal row's lock, the
condition read again, with one harness ``state`` row. A draft's choice (no end yet) is left as it is. Cancels nothing.

``at_start`` (once, when the driver starts): with the flag off, every goal of the agent that is not contained returns
to contained (``flag_off``); with it on, an end later than the host's ceiling from now is moved to it (``clamped``),
the ceiling having been lowered since the end was set. Both are idempotent: a second sweeper finds nothing to do.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from nous.goals import commands as goal_commands
from nous.goals.commands import CONTAINED_VALUES, MODE_CONTAINED, mode_refs
from nous.goals.store import GoalStore
from nous.storage.database import Database
from nous.storage.models import Goal

logger = logging.getLogger(__name__)

FINISHED_STATES: tuple[str, ...] = ("done", "failed", "cancelled")


def _why(goal: Goal, now: datetime) -> str | None:
    """Why ``goal``'s mode ends now, or None."""
    if goal.mode == MODE_CONTAINED:
        return None
    if goal.state in FINISHED_STATES:
        return "finished"
    if goal.mode_until is None:
        return None if goal.state == "draft" else "cleared"
    return "expired" if goal.mode_until <= now else None


class ModeSweeper:
    """The ends of the goals' modes that no owner's change makes (see the module)."""

    def __init__(self, database: Database, settings: Any, store: GoalStore) -> None:
        self._db = database
        self._settings = settings
        self._store = store
        self._agent_id = store.agent_id

    async def expire(self) -> int:
        """Return to contained each goal whose end has passed, whose state is finished, or which has no end past its
        draft. Returns how many."""
        due = or_(
            Goal.mode_until <= func.clock_timestamp(),
            Goal.state.in_(FINISHED_STATES),
            and_(Goal.state != "draft", Goal.mode_until.is_(None)),
        )
        ended = 0
        for goal_id in await self._candidates(due):
            if await self._end(goal_id):
                ended += 1
        return ended

    async def at_start(self) -> int:
        """Once, at the driver's start: with the flag off every goal returns to contained; with it on an end past the
        host's ceiling from now moves to it. Returns how many goals changed."""
        changed = 0
        if not goal_commands.enabled(self._settings):
            for goal_id in await self._candidates(None):
                if await self._end(goal_id, why="flag_off"):
                    changed += 1
            return changed
        ceiling = timedelta(hours=goal_commands.max_hours(self._settings))
        for goal_id in await self._candidates(Goal.mode_until > func.clock_timestamp() + ceiling):
            if await self._clamp(goal_id, ceiling):
                changed += 1
        return changed

    async def _candidates(self, condition: Any) -> list[UUID]:
        query = select(Goal.id).where(Goal.agent_id == self._agent_id, Goal.mode != MODE_CONTAINED)
        if condition is not None:
            query = query.where(condition)
        async with self._db.session() as s:
            return list((await s.execute(query.order_by(Goal.short_id))).scalars())

    async def _locked(self, s: AsyncSession, goal_id: UUID) -> tuple[Goal | None, datetime]:
        goal = (
            await s.execute(select(Goal).where(Goal.id == goal_id, Goal.agent_id == self._agent_id).with_for_update())
        ).scalar_one_or_none()
        now = (await s.execute(select(func.clock_timestamp()))).scalar_one()
        return goal, now

    async def _end(self, goal_id: UUID, *, why: str | None = None) -> bool:
        """Under the goal row's lock: read the condition again (``why`` given: any mode that is not contained), then
        return the goal to contained with one harness row. False when there is nothing to end."""
        async with self._db.session() as s:
            goal, now = await self._locked(s, goal_id)
            if goal is None or goal.mode == MODE_CONTAINED:
                return False
            reason = why or _why(goal, now)
            if reason is None:
                return False
            await s.execute(
                update(Goal)
                .where(Goal.id == goal_id, Goal.agent_id == self._agent_id)
                .values(**CONTAINED_VALUES, updated_at=func.clock_timestamp())
                .execution_options(synchronize_session=False)
            )
            self._store.add_journal_row(
                s,
                goal_id,
                "state",
                goal_commands.ENDED[reason],
                actor="harness",
                evidence=mode_refs("off", why=reason),
            )
            await s.commit()
        logger.info("Goal %s: its tasks no longer run any command (%s)", goal_id, reason)
        return True

    async def _clamp(self, goal_id: UUID, ceiling: timedelta) -> bool:
        """Under the goal row's lock: an end later than ``ceiling`` from now moves to it, with one harness row."""
        async with self._db.session() as s:
            goal, now = await self._locked(s, goal_id)
            if goal is None or goal.mode == MODE_CONTAINED or goal.mode_until is None:
                return False
            until = now + ceiling
            if goal.mode_until <= until:
                return False
            await s.execute(
                update(Goal)
                .where(Goal.id == goal_id, Goal.agent_id == self._agent_id)
                .values(mode_until=until, updated_at=func.clock_timestamp())
                .execution_options(synchronize_session=False)
            )
            self._store.add_journal_row(
                s,
                goal_id,
                "state",
                goal_commands.clamped_text(until),
                actor="harness",
                evidence=mode_refs("on", why="clamped"),
            )
            await s.commit()
        return True
