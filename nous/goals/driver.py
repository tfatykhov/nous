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
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, Text, and_, cast, delete, exists, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from nous.brain import continuation, intentions
from nous.cancellation import cancel_requested
from nous.dag.store import TERMINAL_NODE_STATUSES
from nous.goals import charter, roots, spend
from nous.goals.store import GoalStore, clip
from nous.heart.censor_actions import gate_subtask_task
from nous.heart.subtasks import MAX_PENDING, SubtaskQueueFull
from nous.storage.database import Database
from nous.storage.models import (
    DAGNode,
    DynamicCheckModel,
    ExecutionDAG,
    Goal,
    GoalJournal,
    GoalSession,
    Intention,
    IntentionArrival,
    ResultInbox,
    Subtask,
)

logger = logging.getLogger(__name__)

QUEUE_FULL_DELAY_SECONDS = 60  # the subtask queue refused the first subtask: try again in a minute
FAILED_START_DELAY_SECONDS = 300  # any other failed start
OPEN_WORK_DELAY_SECONDS = 60  # a root of an earlier session has open work again (a late result reopened it)
FAILED_STARTS_BEFORE_PAUSE = 3  # the third failed start in a row pauses the goal (``error``)
UNCREATED_AFTER_SECONDS = 120  # a session whose first subtask does not exist after this long never got one
SUMMARY_MAX = 1000  # a session's summary: its claimed report, else its newest arrival's note
SESSION_NOTE_LABEL = "Note written by the session:"  # marks the chain's words inside the harness's end row
# How long before a session's close a question or an answer may have been written and still be journalled after
# it (the close journals what it sees; a row it could not see was written in a transaction open across the close).
QUESTIONS_BEFORE_CLOSE = timedelta(hours=1)
ANSWER_PREFIX_PATTERN = f"{continuation.ANSWER_CORRELATION_PREFIX}%"

# The goal channel's claim: every undelivered row on it is stamped and returned in one statement, with the session
# it belongs to: the session whose root is the row's intention's root, else the one whose first subtask is that
# root's source (its root id not written yet), else the goal's newest session. No age bound and no row limit.
_CLAIM_SQL = text(
    """
    WITH claimed AS (
        UPDATE heart.result_inbox
           SET delivered_at = clock_timestamp(), delivered_session_id = :channel
         WHERE agent_id = :agent AND channel = :channel AND delivered_at IS NULL
        RETURNING id, body, intention_id, created_at
    )
    SELECT c.id, c.body,
           COALESCE(
               (SELECT gs.n FROM brain.intentions i
                  JOIN brain.goal_sessions gs
                    ON gs.agent_id = :agent AND gs.goal_id = :goal AND gs.root_id = i.root_id
                 WHERE i.agent_id = :agent AND i.id = c.intention_id),
               (SELECT gs.n FROM brain.intentions i
                  JOIN brain.intentions r ON r.agent_id = :agent AND r.id = i.root_id AND r.source_kind = 'subtask'
                  JOIN brain.goal_sessions gs
                    ON gs.agent_id = :agent AND gs.goal_id = :goal AND gs.first_subtask_id::text = r.source_id
                 WHERE i.agent_id = :agent AND i.id = c.intention_id),
               (SELECT max(gs.n) FROM brain.goal_sessions gs WHERE gs.agent_id = :agent AND gs.goal_id = :goal)
           ) AS session_n
      FROM claimed c
     ORDER BY c.created_at, c.id
    """
)


ONE_OPEN_SESSION_INDEX = "uq_goal_sessions_one_open"  # migration 113: at most one open session per goal


def _constraint_of(exc: IntegrityError) -> str | None:
    """The name of the constraint or index an INSERT violated, as the driver (asyncpg) reports it, else None."""
    for error in (exc.orig, getattr(exc.orig, "__cause__", None)):
        name = getattr(error, "constraint_name", None)
        if name:
            return str(name)
    return None


@dataclass(frozen=True)
class TickReport:
    """What one tick did: reports journalled from the goal channels, sessions closed, the roots of the sessions
    it started, and why it started no more (``held``: ``halted`` or ``queue``), else None."""

    claimed: int = 0
    closed: int = 0
    started: tuple[UUID, ...] = ()
    held: str | None = None


@dataclass(frozen=True)
class _RootState:
    found: bool
    marked: bool  # the root carries a cancel or an expiry marker
    open_work: bool  # an intention of the root is in an open state


