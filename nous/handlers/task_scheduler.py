"""Task Scheduler -- fires due scheduled tasks by creating subtasks.

Runs a periodic check loop that:
1. Queries schedules whose next_fire_at <= now
2. Creates a subtask for each due schedule
3. Deactivates one-shot schedules, advances recurring ones

Handles queue-full errors gracefully (logs warning, skips that schedule).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from nous.config import Settings
from nous.heart.heart import Heart

logger = logging.getLogger(__name__)


class TaskScheduler:
    """Background scheduler that checks for due tasks and enqueues them.

    Runs a single asyncio task that wakes every schedule_check_interval
    seconds to fire any overdue schedules.
    """

    def __init__(self, heart: Heart, settings: Settings) -> None:
        self._heart = heart
        self._settings = settings
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        """Start the scheduler check loop."""
        self._running = True
        self._task = asyncio.create_task(
            self._check_loop(), name="task-scheduler"
        )
        logger.info(
            "Task scheduler started (check_interval=%ds)",
            self._settings.schedule_check_interval,
        )

    async def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Task scheduler stopped")

    # ------------------------------------------------------------------
    # Check loop
    # ------------------------------------------------------------------

    async def _check_loop(self) -> None:
        """Periodic loop: sleep -> check due schedules -> repeat."""
        while self._running:
            try:
                await asyncio.sleep(self._settings.schedule_check_interval)
                fired = await self._fire_due_tasks()
                if fired:
                    logger.info("Fired %d due schedule(s)", fired)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Schedule check failed")

    async def _fire_due_tasks(self) -> int:
        """Check for due schedules, create subtasks, and advance/deactivate.

        Returns the number of schedules fired.
        """
        now = datetime.now(UTC)
        due_schedules = await self._heart.schedules.get_due(now)

        if not due_schedules:
            return 0

        fired = 0
        for schedule in due_schedules:
            try:
                # Create a subtask from the schedule
                await self._heart.subtasks.create(
                    task=schedule.task,
                    parent_session_id=schedule.created_by_session,
                    priority="normal",
                    timeout=schedule.timeout_seconds,
                    notify=schedule.notify,
                    model=schedule.model,
                    frame_type=schedule.frame_type,
                    metadata={"schedule_id": schedule.id.hex},
                )

                # Handle schedule lifecycle
                if schedule.schedule_type == "once":
                    await self._heart.schedules.deactivate(schedule.id)
                else:
                    # Recurring: advance to next fire time
                    await self._heart.schedules.advance(schedule.id, now)

                fired += 1
                logger.debug(
                    "Fired schedule %s (%s): %s",
                    schedule.id.hex[:8],
                    schedule.schedule_type,
                    schedule.task[:80],
                )

            except ValueError as exc:
                # Queue full — skip this schedule, it will be retried next cycle
                logger.warning(
                    "Could not fire schedule %s: %s",
                    schedule.id.hex[:8],
                    exc,
                )
            except Exception:
                logger.exception(
                    "Failed to fire schedule %s", schedule.id.hex[:8]
                )

        return fired
