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

from sqlalchemy import func, select

from nous.config import Settings
from nous.heart.heart import Heart
from nous.storage.models import Subtask

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

    async def _has_active_subtask_for_schedule(self, schedule_id) -> bool:
        """F064.5 (architecture P1-A): true when a pending/running subtask
        already exists for this schedule. Used to debounce overlapping fires
        when a previous subtask is slow.

        Query keyed on heart.subtasks.metadata->>'schedule_id' = <hex>.

        Fail-OPEN on DB error: if the check itself raises (mock, transient
        DB hiccup, schema mismatch in a downstream test fixture), we treat
        the schedule as having no active subtask. The schedule's own
        next_fire_at gating already prevents trivial double-fires; this
        check is a safety belt over that, not a correctness gate.
        """
        try:
            async with self._heart.db.session() as session:
                count = await session.scalar(
                    select(func.count())
                    .select_from(Subtask)
                    .where(Subtask.agent_id == self._settings.agent_id)
                    .where(Subtask.status.in_(["pending", "running"]))
                    .where(Subtask.metadata_["schedule_id"].astext == schedule_id.hex)
                )
                if not isinstance(count, int):
                    # Mock returned a non-int (e.g. AsyncMock object) — fail open.
                    return False
                return count > 0
        except Exception:
            logger.debug(
                "_has_active_subtask_for_schedule failed (fail-open) for %s",
                getattr(schedule_id, "hex", schedule_id),
                exc_info=True,
            )
            return False

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
                # F064.5 (architecture P1-A): running-subtask debounce guard.
                # Skip this fire if a previous fire's subtask is still active
                # for the same schedule. Without this, a slow continuation
                # subtask could overlap with the next interval's fire and
                # double up. Inline grouped count keeps the query cheap.
                if await self._has_active_subtask_for_schedule(schedule.id):
                    logger.info(
                        "Skipping schedule %s fire — previous subtask still active",
                        schedule.id.hex[:8],
                    )
                    continue

                # F064.5 (v1 Episode reuse, architecture P1-B): if continuation
                # is enabled AND we're mid-cycle, reuse the stable session_id
                # so the runner re-uses the existing Episode. The LLM context
                # itself is still fresh each fire — true thread continuity is
                # deferred to F064.5-v2.
                #
                # @codex P2 on e119510: COMPUTE the planned session_id +
                # cycle classification first, but DO NOT persist any state
                # until AFTER subtasks.create succeeds. Otherwise a failed
                # enqueue (queue full → ValueError) would leave the schedule
                # pointing at a phantom continuation session that was never
                # actually used. The post-success state-commit block below
                # is gated on `_enqueued = True`.
                continuation_metadata: dict = {"schedule_id": schedule.id.hex}
                cycle_action: str | None = None  # 'reuse' | 'fresh' | None
                planned_session: str | None = None
                if (
                    self._settings.schedule_continuation_enabled
                    and schedule.continuation_turns > 0
                ):
                    effective_cap = min(
                        schedule.continuation_turns,
                        self._settings.schedule_max_continuation_turns,
                    )
                    if (
                        schedule.continuation_session_id is not None
                        and schedule.continuation_count < effective_cap
                    ):
                        # Mid-cycle: reuse session_id, bump count after dispatch.
                        planned_session = schedule.continuation_session_id
                        cycle_action = "reuse"
                    else:
                        # Cycle start (or reset): mint a fresh session_id (do
                        # not persist yet — wait for enqueue success).
                        planned_session = (
                            f"schedule-{schedule.id.hex[:8]}-{schedule.fire_count + 1}"
                        )
                        cycle_action = "fresh"
                    continuation_metadata["session_id"] = planned_session

                # Create a subtask from the schedule. This is the failure
                # point — if it raises (queue full, transient DB error),
                # the post-success state-commit below is skipped.
                _enqueued = False
                try:
                    await self._heart.subtasks.create(
                        task=schedule.task,
                        parent_session_id=schedule.created_by_session,
                        priority="normal",
                        timeout=schedule.timeout_seconds,
                        notify=schedule.notify,
                        model=schedule.model,
                        frame_type=schedule.frame_type,
                        metadata=continuation_metadata,
                    )
                    _enqueued = True
                except ValueError:
                    # Queue-full path: re-raise so the outer except logs
                    # warning + skips. Continuation state NOT touched —
                    # phantom-session bug is closed.
                    raise

                # F064.5 v1: state-commit only AFTER enqueue succeeded.
                # @codex P2 on a167217: each continuation state write is
                # wrapped in its own try/except so a transient DB failure
                # in (e.g.) bump_continuation_count doesn't skip the
                # schedule.advance() call below — that would leave the
                # schedule with stale next_fire_at and cause infinite
                # re-fires. Continuation state is a soft observability
                # channel, the lifecycle advance is the hard correctness
                # invariant.
                if _enqueued and cycle_action is not None:
                    try:
                        if cycle_action == "fresh":
                            await self._heart.schedules.set_continuation_session(
                                schedule.id, planned_session
                            )
                            next_count = 1
                        else:  # reuse
                            await self._heart.schedules.bump_continuation_count(
                                schedule.id
                            )
                            next_count = schedule.continuation_count + 1

                        effective_cap = min(
                            schedule.continuation_turns,
                            self._settings.schedule_max_continuation_turns,
                        )
                        if next_count >= effective_cap:
                            await self._heart.schedules.reset_continuation(
                                schedule.id
                            )
                    except Exception:
                        logger.exception(
                            "Failed to update continuation state for schedule %s — "
                            "lifecycle advance will proceed regardless",
                            schedule.id.hex[:8],
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
