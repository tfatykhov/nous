"""F114: the goal driver. It acts on goal rows in the app process, one tick at a time.

A session of a goal is an ordinary continuation chain: its root is the first subtask's intention (internal
authority, wake policy ``continue``, ``nous.goals.roots``), and the continuation runner works it like any other root.
The driver starts a session when a goal is due and closes it, by polling, once its chain has ended.

The driver keeps no state that matters beyond its loop: wakes come from the rows (``next_wake_at``), so a restart
loses nothing but the count of failed starts in a row. Every time it writes comes from the database's clock. Every
change of a goal's columns is a targeted UPDATE guarded by ``state = 'active'``, never a save of the whole row, so an
owner's pause or cancel made meanwhile is never overwritten.

Nothing builds the driver yet: it is constructed by tests only.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from nous.brain import continuation, intentions
from nous.cancellation import cancel_requested
from nous.goals import charter, roots, spend
from nous.goals.store import GoalStore, clip
from nous.heart.censor_actions import gate_subtask_task
from nous.heart.subtasks import MAX_PENDING, SubtaskQueueFull
from nous.storage.database import Database
from nous.storage.models import Goal, GoalJournal, GoalSession, Intention, Subtask

logger = logging.getLogger(__name__)

QUEUE_FULL_DELAY_SECONDS = 60  # the subtask queue refused the first subtask: try again in a minute
FAILED_START_DELAY_SECONDS = 300  # any other failed start
OPEN_WORK_DELAY_SECONDS = 60  # a root of an earlier session has open work again (a late result reopened it)
FAILED_STARTS_BEFORE_PAUSE = 3  # the third failed start in a row pauses the goal (``error``)
FAILURE_TEXT_MAX = 300


@dataclass(frozen=True)
class TickReport:
    """What one tick did: the roots of the sessions it started, and why it started no more (``held``:
    ``halted`` or ``queue``), else None."""

    started: tuple[UUID, ...] = ()
    held: str | None = None


class GoalDriver:
    """Starts goal sessions and closes them. ``runner`` is the continuation runner (``wake``)."""

    def __init__(self, database: Database, settings: Any, heart: Any, store: GoalStore, runner: Any) -> None:
        self._db = database
        self._settings = settings
        self._heart = heart
        self._store = store
        self._runner = runner
        self._agent_id = store.agent_id
        self._failures: dict[UUID, int] = {}  # failed starts in a row, per goal (in memory: a restart resets it)
        self._task: asyncio.Task | None = None
        self._running = False

    # ---- the loop ------------------------------------------------------------------------------------------

    async def start(self) -> None:
        """Run the loop: one tick every ``goals_tick_seconds``. Does nothing, and builds no task, with goals off."""
        if getattr(self._settings, "goals_enabled", False) is not True or self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="goal-driver")
        logger.info("Goal driver started (tick=%ds)", self._settings.goals_tick_seconds)

    async def stop(self) -> None:
        """Cancel the loop and wait for it. A session in flight is the continuation runner's, not the loop's."""
        self._running = False
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            logger.info("Goal driver stopped")

    async def _loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._settings.goals_tick_seconds)
                await self.tick()
            except asyncio.CancelledError:
                if cancel_requested():
                    break
                logger.exception("Goal tick was cancelled from within; the loop continues")
            except Exception:
                logger.exception("Goal tick failed")

    async def tick(self) -> TickReport:
        """One pass: start the sessions that are due."""
        started, held = await self._start_due()
        return TickReport(started=started, held=held)

    # ---- starting a session --------------------------------------------------------------------------------

    def background_halted(self) -> bool:
        """Whether background work is halted, so no session may start. Nothing halts it yet: always False."""
        return False

    async def _start_held(self) -> str | None:
        """Why no session may start now, or None: background work is halted, or fewer than
        ``goals_queue_headroom`` places are free in the pending subtask queue (the count is not locked, so a start
        can still find the queue full: ``start_session`` handles that)."""
        if self.background_halted():
            return "halted"
        async with self._db.session() as s:
            pending = await s.scalar(
                select(func.count())
                .select_from(Subtask)
                .where(Subtask.agent_id == self._agent_id, Subtask.status == "pending")
            )
        if int(pending or 0) > MAX_PENDING - int(self._settings.goals_queue_headroom):
            return "queue"
        return None

    async def _due_goals(self) -> list[UUID]:
        """Active goals whose wake has come (by the database's clock) and that have no open session."""
        open_session = exists().where(GoalSession.goal_id == Goal.id, GoalSession.ended_at.is_(None))
        query = (
            select(Goal.id)
            .where(
                Goal.agent_id == self._agent_id,
                Goal.state == "active",
                Goal.next_wake_at <= func.clock_timestamp(),
                ~open_session,
            )
            .order_by(Goal.next_wake_at, Goal.id)
        )
        async with self._db.session() as s:
            return list((await s.execute(query)).scalars())

    async def _start_due(self) -> tuple[tuple[UUID, ...], str | None]:
        started: list[UUID] = []
        for goal_id in await self._due_goals():
            held = await self._start_held()
            if held is not None:
                return tuple(started), held
            try:
                root_id = await self.start_session(goal_id, trigger="timer")
            except Exception:
                logger.exception("Goal %s: the session start failed", goal_id)
                continue
            if root_id is not None:
                started.append(root_id)
        return tuple(started), None

    async def start_session(self, goal_id: UUID, *, trigger: str) -> UUID | None:
        """Start a session of an active goal: its root's id, or None when nothing started.

        Transaction 1 takes the goal row ``FOR UPDATE``, re-reads that the goal is active and has no open session
        and no open work left on an earlier session's root, and inserts the session row (its ``first_subtask_id``
        drawn here, before the subtask exists) and commits; the partial unique index on open sessions is the final
        word against a second start. Then the first task is built and goes through the censor gate that every
        spawned task goes through, and the subtask and its root intention are created in a transaction of their own.
        If that fails, the session row is undone. A full queue only delays the goal a minute; a refusal by the gate
        or any other failure is journalled and counted, and the third in a row pauses the goal (``error``)."""
        subtask_id = uuid.uuid4()
        open_work = False
        async with self._db.session() as s:
            goal = (
                await s.execute(
                    select(Goal).where(Goal.id == goal_id, Goal.agent_id == self._agent_id).with_for_update()
                )
            ).scalar_one_or_none()
            if goal is None or goal.state != "active":
                return None
            now = (await s.execute(select(func.clock_timestamp()))).scalar_one()
            if await self._has_open_session(s, goal_id):
                return None
            if await self._lineage_has_open_work(s, goal_id, now):
                open_work = True
            else:
                n = (
                    await s.execute(
                        select(func.coalesce(func.max(GoalSession.n), 0) + 1).where(GoalSession.goal_id == goal_id)
                    )
                ).scalar_one()
                row = GoalSession(
                    agent_id=self._agent_id,
                    goal_id=goal_id,
                    n=n,
                    trigger=trigger,
                    first_subtask_id=subtask_id,
                    started_at=now,
                )
                s.add(row)
                try:
                    await s.commit()
                except IntegrityError:  # the one-open-session index: another start won
                    await s.rollback()
                    return None
        seen = goal.next_wake_at
        if open_work:
            await self._store.delay_wake(goal_id, seconds=OPEN_WORK_DELAY_SECONDS, seen=seen)
            return None
        session_id = row.id
        try:
            task = await self._task_for(goal)
            rejection, task = await gate_subtask_task(self._heart, task)
            if rejection is not None:
                failure = f"Session {n} was refused by the censor gate: {rejection}"
            else:
                subtask = await roots.create_session_root(
                    self._heart, self._settings, goal=goal, session_n=n, subtask_id=subtask_id, task=task
                )
                failure = None
        except SubtaskQueueFull:
            if await self._undo_start(session_id, subtask_id):
                await self._store.delay_wake(goal_id, seconds=QUEUE_FULL_DELAY_SECONDS, seen=seen)
                logger.info("Goal %s: the subtask queue is full; session %d waits a minute", goal_id, n)
            return None
        except Exception as exc:
            logger.warning("Goal %s: session %d could not start", goal_id, n, exc_info=True)
            failure = f"Session {n} could not start: {type(exc).__name__}: {clip(str(exc), FAILURE_TEXT_MAX)}"
        if failure is not None:
            if await self._undo_start(session_id, subtask_id):
                await self._failed_start(goal_id, n, failure, seen=seen)
            return None
        root = await self._heart.intentions.get_for_source(intentions.SOURCE_SUBTASK, subtask.id)
        await self._set_root(session_id, root.id)
        self._failures.pop(goal_id, None)
        self._runner.wake()
        logger.info("Goal %s: session %d started (root %s)", goal_id, n, root.id)
        return root.id

    async def _task_for(self, goal: Goal) -> str:
        """The session's first task, from the goal as read under its lock, its journal tail and its spend."""
        rows = await self._store.journal_tail(goal.id)
        async with self._db.session() as s:
            total, today = await spend.goal_spent(s, self._agent_id, goal.id)
        return charter.build_session_task(goal, list(goal.plan or []), rows, spent=total, day_spent=today)

    async def _has_open_session(self, s: AsyncSession, goal_id: UUID) -> bool:
        query = select(
            exists().where(
                GoalSession.agent_id == self._agent_id,
                GoalSession.goal_id == goal_id,
                GoalSession.ended_at.is_(None),
            )
        )
        return bool((await s.execute(query)).scalar_one())

    async def _lineage_has_open_work(self, s: AsyncSession, goal_id: UUID, now: datetime) -> bool:
        """Whether a root of one of the goal's sessions, its deadline not passed, has an open intention. A result
        reopens a closed intention of a root with no marker until that root's deadline, so a closed session's chain
        can come back to life; a new session waits while it does."""
        root, member = aliased(Intention), aliased(Intention)
        query = select(
            exists().where(
                GoalSession.agent_id == self._agent_id,
                GoalSession.goal_id == goal_id,
                GoalSession.root_id.is_not(None),
                root.agent_id == self._agent_id,
                root.id == GoalSession.root_id,
                or_(root.deadline.is_(None), root.deadline > now),
                exists().where(
                    member.agent_id == self._agent_id,
                    member.root_id == root.id,
                    member.state.in_(continuation.OPEN_STATES),
                ),
            )
        )
        return bool((await s.execute(query)).scalar_one())

    async def _undo_start(self, session_id: UUID, subtask_id: UUID) -> bool:
        """Delete a session row whose first subtask was never created (the driver's only DELETE). Never one whose
        subtask or its intention exists: a create that committed and then raised left a live root, which the close
        attaches. True when the row was deleted."""
        async with self._db.session() as s:
            row = (
                await s.execute(
                    select(GoalSession.id)
                    .where(
                        GoalSession.id == session_id,
                        GoalSession.agent_id == self._agent_id,
                        GoalSession.root_id.is_(None),
                        GoalSession.ended_at.is_(None),
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            created = (
                await s.execute(
                    select(
                        exists().where(Subtask.agent_id == self._agent_id, Subtask.id == subtask_id)
                        | exists().where(
                            Intention.agent_id == self._agent_id,
                            Intention.source_kind == intentions.SOURCE_SUBTASK,
                            Intention.source_id == str(subtask_id),
                        )
                    )
                )
            ).scalar_one()
            if created:
                return False
            await s.execute(
                delete(GoalSession).where(
                    GoalSession.id == session_id, GoalSession.root_id.is_(None), GoalSession.ended_at.is_(None)
                )
            )
            await s.commit()
            return True

    async def _set_root(self, session_id: UUID, root_id: UUID) -> None:
        async with self._db.session() as s:
            await s.execute(
                update(GoalSession)
                .where(
                    GoalSession.id == session_id, GoalSession.agent_id == self._agent_id, GoalSession.root_id.is_(None)
                )
                .values(root_id=root_id)
            )
            await s.commit()

    async def _failed_start(self, goal_id: UUID, n: int, failure: str, *, seen: datetime | None) -> None:
        """Count a failed start. Below the limit: journal it and try again in five minutes. At the limit: pause the
        goal (``error``), saying why."""
        count = self._failures.get(goal_id, 0) + 1
        if count >= FAILED_STARTS_BEFORE_PAUSE:
            self._failures.pop(goal_id, None)
            note = f"{failure}\nPaused: the start failed {count} times in a row."
            await self._pause(goal_id, "error", note, session_n=n)
            return
        self._failures[goal_id] = count
        async with self._db.session() as s:
            self._journal(s, goal_id, "state", failure, actor="harness", session_n=n)
            await s.commit()
        await self._store.delay_wake(goal_id, seconds=FAILED_START_DELAY_SECONDS, seen=seen)

    # ---- goal rows -----------------------------------------------------------------------------------------

    async def _pause(self, goal_id: UUID, reason: str, note: str, *, session_n: int | None = None) -> bool:
        """An active goal to ``paused`` for ``reason``, its wake and any request cleared, with a harness ``state``
        row saying why, in one transaction. False (nothing written) when the goal is no longer active."""
        async with self._db.session() as s:
            result = await s.execute(
                update(Goal)
                .where(Goal.id == goal_id, Goal.agent_id == self._agent_id, Goal.state == "active")
                .values(
                    state="paused",
                    pause_reason=reason,
                    next_wake_at=None,
                    pending_request=None,
                    pending_request_at=None,
                    updated_at=func.clock_timestamp(),
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                return False
            self._journal(s, goal_id, "state", note, actor="harness", session_n=session_n)
            await s.commit()
            return True

    def _journal(
        self,
        s: AsyncSession,
        goal_id: UUID,
        kind: str,
        text: str,
        *,
        actor: str,
        session_n: int | None = None,
        evidence: Iterable[str] = (),
    ) -> GoalJournal:
        """A journal row in the caller's transaction, through the store's one row builder (its text bound)."""
        return self._store._add_journal(s, goal_id, kind, text, actor=actor, session_n=session_n, evidence=evidence)
