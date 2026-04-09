"""Subtask queue manager -- CRUD and atomic dequeue for background tasks."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, text, update

from nous.storage.database import Database
from nous.storage.models import Subtask

logger = logging.getLogger(__name__)

_PRIORITY_MAP = {"urgent": 50, "normal": 100, "low": 200}
_MAX_PENDING = 5


class SubtaskManager:
    """Manages the subtask queue in heart.subtasks."""

    def __init__(self, database: Database, agent_id: str) -> None:
        self._db = database
        self._agent_id = agent_id

    async def create(
        self,
        task: str,
        parent_session_id: str | None = None,
        priority: str = "normal",
        timeout: int = 120,
        notify: bool = False,
        metadata: dict | None = None,
        frame_type: str | None = None,
        model: str | None = None,
    ) -> Subtask:
        """Create a new pending subtask. Raises ValueError if pending limit reached."""
        pri_val = _PRIORITY_MAP.get(priority, 100)

        async with self._db.session() as session:
            # Check pending limit
            count = await session.scalar(
                select(func.count())
                .select_from(Subtask)
                .where(Subtask.agent_id == self._agent_id)
                .where(Subtask.status == "pending")
            )
            if count >= _MAX_PENDING:
                raise ValueError(f"pending subtask limit ({_MAX_PENDING}) reached")

            subtask = Subtask(
                agent_id=self._agent_id,
                parent_session_id=parent_session_id,
                task=task,
                priority=pri_val,
                timeout_seconds=timeout,
                notify=notify,
                metadata_=metadata or {},
                frame_type=frame_type,
                model=model,
            )
            session.add(subtask)
            await session.commit()
            await session.refresh(subtask)
            logger.info("Created subtask %s: %s", subtask.id.hex[:8], task[:80])
            return subtask

    async def dequeue(self, worker_id: str) -> Subtask | None:
        """Atomically dequeue the highest-priority pending subtask."""
        async with self._db.session() as session:
            # SELECT FOR UPDATE SKIP LOCKED
            result = await session.execute(
                select(Subtask)
                .where(Subtask.agent_id == self._agent_id)
                .where(Subtask.status == "pending")
                .order_by(Subtask.priority, Subtask.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            subtask = result.scalar_one_or_none()
            if subtask is None:
                return None

            subtask.status = "running"
            subtask.worker_id = worker_id
            subtask.started_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(subtask)
            logger.info("Dequeued subtask %s -> %s", subtask.id.hex[:8], worker_id)
            return subtask

    async def complete(self, subtask_id: UUID, result: str) -> None:
        """Mark a subtask as completed with its result."""
        async with self._db.session() as session:
            await session.execute(
                update(Subtask)
                .where(Subtask.id == subtask_id)
                .values(
                    status="completed",
                    result=result,
                    completed_at=datetime.now(UTC),
                )
            )
            await session.commit()
            logger.info("Completed subtask %s", subtask_id.hex[:8])

    async def fail(self, subtask_id: UUID, error: str) -> None:
        """Mark a subtask as failed with error message."""
        async with self._db.session() as session:
            await session.execute(
                update(Subtask)
                .where(Subtask.id == subtask_id)
                .values(
                    status="failed",
                    error=error,
                    completed_at=datetime.now(UTC),
                )
            )
            await session.commit()
            logger.warning("Failed subtask %s: %s", subtask_id.hex[:8], error)

    async def cancel(self, subtask_id: UUID) -> bool:
        """Cancel a pending subtask. Returns False if not pending."""
        async with self._db.session() as session:
            result = await session.execute(
                update(Subtask)
                .where(Subtask.id == subtask_id)
                .where(Subtask.status == "pending")
                .values(status="cancelled", completed_at=datetime.now(UTC))
            )
            await session.commit()
            return result.rowcount > 0

    async def get(self, subtask_id: UUID) -> Subtask | None:
        """Get a subtask by ID."""
        async with self._db.session() as session:
            return await session.get(Subtask, subtask_id)

    async def list(
        self,
        status: str | None = None,
        limit: int = 20,
    ) -> list[Subtask]:
        """List subtasks, optionally filtered by status."""
        async with self._db.session() as session:
            q = (
                select(Subtask)
                .where(Subtask.agent_id == self._agent_id)
                .order_by(Subtask.created_at.desc())
                .limit(limit)
            )
            if status:
                q = q.where(Subtask.status == status)
            result = await session.execute(q)
            return list(result.scalars().all())

    async def get_undelivered(self, parent_session_id: str) -> list[Subtask]:
        """Get completed/failed subtasks not yet delivered to parent session."""
        async with self._db.session() as session:
            result = await session.execute(
                select(Subtask)
                .where(Subtask.agent_id == self._agent_id)
                .where(Subtask.parent_session_id == parent_session_id)
                .where(Subtask.status.in_(["completed", "failed"]))
                .where(Subtask.delivered.is_(False))
                .order_by(Subtask.completed_at)
            )
            return list(result.scalars().all())

    async def mark_delivered(self, subtask_ids: list[UUID]) -> None:
        """Mark subtasks as delivered to parent session."""
        if not subtask_ids:
            return
        async with self._db.session() as session:
            await session.execute(update(Subtask).where(Subtask.id.in_(subtask_ids)).values(delivered=True))
            await session.commit()

    async def reclaim_stale(self) -> int:
        """Re-enqueue running subtasks that exceeded their timeout."""
        async with self._db.session() as session:
            now = datetime.now(UTC)
            # Find running tasks past their timeout
            result = await session.execute(
                update(Subtask)
                .where(Subtask.agent_id == self._agent_id)
                .where(Subtask.status == "running")
                .where(Subtask.started_at + Subtask.timeout_seconds * text("interval '1 second'") < now)
                .values(status="pending", worker_id=None, started_at=None)
            )
            await session.commit()
            if result.rowcount > 0:
                logger.warning("Reclaimed %d stale subtasks", result.rowcount)
            return result.rowcount

    async def count_by_status(self) -> dict[str, int]:
        """Count subtasks grouped by status."""
        async with self._db.session() as session:
            result = await session.execute(
                select(Subtask.status, func.count()).where(Subtask.agent_id == self._agent_id).group_by(Subtask.status)
            )
            return dict(result.all())
