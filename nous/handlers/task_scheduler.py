"""Task Scheduler -- fires due scheduled tasks by creating subtasks.

Runs a periodic check loop that:
1. Queries schedules whose next_fire_at <= now
2. Creates a subtask for each due schedule
3. Deactivates one-shot schedules, advances recurring ones

Handles queue-full errors gracefully (logs warning, skips that schedule).

F101 (NOUS_SCHEDULE_POLICIES_ENABLED): a recurring schedule's concurrency_policy
decides what a fire does while the schedule's previous run is still active, and
its catch_up_policy how many runs a check starts for slots missed during downtime
(docs/features/F101-schedule-concurrency-catchup.md).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from croniter import croniter
from sqlalchemy import and_, func, or_, select, text

from nous.brain import intentions
from nous.brain.intentions import IntentionSpec
from nous.cancellation import cancel_requested
from nous.config import Settings
from nous.dag.store import LIVE_DAG_STATUSES
from nous.events import Event
from nous.heart.heart import Heart
from nous.storage.models import ExecutionDAG, Schedule, Subtask

logger = logging.getLogger(__name__)

# F101: a running subtask stops counting as its schedule's active run this long
# after its own timeout, so a run whose worker died cannot hold a schedule.
_STALE_GRACE_SECONDS = 300


@dataclass(frozen=True)
class ActiveRun:
    """F101: a schedule's run that is still going -- a subtask or a DAG it created."""

    kind: str  # "subtask" | "dag"
    id: UUID


@dataclass(frozen=True)
class FireResult:
    """F101: what one fire did. ``run_id`` is the new run ("started") or the active one."""

    outcome: str  # "started" | "coalesced" | "skipped"
    run_id: UUID


class ScheduleBusy(RuntimeError):
    """F101: another fire of the same schedule holds its fire lock."""


class ScheduleNotFound(LookupError):
    """F101: no active recurring schedule of this agent has that id."""


async def create_fire(
    heart: Heart,
    settings: Settings,
    schedule: Schedule,
    *,
    task: str | None = None,
    intent: str | None = None,
    metadata: dict | None = None,
    notify: bool | None = None,
    timeout: int | None = None,
) -> Subtask:
    """Enqueue one run of ``schedule`` as a new root under the schedule's container intention.

    Defaults come from the schedule (``task``, ``notify``, ``timeout_seconds``). ``intent`` is the
    intention's one-line description and falls back to the run's text. The run's metadata carries
    ``schedule_id`` plus ``metadata``. ``SubtaskQueueFull`` and ``IntentionRootClosed`` (a schedule
    stopped since it was loaded) propagate; both are ``ValueError``.
    """
    text = schedule.task if task is None else task
    notify = schedule.notify if notify is None else notify
    timeout = schedule.timeout_seconds if timeout is None else timeout
    fire_intention = None
    if intentions.enabled(settings):
        fire_intention = intentions.with_bounds(
            IntentionSpec(
                intent=intent or text,
                origin_kind=intentions.ORIGIN_SCHEDULER,
                wake_policy=intentions.WAKE_REMEMBER if notify else intentions.WAKE_NONE,
                parent_source=(intentions.SOURCE_SCHEDULE, str(schedule.id)),
            ),
            settings,
        )
    return await heart.subtasks.create(
        task=text,
        parent_session_id=schedule.created_by_session,
        priority="normal",
        timeout=timeout,
        notify=notify,
        model=schedule.model,
        frame_type=schedule.frame_type,
        metadata={"schedule_id": schedule.id.hex, **(metadata or {})},
        **intentions.intention_kwargs(fire_intention),
    )


async def find_active_run(heart: Heart, settings: Settings, schedule_id: UUID, now: datetime) -> ActiveRun | None:
    """F101: the oldest run of ``schedule_id`` that is still active, or None.

    Active: a pending subtask with ``metadata.schedule_id`` (fix runs carry it too), a running
    one within its timeout plus a grace, or a pending/running DAG whose ``schedule_id`` is this
    schedule (created by a scheduled turn's dag_create). A pending subtask or a live DAG older
    than NOUS_SCHEDULE_ACTIVE_RUN_MAX_AGE_SECONDS is not active: a wedged queue or a stuck DAG
    cannot hold a schedule forever. Database errors propagate (no fail-open double run).
    """
    oldest = now - timedelta(seconds=settings.schedule_active_run_max_age_seconds)
    timeout = func.coalesce(func.nullif(Subtask.timeout_seconds, 0), settings.subtask_default_timeout)
    running_until = func.coalesce(Subtask.started_at, Subtask.created_at) + func.make_interval(
        0, 0, 0, 0, 0, 0, timeout + _STALE_GRACE_SECONDS
    )
    async with heart.db.session() as session:
        subtask_id = await session.scalar(
            select(Subtask.id)
            .where(Subtask.agent_id == settings.agent_id)
            .where(Subtask.metadata_["schedule_id"].astext == schedule_id.hex)
            .where(
                or_(
                    and_(Subtask.status == "pending", Subtask.created_at > oldest),
                    and_(Subtask.status == "running", running_until > now),
                )
            )
            .order_by(Subtask.created_at)
            .limit(1)
        )
        if subtask_id is not None:
            return ActiveRun("subtask", subtask_id)
        dag_id = await session.scalar(
            select(ExecutionDAG.id)
            .where(ExecutionDAG.agent_id == settings.agent_id)
            .where(ExecutionDAG.schedule_id == schedule_id)
            .where(ExecutionDAG.status.in_(sorted(LIVE_DAG_STATUSES)))
            .where(ExecutionDAG.created_at > oldest)
            .order_by(ExecutionDAG.created_at)
            .limit(1)
        )
    return ActiveRun("dag", dag_id) if dag_id is not None else None


