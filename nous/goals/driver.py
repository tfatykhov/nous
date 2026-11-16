"""F114: the goal driver. It acts on goal rows in the app process, one tick at a time.

A session of a goal is an ordinary continuation chain: its root is the first subtask's intention (internal
authority, wake policy ``continue``, ``nous.goals.roots``), and the continuation runner works it like any other root.
The driver starts a session when a goal is due and closes it, by polling, once its chain has ended.

The driver keeps no state that matters beyond its loop: wakes come from the rows (``next_wake_at``,
``pending_request``), so a restart loses nothing but the count of failed starts in a row. Every time it writes comes
from the database's clock. Every change of a goal's columns is a targeted UPDATE guarded by ``state = 'active'``
(or by the request it read), never a save of the whole row, so an owner's pause or cancel made meanwhile is never
overwritten.

It also acts on the owner's requests (run now, stop, cancel), stops the chain of a session whose task ran past its
time limit, pauses a goal on a stop condition (deadline, total budget, a stall, errors in a row) and holds a start
on the limits of the goal (day budget, sessions of the day, quiet hours, job launches of the day) and of all goals
(sessions open at once). ``goal_roots`` tells the continuation runner which roots are goal sessions', so a goal's
chain never takes the slots kept for the owner's own chains.

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

from sqlalchemy import ColumnElement, Text, and_, cast, delete, exists, false, func, or_, select, text, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from nous.brain import continuation, intentions
from nous.cancellation import cancel_requested
from nous.dag.store import TERMINAL_NODE_STATUSES
from nous.goals import charter, roots, spend
from nous.goals.store import SESSION_NOTE_LABEL, GoalStore, clip
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
# How long before a session's close a question or an answer may have been written and still be journalled after
# it (the close journals what it sees; a row it could not see was written in a transaction open across the close).
QUESTIONS_BEFORE_CLOSE = timedelta(hours=1)
# Closed sessions the restart catch-up reads per tick (``refresh_sessions``).
CATCH_UP_BATCH = 200
ANSWER_PREFIX_PATTERN = f"{continuation.ANSWER_CORRELATION_PREFIX}%"
# A running subtask is stuck this long past its timeout: the task scheduler's grace for a run (``find_active_run``).
STUCK_GRACE_SECONDS = 300
ERRORS_BEFORE_PAUSE = 3  # sessions in a row that end in an error before the goal pauses (``error``)
# The refresh reads every closed session whose root's deadline has not passed. A root's deadline is the TTL counted
# from the publish of its first task, which comes after the session's start; the scan is bounded by the start, with
# this much room for the publish's delay and for a difference between this process's clock and the database's.
DEADLINE_SCAN_SLACK = timedelta(hours=1)
# Evidence refs of the driver's own journal rows (``state``, actor ``harness``), read back by the driver:
ENDING_REF = "ending:"  # + session id: the driver is ending this session's chain (a stop, a cancel, a stuck task)
REQUEST_REF = "request:"  # + kind@time: the owner's stop or cancel an ending row was written for
ARRIVALS_REF = "arrivals:"  # + n: a session's end row records its root's newest arrival number at the close
RESUMED_REF = "resumed:"  # + arrival id: a closed session's chain resumed and ended again, up to that arrival
STOP_REF = "stop:"  # + reason: a stop condition paused the goal; runs of outcomes restart after its session_n
# Why a start did not happen, in the words of the journal row a run-now that did not start leaves.
HELD_WORDS: dict[str, str] = {
    "halted": "background work is halted",
    "queue": "the subtask queue has too few free places",
    "open_session": "a session is open",
    "open_work": "an earlier session's chain is working again",
    "deadline": "the deadline has passed",
    "budget": "the total budget is spent",
    "day_budget": "today's budget is spent",
    "day_sessions": "today's sessions are used up",
    "quiet": "it is quiet hours",
    "jobs": "today's job launches are used up",
    "sessions": "the most goal sessions allowed at once are open",
    "closed": "its session was closed before its first task was published",
}
REQUEST_WORDS = {"run_now": "run-now", "stop": "stop", "cancel": "cancel"}
# How the driver ends a session's chain: (the root's cancel reason, the cancel's actor).
_ENDINGS = {
    "stop": ("goal stopped by the owner", "goal:owner"),
    "cancel": ("goal cancelled by the owner", "goal:owner"),
    "stuck": ("goal session stuck", "goal:harness"),
}

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


def _deadline_passed(goal: Goal, now: datetime) -> bool:
    """Whether the goal's deadline has passed at ``now``: a deadline at ``now`` has (the store's rule for
    ``resume``). One comparison for the start's hold and the stop condition, so the two cannot disagree."""
    return goal.deadline_at is not None and goal.deadline_at <= now


def _budget_spent(spent: int, budget: int) -> bool:
    """Whether ``spent`` tokens use up ``budget``: spent to the token is spent (the store's rule for ``resume``)."""
    return spent >= budget


def _not_started(held: str) -> str:
    """The harness's row for a run-now request that a hold kept from starting."""
    return f"Run now was not started: {HELD_WORDS[held]}."


def _request_ref(kind: str, at: datetime) -> str:
    """The evidence ref of an ending row written for the owner's request ``kind`` made at ``at``."""
    return f"{REQUEST_REF}{kind}@{at.isoformat()}"


def _ending_text(why: str, n: int, *, resumed: bool = False) -> str:
    """The harness's row saying it is ending a session's chain, and why."""
    if resumed:
        return f"Session {n}'s chain, resumed after the session closed, is being stopped: a task ran past its limit."
    because = {
        "stop": "the owner asked to stop it",
        "cancel": "the owner cancelled the goal",
        "stuck": "a task of its chain ran past its time limit",
    }[why]
    return f"Session {n} is being stopped: {because}."


def _constraint_of(exc: IntegrityError) -> str | None:
    """The name of the constraint or index an INSERT violated, as the driver (asyncpg) reports it, else None."""
    for error in (exc.orig, getattr(exc.orig, "__cause__", None)):
        name = getattr(error, "constraint_name", None)
        if name:
            return str(name)
    return None


@dataclass(frozen=True)
class TickReport:
    """What one tick did: reports journalled from the goal channels, sessions closed, chains the driver ended (the
    owner's stop or cancel, a stuck task), goals paused by a stop condition, the roots of the sessions it started,
    and why it started no more (``held``: ``halted``, ``queue`` or ``sessions``), else None."""

    claimed: int = 0
    closed: int = 0
    stopped: int = 0
    paused: int = 0
    started: tuple[UUID, ...] = ()
    held: str | None = None


@dataclass(frozen=True)
class _RootState:
    found: bool
    marked: bool  # the root carries a cancel or an expiry marker
    open_work: bool  # an intention of the root is in an open state
    cancelled: bool = False  # the marker is a cancel


@dataclass(frozen=True)
class _Start:
    """What a start did: the root of the session it started, else why it held (a ``HELD_WORDS`` key) or None."""

    root_id: UUID | None = None
    held: str | None = None


@dataclass(frozen=True)
class _JournalItem:
    """A question or an answer to journal: its kind, actor, text and evidence refs."""

    kind: str
    actor: str
    text: str
    refs: list[str]


