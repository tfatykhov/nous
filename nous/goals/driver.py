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

from sqlalchemy import Text, and_, cast, delete, exists, func, or_, select, text, update
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
from nous.storage.models import Goal, GoalJournal, GoalSession, Intention, IntentionArrival, ResultInbox, Subtask

logger = logging.getLogger(__name__)

QUEUE_FULL_DELAY_SECONDS = 60  # the subtask queue refused the first subtask: try again in a minute
FAILED_START_DELAY_SECONDS = 300  # any other failed start
OPEN_WORK_DELAY_SECONDS = 60  # a root of an earlier session has open work again (a late result reopened it)
FAILED_STARTS_BEFORE_PAUSE = 3  # the third failed start in a row pauses the goal (``error``)
UNCREATED_AFTER_SECONDS = 120  # a session whose first subtask does not exist after this long never got one
SUMMARY_MAX = 1000  # a session's summary: its claimed report, else its newest arrival's note
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
        read the spend of open and recently closed sessions again, start the sessions that are due. With goals off
        it does nothing."""
        if not self._enabled():
            return TickReport()
        claimed = await self.claim_reports()
        closed = await self.close_finished()
        await self.refresh_spent()
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

        Transaction 1 takes the goal row ``FOR UPDATE``, re-reads that the goal is active and has no open session
        and no open work left on an earlier session's root, and inserts the session row (its ``first_subtask_id``
        drawn here, before the subtask exists) and commits; the partial unique index on open sessions is the final
        word against a second start. Then the first task is built and goes through the censor gate that every
        spawned task goes through, and the subtask and its root intention are created in a transaction of their own.
        If that fails, the session row is undone. A full queue only delays the goal a minute; a refusal by the gate
        or any other failure is journalled and counted, and the third in a row pauses the goal (``error``). With
        goals off it does nothing."""
        if not self._enabled():
            return None
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
            # The class only: the journal is read by later sessions, the exception's text stays in the log.
            failure = f"Session {n} could not start: {type(exc).__name__}."
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
                    root.id.is_(None),
                    root.root_cancelled_at.is_not(None),
                    root.root_expired_at.is_not(None),
                    ~open_work,
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
        after a closed step and two otherwise, and journals the end with the summary, so even a session that ended
        in ``drop`` leaves the next one a memory. True when it closed the session."""
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
                await s.execute(update(GoalSession).where(GoalSession.id == row.id).values(root_id=root_id))
                row.root_id = root_id
            state = await self._root_state(s, row.root_id)
            if state.found and not state.marked and state.open_work:
                await s.commit()  # keeps a repaired root id
                return False
            spent = (await spend.session_spent(s, self._agent_id, [row.root_id]))[row.root_id]
            steps = sum(1 for step in goal.plan or [] if isinstance(step, dict) and step.get("closed_session") == row.n)
            outcome = await self._outcome(s, row.root_id, steps=steps, ended_by_marker=state.marked or not state.found)
            summary = await self._summary(s, row)
            await self._journal_questions_and_answers(s, goal.id, row)
            if not await self._end(s, goal, row, now, outcome=outcome, spent=spent, steps=steps, summary=summary):
                return False
            await s.commit()
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

    async def _summary(self, s: AsyncSession, row: GoalSession) -> str | None:
        """The session's newest claimed report, else its root's newest arrival note, clipped to ``SUMMARY_MAX``."""
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
        if not text_ and row.root_id is not None:
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
        return clip(text_, SUMMARY_MAX) if text_ else None

    async def _journal_questions_and_answers(self, s: AsyncSession, goal_id: UUID, row: GoalSession) -> int:
        """Journal the session's questions to the owner (``question``) and the owner's answers (``answer``), each
        once: a row's evidence names its inbox row (``row:<id>``), and an answer's also its arrival, because an
        answer is written once per intention it wakes. Returns the rows journalled."""
        if row.root_id is None:
            return 0
        lineage = select(Intention.id).where(Intention.agent_id == self._agent_id, Intention.root_id == row.root_id)
        source = (
            await s.execute(
                select(ResultInbox.id, ResultInbox.msg_type, ResultInbox.body, ResultInbox.arrival_id)
                .where(
                    ResultInbox.agent_id == self._agent_id,
                    ResultInbox.source_kind == continuation.SOURCE_INTENTION_REPORT,
                    ResultInbox.intention_id.in_(lineage),
                    or_(
                        ResultInbox.msg_type == continuation.MSG_QUESTION,
                        and_(ResultInbox.msg_type == "INFORM", ResultInbox.correlation_id.like(ANSWER_PREFIX_PATTERN)),
                    ),
                )
                .order_by(ResultInbox.created_at, ResultInbox.id)
            )
        ).all()
        if not source:
            return 0
        seen: set[str] = set()
        for evidence in (
            await s.execute(
                select(GoalJournal.evidence).where(
                    GoalJournal.agent_id == self._agent_id,
                    GoalJournal.goal_id == goal_id,
                    GoalJournal.kind.in_(("question", "answer")),
                )
            )
        ).scalars():
            seen.update(str(ref) for ref in evidence or [])
        written = 0
        for item in source:
            refs = [f"row:{item.id}"]
            if item.msg_type == continuation.MSG_QUESTION:
                kind, actor = "question", "chain"
            else:
                kind, actor = "answer", "owner"
                refs.append(f"arrival:{item.arrival_id}")
            if any(ref in seen for ref in refs):
                continue
            seen.update(refs)
            self._journal(s, goal_id, kind, item.body, actor=actor, session_n=row.n, evidence=refs)
            written += 1
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
        detail: str | None = None,
    ) -> bool:
        """The close's writes, in the caller's transaction: the session row (only while still open), an active
        goal's next wake, and the harness ``state`` row with the summary. False when the session was closed
        meanwhile (nothing else is written)."""
        result = await s.execute(
            update(GoalSession)
            .where(GoalSession.id == row.id, GoalSession.ended_at.is_(None))
            .values(ended_at=now, outcome=outcome, spent=spent, steps_closed=steps, summary=summary)
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
        for extra in (detail, summary):
            if extra:
                note += f"\n{extra}"
        self._journal(s, goal.id, "state", note, actor="harness", session_n=row.n)
        return True

    async def refresh_spent(self) -> int:
        """Read the spend of open sessions, and of closed ones whose root's deadline has not passed (a late result
        can still add to it), again. Older sessions keep what they had. Returns the sessions whose spend changed."""
        root = aliased(Intention)
        query = (
            select(GoalSession.id, GoalSession.root_id, GoalSession.spent)
            .join(root, and_(root.agent_id == self._agent_id, root.id == GoalSession.root_id))
            .where(
                GoalSession.agent_id == self._agent_id,
                or_(GoalSession.ended_at.is_(None), root.deadline > func.clock_timestamp()),
            )
        )
        changed = 0
        async with self._db.session() as s:
            rows = (await s.execute(query)).all()
            if not rows:
                return 0
            totals = await spend.session_spent(s, self._agent_id, [r.root_id for r in rows])
            for r in rows:
                if totals[r.root_id] != r.spent:
                    await s.execute(update(GoalSession).where(GoalSession.id == r.id).values(spent=totals[r.root_id]))
                    changed += 1
            await s.commit()
        return changed

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