@asynccontextmanager
async def _fire_lock(heart: Heart, schedule_id: UUID) -> AsyncIterator[bool]:
    """F101: try to take ``schedule_id``'s fire lock for the block; yields whether it was taken.

    A transaction-scoped advisory lock, so the check loop and a manual run cannot both see
    no active run and both start one. A try-lock: a waiter would hold a pooled connection.
    """
    async with heart.db.session() as session:
        taken = await session.scalar(
            text("SELECT pg_try_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"schedule_fire:{schedule_id.hex}"}
        )
        yield bool(taken)
        # Leaving the session ends its transaction, which releases the lock.


async def _suppress_if_active(
    heart: Heart, settings: Settings, schedule: Schedule, now: datetime, bus: Any, trigger: str
) -> FireResult | None:
    """F101: the coalesced/skipped result when ``schedule``'s policy holds this fire back, else None.

    A held-back fire is logged and emitted as a ``schedule_fire_suppressed`` event (persisted
    by the bus when it is on) that names the active run it was folded into.
    """
    if schedule.concurrency_policy == "always_enqueue":
        return None
    active = await find_active_run(heart, settings, schedule.id, now)
    if active is None:
        return None
    outcome = "skipped" if schedule.concurrency_policy == "skip_if_active" else "coalesced"
    logger.info(
        "Schedule %s %s fire %s: %s %s is still active",
        schedule.id.hex[:8], trigger, outcome, active.kind, active.id,
    )
    if bus is not None:
        await bus.emit(
            Event(
                type="schedule_fire_suppressed",
                agent_id=settings.agent_id,
                data={
                    "schedule_id": schedule.id.hex,
                    "outcome": outcome,
                    "policy": schedule.concurrency_policy,
                    "trigger": trigger,
                    "active_run": {"kind": active.kind, "id": str(active.id)},
                },
            )
        )
    return FireResult(outcome, active.id)


async def run_schedule_now(heart: Heart, settings: Settings, schedule_id: UUID, *, bus: Any = None) -> FireResult:
    """F101: start one run of an active schedule now, unless its concurrency policy holds it back.

    Leaves next_fire_at, fire_count and the continuation cycle alone. Raises ``ScheduleNotFound``
    for an unknown, inactive, one-shot or other agent's schedule (a one-shot schedule's own fire
    does not take the fire lock), ``ScheduleBusy`` when another fire of it is in progress, and
    create_fire's ``ValueError`` (queue full, container closed).
    """
    schedule = await heart.schedules.get(schedule_id)
    if (
        schedule is None
        or schedule.agent_id != settings.agent_id
        or not schedule.active
        or schedule.schedule_type != "recurring"
    ):
        raise ScheduleNotFound(f"no active recurring schedule {schedule_id}")
    async with _fire_lock(heart, schedule_id) as taken:
        if not taken:
            raise ScheduleBusy(f"schedule {schedule_id} is being fired")
        held = await _suppress_if_active(heart, settings, schedule, datetime.now(UTC), bus, "manual")
        if held is not None:
            return held
        run = await create_fire(heart, settings, schedule)
    return FireResult("started", run.id)


def _runs_due(schedule: Schedule, now: datetime, settings: Settings) -> int:
    """F101: how many runs a check starts for ``schedule``: 1, or one per missed slot up to the cap.

    A slot counts as missed only when it is older than one check interval: a check that is
    merely late must not turn a short interval into a double fire. Never more than max_fires allows.
    """
    runs = 1
    if schedule.catch_up_policy == "enqueue_missed_with_cap":
        cap = schedule.catch_up_cap or settings.schedule_catch_up_cap
        missed_before = now - timedelta(seconds=settings.schedule_check_interval)
        slot = schedule.next_fire_at
        while runs < cap:
            if schedule.cron_expr:
                slot = croniter(schedule.cron_expr, slot).get_next(datetime)
            elif schedule.interval_seconds:
                slot = slot + timedelta(seconds=schedule.interval_seconds)
            else:
                break
            if slot > missed_before:
                break
            runs += 1
    if schedule.max_fires:
        runs = max(1, min(runs, schedule.max_fires - schedule.fire_count))
    return runs


class TaskScheduler:
    """Background scheduler that checks for due tasks and enqueues them.

    Runs a single asyncio task that wakes every schedule_check_interval
    seconds to fire any overdue schedules.
    """

    def __init__(self, heart: Heart, settings: Settings, bus: Any = None) -> None:
        self._heart = heart
        self._settings = settings
        self._bus = bus  # F101: coalesced/skipped fires; None when the event bus is off
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
                if cancel_requested():
                    break
                logger.exception("Schedule check was cancelled from within — the loop continues")
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

        Returns the number of runs started.
        """
        now = datetime.now(UTC)
        due_schedules = await self._heart.schedules.get_due(now)

        if not due_schedules:
            return 0

        # getattr: test doubles pass a bare settings namespace (the legacy path).
        policies = getattr(self._settings, "schedule_policies_enabled", False)
        fired = 0
        for schedule in due_schedules:
            try:
                if policies and schedule.schedule_type == "recurring":
                    # F101: once schedules keep the legacy path below.
                    fired += await self._fire_with_policies(schedule, now)
                    continue

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

                # Raises on a failed enqueue (queue full -> ValueError):
                # the lifecycle advance below is then skipped.
                await self._enqueue(schedule)

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
            except (asyncio.CancelledError, Exception) as exc:
                # A schedule cancelled from within fails alone, like a
                # schedule that raised; the other due schedules still fire.
                if isinstance(exc, asyncio.CancelledError) and cancel_requested():
                    raise
                logger.exception(
                    "Failed to fire schedule %s", schedule.id.hex[:8]
                )

        return fired

    async def _fire_with_policies(self, schedule: Schedule, now: datetime) -> int:
        """F101: fire one due recurring schedule under its policies; returns the runs started.

        Under the schedule's fire lock (a lock held elsewhere: nothing happens, retried next
        check), the row is re-read and each due run passes the concurrency gate. The slot then
        advances, also when every run was coalesced or skipped. An error before the first run
        started propagates and leaves the schedule unadvanced (retried, as before F101); one
        after it (a cancellation too) ends the catch-up there, and the runs already started
        are counted, so the next check cannot start the same slots again.
        """
        async with _fire_lock(self._heart, schedule.id) as taken:
            if not taken:
                logger.info("Schedule %s is being fired elsewhere — retried next check", schedule.id.hex[:8])
                return 0
            schedule = await self._heart.schedules.get(schedule.id)
            if schedule is None or not schedule.active or schedule.next_fire_at is None or schedule.next_fire_at > now:
                return 0
            schedule_id = schedule.id
            runs = _runs_due(schedule, now, self._settings)
            started = 0
            finished = False
            try:
                for i in range(runs):
                    if i and schedule.continuation_turns:
                        # The continuation cycle moved on with the previous run.
                        schedule = await self._heart.schedules.get(schedule_id)
                    if await _suppress_if_active(self._heart, self._settings, schedule, now, self._bus, "schedule"):
                        continue
                    await self._enqueue(schedule, started)
                    started += 1
                finished = True
            except Exception:
                if not started:
                    raise
                logger.warning(
                    "Schedule %s: catch-up stopped after %d of %d run(s)",
                    schedule_id.hex[:8], started, runs, exc_info=True,
                )
            finally:
                if finished or started:
                    await self._heart.schedules.advance(schedule_id, now, fires=started)
        return started

    async def _enqueue(self, schedule: Schedule, earlier_runs: int = 0) -> None:
        """Create one run of ``schedule``, then update its F064.5 continuation cycle.

        ``earlier_runs``: runs this check already started (F101 catch-up), whose fires
        are not yet in ``fire_count``. Raises when the run could not be created.
        """
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
                    f"schedule-{schedule.id.hex[:8]}-{schedule.fire_count + earlier_runs + 1}"
                )
                cycle_action = "fresh"
            continuation_metadata["session_id"] = planned_session

        # Create a subtask from the schedule. This is the failure
        # point — if it raises (queue full, transient DB error),
        # the post-success state-commit below is skipped.
        # F099: a fire is a new root under its schedule's container
        # (section 4.1 Schedules): remember when it notifies, else none.
        # A schedule from before the flag has no container: no parent.
        # A schedule stopped since get_due loaded it is refused inside
        # create (IntentionRootClosed, a ValueError): the queue-full arm
        # of the caller logs it and skips the fire without advancing.
        _enqueued = False
        try:
            await create_fire(self._heart, self._settings, schedule, metadata=continuation_metadata)
            _enqueued = True
        except ValueError:
            # Queue-full path: re-raise so the caller logs
            # warning + skips. Continuation state NOT touched —
            # phantom-session bug is closed.
            raise

        # F064.5 v1: state-commit only AFTER enqueue succeeded.
        # @codex P2 on a167217: each continuation state write is
        # wrapped in its own try/except so a transient DB failure
        # in (e.g.) bump_continuation_count doesn't skip the
        # caller's schedule.advance() call — that would leave the
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
            except (asyncio.CancelledError, Exception) as exc:
                if isinstance(exc, asyncio.CancelledError) and cancel_requested():
                    raise
                logger.exception(
                    "Failed to update continuation state for schedule %s — "
                    "lifecycle advance will proceed regardless",
                    schedule.id.hex[:8],
                )