@dataclass(frozen=True)
class _JournalItem:
    """A question or an answer to journal: its kind, actor, text and evidence refs."""

    kind: str
    actor: str
    text: str
    refs: list[str]


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
        # Closed sessions whose spend is read again at the next tick: closed since, or with lineage work whose
        # usage is not settled yet (``refresh_sessions``). In memory; the first tick after a start catches up.
        self._following: set[UUID] = set()
        self._caught_up = False
        self._task: asyncio.Task | None = None
        self._running = False

    # ---- the loop ------------------------------------------------------------------------------------------

    def _enabled(self) -> bool:
        return getattr(self._settings, "goals_enabled", False) is True

    async def start(self) -> None:
        """Run the loop: one tick every ``goals_tick_seconds``. Does nothing, and builds no task, with goals off."""
        if not self._enabled() or self._task is not None:
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
        """One pass, in order: claim the goal channels into the journals, close the sessions whose chain has ended,
        read again what open and recently closed sessions spent (and journal what a resumed chain of a closed one
        asked the owner), start the sessions that are due. With goals off it does nothing."""
        if not self._enabled():
            return TickReport()
        claimed = await self.claim_reports()
        closed = await self.close_finished()
        await self.refresh_sessions()
        started, held = await self._start_due()
        return TickReport(claimed=claimed, closed=closed, started=started, held=held)

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

        Transaction 1 takes the goal row ``FOR UPDATE``, re-reads that the goal is active (and, for a ``timer``
        start, that its wake has still come, by the database's clock: another tick may have started, ended and
        rescheduled it since the due list was read) and has no open session and no open work left on an earlier
        session's root, and inserts the session row (its ``first_subtask_id``
        drawn here, before the subtask exists) and commits; the partial unique index on open sessions is the final
        word against a second start (any other violation of the insert is a failed start). Then the first task is
        built and goes through the censor gate that every spawned task goes through, and the subtask and its root
        intention are created in a transaction of their own.
        If that fails, the session row is undone. A full queue only delays the goal a minute; a refusal by the gate
        or any other failure is journalled and counted, and the third in a row pauses the goal (``error``). With
        goals off, or while background work is halted, it does nothing: every caller (the tick, the owner's run-now)
        gets that refusal here. The queue headroom is the tick's check."""
        if not self._enabled() or self.background_halted():
            return None
        subtask_id = uuid.uuid4()
        open_work = False
        insert_failure: str | None = None
        async with self._db.session() as s:
            goal = (
                await s.execute(
                    select(Goal).where(Goal.id == goal_id, Goal.agent_id == self._agent_id).with_for_update()
                )
            ).scalar_one_or_none()
            if goal is None or goal.state != "active":
                return None
            seen = goal.next_wake_at  # the wake as read under the lock (a rollback below expires the object)
            now = (await s.execute(select(func.clock_timestamp()))).scalar_one()
            if trigger == "timer" and (seen is None or seen > now):
                return None  # another tick started, ended and rescheduled it since the due list was read
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
                except IntegrityError as exc:
                    await s.rollback()
                    if _constraint_of(exc) == ONE_OPEN_SESSION_INDEX:
                        return None  # another start won
                    logger.warning("Goal %s: session %d could not be recorded", goal_id, n, exc_info=True)
                    insert_failure = f"Session {n} could not start: {type(exc).__name__}."
        if open_work:
            await self._store.delay_wake(goal_id, seconds=OPEN_WORK_DELAY_SECONDS, seen=seen)
            return None
        if insert_failure is not None:  # no session row was written: nothing to undo
            await self._failed_start(goal_id, n, insert_failure, seen=seen)
            return None
        session_id = row.id
        try:
            rejection, task = await self._gated_task(goal)
            if rejection is not None:
                # A fixed sentence: the journal is part of the next task, which goes through the same gate, so the
                # censor's words in it would refuse every later start too. The message goes to the log only.
                logger.warning("Goal %s: session %d was refused by the censor gate: %s", goal_id, n, rejection)
                failure = f"Session {n} was refused by the censor gate; the log has the reason."
            else:
                root_id = await self._publish(goal, n, session_id, subtask_id, task)
                if root_id is None:  # recovery closed the session meanwhile: nothing was published
                    return None
                failure = None
        except SubtaskQueueFull:
            if await self._undo_start(session_id, subtask_id):
                await self._store.delay_wake(goal_id, seconds=QUEUE_FULL_DELAY_SECONDS, seen=seen)
                logger.info("Goal %s: the subtask queue is full; session %d waits a minute", goal_id, n)
            return None
        except Exception as exc:
            logger.warning("Goal %s: session %d could not start", goal_id, n, exc_info=True)
            # The class only: the journal is read by later sessions, the exception's text stays in the log.
            failure = f"Session {n} could not start: {type(exc).__name__}."
        if failure is not None:
            if await self._undo_start(session_id, subtask_id):
                await self._failed_start(goal_id, n, failure, seen=seen)
            return None
        self._failures.pop(goal_id, None)
        self._runner.wake()
        logger.info("Goal %s: session %d started (root %s)", goal_id, n, root_id)
        return root_id

    async def _publish(self, goal: Goal, n: int, session_id: UUID, subtask_id: UUID, task: str) -> UUID | None:
        """Create the session's first subtask and its root intention, and attach the root to the session, in ONE
        transaction that holds the session row ``FOR UPDATE`` and finds it still open with no root. The close's
        recovery of a session whose first subtask never appeared takes the same lock, so the two cannot both win:
        either recovery closed the session first and nothing is published (None; the start counts no failure), or
        the work and the root id are committed together and recovery then finds the root. The root's id, or None.
        A failure raises with nothing published."""
        async with self._db.session() as s:
            still_open = (
                await s.execute(
                    select(GoalSession.id)
                    .where(
                        GoalSession.id == session_id,
                        GoalSession.agent_id == self._agent_id,
                        GoalSession.ended_at.is_(None),
                        GoalSession.root_id.is_(None),
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if still_open is None:
                logger.info("Goal %s: session %d was closed before its first task was published", goal.id, n)
                return None
            subtask = await roots.create_session_root(
                self._heart, self._settings, goal=goal, session_n=n, subtask_id=subtask_id, task=task, session=s
            )
            root_id = (
                await s.execute(  # read in this transaction: another connection cannot see the new rows yet
                    select(Intention.id).where(
                        Intention.agent_id == self._agent_id,
                        Intention.source_kind == intentions.SOURCE_SUBTASK,
                        Intention.source_id == str(subtask.id),
                    )
                )
            ).scalar_one()
            if not await self._set_root(s, session_id, root_id):
                await s.rollback()
                return None
            await s.commit()
            return root_id

    async def _gated_task(self, goal: Goal) -> tuple[str | None, str]:
        """``(rejection, task)``: the session's first task (from the goal as read under its lock, its journal tail
        and its spend) through the censor gate that every spawned task goes through. A steering censor appends its
        guidance; the task is then built again with that guidance's length kept free, so the stored task stays
        within ``charter.MAX_TASK_CHARS`` and the guidance stays whole."""
        rows = await self._store.journal_tail(goal.id)
        async with self._db.session() as s:
            total, today = await spend.goal_spent(s, self._agent_id, goal.id)

        def build(max_chars: int) -> str:
            plan = list(goal.plan or [])
            return charter.build_session_task(goal, plan, rows, spent=total, day_spent=today, max_chars=max_chars)

        task = build(charter.MAX_TASK_CHARS)
        rejection, gated = await gate_subtask_task(self._heart, task)
        if rejection is not None or len(gated) <= charter.MAX_TASK_CHARS:
            return rejection, gated
        if not gated.startswith(task):
            raise ValueError("the censor gate changed the task instead of adding to it")
        guidance = gated[len(task) :]
        return None, build(charter.MAX_TASK_CHARS - len(guidance)) + guidance

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
        """Whether a root of one of the goal's sessions, with no cancel or expiry marker and its deadline not passed,
        has an open intention. A result reopens a closed intention of a root with no marker until that root's
        deadline, so a closed session's chain can come back to life; a new session waits while it does. A marked
        root has ended, even while an intention of it is not closed yet: it holds nothing."""
        root, member = aliased(Intention), aliased(Intention)
        query = select(
            exists().where(
                GoalSession.agent_id == self._agent_id,
                GoalSession.goal_id == goal_id,
                GoalSession.root_id.is_not(None),
                root.agent_id == self._agent_id,
                root.id == GoalSession.root_id,
                root.root_cancelled_at.is_(None),
                root.root_expired_at.is_(None),
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

    async def _set_root(self, s: AsyncSession, session_id: UUID, root_id: UUID) -> bool:
        """Attach ``root_id`` to a session that is still open and has none, in the caller's transaction. False when
        the session was closed or got a root meanwhile (nothing written)."""
        result = await s.execute(
            update(GoalSession)
            .where(
                GoalSession.id == session_id,
                GoalSession.agent_id == self._agent_id,
                GoalSession.root_id.is_(None),
                GoalSession.ended_at.is_(None),
            )
            .values(root_id=root_id)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

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

    # ---- the goal channel ----------------------------------------------------------------------------------

    async def claim_reports(self) -> int:
        """Journal every undelivered row on each goal's channel (``goal:<id>``) as a ``report`` row. One
        transaction per goal stamps the rows and writes their journal rows, so a failure between the two loses
        nothing: the rows stay undelivered for the next tick. Returns the rows journalled."""
        channel_of_goal = func.concat("goal:", cast(Goal.id, Text))
        waiting = exists().where(
            ResultInbox.agent_id == self._agent_id,
            ResultInbox.channel == channel_of_goal,
            ResultInbox.delivered_at.is_(None),
        )
        async with self._db.session() as s:
            goal_ids = list(
                (await s.execute(select(Goal.id).where(Goal.agent_id == self._agent_id, waiting))).scalars()
            )
        claimed = 0
        for goal_id in goal_ids:
            try:
                claimed += await self._claim_channel(goal_id)
            except Exception:
                logger.exception("Goal %s: its channel could not be claimed; the rows wait for the next tick", goal_id)
        return claimed

    async def _claim_channel(self, goal_id: UUID) -> int:
        channel = f"goal:{goal_id}"
        async with self._db.session() as s:
            rows = (await s.execute(_CLAIM_SQL, {"agent": self._agent_id, "channel": channel, "goal": goal_id})).all()
            for row in rows:
                self._journal(
                    s, goal_id, "report", row.body, actor="chain", session_n=row.session_n, evidence=[f"row:{row.id}"]
                )
            await s.commit()
        return len(rows)

    # ---- closing a session ---------------------------------------------------------------------------------

    async def close_finished(self) -> int:
        """Close every open session whose chain has ended. Returns how many were closed."""
        closed = 0
        for session_id in await self._sessions_to_close():
            try:
                if await self._close(session_id):
                    closed += 1
            except Exception:
                logger.exception("Goal session %s could not be closed; it is tried again next tick", session_id)
        return closed

    async def _sessions_to_close(self) -> list[UUID]:
        """The first, unlocked read: open sessions with no root id yet, or whose root is marked or has no open
        intention. ``_close`` reads all of it again under the session's lock."""
        root, member = aliased(Intention), aliased(Intention)
        open_work = exists().where(
            member.agent_id == self._agent_id,
            member.root_id == root.id,
            member.state.in_(continuation.OPEN_STATES),
        )
        query = (
            select(GoalSession.id)
            .outerjoin(root, and_(root.agent_id == self._agent_id, root.id == GoalSession.root_id))
            .where(
                GoalSession.agent_id == self._agent_id,
                GoalSession.ended_at.is_(None),
                or_(
                    GoalSession.root_id.is_(None),
                    root.root_cancelled_at.is_not(None),
                    root.root_expired_at.is_not(None),
                    ~open_work,  # also a root whose row is missing: nothing has its id as root_id
                ),
            )
            .order_by(GoalSession.started_at, GoalSession.id)
        )
        async with self._db.session() as s:
            return list((await s.execute(query)).scalars())

    async def _close(self, session_id: UUID) -> bool:
        """Close one session if its chain has ended, in one transaction holding the session row ``FOR UPDATE``.

        A session with no root id yet gets it from its first subtask's intention; with no such intention after
        ``UNCREATED_AFTER_SECONDS`` it is closed ``interrupted``. Whether the chain has ended is read again inside
        the transaction. The close writes the session's outcome, spend, steps and summary (``WHERE ended_at IS
        NULL``), journals its questions and the owner's answers, moves an active goal's next wake one interval on
        after a closed step and two otherwise, and journals the end (``_end``). True when it closed the session."""
        async with self._db.session() as s:
            row = (
                await s.execute(
                    select(GoalSession)
                    .where(
                        GoalSession.id == session_id,
                        GoalSession.agent_id == self._agent_id,
                        GoalSession.ended_at.is_(None),
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            now = (await s.execute(select(func.clock_timestamp()))).scalar_one()
            goal = (
                await s.execute(select(Goal).where(Goal.id == row.goal_id, Goal.agent_id == self._agent_id))
            ).scalar_one()
            if row.root_id is None:
                root_id = (
                    await s.execute(
                        select(Intention.id).where(
                            Intention.agent_id == self._agent_id,
                            Intention.source_kind == intentions.SOURCE_SUBTASK,
                            Intention.source_id == str(row.first_subtask_id),
                        )
                    )
                ).scalar_one_or_none()
                if root_id is None:
                    if (now - row.started_at).total_seconds() < UNCREATED_AFTER_SECONDS:
                        return False  # the start may still be creating it
                    await self._end(
                        s,
                        goal,
                        row,
                        now,
                        outcome="interrupted",
                        spent=0,
                        steps=0,
                        summary=None,
                        detail="Its first task was never created.",
                    )
                    await s.commit()
                    return True
                if not await self._set_root(s, row.id, root_id):  # cannot fail under this lock; checked all the same
                    return False
                row.root_id = root_id
            state = await self._root_state(s, row.root_id)
            if state.found and not state.marked and state.open_work:
                await s.commit()  # keeps a repaired root id
                return False
            spent = (await spend.session_spent(s, self._agent_id, [row.root_id]))[row.root_id]
            steps = sum(1 for step in goal.plan or [] if isinstance(step, dict) and step.get("closed_session") == row.n)
            outcome = await self._outcome(s, row.root_id, steps=steps, ended_by_marker=state.marked or not state.found)
            summary, from_report = await self._summary(s, row)
            await self._journal_questions_and_answers(s, goal.id, row)
            ended = await self._end(
                s,
                goal,
                row,
                now,
                outcome=outcome,
                spent=spent,
                steps=steps,
                summary=summary,
                session_note=None if from_report else summary,
            )
            if not ended:
                return False
            await s.commit()
            self._following.add(row.id)  # read once more after the close, and while its lineage is unsettled
            logger.info("Goal %s: session %d closed (%s, %d tokens)", goal.id, row.n, outcome, spent)
            return True

    async def _root_state(self, s: AsyncSession, root_id: UUID) -> _RootState:
        marks = (
            await s.execute(
                select(Intention.root_cancelled_at, Intention.root_expired_at).where(
                    Intention.agent_id == self._agent_id, Intention.id == root_id
                )
            )
        ).one_or_none()
        open_work = (
            await s.execute(
                select(
                    exists().where(
                        Intention.agent_id == self._agent_id,
                        Intention.root_id == root_id,
                        Intention.state.in_(continuation.OPEN_STATES),
                    )
                )
            )
        ).scalar_one()
        if marks is None:
            return _RootState(found=False, marked=False, open_work=bool(open_work))
        marked = marks.root_cancelled_at is not None or marks.root_expired_at is not None
        return _RootState(found=True, marked=marked, open_work=bool(open_work))

    async def _outcome(self, s: AsyncSession, root_id: UUID, *, steps: int, ended_by_marker: bool) -> str:
        """The first that holds: ``error`` (an intention of the lineage closed ``failed_report``), ``step_closed``
        (the plan has a step closed in this session), ``asked`` (an arrival of the root decided ``ask``; an ask
        leaves the chain open, so it is never the newest at close), ``interrupted`` (the root was cancelled or
        expired), else ``no_step``."""
        failed = (
            await s.execute(
                select(
                    exists().where(
                        Intention.agent_id == self._agent_id,
                        Intention.root_id == root_id,
                        Intention.close_reason == continuation.CLOSE_FAILED_REPORT,
                    )
                )
            )
        ).scalar_one()
        if failed:
            return "error"
        if steps:
            return "step_closed"
        asked = (
            await s.execute(
                select(
                    exists().where(
                        IntentionArrival.agent_id == self._agent_id,
                        IntentionArrival.root_id == root_id,
                        IntentionArrival.decision == "ask",
                    )
                )
            )
        ).scalar_one()
        if asked:
            return "asked"
        return "interrupted" if ended_by_marker else "no_step"

    async def _summary(self, s: AsyncSession, row: GoalSession) -> tuple[str | None, bool]:
        """``(summary, from_report)``: the session's newest claimed report, else its root's newest arrival note,
        clipped to ``SUMMARY_MAX``; ``from_report`` says which."""
        text_ = (
            await s.execute(
                select(GoalJournal.text)
                .where(
                    GoalJournal.agent_id == self._agent_id,
                    GoalJournal.goal_id == row.goal_id,
                    GoalJournal.kind == "report",
                    GoalJournal.session_n == row.n,
                )
                .order_by(GoalJournal.seq.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if text_:
            return clip(text_, SUMMARY_MAX), True
        if row.root_id is not None:
            text_ = (
                await s.execute(
                    select(IntentionArrival.note)
                    .where(
                        IntentionArrival.agent_id == self._agent_id,
                        IntentionArrival.root_id == row.root_id,
                        IntentionArrival.note.is_not(None),
                    )
                    .order_by(IntentionArrival.n.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        return (clip(text_, SUMMARY_MAX) if text_ else None), False

    async def _new_questions_and_answers(self, s: AsyncSession, rows: Iterable[Any]) -> dict[UUID, list[_JournalItem]]:
        """For each session in ``rows`` (each with ``id``, ``goal_id``, ``root_id`` and ``ended_at``), the questions
        to the owner and the owner's answers of its lineage that its goal's journal does not hold yet, oldest first.
        A row is known by its evidence: ``row:<id>``, and for an answer also ``arrival:<id>``, because an answer is
        written once per intention it wakes. Only rows created from an hour before a closed session's close are
        read: those before were journalled by the close. Reads only."""
        by_root = {r.root_id: r for r in rows if r.root_id is not None}
        if not by_root:
            return {}
        member = aliased(Intention)
        query = (
            select(member.root_id, ResultInbox.id, ResultInbox.msg_type, ResultInbox.body, ResultInbox.arrival_id)
            .join(member, and_(member.agent_id == self._agent_id, member.id == ResultInbox.intention_id))
            .where(
                ResultInbox.agent_id == self._agent_id,
                ResultInbox.source_kind == continuation.SOURCE_INTENTION_REPORT,
                member.root_id.in_(sorted(by_root)),
                or_(
                    ResultInbox.msg_type == continuation.MSG_QUESTION,
                    and_(ResultInbox.msg_type == "INFORM", ResultInbox.correlation_id.like(ANSWER_PREFIX_PATTERN)),
                ),
            )
            .order_by(ResultInbox.created_at, ResultInbox.id)
        )
        ended = [r.ended_at for r in by_root.values()]
        if ended and all(at is not None for at in ended):
            query = query.where(ResultInbox.created_at >= min(ended) - QUESTIONS_BEFORE_CLOSE)
        source = (await s.execute(query)).all()
        if not source:
            return {}
        seen: dict[UUID, set[str]] = {}
        for goal_id, evidence in (
            await s.execute(
                select(GoalJournal.goal_id, GoalJournal.evidence).where(
                    GoalJournal.agent_id == self._agent_id,
                    GoalJournal.goal_id.in_(sorted({by_root[item.root_id].goal_id for item in source})),
                    GoalJournal.kind.in_(("question", "answer")),
                )
            )
        ).all():
            seen.setdefault(goal_id, set()).update(str(ref) for ref in evidence or [])
        found: dict[UUID, list[_JournalItem]] = {}
        for item in source:
            row = by_root[item.root_id]
            refs = [f"row:{item.id}"]
            if item.msg_type == continuation.MSG_QUESTION:
                kind, actor = "question", "chain"
            else:
                kind, actor = "answer", "owner"
                refs.append(f"arrival:{item.arrival_id}")
            known = seen.setdefault(row.goal_id, set())
            if any(ref in known for ref in refs):
                continue
            known.update(refs)
            found.setdefault(row.id, []).append(_JournalItem(kind, actor, item.body, refs))
        return found

    async def _journal_questions_and_answers(self, s: AsyncSession, goal_id: UUID, row: GoalSession) -> int:
        """Journal the session's questions to the owner (``question``) and the owner's answers (``answer``) that the
        journal does not hold yet (``_new_questions_and_answers``), in the caller's transaction. The caller holds the
        session row's lock, so two writers cannot journal the same row twice. Returns the rows journalled."""
        items = (await self._new_questions_and_answers(s, [row])).get(row.id, [])
        for item in items:
            self._journal(s, goal_id, item.kind, item.text, actor=item.actor, session_n=row.n, evidence=item.refs)
        return len(items)

    async def _reconcile_questions_and_answers(self, session_id: UUID) -> int:
        """Journal what a closed session's resumed chain asked the owner and what the owner answered, in a
        transaction of its own that holds the session row's lock (as the close does). Returns the rows journalled."""
        async with self._db.session() as s:
            row = (
                await s.execute(
                    select(GoalSession)
                    .where(GoalSession.id == session_id, GoalSession.agent_id == self._agent_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                return 0
            written = await self._journal_questions_and_answers(s, row.goal_id, row)
            await s.commit()
            return written

    async def _end(
        self,
        s: AsyncSession,
        goal: Goal,
        row: GoalSession,
        now: datetime,
        *,
        outcome: str,
        spent: int,
        steps: int,
        summary: str | None,
        session_note: str | None = None,
        detail: str | None = None,
    ) -> bool:
        """The close's writes, in the caller's transaction: the session row (only while still open), an active
        goal's next wake, and the harness ``state`` row. False when the session was closed meanwhile (nothing else
        is written).

        The ``state`` row's own words are the harness's (``detail`` included). The chain's text goes in only as
        ``session_note`` (its newest arrival note, when the session sent no report), on one line after a fixed label
        and at most ``SUMMARY_MAX`` characters, so a session that ended in ``drop`` still leaves the next one a
        memory. A claimed report is not repeated here:
        it has its own ``report`` row, under the chain's name."""
        result = await s.execute(
            update(GoalSession)
            .where(GoalSession.id == row.id, GoalSession.ended_at.is_(None))
            .values(
                ended_at=now,
                outcome=outcome,
                spent=func.greatest(GoalSession.spent, spent),  # never lower than a newer read already wrote
                steps_closed=steps,
                summary=summary,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            return False
        interval = timedelta(seconds=goal.min_interval_seconds * (1 if outcome == "step_closed" else 2))
        await s.execute(
            update(Goal)
            .where(Goal.id == goal.id, Goal.agent_id == self._agent_id, Goal.state == "active")
            .values(next_wake_at=now + interval, updated_at=func.clock_timestamp())
            .execution_options(synchronize_session=False)
        )
        note = f"Session {row.n} ended: {outcome}, {spent} tokens."
        if detail:
            note += f"\n{detail}"
        # The chain's note on one line after the label (line breaks and runs of whitespace become one space), so no
        # line of it stands alone in the harness's entry.
        flat_note = clip(" ".join((session_note or "").split()), SUMMARY_MAX)
        if flat_note:
            note += f"\n{SESSION_NOTE_LABEL} {flat_note}"
        self._journal(s, goal.id, "state", note, actor="harness", session_n=row.n)
        return True

    async def refresh_sessions(self) -> int:
        """Read the spend of these sessions again, and journal the questions and answers of the closed ones among
        them: the open ones; the closed ones whose root's deadline has not passed (a late result can reopen the chain
        and add to it); and the closed ones followed in memory (``_following``). A session is followed from its
        close while its lineage has work whose tokens are still to come (``_has_unsettled_work``), and is read once
        more after that: the root's deadline is no accounting cutoff, since an expiry or a cancel does not stop a
        running worker or a DAG, which store their tokens later. Then the session keeps what it had and is not read
        again.

        Why a flag in memory rather than a time window: each tick reads only the open sessions, those whose root's
        deadline has not passed and the followed ones, all a handful, and checks unsettled work only over their
        roots. After a restart the flags are gone, so the first tick also reads, once, every session closed within
        ``intention_root_ttl_hours`` and every closed session whose lineage still has unsettled work, whatever its
        age: work that settled while no driver ran is counted then. Within a tick, whether a closed session's work
        has settled is read BEFORE its spend (and its questions), and the session is dropped only on that earlier
        read: what is stored between the reads is in this tick's read, or the session stays followed one more tick.

        A closed session's root can resume (a late result reopens it), and its chain can then ask the owner and get
        an answer after the close journalled the session's questions: every tick, the questions and answers of the
        closed sessions read here that the journal does not hold yet are journalled, each session in a transaction
        of its own holding the session row's lock, as the close does. Returns the sessions whose spend changed."""
        root = aliased(Intention)
        reasons = [GoalSession.ended_at.is_(None), root.deadline > func.clock_timestamp()]
        if self._following:
            reasons.append(GoalSession.id.in_(sorted(self._following)))
        if not self._caught_up:
            window = timedelta(hours=float(self._settings.intention_root_ttl_hours))
            reasons.append(GoalSession.ended_at > func.clock_timestamp() - window)
        columns = (
            GoalSession.id,
            GoalSession.goal_id,
            GoalSession.n,
            GoalSession.root_id,
            GoalSession.spent,
            GoalSession.ended_at,
        )
        query = (
            select(*columns)
            .join(root, and_(root.agent_id == self._agent_id, root.id == GoalSession.root_id))
            .where(GoalSession.agent_id == self._agent_id, or_(*reasons))
        )
        changed = 0
        async with self._db.session() as s:
            rows = {r.id: r for r in (await s.execute(query)).all()}
            if not self._caught_up:
                unsettled_anywhere = select(*columns).where(
                    GoalSession.agent_id == self._agent_id,
                    GoalSession.ended_at.is_not(None),
                    GoalSession.root_id.is_not(None),
                    self._has_unsettled_work(GoalSession.root_id),
                )
                for r in (await s.execute(unsettled_anywhere)).all():
                    rows.setdefault(r.id, r)
            # Settled or not is read BEFORE the spend: when this read finds a closed session's work settled, every
            # token of it was stored before the spend read below, which is then final. Read the other way round,
            # usage stored between the two reads would be missed and the session dropped for good.
            closed_roots = sorted({r.root_id for r in rows.values() if r.ended_at is not None})
            unsettled_roots: set[UUID] = set()
            if closed_roots:
                unsettled_roots = set(
                    (
                        await s.execute(
                            select(Intention.id).where(
                                Intention.agent_id == self._agent_id,
                                Intention.id.in_(closed_roots),
                                self._has_unsettled_work(Intention.id),
                            )
                        )
                    ).scalars()
                )
            asked: dict[UUID, list[_JournalItem]] = {}
            if rows:
                totals = await spend.session_spent(s, self._agent_id, [r.root_id for r in rows.values()])
                for r in rows.values():
                    if totals[r.root_id] > r.spent:
                        # Every source only grows: a read taken before another tick wrote a newer total is older,
                        # and must not put a lower one back.
                        result = await s.execute(
                            update(GoalSession)
                            .where(GoalSession.id == r.id, GoalSession.spent < totals[r.root_id])
                            .values(spent=totals[r.root_id])
                        )
                        changed += result.rowcount
                asked = await self._new_questions_and_answers(s, [r for r in rows.values() if r.ended_at is not None])
            await s.commit()
        self._following = {r.id for r in rows.values() if r.ended_at is not None and r.root_id in unsettled_roots}
        self._caught_up = True
        for session_id in asked:
            try:
                await self._reconcile_questions_and_answers(session_id)
            except Exception:
                logger.exception("Goal session %s: its questions could not be journalled; tried again", session_id)
                self._following.add(session_id)  # read again at the next tick, whatever its deadline
        return changed

    def _unsettled_subtask(self, subtask: Any) -> ColumnElement[bool]:
        """A subtask whose token counters can still change, as ``nous.dag.orchestrator._subtask_usage_settled``
        decides for the DAG roll-up: not yet completed, failed or cancelled; or cancelled after it started and its
        run has not reported (``attempts`` is 0) while its timeout and the grace have not passed (a cancel does not
        stop the worker, which stores its usage when it unwinds)."""
        grace = int(self._settings.dag_node_timeout_grace_seconds)
        report_by = subtask.started_at + func.make_interval(0, 0, 0, 0, 0, 0, subtask.timeout_seconds + grace)
        return or_(
            subtask.status.not_in(("completed", "failed", "cancelled")),
            and_(
                subtask.status == "cancelled",
                subtask.started_at.is_not(None),
                subtask.attempts == 0,
                report_by > func.clock_timestamp(),
            ),
        )

    def _unsettled_dag(self, dag: Any) -> ColumnElement[bool]:
        """A DAG whose ``tokens_consumed`` can still rise, as the orchestrator accounts it: not terminal yet; or
        terminal with a subtask-backed node whose tokens are not claimed into the total yet, while the orchestrator
        still claims them (its sweep's window after the DAG ended, or until the DAG is delivered when delivery is
        on); or with a check node whose heartbeat check is still enabled (each run of it adds its tokens to the
        total, whatever the DAG's status)."""
        node, check_node, check = aliased(DAGNode), aliased(DAGNode), aliased(DynamicCheckModel)
        window = timedelta(
            seconds=int(self._settings.dag_node_max_timeout) + int(self._settings.dag_node_timeout_grace_seconds)
        )
        still_claimed = [and_(dag.completed_at.is_not(None), dag.completed_at + window > func.clock_timestamp())]
        if getattr(self._settings, "dag_result_delivery_enabled", False) is True:
            still_claimed.append(dag.delivered_at.is_(None))
        unclaimed = exists().where(
            node.dag_id == dag.id,
            node.subtask_id.is_not(None),
            node.tokens_counted.is_(False),
            or_(node.status.not_in(sorted(TERMINAL_NODE_STATUSES)), *still_claimed),
        )
        checking = exists().where(
            check_node.dag_id == dag.id,
            check_node.node_type == "check",
            check.agent_id == self._agent_id,
            check.name == check_node.check_name,
            check.enabled.is_(True),
        )
        return or_(dag.status.not_in(intentions.TERMINAL_DAG_STATUSES), unclaimed, checking)

    def _has_unsettled_work(self, root_id: Any) -> ColumnElement[bool]:
        """Whether the lineage of ``root_id`` (a column) can still add to its spend, source by source of
        ``continuation.root_token_parts``: a lineage subtask whose usage is still to come (``_unsettled_subtask``);
        a lineage DAG whose total can still rise (``_unsettled_dag``); and, for a root with no cancel or expiry
        marker, an intention in an open state, since an arrival's tokens and a failed attempt's ``failed_tokens``
        are written only under a live claim on an open intention (both fenced on the claim token, so a claim the
        marker ended writes nothing)."""
        member_subtask, subtask = aliased(Intention), aliased(Subtask)
        member_dag, dag = aliased(Intention), aliased(ExecutionDAG)
        member_open, marked = aliased(Intention), aliased(Intention)
        subtasks = exists().where(
            member_subtask.agent_id == self._agent_id,
            member_subtask.root_id == root_id,
            member_subtask.source_kind == intentions.SOURCE_SUBTASK,
            subtask.agent_id == self._agent_id,
            cast(subtask.id, Text) == member_subtask.source_id,
            self._unsettled_subtask(subtask),
        )
        dags = exists().where(
            member_dag.agent_id == self._agent_id,
            member_dag.root_id == root_id,
            member_dag.source_kind == intentions.SOURCE_DAG,
            dag.agent_id == self._agent_id,
            cast(dag.id, Text) == member_dag.source_id,
            self._unsettled_dag(dag),
        )
        open_claimable = and_(
            exists().where(
                member_open.agent_id == self._agent_id,
                member_open.root_id == root_id,
                member_open.state.in_(continuation.OPEN_STATES),
            ),
            ~exists().where(
                marked.agent_id == self._agent_id,
                marked.id == root_id,
                or_(marked.root_cancelled_at.is_not(None), marked.root_expired_at.is_not(None)),
            ),
        )
        return or_(subtasks, dags, open_claimable)

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
        """A journal row in the caller's transaction (``GoalStore.add_journal_row``, with the journal's bounds)."""
        return self._store.add_journal_row(s, goal_id, kind, text, actor=actor, session_n=session_n, evidence=evidence)
