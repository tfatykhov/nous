"""Schedule manager -- CRUD and due-task operations for recurring/timed tasks."""

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from croniter import croniter
from sqlalchemy import select, update

from nous.storage.database import Database
from nous.storage.models import Schedule

logger = logging.getLogger(__name__)


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
            )
            session.add(schedule)
            await session.commit()
            await session.refresh(schedule)
            logger.info(
                "Created %s schedule %s: %s (next: %s)",
                schedule_type,
                schedule.id.hex[:8],
                task[:80],
                next_fire,
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
                schedule.next_fire_at = fired_at + timedelta(seconds=schedule.interval_seconds)
            else:
                # One-shot schedule or missing timing: deactivate
                schedule.active = False
                schedule.next_fire_at = None

            await session.commit()
            logger.info(
                "Advanced schedule %s (fire #%d, next: %s)",
                schedule_id.hex[:8],
                schedule.fire_count,
                schedule.next_fire_at,
            )

    async def deactivate(self, schedule_id: UUID) -> None:
        """Deactivate a schedule."""
        async with self._db.session() as session:
            await session.execute(update(Schedule).where(Schedule.id == schedule_id).values(active=False))
            await session.commit()
            logger.info("Deactivated schedule %s", schedule_id.hex[:8])

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
