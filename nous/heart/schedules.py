"""Schedule manager -- CRUD and due-task operations for recurring/timed tasks."""

import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from croniter import croniter
from sqlalchemy import insert, select, update

from nous import undo_journal
from nous.brain import intentions
from nous.brain.intentions import IntentionSpec
from nous.storage.database import Database
from nous.storage.models import Schedule

logger = logging.getLogger(__name__)

# Intention states in which a schedule's container no longer accepts fires.
_CLOSED_INTENTION_STATES = ("closed", "cancelled", "expired")


class ScheduleManager:
    """Manages scheduled tasks in heart.schedules."""

    def __init__(self, database: Database, agent_id: str) -> None:
        self._db = database
        self._agent_id = agent_id

    async def create(
        self,
        task: str,
        schedule_type: str,
        fire_at: datetime | None = None,
        interval_seconds: int | None = None,
        cron_expr: str | None = None,
        notify: bool = False,
        timeout: int = 120,
        max_fires: int | None = None,
        session_id: str | None = None,
        metadata: dict | None = None,
        model: str | None = None,
        frame_type: str | None = None,
        continuation_turns: int = 0,
        continuation_prompt: str | None = None,
        intention: IntentionSpec | None = None,
    ) -> Schedule:
        """Create a new schedule."""
        # Compute next_fire_at
        now = datetime.now(UTC)
        if schedule_type == "once":
            next_fire = fire_at
        elif cron_expr:
            cron = croniter(cron_expr, now)
            next_fire = cron.get_next(datetime)
        elif interval_seconds:
            next_fire = now + timedelta(seconds=interval_seconds)
        else:
            raise ValueError("Recurring schedule needs interval_seconds or cron_expr")

        async with self._db.session() as session:
            # F099 I1: a schedule's intention is its container (section 4.1 Schedules).
            prepared = (
                await intentions.prepare_intention(session, self._agent_id, intention)
                if intention is not None
                else None
            )
            schedule = Schedule(
                agent_id=self._agent_id,
                task=task,
                schedule_type=schedule_type,
                fire_at=fire_at,
                interval_seconds=interval_seconds,
                cron_expr=cron_expr,
                next_fire_at=next_fire,
                notify=notify,
                timeout_seconds=timeout,
                max_fires=max_fires,
                created_by_session=session_id,
                metadata_=metadata or {},
                model=model,
                frame_type=frame_type,
                continuation_turns=continuation_turns,
                continuation_prompt=continuation_prompt,
            )
            session.add(schedule)
            await session.flush()
            if prepared is not None:
                await intentions.insert_prepared(
                    session, self._agent_id, prepared, source_kind=intentions.SOURCE_SCHEDULE, source_id=schedule.id
                )
            await session.commit()
            await session.refresh(schedule)
            # Undo journal: before a create there was no row.
            await undo_journal.record_safe(
                undo_journal.KIND_SCHEDULE, "create", str(schedule.id), None, label=task[:80]
            )
            logger.info(
                "Created %s schedule %s: %s (next: %s)",
                schedule_type, schedule.id.hex[:8], task[:80], next_fire,
            )
            return schedule

    async def get_due(self, now: datetime) -> list[Schedule]:
        """Get all active schedules whose next_fire_at <= now."""
        async with self._db.session() as session:
            result = await session.execute(
                select(Schedule)
                .where(Schedule.agent_id == self._agent_id)
                .where(Schedule.active.is_(True))
                .where(Schedule.next_fire_at <= now)
                .order_by(Schedule.next_fire_at)
            )
            return list(result.scalars().all())

    async def set_continuation_session(
        self, schedule_id: UUID, session_id: str
    ) -> None:
        """F064.5 v1: pin a stable session_id for a continuation cycle.

        Called at the START of a continuation cycle when previous fire had
        no continuation_session_id. Resets continuation_count to 1 since
        this fire is the first in the cycle.
        """
        async with self._db.session() as session:
            await session.execute(
                update(Schedule)
                .where(Schedule.id == schedule_id)
                .values(continuation_session_id=session_id, continuation_count=1)
            )
            await session.commit()

    async def bump_continuation_count(self, schedule_id: UUID) -> None:
        """F064.5 v1: increment continuation_count for an in-progress cycle.

        Counts DISPATCHES (not successes) per plan §8.2 — a failed fire
        still consumes its slot. Simpler invariant, less footgun than
        success-counting.
        """
        async with self._db.session() as session:
            schedule = await session.get(Schedule, schedule_id)
            if schedule is None:
                return
            schedule.continuation_count += 1
            await session.commit()

    async def reset_continuation(self, schedule_id: UUID) -> None:
        """F064.5 v1: end a continuation cycle. NULLs the session_id and
        zeroes the count so the next fire starts a fresh cycle (or a
        fresh single-shot if continuation_turns is 0).
        """
        async with self._db.session() as session:
            await session.execute(
                update(Schedule)
                .where(Schedule.id == schedule_id)
                .values(continuation_session_id=None, continuation_count=0)
            )
            await session.commit()

    async def advance(self, schedule_id: UUID, fired_at: datetime) -> None:
        """Advance a recurring schedule after firing."""
        async with self._db.session() as session:
            schedule = await session.get(Schedule, schedule_id)
            if schedule is None:
                return

            schedule.fire_count += 1
            schedule.last_fired_at = fired_at

            # Check max_fires
            if schedule.max_fires and schedule.fire_count >= schedule.max_fires:
                schedule.active = False
                schedule.next_fire_at = None
            elif schedule.cron_expr:
                cron = croniter(schedule.cron_expr, fired_at)
                schedule.next_fire_at = cron.get_next(datetime)
            elif schedule.interval_seconds:
                schedule.next_fire_at = fired_at + timedelta(
                    seconds=schedule.interval_seconds
                )
            else:
                # One-shot schedule or missing timing: deactivate
                schedule.active = False
                schedule.next_fire_at = None

            await session.commit()
            logger.info(
                "Advanced schedule %s (fire #%d, next: %s)",
                schedule_id.hex[:8], schedule.fire_count, schedule.next_fire_at,
            )
            deactivated = not schedule.active
        if deactivated:
            await self._close_container(schedule_id)

    async def _close_container(self, schedule_id: UUID) -> None:
        """F099: a schedule that no longer fires closes its container intention.

        Its own transaction, after the deactivation committed. Closing is
        bookkeeping, and a failure here must never leave a schedule active
        (a one-shot schedule would fire again). A close that fails here, or
        never runs because the process exits first, is repaired by the
        reconciler's intentions pass (intentions.close_finished_containers).
        A schedule created with intentions off has no container, and this
        closes nothing.
        """
        try:
            async with self._db.session() as session:
                await intentions.close_for_source(
                    session, self._agent_id, intentions.SOURCE_SCHEDULE, schedule_id, with_result=False
                )
                await session.commit()
        except Exception:
            logger.warning("F099: could not close the container of schedule %s", schedule_id.hex[:8], exc_info=True)

    async def deactivate(self, schedule_id: UUID) -> None:
        """Deactivate a schedule, and close its F099 container intention."""
        if undo_journal.get_journal() is not None:
            await self._record_before_deactivate(schedule_id)
        async with self._db.session() as session:
            await session.execute(
                update(Schedule)
                .where(Schedule.id == schedule_id)
                .values(active=False)
            )
            await session.commit()
            logger.info("Deactivated schedule %s", schedule_id.hex[:8])
        await self._close_container(schedule_id)

    async def restore_row(
        self,
        schedule_id: UUID,
        before: dict[str, Any] | None,
        *,
        on_current: Callable[[Mapping[str, Any] | None], Awaitable[None]],
    ) -> tuple[bool, str]:
        """Undo journal: put schedule ``schedule_id`` back to ``before`` (its
        column values), or, when ``before`` is None (the snapshot was taken
        at its create), deactivate it -- never a delete. ``on_current`` gets
        the row as it is now, under the row lock, before anything changes;
        if it raises, nothing changes. A row restored active whose F099
        container intention is no longer open is restored inactive instead:
        a restore never re-arms work against a closed intention."""
        table = Schedule.__table__
        async with self._db.session() as session:
            current = (
                await session.execute(
                    select(table)
                    .where(table.c.id == schedule_id)
                    .where(table.c.agent_id == self._agent_id)
                    .with_for_update()
                )
            ).mappings().first()
            await on_current(dict(current) if current is not None else None)
            if before is None:
                if current is None or not current["active"]:
                    return True, f"schedule {schedule_id} is already absent or inactive"
                await session.execute(update(table).where(table.c.id == schedule_id).values(active=False))
                await session.commit()
                deactivated = True
            else:
                if before.get("agent_id") != self._agent_id or before.get("id") != schedule_id:
                    return False, "snapshot belongs to another schedule or agent; nothing changed"
                values = dict(before)
                note = ""
                if values.get("active"):
                    state = await self._container_state(session, schedule_id)
                    if state in _CLOSED_INTENTION_STATES:
                        values["active"] = False
                        note = f"; restored inactive because its container intention is {state}"
                if current is None:
                    await session.execute(insert(table).values(**values))
                else:
                    values.pop("id")
                    await session.execute(update(table).where(table.c.id == schedule_id).values(**values))
                await session.commit()
                return True, f"restored schedule {schedule_id}{note}"
        if deactivated:
            await self._close_container(schedule_id)
        return True, f"deactivated schedule {schedule_id} (undoing its create; rows are never deleted)"

    async def _record_before_deactivate(self, schedule_id: UUID) -> None:
        """Undo journal: the row before a deactivate (never raises). A
        schedule with an F099 container intention is recorded but not
        restorable: the deactivate closes the container, and restore never
        re-arms work against a closed intention."""
        try:
            table = Schedule.__table__
            async with self._db.session() as session:  # its own: a failed read must not abort the deactivate
                prior = (await session.execute(select(table).where(table.c.id == schedule_id))).mappings().first()
                if prior is None:
                    return
                container = await self._container_state(session, schedule_id)
        except Exception:
            logger.warning("Undo journal: could not read schedule %s before deactivate", schedule_id, exc_info=True)
            return
        await undo_journal.record_safe(
            undo_journal.KIND_SCHEDULE,
            "deactivate",
            str(schedule_id),
            undo_journal.encode_row(prior),
            label=prior["task"][:80],
            restorable=container is None,
            note="its F099 container intention is closed by this deactivate" if container else None,
        )

    async def _container_state(self, session: Any, schedule_id: UUID) -> str | None:
        """The state of schedule ``schedule_id``'s F099 container intention, or None without one."""
        from nous.storage.models import Intention

        return (
            await session.execute(
                select(Intention.state).where(
                    Intention.agent_id == self._agent_id,
                    Intention.source_kind == intentions.SOURCE_SCHEDULE,
                    Intention.source_id == str(schedule_id),
                )
            )
        ).scalar_one_or_none()

    async def get(self, schedule_id: UUID) -> Schedule | None:
        """Get a schedule by ID."""
        async with self._db.session() as session:
            return await session.get(Schedule, schedule_id)

    async def list(self, active_only: bool = True, limit: int = 20) -> list[Schedule]:
        """List schedules, optionally filtered to active only."""
        async with self._db.session() as session:
            q = (
                select(Schedule)
                .where(Schedule.agent_id == self._agent_id)
                .order_by(Schedule.created_at.desc())
                .limit(limit)
            )
            if active_only:
                q = q.where(Schedule.active.is_(True))
            result = await session.execute(q)
            return list(result.scalars().all())