class GoalDriver:
    """Starts goal sessions and closes them. ``runner`` is the continuation runner (``wake``, ``cancel_root``).

    One driver object runs one tick at a time: ``tick`` holds ``_tick_lock`` for the whole pass, and a direct
    ``start_session`` takes the same lock, so a second call waits and never interleaves. The state this object
    keeps in memory, each kept only for speed and rebuilt from the rows after a restart:

    - ``_following``: closed sessions whose spend is read again. A close adds to it; a refresh removes only the
      ids it read itself as closed and settled, never the whole set.
    - ``_caught_up`` and ``_catch_up_after``: how far the restart catch-up has read; moved only from the cursor
      the same pass started from.
    - ``_failures``: failed starts in a row, per goal. With P driver processes a goal can see up to 3 x P failed
      starts before one of them pauses it: each counts its own.
    - ``_call_log_noted``: whether the start-up line about an uncounted job cap was logged (a log line only).

    Two driver processes for one agent share none of it and are coordinated only through the rows: every active
    goal row's lock and the one-open-session index for a start (so the count of open sessions across goals is
    read by one start at a time), the session row's lock and ``ended_at IS NULL`` for a close and for the first
    task's publish, the claim's ``delivered_at IS NULL``, spend written only upward, the journal's evidence refs
    under the session row's lock, a request cleared only while it is still the one read, a pause only while its
    condition still holds, and wake changes compared with the wake read.
    """

    def __init__(self, database: Database, settings: Any, heart: Any, store: GoalStore, runner: Any) -> None:
        self._db = database
        self._settings = settings
        self._heart = heart
        self._store = store
        self._runner = runner
        self._agent_id = store.agent_id
        self._failures: dict[UUID, int] = {}  # failed starts in a row, per goal (in memory: a restart resets it)
        # Closed sessions whose spend is read again at the next tick: closed since, or with lineage work whose
        # usage is not settled yet (``refresh_sessions``). In memory; the first ticks after a start catch up.
        self._following: set[UUID] = set()
        self._caught_up = False
        self._catch_up_after: tuple[datetime, UUID] | None = None  # the last closed session the catch-up read
        self._tick_lock = asyncio.Lock()  # one tick, or one direct start, at a time
        self._call_log_noted = False
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
        if getattr(self._settings, "tool_call_log_enabled", False) is not True and not self._call_log_noted:
            self._call_log_noted = True
            logger.info("Goal driver: NOUS_TOOL_CALL_LOG_ENABLED is off, so the goals' job launches are not counted")

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
        """One pass, in order: claim the goal channels into the journals (first, so a session the owner stops
        has its reports journalled before its close), act on the owner's stop and cancel requests, close the
        sessions whose chain has ended, end the chains with a task stuck past its time limit, read again what open
        and recently closed sessions spent (and journal what a resumed chain of a closed one asked the owner and how
        it ended), pause the goals a stop condition holds, then start: the run-now requests first, then the sessions
        that are due. Stop conditions run before any start, so a goal that should pause never gets one more session.
        With goals off it does nothing. A second call waits for this one to end (``_tick_lock``)."""
        if not self._enabled():
            return TickReport()
        async with self._tick_lock:
            claimed = await self.claim_reports()
            stopped = await self.handle_requests()
            closed = await self.close_finished()
            stopped += await self.catch_stuck()
            await self.refresh_sessions()
            paused = await self.apply_stop_conditions()
            started, held = await self._start_due()
            return TickReport(
                claimed=claimed, closed=closed, stopped=stopped, paused=paused, started=started, held=held
            )

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

    async def _run_now_requests(self) -> list[tuple[UUID, datetime]]:
        """``(goal id, request time)`` of the active goals with a run-now request and no open session, oldest
        request first. The request is acted on, and cleared, under the goal's lock (``_start_session``)."""
        open_session = exists().where(GoalSession.goal_id == Goal.id, GoalSession.ended_at.is_(None))
        query = (
            select(Goal.id, Goal.pending_request_at)
            .where(
                Goal.agent_id == self._agent_id,
                Goal.state == "active",
                Goal.pending_request == "run_now",
                ~open_session,
            )
            .order_by(Goal.pending_request_at, Goal.id)
        )
        async with self._db.session() as s:
            return [(row.id, row.pending_request_at) for row in (await s.execute(query)).all()]

    async def _start_due(self) -> tuple[tuple[UUID, ...], str | None]:
        """The owner's run-now requests, then the goals whose wake has come. A run-now request is acted on once:
        it starts a session, or is cleared with one journal row saying what held it. A hold of every start
        (background halted, too few free queue places, the most sessions open at once) ends the timer starts for
        this tick; a goal's own hold (``_held_by_limits``) skips that goal only."""
        started: list[UUID] = []
        for goal_id, request_at in await self._run_now_requests():
            try:
                held = await self._start_held()
                if held is not None:
                    await self._drop_request(goal_id, "run_now", request_at, held=held)
                    continue
                result = await self._start_session(goal_id, trigger="run_now", request_at=request_at)
            except Exception:
                logger.exception("Goal %s: the run-now start failed", goal_id)
                continue
            if result.root_id is not None:
                started.append(result.root_id)
        for goal_id in await self._due_goals():
            held = await self._start_held()
            if held is not None:
                return tuple(started), held
            try:
                result = await self._start_session(goal_id, trigger="timer")  # the tick holds the lock
            except Exception:
                logger.exception("Goal %s: the session start failed", goal_id)
                continue
            if result.root_id is not None:
                started.append(result.root_id)
            elif result.held == "sessions":
                return tuple(started), "sessions"
        return tuple(started), None

    async def start_session(self, goal_id: UUID, *, trigger: str) -> UUID | None:
        """Start a session of an active goal (``_start_session``), holding the tick's lock, so a direct start never
        interleaves with a tick: the start counts failures in memory (``_failures``) at several points across
        awaits. Its root's id, or None when nothing started."""
        async with self._tick_lock:
            return (await self._start_session(goal_id, trigger=trigger)).root_id

    async def _start_session(self, goal_id: UUID, *, trigger: str, request_at: datetime | None = None) -> _Start:
        """Start a session of an active goal: the root of the session it started, else why it held.

        Transaction 1 takes every active goal row of the agent ``FOR UPDATE``, in id order (so two starts of
        different goals never read the count of open sessions at once), re-reads that this goal is active (and,
        for a ``timer`` start, that its wake has still come, by the database's clock: another tick may have
        started, ended and rescheduled it since the due list was read), that it has no open session and no open
        work left on an earlier session's root, and that no limit holds it (``_held_by_limits``), and inserts the
        session row (its ``first_subtask_id`` drawn here, before the subtask exists) and commits; the partial unique
        index on open sessions is the final word against a second start (any other violation of the insert is a
        failed start). Then the first task is built and goes through the censor gate that every spawned task goes
        through, and the subtask, its root intention and the session's root id are written in one transaction that
        holds the session row's lock (``_publish``). If that fails, the session row is undone. A full queue only
        delays the goal a minute; a refusal by the gate or any other failure is journalled and counted, and the
        third in a row pauses the goal (``error``). With goals off, or while background work is halted, it does
        nothing: every caller gets that refusal here. The queue headroom is the tick's check.

        A ``run_now`` start skips the wake, the count of the day's sessions and quiet hours (the owner asked for it
        now, and quiet hours are kept for the owner); every other limit holds it. With
        ``request_at`` it acts on the owner's pending request: only while that request is still the one read, and
        the request is cleared in transaction 1 (with the session row, or with a journal row saying what held it), so
        a request is acted on at most once. A run-now whose session then does not start leaves one row saying so: a
        full queue (in the transaction that undoes the session row), a session closed by recovery before its first
        task was published, or a failed start (``_failed_start``'s row). A delay never moves the wake earlier
        (``GoalStore.delay_wake``), so a run-now that fails does not bring the next timer session forward."""
        if not self._enabled():
            return _Start()
        if self.background_halted():
            return _Start(held="halted")
        subtask_id = uuid.uuid4()
        open_work = False
        insert_failure: str | None = None
        async with self._db.session() as s:
            await s.execute(
                select(Goal.id)
                .where(Goal.agent_id == self._agent_id, Goal.state == "active")
                .order_by(Goal.id)
                .with_for_update()
            )
            goal = (
                await s.execute(
                    select(Goal).where(Goal.id == goal_id, Goal.agent_id == self._agent_id).with_for_update()
                )
            ).scalar_one_or_none()
            if goal is None or goal.state != "active":
                return _Start()
            if request_at is not None and (goal.pending_request, goal.pending_request_at) != ("run_now", request_at):
                return _Start()  # the request was cleared or replaced since it was read
            seen = goal.next_wake_at  # the wake as read under the lock (a rollback below expires the object)
            now = (await s.execute(select(func.clock_timestamp()))).scalar_one()
            if trigger == "timer" and (seen is None or seen > now):
                return _Start()  # another tick started, ended and rescheduled it since the due list was read
            if await self._has_open_session(s, goal_id):
                held: str | None = "open_session"
            elif await self._lineage_has_open_work(s, goal_id, now):
                held, open_work = "open_work", True
            else:
                held = await self._held_by_limits(s, goal, now, trigger=trigger)
            if held is not None:
                if request_at is not None:
                    open_work = False  # the request is spent: the wake is the timer's, left as it is
                    await self._clear_request(s, goal_id, "run_now", request_at)
                    self._journal(s, goal_id, "state", _not_started(held), actor="harness")
                    await s.commit()
                elif held in ("day_budget", "day_sessions"):
                    # Only the next UTC day frees these; the goal lock is held, so the wake read is still the wake.
                    await self._wake_next_day(s, goal_id, seen, now)
                    await s.commit()
                if not open_work:
                    return _Start(held=held)
            else:
                if request_at is not None:  # consumed with the session row, in the same commit
                    await self._clear_request(s, goal_id, "run_now", request_at)
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
                        return _Start(held="open_session")  # another start won
                    logger.warning("Goal %s: session %d could not be recorded", goal_id, n, exc_info=True)
                    insert_failure = f"Session {n} could not start: {type(exc).__name__}."
        if open_work:
            await self._store.delay_wake(goal_id, seconds=OPEN_WORK_DELAY_SECONDS, seen=seen)
            return _Start(held="open_work")
        if insert_failure is not None:  # no session row was written: nothing to undo
            await self._failed_start(goal_id, n, insert_failure, seen=seen, request_at=request_at)
            return _Start()
        session_id = row.id
        root_id: UUID | None = None
        try:
            rejection, task = await self._gated_task(goal)
            if rejection is not None:
                # A fixed sentence: the journal is part of the next task, which goes through the same gate, so the
                # censor's words in it would refuse every later start too. The message goes to the log only.
                logger.warning("Goal %s: session %d was refused by the censor gate: %s", goal_id, n, rejection)
                failure = f"Session {n} was refused by the censor gate; the log has the reason."
            else:
                root_id = await self._publish(goal, n, session_id, subtask_id, task)
                failure = None
        except SubtaskQueueFull:
            # A run-now's request was cleared with the session row: the row that undoes it says it did not start.
            note = _not_started("queue") if request_at is not None else None
            if await self._undo_start(session_id, subtask_id, note=note):
                await self._store.delay_wake(goal_id, seconds=QUEUE_FULL_DELAY_SECONDS, seen=seen)
                logger.info("Goal %s: the subtask queue is full; session %d waits a minute", goal_id, n)
            return _Start(held="queue")
        except Exception as exc:
            logger.warning("Goal %s: session %d could not start", goal_id, n, exc_info=True)
            # The class only: the journal is read by later sessions, the exception's text stays in the log.
            failure = f"Session {n} could not start: {type(exc).__name__}."
        if failure is not None:
            if await self._undo_start(session_id, subtask_id):
                await self._failed_start(goal_id, n, failure, seen=seen)
            return _Start()
        if root_id is None:  # recovery closed the session meanwhile: nothing was published, no failure counted
            if request_at is not None:  # its request was cleared with the session row
                async with self._db.session() as s:
                    self._journal(s, goal_id, "state", _not_started("closed"), actor="harness", session_n=n)
                    await s.commit()
            return _Start()
        self._failures.pop(goal_id, None)
        self._runner.wake()
        logger.info("Goal %s: session %d started (root %s)", goal_id, n, root_id)
        return _Start(root_id=root_id)

    async def _held_by_limits(self, s: AsyncSession, goal: Goal, now: datetime, *, trigger: str) -> str | None:
        """The first limit that holds a start of ``goal`` at ``now`` (a ``HELD_WORDS`` key), else None, read in
        the start's transaction under the goal locks: the deadline, the total budget, the day's budget (spend counts
        for the UTC day its session started), the day's sessions (not for a run-now), quiet hours (unless the goal
        may run then; not for a run-now), the day's job launches (when the tool-call log is on; a cap of 0 holds like
        a cap of 1, once one launch is counted: a launch inside a session cannot be stopped, only the next start
        held), and the sessions open across all goals. The comparisons are the store's: a deadline at ``now``
        has passed, a budget spent to the token is spent. Spend can rise after this read (a refresh writes it): a
        chain can overshoot its goal by at most its own token limit."""
        if _deadline_passed(goal, now):
            return "deadline"
        total, today = await spend.goal_spent(s, self._agent_id, goal.id, now=now)
        if _budget_spent(total, goal.total_budget):
            return "budget"
        if _budget_spent(today, goal.day_budget):
            return "day_budget"
        if trigger != "run_now":
            started = await spend.sessions_started_today(s, self._agent_id, goal.id, now=now)
            if started >= goal.sessions_per_day:
                return "day_sessions"
        if not goal.quiet_hours_ok and trigger != "run_now":
            from nous.heartbeat.quiet_hours import quiet_hours_end  # the heartbeat package is heavy to import

            if quiet_hours_end(self._settings, now) != now:
                return "quiet"
        jobs = await spend.jobs_launched_today(s, self._agent_id, goal.id, self._settings, now=now)
        if jobs is not None and jobs >= max(int(goal.jobs_per_day), 1):
            return "jobs"
        open_sessions = await s.scalar(
            select(func.count()).where(GoalSession.agent_id == self._agent_id, GoalSession.ended_at.is_(None))
        )
        if int(open_sessions or 0) >= int(self._settings.goals_max_active_sessions):
            return "sessions"
        return None

    async def _wake_next_day(self, s: AsyncSession, goal_id: UUID, seen: datetime | None, now: datetime) -> None:
        """An active goal's wake to the start of the next UTC day after ``now``, while it is still ``seen``."""
        await s.execute(
            update(Goal)
            .where(
                Goal.id == goal_id,
                Goal.agent_id == self._agent_id,
                Goal.state == "active",
                Goal.next_wake_at.is_not_distinct_from(seen),
            )
            .values(next_wake_at=spend.day_start(now) + timedelta(days=1), updated_at=func.clock_timestamp())
            .execution_options(synchronize_session=False)
        )

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

    def _root_holds_goal(self, root: Any, now: Any) -> ColumnElement[bool]:
        """Whether ``root`` (an aliased intention, joined to a session) holds its goal at ``now``: no cancel or
        expiry marker, its deadline not passed, and an intention of it in an open state. The start waits on such a
        root of an earlier session (``_lineage_has_open_work``), and ``catch_stuck`` looks for a stuck task on the
        same roots, so the two cannot disagree on which closed sessions still hold a goal."""
        member = aliased(Intention)
        return and_(
            root.root_cancelled_at.is_(None),
            root.root_expired_at.is_(None),
            or_(root.deadline.is_(None), root.deadline > now),
            exists().where(
                member.agent_id == self._agent_id,
                member.root_id == root.id,
                member.state.in_(continuation.OPEN_STATES),
            ),
        )

    async def _lineage_has_open_work(self, s: AsyncSession, goal_id: UUID, now: datetime) -> bool:
        """Whether a root of one of the goal's sessions, with no cancel or expiry marker and its deadline not passed,
        has an open intention. A result reopens a closed intention of a root with no marker until that root's
        deadline, so a closed session's chain can come back to life; a new session waits while it does. A marked
        root has ended, even while an intention of it is not closed yet: it holds nothing."""
        root = aliased(Intention)
        query = select(
            exists().where(
                GoalSession.agent_id == self._agent_id,
                GoalSession.goal_id == goal_id,
                GoalSession.root_id.is_not(None),
                root.agent_id == self._agent_id,
                root.id == GoalSession.root_id,
                self._root_holds_goal(root, now),
            )
        )
        return bool((await s.execute(query)).scalar_one())

    async def _undo_start(self, session_id: UUID, subtask_id: UUID, *, note: str | None = None) -> bool:
        """Delete a session row whose first subtask was never created (the driver's only DELETE), with ``note`` as a
        harness ``state`` row of its goal in the same transaction. Never one whose subtask or its intention exists: a
        create that committed and then raised left a live root, which the close attaches. True when the row was
        deleted."""
        async with self._db.session() as s:
            goal_id = (
                await s.execute(
                    select(GoalSession.goal_id)
                    .where(
                        GoalSession.id == session_id,
                        GoalSession.agent_id == self._agent_id,
                        GoalSession.root_id.is_(None),
                        GoalSession.ended_at.is_(None),
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if goal_id is None:
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
            if note is not None:
                self._journal(s, goal_id, "state", note, actor="harness")
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

    async def _failed_start(
        self, goal_id: UUID, n: int, failure: str, *, seen: datetime | None, request_at: datetime | None = None
    ) -> None:
        """Count a failed start. Below the limit: journal it and try again in five minutes (never earlier than the
        wake was). At the limit: pause the goal (``error``), saying why. A run-now request still pending (the insert
        that would have cleared it rolled back) is cleared with the journal row: it is acted on once."""
        count = self._failures.get(goal_id, 0) + 1
        if count >= FAILED_STARTS_BEFORE_PAUSE:
            self._failures.pop(goal_id, None)
            note = f"{failure}\nPaused: the start failed {count} times in a row."
            await self._pause(goal_id, "error", note, session_n=n)
            return
        self._failures[goal_id] = count
        async with self._db.session() as s:
            if request_at is not None:
                await self._clear_request(s, goal_id, "run_now", request_at)
            self._journal(s, goal_id, "state", failure, actor="harness", session_n=n)
            await s.commit()
        await self._store.delay_wake(goal_id, seconds=FAILED_START_DELAY_SECONDS, seen=seen)

    async def _clear_request(self, s: AsyncSession, goal_id: UUID, kind: str, at: datetime) -> bool:
        """Clear a goal's request in the caller's transaction, only while it is still the one read (``kind`` made
        at ``at``): a request made since is kept. True when it was cleared."""
        result = await s.execute(
            update(Goal)
            .where(
                Goal.id == goal_id,
                Goal.agent_id == self._agent_id,
                Goal.pending_request == kind,
                Goal.pending_request_at == at,
            )
            .values(pending_request=None, pending_request_at=None, updated_at=func.clock_timestamp())
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

    async def _drop_request(
        self, goal_id: UUID, kind: str, at: datetime, *, held: str | None = None, why: str | None = None
    ) -> bool:
        """Clear a request that is not acted on, with one harness row saying why (a run-now that a hold kept from
        starting, or a request with nothing to act on), or with none when ``held`` and ``why`` are both None (a
        request that was acted on). One transaction; nothing when the request changed since it was read."""
        async with self._db.session() as s:
            if not await self._clear_request(s, goal_id, kind, at):
                return False
            if held is not None:
                self._journal(s, goal_id, "state", _not_started(held), actor="harness")
            elif why is not None:
                note = f"The {REQUEST_WORDS[kind]} request was cleared: {why}."
                self._journal(s, goal_id, "state", note, actor="harness")
            await s.commit()
            return True

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

    # ---- the owner's requests and stuck sessions -----------------------------------------------------------

    async def handle_requests(self) -> int:
        """Act on the owner's stop and cancel requests, and clear the requests that have nothing to act on. A stop
        or a cancel (``GoalStore.stop`` / ``cancel`` already paused or cancelled the goal) ends the chain of the
        session that was open when it was made (``_end_chain``) and is then cleared. A request is cleared with one
        harness row, and nothing else is done, when it has nothing to act on: no session is open, or it was made
        before the open session started (a stale stop never ends a later session), or a run-now finds the goal not
        active or a session open. A run-now that may start is left for the start step of the tick, which comes
        after the stop conditions. Returns the sessions ended."""
        query = (
            select(Goal.id, Goal.state, Goal.pending_request, Goal.pending_request_at)
            .where(Goal.agent_id == self._agent_id, Goal.pending_request.is_not(None))
            .order_by(Goal.pending_request_at, Goal.id)
        )
        async with self._db.session() as s:
            requests = (await s.execute(query)).all()
        ended = 0
        for request in requests:
            try:
                ended += await self._handle_request(request)
            except Exception:
                logger.exception("Goal %s: its %s request failed; tried again", request.id, request.pending_request)
        return ended

    async def _handle_request(self, request: Any) -> int:
        kind, at = request.pending_request, request.pending_request_at
        async with self._db.session() as s:
            session = (
                await s.execute(
                    select(GoalSession).where(
                        GoalSession.agent_id == self._agent_id,
                        GoalSession.goal_id == request.id,
                        GoalSession.ended_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
        if kind == "run_now":
            if request.state != "active":
                await self._drop_request(request.id, kind, at, why="the goal is not active")
            elif session is not None:
                await self._drop_request(request.id, kind, at, why=f"session {session.n} is open")
            return 0  # otherwise the start step acts on it
        if session is None:
            await self._drop_request(request.id, kind, at, why="no session is open")
            return 0
        if at < session.started_at:
            await self._drop_request(request.id, kind, at, why=f"it was made before session {session.n} started")
            return 0
        if not await self._end_chain(session, kind, request_at=at):
            return 0  # the request changed, or the session is still open: the next tick reads the row again
        await self._drop_request(request.id, kind, at)
        return 1

    async def _end_chain(self, session: GoalSession, why: str, *, request_at: datetime | None = None) -> bool:
        """End the chain of an open session (``why``: ``stop``, ``cancel`` or ``stuck``) and close the session. In
        order: a harness ``state`` row naming the session (its evidence ``ending:<session id>``, and for a stop or a
        cancel the request's ``request:<kind>@<time>``; written once, committed before anything else, so the close
        never takes the cancel for the owner's), the root's cancel
        through the continuation runner (``CancelRefused``, nothing left running, and ``RootNotFound`` are not
        errors), then the close, which finds the root marked. A session whose first task was never published is
        closed ``interrupted`` under its lock (a start still publishing it then publishes nothing). With
        ``request_at`` (a stop or a cancel) nothing is done unless that request is still the goal's when the row is
        written. True when the session is closed."""
        request = (why, request_at) if request_at is not None else None
        if not await self._journal_ending(session.id, _ending_text(why, session.n), request=request):
            return False
        root_id = session.root_id or await self._root_or_close_unpublished(session.id)
        if root_id is None:
            return True
        reason, actor = _ENDINGS[why]
        try:
            await self._runner.cancel_root(root_id, reason=reason, actor=actor)
        except (continuation.CancelRefused, continuation.RootNotFound) as exc:
            logger.info("Goal session %s: the root's cancel did nothing (%s)", session.id, type(exc).__name__)
        await self._close(session.id)
        async with self._db.session() as s:
            return (await s.scalar(select(GoalSession.ended_at).where(GoalSession.id == session.id))) is not None

    async def _journal_ending(
        self, session_id: UUID, note: str, *, request: tuple[str, datetime] | None = None
    ) -> bool:
        """The harness's row saying it is ending this session's chain, once: under the session row's lock, skipped
        when such a row still holds (``_harness_ended``: a retry after a crash between the row and the cancel). With
        ``request`` (kind, time) the goal row is locked too (after the session row, the close's order), nothing is
        written unless that request is still the goal's (a resume or an approve clears it and a cancel replaces a
        stop, any time after the tick read it), and the row names the request (``request:<kind>@<time>``). False
        when nothing may be done."""
        ref = f"{ENDING_REF}{session_id}"
        async with self._db.session() as s:
            row = (
                await s.execute(
                    select(GoalSession)
                    .where(GoalSession.id == session_id, GoalSession.agent_id == self._agent_id)
                    .with_for_update()
                )
            ).scalar_one()
            query = select(Goal.pending_request, Goal.pending_request_at).where(
                Goal.id == row.goal_id, Goal.agent_id == self._agent_id
            )
            pending = tuple((await s.execute(query.with_for_update() if request is not None else query)).one())
            if request is not None and pending != request:
                return False
            if not await self._harness_ended(s, row, pending):
                refs = [ref] if request is None else [ref, _request_ref(*request)]
                self._journal(s, row.goal_id, "state", note, actor="harness", session_n=row.n, evidence=refs)
            await s.commit()
            return True

    async def _harness_ended(self, s: AsyncSession, row: GoalSession, pending: tuple[Any, ...]) -> bool:
        """Whether a row of the driver's saying it is ending this session's chain (``_journal_ending``) still holds.
        One written for a stuck task always does; one written for the owner's stop or cancel only while that request
        is still the goal's (``pending``: its kind and time, as read from the goal row). So when a driver stopped
        between such a row and its cancel and the owner then resumed the goal (the request cleared), a cancel the
        owner makes later is not taken for the driver's: the close reads it as the owner's."""
        ref = f"{ENDING_REF}{row.id}"
        kind, at = pending
        current = _request_ref(kind, at) if kind is not None and at is not None else None
        rows = await s.scalars(
            select(GoalJournal.evidence).where(
                GoalJournal.agent_id == self._agent_id,
                GoalJournal.goal_id == row.goal_id,
                GoalJournal.kind == "state",
                GoalJournal.actor == "harness",
                GoalJournal.evidence.contains([ref]),
            )
        )
        for evidence in rows:
            asked = [e for e in evidence or [] if isinstance(e, str) and e.startswith(REQUEST_REF)]
            if not asked or current in asked:
                return True
        return False

    async def _root_or_close_unpublished(self, session_id: UUID) -> UUID | None:
        """For an open session with no root id, under its lock: the root its first subtask has (attached), else
        the session closed ``interrupted`` (nothing was published; a start still publishing finds it closed)."""
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
                return None
            if row.root_id is not None:
                return row.root_id
            root_id = (
                await s.execute(
                    select(Intention.id).where(
                        Intention.agent_id == self._agent_id,
                        Intention.source_kind == intentions.SOURCE_SUBTASK,
                        Intention.source_id == str(row.first_subtask_id),
                    )
                )
            ).scalar_one_or_none()
            if root_id is not None:
                await self._set_root(s, row.id, root_id)
                await s.commit()
                return root_id
            now = (await s.execute(select(func.clock_timestamp()))).scalar_one()
            goal = (await s.execute(select(Goal).where(Goal.id == row.goal_id))).scalar_one()
            await self._end(
                s,
                goal,
                row,
                now,
                outcome="interrupted",
                spent=0,
                steps=0,
                summary=None,
                detail="It was stopped before its first task was created.",
            )
            await s.commit()
            return None

    async def catch_stuck(self) -> int:
        """End the chains with a lineage subtask stuck: ``running`` past its timeout and the grace
        (``STUCK_GRACE_SECONDS``, from its start, as ``task_scheduler.find_active_run`` reads a run). A worker
        that stopped (a restart) leaves such a row ``running``, and ``reclaim_stale`` puts it back only at the next
        restart, so the chain would wait on it until its root's deadline.

        Two kinds of session are looked at: open ones whose root has no marker, and closed ones whose root still
        holds the goal (``_root_holds_goal``: a late result reopened it, no marker, its deadline not passed; the next
        session waits on it). An open one's chain is ended and the session closed (``_end_chain``); the goal stays
        active, and the session's outcome is the close's (``interrupted``, from the cancel's marker, unless an earlier
        rule holds: the stuck task itself is not an error). A closed one's chain is ended the same way, the session
        row is not touched (``_stop_resumed``).
        Waiting is not stuck: an intention ``awaiting_owner``, a subtask still ``pending`` in the queue, a DAG are
        left alone. Returns the chains ended."""
        rows = await self._stuck_sessions()
        ended = 0
        for row in rows:
            try:
                if row.ended_at is None:
                    ended += 1 if await self._end_chain(row, "stuck") else 0
                else:
                    ended += 1 if await self._stop_resumed(row) else 0
            except Exception:
                logger.exception("Goal session %s: its stuck chain could not be ended; tried again next tick", row.id)
        return ended

    async def _stuck_sessions(self) -> list[GoalSession]:
        root, member, subtask = aliased(Intention), aliased(Intention), aliased(Subtask)
        timeout = func.coalesce(func.nullif(subtask.timeout_seconds, 0), int(self._settings.subtask_default_timeout))
        stuck = exists().where(
            member.agent_id == self._agent_id,
            member.root_id == root.id,
            member.source_kind == intentions.SOURCE_SUBTASK,
            subtask.agent_id == self._agent_id,
            cast(subtask.id, Text) == member.source_id,
            subtask.status == "running",
            func.coalesce(subtask.started_at, subtask.created_at)
            + func.make_interval(0, 0, 0, 0, 0, 0, timeout + STUCK_GRACE_SECONDS)
            < func.clock_timestamp(),
        )
        query = (
            select(GoalSession)
            .join(root, and_(root.agent_id == self._agent_id, root.id == GoalSession.root_id))
            .where(
                GoalSession.agent_id == self._agent_id,
                or_(
                    and_(
                        GoalSession.ended_at.is_(None),
                        root.root_cancelled_at.is_(None),
                        root.root_expired_at.is_(None),
                    ),
                    and_(GoalSession.ended_at.is_not(None), self._root_holds_goal(root, func.clock_timestamp())),
                ),
                stuck,
            )
            .order_by(GoalSession.started_at, GoalSession.id)
        )
        async with self._db.session() as s:
            return list((await s.execute(query)).scalars())

    async def _stop_resumed(self, row: GoalSession) -> bool:
        """End the stuck chain of a closed session whose root came back to life: the harness row (once), then the
        root's cancel. The session row keeps what its close wrote; the refresh journals how the chain ended."""
        await self._journal_ending(row.id, _ending_text("stuck", row.n, resumed=True))
        reason, actor = _ENDINGS["stuck"]
        try:
            await self._runner.cancel_root(row.root_id, reason=reason, actor=actor)
        except (continuation.CancelRefused, continuation.RootNotFound) as exc:
            logger.info("Goal session %s: the root's cancel did nothing (%s)", row.id, type(exc).__name__)
            return False
        return True

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
            # Never lower than what an earlier read stored: the row lock is held, so row.spent is what is stored.
            spent = max((await spend.session_spent(s, self._agent_id, [row.root_id]))[row.root_id], row.spent)
            steps = sum(1 for step in goal.plan or [] if isinstance(step, dict) and step.get("closed_session") == row.n)
            outcome = await self._outcome(s, row.root_id, steps=steps, ended_by_marker=state.marked or not state.found)
            summary, from_report = await self._summary(s, row)
            # Read after the root's state: an arrival that committed before that read is in it. A later arrival
            # needs the root reopened, so an arrival numbered above this one means the chain resumed after the close.
            last_arrival = await s.scalar(
                select(func.coalesce(func.max(IntentionArrival.n), 0)).where(
                    IntentionArrival.agent_id == self._agent_id, IntentionArrival.root_id == row.root_id
                )
            )
            # A cancel marker the driver did not announce is the owner's (the routes, the bot, the companion): the
            # driver commits its ending row before it cancels, so its own cancels are never read as the owner's. A row
            # written for a request the owner has since cleared announces nothing (``_harness_ended``).
            pending = (goal.pending_request, goal.pending_request_at)
            by_owner = state.cancelled and not await self._harness_ended(s, row, pending)
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
                evidence=[f"{ARRIVALS_REF}{int(last_arrival or 0)}"],
                cancelled_by_owner=by_owner,
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
        cancelled = marks.root_cancelled_at is not None
        marked = cancelled or marks.root_expired_at is not None
        return _RootState(found=True, marked=marked, open_work=bool(open_work), cancelled=cancelled)

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

    async def _resumed_endings(self, s: AsyncSession, rows: Iterable[Any]) -> dict[UUID, list[_JournalItem]]:
        """For each closed session in ``rows`` (each with ``id``, ``goal_id``, ``n``, ``root_id``, ``ended_at`` and
        ``steps_closed``) whose chain resumed after the close and has ended again, the harness row that says so,
        when the journal does not hold it yet. Reads only.

        Resumed: its root has an arrival numbered above the one its close recorded (``arrivals:<k>`` on its end
        row): a later arrival needs a reopened root, and arrival numbers do not depend on any clock. Ended again: no
        intention of the root is open, or it carries a marker. The row's evidence is ``resumed:<newest arrival id>``,
        so it is written once for that arrival, and once more only if the chain resumes again later. Its words are
        fixed, with the outcome of the arrivals after the close by the close's rules: ``error`` (one failed),
        ``step_closed`` (the plan has more steps closed in this session than the close counted), ``asked`` (one
        decided ``ask``), ``interrupted`` (the root is marked), else ``no_step``. The session row is not changed."""
        closed = {r.root_id: r for r in rows if r.root_id is not None and r.ended_at is not None}
        if not closed:
            return {}
        pairs = sorted({(r.goal_id, r.n) for r in closed.values()})
        recorded: dict[tuple[UUID, int], int] = {}
        known: set[str] = set()
        for goal_id, session_n, evidence in (
            await s.execute(
                select(GoalJournal.goal_id, GoalJournal.session_n, GoalJournal.evidence).where(
                    GoalJournal.agent_id == self._agent_id,
                    GoalJournal.kind == "state",
                    GoalJournal.actor == "harness",
                    tuple_(GoalJournal.goal_id, GoalJournal.session_n).in_(pairs),
                )
            )
        ).all():
            for ref in (str(item) for item in evidence or []):
                if ref.startswith(ARRIVALS_REF) and ref[len(ARRIVALS_REF) :].isdigit():
                    recorded[(goal_id, session_n)] = int(ref[len(ARRIVALS_REF) :])
                elif ref.startswith(RESUMED_REF):
                    known.add(ref)
        roots = sorted(closed)
        arrivals: dict[UUID, list[Any]] = {}
        for item in (
            await s.execute(
                select(
                    IntentionArrival.root_id,
                    IntentionArrival.id,
                    IntentionArrival.n,
                    IntentionArrival.decision,
                    IntentionArrival.outcome,
                )
                .where(IntentionArrival.agent_id == self._agent_id, IntentionArrival.root_id.in_(roots))
                .order_by(IntentionArrival.root_id, IntentionArrival.n)
            )
        ).all():
            arrivals.setdefault(item.root_id, []).append(item)
        member = aliased(Intention)
        states = {
            item.id: item
            for item in (
                await s.execute(
                    select(
                        Intention.id,
                        Intention.root_cancelled_at,
                        Intention.root_expired_at,
                        exists()
                        .where(
                            member.agent_id == self._agent_id,
                            member.root_id == Intention.id,
                            member.state.in_(continuation.OPEN_STATES),
                        )
                        .label("open_work"),
                    ).where(Intention.agent_id == self._agent_id, Intention.id.in_(roots))
                )
            ).all()
        }
        plans = dict(
            (
                await s.execute(
                    select(Goal.id, Goal.plan).where(
                        Goal.agent_id == self._agent_id, Goal.id.in_(sorted({r.goal_id for r in closed.values()}))
                    )
                )
            ).all()
        )
        found: dict[UUID, list[_JournalItem]] = {}
        for root_id, row in closed.items():
            since = recorded.get((row.goal_id, row.n))
            after = [a for a in arrivals.get(root_id, []) if since is not None and a.n > since]
            state = states.get(root_id)
            if not after or state is None:
                continue
            marked = state.root_cancelled_at is not None or state.root_expired_at is not None
            ref = f"{RESUMED_REF}{after[-1].id}"
            if (state.open_work and not marked) or ref in known:
                continue
            steps = sum(
                1
                for step in plans.get(row.goal_id) or []
                if isinstance(step, dict) and step.get("closed_session") == row.n
            )
            if any(a.outcome == continuation.OUTCOME_FAILED for a in after):
                outcome = "error"
            elif steps > row.steps_closed:
                outcome = "step_closed"
            elif any(a.decision == "ask" for a in after):
                outcome = "asked"
            else:
                outcome = "interrupted" if marked else "no_step"
            text_ = f"Session {row.n}'s chain resumed after the session closed and ended again: {outcome}."
            found[row.id] = [_JournalItem("state", "harness", text_, [ref])]
        return found

    async def _reconcile_resumed(self, session_id: UUID) -> int:
        """Journal that a closed session's resumed chain ended (``_resumed_endings``), in a transaction of its own
        that holds the session row's lock, so two drivers cannot both write it. Returns the rows journalled."""
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
            items = (await self._resumed_endings(s, [row])).get(row.id, [])
            for item in items:
                self._journal(
                    s, row.goal_id, item.kind, item.text, actor=item.actor, session_n=row.n, evidence=item.refs
                )
            await s.commit()
            return len(items)

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
        evidence: Iterable[str] = (),
        cancelled_by_owner: bool = False,
    ) -> bool:
        """The close's writes, in the caller's transaction: the session row (only while still open), an active
        goal's next wake, and the harness ``state`` row (with ``evidence``). False when the session was closed
        meanwhile (nothing else is written). When the owner cancelled the session's root (``cancelled_by_owner``),
        an active goal is paused (``owner``) instead, its wake and request cleared, with a harness row saying so.

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
                spent=spent,
                steps_closed=steps,
                summary=summary,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            return False
        active = [Goal.id == goal.id, Goal.agent_id == self._agent_id, Goal.state == "active"]
        paused = False
        if cancelled_by_owner:
            values = {"state": "paused", "pause_reason": "owner", "next_wake_at": None}
            paused = (
                await s.execute(
                    update(Goal)
                    .where(*active)
                    .values(**values, pending_request=None, pending_request_at=None, updated_at=func.clock_timestamp())
                    .execution_options(synchronize_session=False)
                )
            ).rowcount == 1
        else:
            interval = timedelta(seconds=goal.min_interval_seconds * (1 if outcome == "step_closed" else 2))
            await s.execute(
                update(Goal)
                .where(*active)
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
        self._journal(s, goal.id, "state", note, actor="harness", session_n=row.n, evidence=evidence)
        if paused:
            self._journal(
                s,
                goal.id,
                "state",
                f"Paused: the owner cancelled session {row.n}'s chain.",
                actor="harness",
                session_n=row.n,
            )
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
        roots. After a restart the flags are gone, and no age of a close proves its spend final (a worker may have
        stored its tokens while no driver ran), so the first ticks after a start read EVERY closed session once:
        ``CATCH_UP_BATCH`` a tick, newest close first, each batch as one set (one settled read, one tally of all its
        roots, one read of their questions); the catch-up is done when a batch comes back short. A crash midway
        starts it over. Within a tick, whether a closed session's work has settled is read BEFORE its spend (and
        its questions), and the session is dropped only on that earlier read: what is stored between the reads is
        in this tick's read, or the session stays followed one more tick.

        A closed session's root can resume (a late result reopens it), and its chain can then ask the owner and get
        an answer after the close journalled the session's questions: every tick, the questions and answers of the
        closed sessions read here that the journal does not hold yet are journalled, each session in a transaction
        of its own holding the session row's lock, as the close does. When such a chain has ended again, one harness
        row says so and how it ended (``_resumed_endings``); the session's outcome, steps and summary stay what the
        close wrote, and runs of outcomes (a stall, errors in a row) read only those.

        The closed sessions whose root's deadline has not passed are found among those started within the root
        TTL plus ``DEADLINE_SCAN_SLACK`` (an index range, not every session the agent ever had); one whose first task
        was published more than the slack after its start, or which started under a longer TTL than today's, is
        read from that branch only while it falls inside the range. A lineage DAG that leaves a terminal state again
        (``retry_node`` reactivates a failed one) makes its closed session unsettled: the agent's DAGs that are not
        terminal (an index on status) bring their closed sessions into the read, and so into ``_following``. Not
        followed: a retry that starts, finishes and has its tokens claimed between two ticks after the session was
        dropped, and a check run already under way when its heartbeat check was disabled (its tokens land after the
        check stopped holding the session).
        Returns the sessions whose spend changed."""
        root = aliased(Intention)
        following = GoalSession.id.in_(sorted(self._following)) if self._following else false()
        # now(), the transaction's start: stable, so the planner can use it as an index bound (clock_timestamp()
        # is volatile and cannot be one). The slack is far larger than the moments between the two.
        recent = GoalSession.started_at > func.now() - (
            timedelta(hours=float(self._settings.intention_root_ttl_hours)) + DEADLINE_SCAN_SLACK
        )
        columns = (
            GoalSession.id,
            GoalSession.goal_id,
            GoalSession.n,
            GoalSession.root_id,
            GoalSession.spent,
            GoalSession.ended_at,
            GoalSession.steps_closed,
        )
        query = (
            select(*columns)
            .join(root, and_(root.agent_id == self._agent_id, root.id == GoalSession.root_id))
            .where(
                GoalSession.agent_id == self._agent_id,
                # On the session row alone, so each branch is an index range: open, recent, followed.
                or_(GoalSession.ended_at.is_(None), recent, following),
                or_(GoalSession.ended_at.is_(None), root.deadline > func.clock_timestamp(), following),
            )
        )
        member, dag = aliased(Intention), aliased(ExecutionDAG)
        reactivated = (
            select(*columns)
            .join(member, and_(member.agent_id == self._agent_id, member.root_id == GoalSession.root_id))
            .join(dag, and_(dag.agent_id == self._agent_id, cast(dag.id, Text) == member.source_id))
            .where(
                GoalSession.agent_id == self._agent_id,
                GoalSession.ended_at.is_not(None),
                member.source_kind == intentions.SOURCE_DAG,
                dag.status.not_in(intentions.TERMINAL_DAG_STATUSES),
            )
        )
        changed = 0
        async with self._db.session() as s:
            rows = {r.id: r for r in (await s.execute(query)).all()}
            for r in (await s.execute(reactivated)).all():
                rows.setdefault(r.id, r)
            batch: list[Any] = []
            catching_up, cursor_read = not self._caught_up, self._catch_up_after
            if catching_up:
                batch_query = (
                    select(*columns)
                    .where(
                        GoalSession.agent_id == self._agent_id,
                        GoalSession.ended_at.is_not(None),
                        GoalSession.root_id.is_not(None),
                    )
                    .order_by(GoalSession.ended_at.desc(), GoalSession.id.desc())
                    .limit(CATCH_UP_BATCH)
                )
                if cursor_read is not None:
                    batch_query = batch_query.where(tuple_(GoalSession.ended_at, GoalSession.id) < tuple_(*cursor_read))
                batch = list((await s.execute(batch_query)).all())
                for r in batch:
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
            resumed: dict[UUID, list[_JournalItem]] = {}
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
                closed_rows = [r for r in rows.values() if r.ended_at is not None]
                asked = await self._new_questions_and_answers(s, closed_rows)
                resumed = await self._resumed_endings(s, closed_rows)
            await s.commit()
        # Only what this pass read itself changes the followed set: a closed session it read as settled leaves,
        # one it read as unsettled joins. An id a close added meanwhile (a session this pass read while it was
        # still open) stays, so its later usage is still read.
        read_closed = [r for r in rows.values() if r.ended_at is not None]
        self._following -= {r.id for r in read_closed if r.root_id not in unsettled_roots}
        self._following |= {r.id for r in read_closed if r.root_id in unsettled_roots}
        if catching_up and self._catch_up_after == cursor_read:  # moved only from the cursor this pass read from
            if len(batch) < CATCH_UP_BATCH:
                self._caught_up, self._catch_up_after = True, None
            else:
                self._catch_up_after = (batch[-1].ended_at, batch[-1].id)
        for session_id in dict.fromkeys([*asked, *resumed]):  # a session's questions before how its chain ended
            try:
                if session_id in asked:
                    await self._reconcile_questions_and_answers(session_id)
                if session_id in resumed:
                    await self._reconcile_resumed(session_id)
            except Exception:
                logger.exception("Goal session %s: its resumed chain could not be journalled; tried again", session_id)
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

    # ---- stop conditions -----------------------------------------------------------------------------------

    async def apply_stop_conditions(self, *, now: datetime | None = None) -> int:
        """Pause every active goal a stop condition holds, the first that holds (``_stop_reason``): ``deadline``,
        ``budget``, ``stalled``, ``error``. Each pause is ``_pause``: only while the goal is still active and the
        condition still holds in its row, its wake and any request cleared, with one harness row whose evidence is
        ``stop:<reason>`` and whose session is the goal's newest, so runs of outcomes restart after it. A paused
        goal (``waiting_for_owner`` included) is never looked at, and a pause cannot happen twice. ``now`` defaults
        to the database's clock. Returns the goals paused.

        What gets each one going again is the store's: ``resume`` (refused while the budget is spent or the
        deadline has passed: ``raise_budget`` or ``extend_deadline`` first). A stall or an error run counts only
        sessions after the pause that ended it, so a resumed goal is not paused again for the same sessions."""
        async with self._db.session() as s:
            if now is None:
                now = (await s.execute(select(func.clock_timestamp()))).scalar_one()
            goals = list(
                (await s.execute(select(Goal).where(Goal.agent_id == self._agent_id, Goal.state == "active"))).scalars()
            )
            found = [(goal.id, await self._stop_reason(s, goal, now)) for goal in goals]
        paused = 0
        for goal_id, stop in found:
            if stop is None:
                continue
            reason, note, still = stop
            try:
                if await self._pause(
                    goal_id, reason, note, evidence=[f"{STOP_REF}{reason}"], still=still, newest_session=True
                ):
                    paused += 1
            except Exception:
                logger.exception("Goal %s: the pause (%s) failed; tried again next tick", goal_id, reason)
        return paused

    async def _stop_reason(
        self, s: AsyncSession, goal: Goal, now: datetime
    ) -> tuple[str, str, list[ColumnElement[bool]]] | None:
        """The first stop condition that holds for ``goal`` at ``now``: ``(reason, the pause row's words, the
        condition again as guards on the goal row)``, else None. The guards make the pause a no-op when the owner
        changed the goal between this read and the pause (extended the deadline, raised the budget). Spend only
        grows, so a budget read as spent stays spent; the runs are read from closing outcomes only."""
        if _deadline_passed(goal, now):
            return "deadline", "Paused: the deadline has passed.", [Goal.deadline_at <= now]
        total, _today = await spend.goal_spent(s, self._agent_id, goal.id, now=now)
        if _budget_spent(total, goal.total_budget):
            note = f"Paused: the total budget is spent ({total} of {goal.total_budget} tokens)."
            return "budget", note, [Goal.total_budget <= total]
        stall = await self._run_length(s, goal.id, counts="no_step", breaks=("step_closed", "error"), reason="stalled")
        if stall >= goal.stall_limit:
            return "stalled", f"Paused: {stall} sessions in a row took no step.", [Goal.stall_limit <= stall]
        errors = await self._run_length(
            s, goal.id, counts="error", breaks=("step_closed", "asked", "no_step"), reason="error"
        )
        if errors >= ERRORS_BEFORE_PAUSE:
            return "error", f"Paused: {errors} sessions in a row ended in an error.", []
        return None

    async def _run_length(
        self, s: AsyncSession, goal_id: UUID, *, counts: str, breaks: tuple[str, ...], reason: str
    ) -> int:
        """The newest run of ``counts`` outcomes of the goal's closed sessions: those after the newest session whose
        outcome is in ``breaks`` and after the session of the newest pause for ``reason`` (``stop:<reason>``); any
        other outcome neither counts nor breaks it. One count, with nothing stored to drift."""
        broken = (
            select(func.coalesce(func.max(GoalSession.n), 0))
            .where(
                GoalSession.agent_id == self._agent_id,
                GoalSession.goal_id == goal_id,
                GoalSession.outcome.in_(breaks),
            )
            .scalar_subquery()
        )
        paused_at = (
            select(func.coalesce(func.max(GoalJournal.session_n), 0))
            .where(
                GoalJournal.agent_id == self._agent_id,
                GoalJournal.goal_id == goal_id,
                GoalJournal.kind == "state",
                GoalJournal.actor == "harness",
                GoalJournal.evidence.contains([f"{STOP_REF}{reason}"]),
            )
            .scalar_subquery()
        )
        return int(
            await s.scalar(
                select(func.count()).where(
                    GoalSession.agent_id == self._agent_id,
                    GoalSession.goal_id == goal_id,
                    GoalSession.outcome == counts,
                    GoalSession.n > broken,
                    GoalSession.n > paused_at,
                )
            )
            or 0
        )

    # ---- the continuation runner's priority ----------------------------------------------------------------

    async def goal_roots(self, root_ids: Iterable[UUID]) -> frozenset[UUID]:
        """Which of ``root_ids`` are roots of goal sessions: one indexed read (``idx_goal_sessions_root``). The
        continuation runner keeps such a root out of the slots it reserves for the owner's chains. A session's root
        id is written in the transaction that creates the root (``_publish``), so a root that can be launched has
        it."""
        ids = list(dict.fromkeys(root_ids))
        if not ids:
            return frozenset()
        async with self._db.session() as s:
            found = await s.execute(
                select(GoalSession.root_id).where(GoalSession.agent_id == self._agent_id, GoalSession.root_id.in_(ids))
            )
            return frozenset(found.scalars())

    # ---- goal rows -----------------------------------------------------------------------------------------

    async def _pause(
        self,
        goal_id: UUID,
        reason: str,
        note: str,
        *,
        session_n: int | None = None,
        evidence: Iterable[str] = (),
        still: Iterable[ColumnElement[bool]] = (),
        newest_session: bool = False,
    ) -> bool:
        """An active goal to ``paused`` for ``reason``, its wake and any request cleared, with a harness ``state``
        row saying why (with ``evidence``), in one transaction. ``still`` are more guards on the goal row (the
        condition read again). With ``newest_session`` the row's session is the goal's newest, read after the
        update has locked the goal row (no start of this goal can add one before the commit). False (nothing
        written) when the goal is no longer active or a guard fails."""
        async with self._db.session() as s:
            result = await s.execute(
                update(Goal)
                .where(Goal.id == goal_id, Goal.agent_id == self._agent_id, Goal.state == "active", *still)
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
            if newest_session:
                session_n = await s.scalar(
                    select(func.max(GoalSession.n)).where(
                        GoalSession.agent_id == self._agent_id, GoalSession.goal_id == goal_id
                    )
                )
            self._journal(s, goal_id, "state", note, actor="harness", session_n=session_n, evidence=evidence)
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
