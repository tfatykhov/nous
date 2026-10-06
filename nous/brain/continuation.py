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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, Text, and_, cast, exists
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain import intentions
from nous.storage.models import Intention, ResultInbox

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
INBOX_SOURCE_KEY = ["source_kind", "source_id", "source_generation", "agent_id"]


@dataclass(frozen=True, slots=True)
class ResultRecorded:
    """What ``record_result`` did (contract section 4.7)."""

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
    closed intention counts only when ``include_closed`` (a DAG's retry re-arrives)."""
    states = OPEN_STATES + ((STATE_CLOSED,) if include_closed else ())
    return exists().where(
        Intention.agent_id == agent_id,
        Intention.source_kind == source_kind,
        Intention.source_id == cast(source_id_col, Text),
        Intention.wake_policy == intentions.WAKE_CONTINUE,
        Intention.state.in_(states),
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
        channel=channel,
        correlation_id=str(report_id),
        created_at=created_at,
        intention_id=intention_id,
        arrival_id=arrival_id,
        proposal_id=proposal_id,
        push_after=push_after,
    )
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
