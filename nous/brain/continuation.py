"""F099 Phase 2: the continuation store (data and routing; the runner follows).

Phase 2b puts the data and the routing here: the inbox primitives, the
same-transaction move of an intention to ``result_ready``, owner-facing rows,
and the startup rollback. The runner, the claim, proposals and cancel are
later PRs and fill this module in. Callers use the module
(``continuation.record_result(...)``), not its names, so one monkeypatch reaches
every writer.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, Text, and_, cast, exists, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain import intentions
from nous.storage.models import Intention, IntentionProposal, ResultInbox

logger = logging.getLogger(__name__)

# Flipped to True by PR-2e, in the commit that wires the runner into main.py.
# While it is False, main.py forces NOUS_CONTINUATION_ENABLED off: with the flag
# on and no runner, a continue result is written NULL-keyed and nothing claims it.
CONTINUATION_RUNNER_READY: bool = False

INTENT_SESSION_PREFIX = "intent-"  # session id of a root's thread: f"intent-{root_id}"
SOURCE_INTENTION_REPORT = "intention_report"  # inbox source kind of an owner-facing row
MSG_REPORT, MSG_QUESTION, MSG_PROPOSAL = "REPORT", "QUESTION", "PROPOSAL"
REPORT_KINDS = (MSG_REPORT, MSG_QUESTION, MSG_PROPOSAL)
CLOSE_DELIVERED, CLOSE_RESOLVED, CLOSE_CANCELLED, CLOSE_EXPIRED = "delivered", "resolved", "cancelled", "expired"
CLOSE_FALLBACK_REPORT, CLOSE_FAILED_REPORT = "fallback_report", "failed_report"
OUTCOME_RESOLVED, OUTCOME_FALLBACK, OUTCOME_FAILED = "resolved", "fallback_report", "failed_report"
DECISIONS = ("continue", "revise", "drop", "report", "ask")
PROPOSAL_TERMINAL = frozenset({"executed", "failed", "rejected", "expired", "cancelled"})
OPEN_STATES = ("pending", "result_ready", "deciding", "awaiting_owner")

STATE_PENDING, STATE_RESULT_READY, STATE_CLOSED = "pending", "result_ready", "closed"
STATE_CANCELLED, STATE_EXPIRED = "cancelled", "expired"

# heart.result_inbox.title is VARCHAR(200); tests pin it equal to result_inbox._TITLE_MAX.
INBOX_TITLE_MAX = 200
# The UNIQUE key of heart.result_inbox, in column order (migration 084): the one
# conflict target every insert uses.
INBOX_SOURCE_KEY = ("source_kind", "source_id", "source_generation", "agent_id")


@dataclass(frozen=True, slots=True)
class ResultRecorded:
    """What ``record_result`` did (contract section 4.7).

    ``inserted`` is True when the row this call was asked for (the continue row or the REPORT) was new.
    The settled source-keyed twin row of a report is never counted in ``inserted``, so callers decide on
    ``reported`` / ``state_after``.
    """

    inbox_id: UUID | None
    inserted: bool
    state_after: str
    reopened: bool
    reported: bool
    intention_id: UUID
    root_id: UUID


def enabled(settings: Any) -> bool:
    """NOUS_CONTINUATION_ENABLED, read so that a mocked Settings counts as off.

    Also needs intentions: the validator forces both, this re-checks for a
    settings object that bypassed it."""
    return getattr(settings, "continuation_enabled", False) is True and intentions.enabled(settings)


def close_reason_for(settings: Any) -> str:
    """The close reason of a ``none``, ``remember`` or ``report`` intention (ruling 2):
    ``delivered`` with the flag on, ``legacy`` (Phase 1's) otherwise."""
    return CLOSE_DELIVERED if enabled(settings) else intentions.CLOSE_LEGACY


def owner_channel(settings: Any, origin_channel: str | None) -> str | None:
    """Where an owner-facing row goes: the intention's origin channel, else the
    default chat; None when neither exists (nothing can be routed)."""
    if origin_channel and origin_channel.strip():
        return origin_channel.strip()
    chat_id = str(getattr(settings, "telegram_chat_id", "") or "").strip()
    return f"telegram:{chat_id}" if chat_id else None


def intention_keyed(agent_id: str, intention_ids: Any) -> ColumnElement[bool]:
    """The inbox rows only the continuation may read: keyed by one of
    ``intention_ids`` alone. Owner-facing rows also carry an ``intention_id``
    but have a channel, so chat reads them and this does not (contract C9)."""
    return and_(
        ResultInbox.agent_id == agent_id,
        ResultInbox.intention_id.in_(list(intention_ids)),
        ResultInbox.channel.is_(None),
        ResultInbox.session_id.is_(None),
    )


def has_continue_intention(
    agent_id: str, source_kind: str, source_id_col: Any, *, include_closed: bool = False
) -> ColumnElement[bool]:
    """EXISTS: the work row (``source_id_col``, a uuid column of a subtask or DAG)
    has a ``continue`` intention that is still owed a result. The reconciler
    passes use it so a row keyed by the intention alone is still repaired. A
    closed intention counts only when ``include_closed`` (a DAG's retry re-arrives),
    and never one closed as ``legacy``: Phase 1 or the startup rollback closed it
    and F098 delivered its result to chat, so nothing may reopen it."""
    state = Intention.state.in_(OPEN_STATES)
    if include_closed:
        state = or_(
            state,
            and_(Intention.state == STATE_CLOSED, Intention.close_reason.is_distinct_from(intentions.CLOSE_LEGACY)),
        )
    return exists().where(
        Intention.agent_id == agent_id,
        Intention.source_kind == source_kind,
        Intention.source_id == cast(source_id_col, Text),
        Intention.wake_policy == intentions.WAKE_CONTINUE,
        state,
    )


async def insert_inbox_row(
    session: AsyncSession,
    agent_id: str,
    *,
    source_kind: str,
    source_id: UUID,
    msg_type: str,
    title: str,
    body: str,
    channel: str | None = None,
    session_id: str | None = None,
    source_generation: int = 0,
    correlation_id: str | None = None,
    created_at: datetime | None = None,
    intention_id: UUID | None = None,
    arrival_id: UUID | None = None,
    proposal_id: UUID | None = None,
    push_after: datetime | None = None,
    delivered_at: datetime | None = None,
    delivered_session_id: str | None = None,
) -> UUID | None:
    """The one INSERT into ``heart.result_inbox``, in the caller's transaction.

    Returns the new row's id, or None when the UNIQUE key already had the row
    (every writer is idempotent). Does not commit. ``delivered_at`` writes a row
    already settled (the source-keyed row of a result that became a report).
    """
    row_id = uuid.uuid4()
    stmt = (
        pg_insert(ResultInbox)
        .values(
            id=row_id,
            agent_id=agent_id,
            channel=channel,
            session_id=session_id,
            source_kind=source_kind,
            source_id=source_id,
            source_generation=source_generation,
            msg_type=msg_type,
            correlation_id=correlation_id,
            reply_to=channel,
            title=title[:INBOX_TITLE_MAX],
            body=body,
            created_at=created_at or datetime.now(UTC),
            intention_id=intention_id,
            arrival_id=arrival_id,
            proposal_id=proposal_id,
            push_after=push_after,
            delivered_at=delivered_at,
            delivered_session_id=delivered_session_id,
        )
        .on_conflict_do_nothing(index_elements=INBOX_SOURCE_KEY)
    )
    result = await session.execute(stmt)
    return row_id if result.rowcount else None


async def insert_report(
    session: AsyncSession,
    agent_id: str,
    *,
    kind: str,
    title: str,
    body: str,
    channel: str,
    intention_id: UUID,
    root_id: UUID,
    arrival_id: UUID | None = None,
    proposal_id: UUID | None = None,
    push_after: datetime | None = None,
    report_id: UUID | None = None,
) -> UUID:
    """An owner-facing row (REPORT, QUESTION or PROPOSAL) in the caller's transaction.

    Keyed to ``channel`` (never NULL: section 4.3 item 4); its ``source_id`` is
    ``report_id`` (a fresh uuid unless the caller needs the write to be
    idempotent) and its generation 0. Returns ``report_id``. ``root_id`` is for
    the log only: the table has no root column (contract C2).
    """
    if kind not in REPORT_KINDS:
        raise ValueError(f"an owner-facing row is one of {REPORT_KINDS}, not {kind!r}")
    rid = report_id or uuid.uuid4()
    await _insert_report_row(
        session,
        agent_id,
        rid,
        kind=kind,
        title=title,
        body=body,
        channel=channel,
        intention_id=intention_id,
        root_id=root_id,
        arrival_id=arrival_id,
        proposal_id=proposal_id,
        push_after=push_after,
    )
    return rid


async def _insert_report_row(
    session: AsyncSession,
    agent_id: str,
    report_id: UUID,
    *,
    kind: str,
    title: str,
    body: str,
    channel: str,
    intention_id: UUID,
    root_id: UUID,
    arrival_id: UUID | None = None,
    proposal_id: UUID | None = None,
    push_after: datetime | None = None,
    created_at: datetime | None = None,
) -> UUID | None:
    """The row of ``insert_report``; its id, or None when ``report_id`` was already written."""
    if not channel or not channel.strip():
        raise ValueError("an owner-facing row needs a channel (spec section 4.3 item 4)")
    row_id = await insert_inbox_row(
        session,
        agent_id,
        source_kind=SOURCE_INTENTION_REPORT,
        source_id=report_id,
        msg_type=kind,
        title=title,
        body=body,
        channel=channel.strip(),
        correlation_id=str(report_id),
        created_at=created_at,
        intention_id=intention_id,
        arrival_id=arrival_id,
        proposal_id=proposal_id,
        push_after=push_after,
    )
    if row_id is not None:
        logger.info("F099: %s row %s for intention %s (root %s)", kind, report_id.hex[:8], intention_id, root_id)
    return row_id


async def close_delivered(
    session: AsyncSession, agent_id: str, source_kind: str, source_id: Any, *, with_result: bool = True
) -> UUID | None:
    """T3: close the pending intention of a finished source as ``delivered``, in the
    caller's transaction. Its id, or None when the source recorded none."""
    return await intentions.close_for_source(
        session, agent_id, source_kind, source_id, reason=CLOSE_DELIVERED, with_result=with_result
    )


# A fixed namespace: the report of an arrival nothing can reopen has a
# deterministic id, so the DAG bus listener and DAGResultDelivery.deliver, which
# both reach the writer, collapse on the inbox's UNIQUE key (contract C2).
_REPORT_NAMESPACE = uuid.UUID("5d0c7e1e-6a7b-4f0e-9a52-0f0990b2c3d4")


def arrival_report_id(source_kind: str, source_id: Any, generation: int) -> UUID:
    """The ``source_id`` of the ``intention_report`` a re-arrival becomes."""
    return uuid.uuid5(_REPORT_NAMESPACE, f"{source_kind}:{source_id}:{int(generation)}")


async def _root_is_open(session: AsyncSession, agent_id: str, root_id: UUID) -> bool:
    """A root is open while neither root marker is set. A separate statement from the
    intention's lock: under READ COMMITTED it sees a cancel that committed first."""
    markers = (
        await session.execute(
            select(Intention.root_cancelled_at, Intention.root_expired_at).where(
                Intention.agent_id == agent_id, Intention.id == root_id
            )
        )
    ).first()
    return markers is not None and markers.root_cancelled_at is None and markers.root_expired_at is None


async def _set_result_ready(
    session: AsyncSession, agent_id: str, intention_id: UUID, *, from_state: str, now: datetime
) -> None:
    """T4 (``pending``) and T6 (``closed``, a reopen): the conditional UPDATE. The caller holds
    the row lock, so a miss is a bug, not a race: it raises and the caller's transaction rolls back."""
    values: dict[str, Any] = {"state": STATE_RESULT_READY, "result_at": now, "updated_at": now}
    if from_state == STATE_CLOSED:
        values.update(close_reason=None, closed_at=None)
    moved = (
        await session.execute(
            update(Intention)
            .where(
                Intention.agent_id == agent_id,
                Intention.id == intention_id,
                Intention.state == from_state,
                Intention.wake_policy == intentions.WAKE_CONTINUE,
            )
            .values(**values)
            .returning(Intention.id)
            .execution_options(synchronize_session=False)
        )
    ).scalar_one_or_none()
    if moved is None:
        raise RuntimeError(f"intention {intention_id} left {from_state!r} while its row was locked")


async def record_result(
    session: AsyncSession,
    agent_id: str,
    *,
    intention_id: UUID,
    source_kind: str,
    source_id: UUID,
    msg_type: str,
    title: str,
    body: str,
    source_generation: int = 0,
    correlation_id: str | None = None,
    created_at: datetime | None = None,
    arrival_id: UUID | None = None,
    settings: Any,
) -> ResultRecorded:
    """The one Phase 2 writer for a ``continue`` result, in the caller's transaction (spec 4.3).

    Locks the intention ``FOR UPDATE``, then: a ``continue`` intention with an open root gets a row
    keyed by the intention alone (``channel`` and ``session_id`` NULL) and, if it was ``pending`` (T4)
    or ``closed`` (T6, a reopen), moves to ``result_ready`` in the same transaction. A row arriving
    while the intention is ``result_ready``, ``deciding`` or ``awaiting_owner`` is inserted and held:
    the state is left alone. Anything nothing can reopen (another policy, a closed root, a
    ``cancelled`` or ``expired`` intention, one closed as ``legacy`` by Phase 1 or the startup rollback,
    whose result F098 already delivered) becomes an owner-facing ``intention_report`` carrying the
    raw result, plus the work row's own inbox row, NULL-keyed and stamped delivered, so the reconciler
    passes see the source as written. The state UPDATE runs only when the row was written, so a
    duplicate delivery is a no-op; the REPORT is written only with a newly written twin, so a duplicate
    of a generation that first landed on the continue path (before a root cancel) reports nothing.
    ``arrival_id`` is the arrival an owner answer belongs to (2d). Emits nothing: the caller emits
    ``intention.result_ready`` after it commits.

    The intention is locked and read by its columns, never as an ORM entity: a ``SELECT`` of the entity
    returns, unrefreshed, an ``Intention`` the caller's session already holds. Such an entity is not
    refreshed by the Core UPDATE here either: a caller that loaded one must ``session.refresh`` it.
    """
    row = (
        await session.execute(
            select(
                Intention.state,
                Intention.close_reason,
                Intention.wake_policy,
                Intention.root_id,
                Intention.origin_channel,
            )
            .where(Intention.agent_id == agent_id, Intention.id == intention_id)
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise LookupError(f"intention {intention_id} does not exist for agent {agent_id}")
    state, policy, root_id, origin_channel = row.state, row.wake_policy, row.root_id, row.origin_channel
    now = datetime.now(UTC)

    if (
        policy != intentions.WAKE_CONTINUE
        or state in (STATE_CANCELLED, STATE_EXPIRED)
        # A legacy close is never reopened (a DAG's retry_node re-arrival included): it reports, and its
        # settled twin keeps InboxDagPass from re-selecting the work row (MF-1).
        or (state == STATE_CLOSED and row.close_reason == intentions.CLOSE_LEGACY)
        or not await _root_is_open(session, agent_id, root_id)
    ):
        report_id = arrival_report_id(source_kind, source_id, source_generation)
        # MF-1: the work row's own inbox row, NULL-keyed and already delivered. The F098 reconciler passes
        # decide "needs repair" by this row (has_row): without it they would re-select the work row on
        # every tick for good. NULL-keyed and delivered, no chat turn can claim it.
        twin = await insert_inbox_row(
            session,
            agent_id,
            source_kind=source_kind,
            source_id=source_id,
            msg_type=msg_type,
            title=title,
            body=body,
            channel=None,
            session_id=None,
            source_generation=source_generation,
            correlation_id=correlation_id,
            created_at=created_at,
            intention_id=intention_id,
            arrival_id=arrival_id,
            delivered_at=now,
            delivered_session_id=f"report:{report_id.hex[:8]}",
        )
        if twin is None:
            # This generation was already written (on this path, or on the continue path before the
            # root closed): the first delivery decided its fate, so a duplicate writes no REPORT.
            return ResultRecorded(None, False, state, False, False, intention_id, root_id)
        channel = owner_channel(settings, origin_channel)
        if channel is None:
            logger.warning(
                "F099: a result of %s %s has no owner channel (intention %s: no origin channel, no default chat); "
                "it stays on its work row",
                source_kind,
                str(source_id)[:8],
                intention_id,
            )
            return ResultRecorded(None, False, state, False, False, intention_id, root_id)
        report_row = await _insert_report_row(
            session,
            agent_id,
            report_id,
            kind=MSG_REPORT,
            title=title,
            body=body,
            channel=channel,
            intention_id=intention_id,
            root_id=root_id,
            arrival_id=arrival_id,
            created_at=created_at,
        )
        wrote = report_row is not None
        return ResultRecorded(report_row, wrote, state, False, wrote, intention_id, root_id)

    inbox_id = await insert_inbox_row(
        session,
        agent_id,
        source_kind=source_kind,
        source_id=source_id,
        msg_type=msg_type,
        title=title,
        body=body,
        channel=None,
        session_id=None,
        source_generation=source_generation,
        correlation_id=correlation_id,
        created_at=created_at,
        intention_id=intention_id,
        arrival_id=arrival_id,
    )
    if inbox_id is None:
        return ResultRecorded(None, False, state, False, False, intention_id, root_id)
    reopened = state == STATE_CLOSED
    if state in (STATE_PENDING, STATE_CLOSED):
        await _set_result_ready(session, agent_id, intention_id, from_state=state, now=now)
        state = STATE_RESULT_READY
    return ResultRecorded(inbox_id, True, state, reopened, False, intention_id, root_id)


@dataclass(frozen=True, slots=True)
class RollbackReport:
    """What ``rollback_at_startup`` did (contract section 4.7)."""

    closed: int
    rerouted_rows: int
    expired_proposals: int
    pushed_raw: int


_ROLLBACK_STATES = (STATE_RESULT_READY, "deciding", "awaiting_owner")
_SWEEP_BATCH = 200
_RAW_PUSH_CHARS = 3900
# delivered_session_id of a row the rollback sent by Telegram instead of routing.
ROLLBACK_SESSION_ID = "rollback"


async def rollback_at_startup(
    database: Any, settings: Any, *, telegram_push: Callable[[str], Awaitable[bool]] | None
) -> RollbackReport:
    """Spec 4.3 item 6, T15: take the continuation out of the loop at startup (flag off).

    Open ``continue`` intentions in ``result_ready``, ``deciding`` or ``awaiting_owner`` have their
    undelivered intention-keyed inbox rows re-routed to ``owner_channel`` (so F098's chat turn shows
    them), their ``staged`` and ``pending`` proposals expired (a later tap is refused), and are closed
    as ``legacy`` with the claim cleared; so is every ``pending`` intention whose source is already
    terminal (work that finished while the flags were off: task-1.9 carry-over 2). It runs whenever
    ``brain.intentions`` exists, ``NOUS_INTENTIONS_ENABLED`` off included, and does nothing when the
    continuation flag is on. If the inbox is off too, a re-routed row would be invisible: each row is
    sent by ``telegram_push`` instead and stamped delivered, and an intention whose push failed stays
    open for the next start (a result is never dropped to make the close succeed).

    One transaction applies the re-route, the expiry and the close, so a close can never outrun its
    rows. The network sends happen before it, outside any transaction, so the raw push is
    at-least-once: a crash between the push and the commit re-sends on the next start (a duplicate
    costs less than a lost result; the inbox-off state is not prod's).
    """
    if enabled(settings):
        return RollbackReport(0, 0, 0, 0)
    agent_id = settings.agent_id
    inbox_on = getattr(settings, "result_inbox_enabled", False) is True

    async with database.session() as session:
        open_rows = list(
            (
                await session.execute(
                    select(Intention)
                    .where(
                        Intention.agent_id == agent_id,
                        Intention.wake_policy == intentions.WAKE_CONTINUE,
                        Intention.state.in_(_ROLLBACK_STATES),
                    )
                    .order_by(Intention.created_at)
                )
            )
            .scalars()
            .all()
        )
        stuck: dict[UUID, list[ResultInbox]] = {it.id: [] for it in open_rows}
        if open_rows:
            rows = (
                await session.execute(
                    select(ResultInbox)
                    .where(intention_keyed(agent_id, list(stuck)), ResultInbox.delivered_at.is_(None))
                    .order_by(ResultInbox.created_at)
                )
            ).scalars()
            for row in rows:
                stuck[row.intention_id].append(row)

    pushed_ids: list[UUID] = []
    keep_open: set[UUID] = set()
    if not inbox_on:
        waiting = sum(len(rows) for rows in stuck.values())
        if telegram_push is None and waiting:
            logger.warning(
                "F099: the rollback found %d result(s) that cannot be delivered (the inbox is off and Telegram is not "
                "configured); they stay on their work rows",
                waiting,
            )
        elif telegram_push is not None:
            for it in open_rows:
                for row in stuck[it.id]:
                    if await telegram_push(f"{row.title}\n\n{row.body}"[:_RAW_PUSH_CHARS]):
                        pushed_ids.append(row.id)
                    else:
                        keep_open.add(it.id)

    now = datetime.now(UTC)
    closing = [it for it in open_rows if it.id not in keep_open]
    close_ids = [it.id for it in closing]
    rerouted = expired = closed = 0
    async with database.session() as session:
        if pushed_ids:
            await session.execute(
                update(ResultInbox)
                .where(ResultInbox.id.in_(pushed_ids), ResultInbox.delivered_at.is_(None))
                .values(delivered_at=now, delivered_session_id=ROLLBACK_SESSION_ID)
                .execution_options(synchronize_session=False)
            )
        if inbox_on:
            for it in closing:
                row_ids = [r.id for r in stuck[it.id]]
                channel = owner_channel(settings, it.origin_channel)
                if row_ids and channel is None:
                    logger.warning(
                        "F099: the rollback found %d result(s) of intention %s with no owner channel (no origin "
                        "channel, no default chat); they stay on their work row",
                        len(row_ids),
                        it.id,
                    )
                elif row_ids:
                    moved = await session.execute(
                        update(ResultInbox)
                        .where(ResultInbox.id.in_(row_ids), ResultInbox.delivered_at.is_(None))
                        .values(channel=channel, reply_to=channel)
                        .execution_options(synchronize_session=False)
                    )
                    rerouted += moved.rowcount or 0
        if close_ids:
            gone = await session.execute(
                update(IntentionProposal)
                .where(
                    IntentionProposal.agent_id == agent_id,
                    IntentionProposal.intention_id.in_(close_ids),
                    IntentionProposal.state.in_(("staged", "pending")),
                )
                .values(state="expired", updated_at=now)
                .execution_options(synchronize_session=False)
            )
            expired = gone.rowcount or 0
            closed += len(
                (
                    await session.execute(
                        update(Intention)
                        .where(
                            Intention.agent_id == agent_id,
                            Intention.id.in_(close_ids),
                            Intention.state.in_(_ROLLBACK_STATES),
                        )
                        .values(
                            state=STATE_CLOSED,
                            close_reason=intentions.CLOSE_LEGACY,
                            closed_at=now,
                            updated_at=now,
                            claim_token=None,
                            claimed_at=None,
                        )
                        .returning(Intention.id)
                        .execution_options(synchronize_session=False)
                    )
                )
                .scalars()
                .all()
            )
        while True:
            swept = await intentions.close_finished_sources(session, agent_id, limit=_SWEEP_BATCH)
            closed += len(swept)
            if len(swept) < _SWEEP_BATCH:
                break
        await session.commit()
    return RollbackReport(closed, rerouted, expired, len(pushed_ids))
