"""F099 Phase 2: the continuation store (data and routing; the runner follows).

Phase 2b puts the data and the routing here: the inbox primitives, the
same-transaction move of an intention to ``result_ready``, owner-facing rows,
and the startup rollback. The runner, the claim, proposals and cancel are
later PRs and fill this module in. Callers use the module
(``continuation.record_result(...)``), not its names, so one monkeypatch reaches
every writer.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, Text, and_, any_, cast, exists, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from nous.brain import intentions
from nous.brain.schemas import ReasonInput, RecordInput
from nous.storage.models import (
    Decision,
    ExecutionDAG,
    Intention,
    IntentionArrival,
    IntentionProposal,
    ResultInbox,
    Subtask,
)

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
OUTCOMES = (OUTCOME_RESOLVED, OUTCOME_FALLBACK, OUTCOME_FAILED)
DECISIONS = ("continue", "revise", "drop", "report", "ask")
PROPOSAL_TERMINAL = frozenset({"executed", "failed", "rejected", "expired", "cancelled"})
OPEN_STATES = ("pending", "result_ready", "deciding", "awaiting_owner")

STATE_PENDING, STATE_RESULT_READY, STATE_CLOSED = "pending", "result_ready", "closed"
STATE_CANCELLED, STATE_EXPIRED = "cancelled", "expired"
STATE_DECIDING, STATE_AWAITING_OWNER = "deciding", "awaiting_owner"

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


@dataclass(frozen=True, slots=True)
class Claim:
    """What one claim took (contract section 4.7). ``deepest`` is the parent of every child the turn spawns."""

    root_id: UUID
    claim_token: UUID
    intentions: tuple[Intention, ...]
    deepest: Intention
    inbox_rows: tuple[ResultInbox, ...]


GATE_REASONS = (
    "cancelled",
    "expired",
    "past_deadline",
    "budget_turns",
    "budget_tokens",
    "budget_stall",
    "limit_depth",
    "limit_spawns",
    "plan_resolved",
)
# The gate reasons whose arrival drops the work (the others escalate: a report). Spec 4.5.3.
GATE_DROP_REASONS = ("cancelled", "expired", "plan_resolved")
PLAN_DROP_OUTCOMES = ("superseded", "noise")
# The owner-facing sentence for each gate reason (the arrival's note, and the head of a report).
GATE_TEXT = {
    "cancelled": "The owner cancelled this work.",
    "expired": "This work expired before its result could be acted on.",
    "past_deadline": "This result arrived after its deadline, so I did not act on it.",
    "budget_turns": "The follow-up budget for this work is used up, so I stopped here.",
    "budget_tokens": "The token budget for this work is used up, so I stopped here.",
    "budget_stall": "The last follow-ups made no progress, so I stopped here.",
    "limit_depth": "This work reached its depth limit and cannot spawn more, so I stopped here.",
    "limit_spawns": "This work reached its spawn limit and cannot spawn more, so I stopped here.",
    "plan_resolved": "The plan this work served has been resolved or superseded.",
}


@dataclass(frozen=True, slots=True)
class RootLimits:
    """A root's budgets, derived from rows when checked: nothing is counted, so nothing drifts.

    ``stalls`` saturates at ``continuation_stall_limit`` (only that many arrivals are read): it says whether
    the stall budget is spent, not how long the run is, so a view must not show it as a total.
    """

    depth: int
    spawns: int
    turns: int
    tokens: int
    stalls: int
    spawn_blocked: bool  # depth >= max_depth or spawns >= max_spawns
    escalate: str | None  # 'budget_turns' | 'budget_tokens' | 'budget_stall' | 'limit_depth' | 'limit_spawns' | None


@dataclass(frozen=True, slots=True)
class Resolution:
    """What a continuation decided (resolve_intention's arguments, or a gate's or a fallback's synthetic one)."""

    decision: str
    note: str
    progress_claimed: bool
    confidence: float


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
) -> UUID | None:
    """The row of ``insert_report``; its id, or None when ``report_id`` was already written.

    Always stamped now, never with the work's age: an owner-facing row's ``created_at`` is when the
    owner can see it, so F098's claim window (``result_inbox_max_age_hours``) starts then."""
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


# Spec 4.5.2, with one change (contract conflict C4): the per-root mutex is FOR NO KEY UPDATE, so the
# FK checks of a child intention's INSERT and an arrival's INSERT (FOR KEY SHARE on the root) do not wait
# on it. FOR UPDATE would let a sweep that holds the root and waits for a claimed row deadlock against a
# commit that holds the claimed row and needs the root. NO KEY UPDATE still excludes every other claimer
# and the spawn path's FOR SHARE (a spawn in flight and a claim conflict on the root row, as I1 needs).
# The UPDATE locks the claimed rows in scan order, not id order; that is harmless, because every 2c path
# takes the root lock first and record_result holds no second lock.
_CLAIM_SQL = text(
    """
    UPDATE brain.intentions i
       SET state = 'deciding', claimed_at = now(), claim_token = :token, updated_at = now()
     WHERE i.agent_id = :agent AND i.root_id = :root AND i.state = 'result_ready' AND i.wake_policy = 'continue'
       AND NOT EXISTS (SELECT 1 FROM brain.intentions d
                        WHERE d.agent_id = :agent AND d.root_id = :root AND d.state = 'deciding')
       AND (   (SELECT max(result_at) FROM brain.intentions
                 WHERE agent_id = :agent AND root_id = :root AND state = 'result_ready')
                 <= now() - make_interval(secs => CAST(:debounce AS double precision))
            OR (SELECT min(result_at) FROM brain.intentions
                 WHERE agent_id = :agent AND root_id = :root AND state = 'result_ready')
                 <= now() - make_interval(secs => CAST(:max_wait AS double precision)))
    RETURNING i.id
    """
)

_ELIGIBLE_SQL = text(
    """
    SELECT i.root_id AS root_id,
           LEAST(max(i.result_at) + make_interval(secs => CAST(:debounce AS double precision)),
                 min(i.result_at) + make_interval(secs => CAST(:max_wait AS double precision))) AS due
      FROM brain.intentions i
     WHERE i.agent_id = :agent AND i.state = 'result_ready' AND i.wake_policy = 'continue'
       AND NOT EXISTS (SELECT 1 FROM brain.intentions d
                        WHERE d.agent_id = :agent AND d.root_id = i.root_id AND d.state = 'deciding')
     GROUP BY i.root_id
     ORDER BY due
     LIMIT :limit
    """
)


async def claim_root(
    session: AsyncSession,
    agent_id: str,
    root_id: UUID,
    *,
    token: UUID,
    debounce_s: float,
    max_wait_s: float,
) -> Claim | None:
    """T7: claim every ``result_ready`` ``continue`` intention of ``root_id`` for one arrival (spec 4.5.2).

    One transaction, READ COMMITTED: the root row is locked first (the per-root mutex), then one UPDATE
    does the whole claim, so two claimers on a root are serialised and the second sees the first's
    ``deciding`` row. Returns None when nothing was claimable (the debounce has not elapsed, a claim is
    already live, or a ``report`` intention was forced into ``result_ready``); the caller commits only
    when a Claim comes back. The inbox rows are read here but stamped delivered only by the fenced commit.
    """
    locked = (
        await session.execute(
            select(Intention.id)
            .where(Intention.agent_id == agent_id, Intention.id == root_id)
            .with_for_update(key_share=True)
        )
    ).scalar_one_or_none()
    if locked is None:
        return None
    moved = (
        (
            await session.execute(
                _CLAIM_SQL,
                {
                    "agent": agent_id,
                    "root": root_id,
                    "token": token,
                    "debounce": float(debounce_s),
                    "max_wait": float(max_wait_s),
                },
            )
        )
        .scalars()
        .all()
    )
    if not moved:
        return None
    rows = (
        (
            await session.execute(
                select(Intention)
                .where(Intention.agent_id == agent_id, Intention.id.in_(list(moved)))
                .order_by(Intention.depth.desc(), Intention.created_at, Intention.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    inbox = (
        (
            await session.execute(
                select(ResultInbox)
                .where(intention_keyed(agent_id, list(moved)), ResultInbox.delivered_at.is_(None))
                .order_by(ResultInbox.created_at, ResultInbox.id)
            )
        )
        .scalars()
        .all()
    )
    return Claim(root_id, token, tuple(rows), rows[0], tuple(inbox))


async def eligible_roots(
    session: AsyncSession, agent_id: str, *, debounce_s: float, max_wait_s: float, limit: int = 50
) -> list[tuple[UUID, datetime]]:
    """The roots with a ``result_ready`` ``continue`` intention and no ``deciding`` one, each with the
    instant it becomes claimable (the claim's own rule: the newest result is debounce old, or the
    oldest has waited max-wait), earliest first, at most ``limit`` of them. The runner sleeps until the
    first. A root whose results carry no ``result_at`` is left out (code never writes one; a hand-made
    row is not claimable either, so listing it would spin the loop)."""
    result = await session.execute(
        _ELIGIBLE_SQL,
        {"agent": agent_id, "debounce": float(debounce_s), "max_wait": float(max_wait_s), "limit": int(limit)},
    )
    return [(row.root_id, row.due) for row in result if row.due is not None]


async def root_limits(session: AsyncSession, agent_id: str, root_id: UUID, *, settings: Any) -> RootLimits:
    """Every budget of a root, read from rows (spec 4.6: "all budgets are derived from rows when checked").

    Tokens are the lineage's subtask ``tokens_in/out`` (a DAG-node subtask has no intention of its own, and
    ``dag_node_id IS NULL`` keeps it out should one ever have one: its usage is already in its DAG's
    ``tokens_consumed``), plus its DAGs' ``tokens_consumed`` (which Task 2c1-8 feeds with check-node usage),
    plus its arrivals' tokens.
    """
    depth, spawns = (
        await session.execute(
            select(
                func.coalesce(func.max(Intention.depth), 0),
                func.count(Intention.id).filter(Intention.depth > 0),
            ).where(Intention.agent_id == agent_id, Intention.root_id == root_id)
        )
    ).one()
    subtask_tokens = (
        await session.execute(
            select(func.coalesce(func.sum(Subtask.tokens_in + Subtask.tokens_out), 0)).where(
                Subtask.agent_id == agent_id,
                Subtask.dag_node_id.is_(None),
                exists().where(
                    Intention.agent_id == agent_id,
                    Intention.root_id == root_id,
                    Intention.source_kind == "subtask",
                    Intention.source_id == cast(Subtask.id, Text),
                ),
            )
        )
    ).scalar_one()
    dag_tokens = (
        await session.execute(
            select(func.coalesce(func.sum(ExecutionDAG.tokens_consumed), 0)).where(
                ExecutionDAG.agent_id == agent_id,
                exists().where(
                    Intention.agent_id == agent_id,
                    Intention.root_id == root_id,
                    Intention.source_kind == "dag",
                    Intention.source_id == cast(ExecutionDAG.id, Text),
                ),
            )
        )
    ).scalar_one()
    arrival_tokens, turns = (
        await session.execute(
            select(
                func.coalesce(func.sum(IntentionArrival.tokens_in + IntentionArrival.tokens_out), 0),
                func.count(IntentionArrival.id).filter(IntentionArrival.gate_reason.is_(None)),
            ).where(IntentionArrival.agent_id == agent_id, IntentionArrival.root_id == root_id)
        )
    ).one()
    progress = (
        (
            await session.execute(
                select(IntentionArrival.progress)
                .where(
                    IntentionArrival.agent_id == agent_id,
                    IntentionArrival.root_id == root_id,
                    IntentionArrival.gate_reason.is_(None),
                    IntentionArrival.progress.is_not(None),
                )
                .order_by(IntentionArrival.n.desc())
                .limit(int(settings.continuation_stall_limit))
            )
        )
        .scalars()
        .all()
    )
    stalls = 0
    for verified in progress:  # the trailing run of verified no-progress arrivals, newest first
        if verified is not False:
            break
        stalls += 1
    tokens = int(subtask_tokens) + int(dag_tokens) + int(arrival_tokens)
    depth, spawns, turns = int(depth), int(spawns), int(turns)
    max_depth, max_spawns = settings.continuation_max_depth, settings.continuation_max_spawns_per_root
    escalate: str | None = None
    if turns >= settings.continuation_max_turns_per_root:
        escalate = "budget_turns"
    elif tokens >= settings.continuation_max_tokens_per_root:
        escalate = "budget_tokens"
    elif stalls >= settings.continuation_stall_limit:
        escalate = "budget_stall"
    elif depth >= max_depth:
        escalate = "limit_depth"
    elif spawns >= max_spawns:
        escalate = "limit_spawns"
    return RootLimits(depth, spawns, turns, tokens, stalls, depth >= max_depth or spawns >= max_spawns, escalate)


async def decision_outcome(session: AsyncSession, agent_id: str, decision_id: UUID) -> str | None:
    """A Brain decision's outcome (``pending``, ``success``, ``superseded``, ``noise`` ...), or None."""
    return (
        await session.execute(select(Decision.outcome).where(Decision.agent_id == agent_id, Decision.id == decision_id))
    ).scalar_one_or_none()


async def gate(
    session: AsyncSession,
    agent_id: str,
    claim: Claim,
    *,
    settings: Any,
    plan_outcome_of: Callable[[UUID], Awaitable[str | None]],
) -> str | None:
    """Spec 4.5.3: the deterministic checks before a turn, in order. Returns the ``gate_reason`` of the
    first that fails, or None (run the turn). No model is called.

    ``plan_outcome_of`` answers "what became of this Plan decision?" (2c-2 passes ``decision_outcome``
    bound to a session). It is required, so the Plan row cannot be switched off by leaving it out. The
    decision asked about is the originating one, the root's: a continuation child carries none (its turn
    has no Plan step, spec 4.5.4). Only a root without one falls back to the deepest claimed intention's;
    with neither, nothing is asked. A NULL ``deadline`` never trips ``past_deadline``: Phase 1 wrote none
    (task-1.9 carry-over 3).
    """
    markers = (
        await session.execute(
            select(Intention.root_cancelled_at, Intention.root_expired_at, Intention.origin_decision_id).where(
                Intention.agent_id == agent_id, Intention.id == claim.root_id
            )
        )
    ).first()
    # None cannot happen for a claim (claim_root locked the root row, and nothing deletes intentions).
    if markers is not None and markers.root_cancelled_at is not None:
        return "cancelled"
    if markers is not None and markers.root_expired_at is not None:
        return "expired"
    deadline = claim.deepest.deadline
    if deadline is not None and deadline <= datetime.now(UTC):
        return "past_deadline"
    limits = await root_limits(session, agent_id, claim.root_id, settings=settings)
    if limits.escalate is not None:
        return limits.escalate
    decision_id = markers.origin_decision_id if markers is not None else None
    if decision_id is None:
        decision_id = claim.deepest.origin_decision_id
    if decision_id is not None and await plan_outcome_of(decision_id) in PLAN_DROP_OUTCOMES:
        return "plan_resolved"
    return None


def raw_results_text(rows: Any) -> str:
    """The claimed results as the owner may read them: each row's title and body, as they arrived."""
    return "\n\n---\n\n".join(f"{row.title}\n{row.body}".strip() for row in rows)


def gate_inputs(reason: str, claim: Claim) -> tuple[Resolution, str | None]:
    """What a gate arrival commits: a synthetic decision and the text of its report.

    A drop (cancelled, expired, plan resolved) writes no report. An escalation (past deadline, a
    budget, a limit) reports the claimed results without acting on them: the reason, then the raw
    results (spec 4.5.3).
    """
    explanation = GATE_TEXT[reason]
    if reason in GATE_DROP_REASONS:
        return Resolution("drop", explanation, False, 1.0), None
    raw = raw_results_text(claim.inbox_rows)
    text_ = f"{explanation}\n\nWhat came back:\n{raw}" if raw else explanation
    return Resolution("report", explanation, False, 1.0), text_


@dataclass(frozen=True, slots=True)
class ArrivalCommit:
    """What ``commit_arrival`` wrote (contract section 4.14)."""

    arrival_id: UUID
    n: int
    next_states: dict[UUID, str]
    decision_record_id: UUID | None
    report_ids: tuple[UUID, ...]


class _FenceLost(Exception):
    """A fenced UPDATE moved fewer rows than the claim holds: the lease was released, or the root was
    cancelled or expired, since the claim. Raised inside a SAVEPOINT so nothing the function wrote survives."""


# The Brain record runs while the claimed rows are locked, so each of its statements is bounded (SET LOCAL
# statement_timeout inside its SAVEPOINT). The bound is per statement, not per call: the record's embedding
# request has its own HTTP timeout, so the total is that plus a few short statements. Not asyncio.wait_for:
# cancelling the coroutine with a statement in flight makes SQLAlchemy invalidate the connection, and the
# commit's next statement would raise. A cut-off statement is one more logged failure that leaves
# decision_record_id NULL (the commit goes on).
BRAIN_RECORD_STATEMENT_TIMEOUT_MS = 10_000


def push_after_for(settings: Any, now: datetime | None = None) -> datetime:
    """When an owner-facing row may go to Telegram: ``now``, or the end of the quiet hours (spec 4.5.8)."""
    # Late: importing nous.heartbeat runs its package __init__, which imports modules that import this one.
    from nous.heartbeat.quiet_hours import quiet_hours_end

    return quiet_hours_end(settings, now or datetime.now(UTC))


def _close_reason(outcome: str) -> str:
    if outcome == OUTCOME_FALLBACK:
        return CLOSE_FALLBACK_REPORT
    if outcome == OUTCOME_FAILED:
        return CLOSE_FAILED_REPORT
    return CLOSE_RESOLVED


def clip_body(text: str, settings: Any, *, limit: int | None = None) -> str:
    """``text`` cut to the inbox's body limit (``result_inbox_body_max_chars``), or to ``limit`` when that is
    smaller (never below 40 characters), marked ``[truncated]`` when cut."""
    cap = int(getattr(settings, "result_inbox_body_max_chars", 4000))
    if limit is not None:
        cap = min(cap, limit)
    cap = max(cap, 40)  # a tiny limit still leaves room for the marker
    return text if len(text) <= cap else text[: cap - 20].rstrip() + "\n[truncated]"


async def _lock_claimed(session: AsyncSession, agent_id: str, root_id: UUID, ids: list[UUID]) -> None:
    """Lock the root row FIRST, then the claimed rows in id order, all FOR NO KEY UPDATE (conflict C4: the
    FK checks of a late child INSERT and of the arrival row take FOR KEY SHARE and must not wait on this).

    Root first, always: ``expire_roots`` (and 2e's ``cancel_root``) lock the root and then update the
    lineage's open rows, so a path that held a claimed row and then asked for the root would close a
    cycle with them (a deadlock Postgres breaks by aborting one side with an error that is not a lost
    fence). Deliberately NOT a fence: there is no state or token predicate here, so each fenced UPDATE
    below is independently necessary. What the locks buy is that a writer of an inbox row
    (``record_result`` locks the intention first) either finished before we look for late rows, or waits
    for our commit."""
    await session.execute(
        select(Intention.id)
        .where(Intention.agent_id == agent_id, Intention.id == root_id)
        .with_for_update(key_share=True)
    )
    await session.execute(
        select(Intention.id)
        .where(Intention.agent_id == agent_id, Intention.id.in_(ids))
        .order_by(Intention.id)
        .with_for_update(key_share=True)
    )


async def _fenced_move(
    session: AsyncSession, agent_id: str, ids: list[UUID], token: UUID, values: dict[str, Any]
) -> set[UUID]:
    """The fenced UPDATE of claimed intentions: the ids it moved. Every statement that changes a claimed
    intention goes through here: ``state = 'deciding' AND claim_token = :token`` is in the WHERE."""
    if not ids:
        return set()
    moved = await session.execute(
        update(Intention)
        .where(
            Intention.agent_id == agent_id,
            Intention.id.in_(ids),
            Intention.state == STATE_DECIDING,
            Intention.claim_token == token,
        )
        .values(**values)
        .returning(Intention.id)
        .execution_options(synchronize_session=False)
    )
    return set(moved.scalars().all())


async def _root_origin_channel(session: AsyncSession, agent_id: str, root_id: UUID, fallback: str | None) -> str | None:
    """Where the conversation came from: the root's origin channel (a continuation turn has none,
    so every descendant's is NULL), else ``fallback``."""
    channel = (
        await session.execute(
            select(Intention.origin_channel).where(Intention.agent_id == agent_id, Intention.id == root_id)
        )
    ).scalar_one_or_none()
    return channel or fallback


async def claim_owner_channel(session: AsyncSession, agent_id: str, claim: Claim, *, settings: Any) -> str | None:
    """Where an owner-facing row of this claim goes (contract 4.14 item 5): the root's origin channel, else the
    deepest claimed intention's, else the default chat; None when there is none. The commit and the runner ask
    this one function, so an ask the runner lets through is never one the commit refuses."""
    origin = await _root_origin_channel(session, agent_id, claim.root_id, claim.deepest.origin_channel)
    return owner_channel(settings, origin)


async def _verified_progress(
    session: AsyncSession,
    agent_id: str,
    ids: list[UUID],
    *,
    since: datetime | None,
    resolution: Resolution,
    outcome: str,
    gate_reason: str | None,
    wrote_memory: bool,
) -> bool | None:
    """Spec 4.5.4: the model's claim, kept only if the arrival spawned work (a child of a claimed
    intention created at or after ``since``, the claim's ``claimed_at``), changed the plan (``revise``)
    or wrote memory. NULL where no model decided (a gate arrival, ``failed_report``); a fallback made no
    decision about progress: false.

    Both stamps are the database's ``now()``, and the turn's spawns begin after the claim's transaction
    committed, so the bound is exact. A child an earlier arrival spawned under an intention claimed again
    (a T11 send-back, an answered question) does not count. Nor does one an earlier attempt spawned before
    its lease was released: that spawn belongs to no arrival. Conservative: the stall budget trips sooner,
    and the turn budget still caps the lineage."""
    if gate_reason is not None or outcome == OUTCOME_FAILED:
        return None
    if outcome == OUTCOME_FALLBACK or not resolution.progress_claimed:
        return False
    if resolution.decision == "revise" or wrote_memory:
        return True
    spawned = (
        await session.execute(
            select(
                exists().where(
                    Intention.agent_id == agent_id, Intention.parent_id.in_(ids), Intention.created_at >= since
                )
            )
        )
    ).scalar_one()
    return bool(spawned)


async def has_open_work(session: AsyncSession, agent_id: str, claim: Claim) -> bool:
    """Whether a ``continue`` or ``revise`` of this claim would leave anything running under its root (final review
    I1): an open intention of the root other than the claimed ones (a fan-out's sibling still running, or this
    turn's spawn, which a lineage makes ``continue`` and so keeps open until the next claim). The commit closes the
    claimed intentions, so without one nothing would ever wake the root again. A child that already closed does
    not count, whenever it was spawned: an inline spawn (``await_result``) closes within the turn and leaves
    nothing running (re-review N1). Rows only."""
    ids = [i.id for i in claim.intentions]
    open_elsewhere = exists().where(
        Intention.agent_id == agent_id,
        Intention.root_id == claim.root_id,
        Intention.id.notin_(ids),
        Intention.state.in_(OPEN_STATES),
    )
    return bool((await session.execute(select(open_elsewhere))).scalar_one())


async def _record_brain(
    session: AsyncSession,
    brain: Any,
    *,
    claim: Claim,
    resolution: Resolution,
    ids: list[UUID],
    n: int,
) -> UUID | None:
    """One Brain decision per arrival (G5), category process, stakes low (spec 4.5.6). Runs in its own
    SAVEPOINT: whatever the Brain does (it rejects a description that reads as noise, its store can be
    down), the arrival commits. The description is fixed-shape so it is never noise; the note, which
    may be a single word, is the reason."""
    if brain is None:
        return None
    root_id = claim.root_id
    note = (resolution.note or "").strip()
    decision_id: UUID | None = None
    try:
        async with session.begin_nested():
            # SET takes no bind parameters: the int constant is interpolated. ROLLBACK TO SAVEPOINT undoes it;
            # a cut-off statement raises QueryCanceled (an ordinary Exception) and the savepoint rolls back.
            await session.execute(text(f"SET LOCAL statement_timeout = {int(BRAIN_RECORD_STATEMENT_TIMEOUT_MS)}"))
            detail = await brain.record(
                RecordInput(
                    description=f"F099 arrival {n} on '{claim.deepest.intent[:120]}': {resolution.decision}",
                    confidence=min(1.0, max(0.0, float(resolution.confidence))),
                    category="process",
                    stakes="low",
                    context=json.dumps(
                        {"root_id": str(root_id), "intention_ids": [str(i) for i in ids], "arrival_n": n}
                    ),
                    tags=["f099", resolution.decision],
                    reasons=[ReasonInput(type="analysis", text=note[:2000])] if note else [],
                    session_id=f"{INTENT_SESSION_PREFIX}{root_id}",
                ),
                session=session,
            )
        decision_id = detail.id
    except Exception:
        logger.warning("F099: could not record the Brain decision of arrival %s of root %s", n, root_id, exc_info=True)
    # RELEASE SAVEPOINT keeps a SET LOCAL for the rest of the transaction: give the commit back the bound it had.
    # DEFAULT is the value the connection was opened with.
    await session.execute(text("SET LOCAL statement_timeout = DEFAULT"))
    return decision_id


async def commit_arrival(
    session: AsyncSession,
    agent_id: str,
    claim: Claim,
    *,
    resolution: Resolution,
    outcome: str,
    gate_reason: str | None = None,
    tokens: tuple[int, int] = (0, 0),
    brain: Any = None,
    settings: Any,
    report_text: str | None = None,
    wrote_memory: bool = False,
    arrival_id: UUID | None = None,
    now: datetime | None = None,
) -> ArrivalCommit | None:
    """T9, T10, T11: the one fenced commit of an arrival (spec 4.5.6, contract 4.14), in the caller's
    transaction. None when the fence rejected it (the claim was released, or the root was cancelled
    or expired): nothing is written then, and the caller discards the turn's result.

    ``tokens`` is ``(tokens_in, tokens_out)``. ``report_text`` is the body of the REPORT of a gate
    escalation or a fallback (the note otherwise); a fallback always writes one, whatever its decision.
    An ``ask`` with no owner channel (no origin channel, no default chat) is refused with ``ValueError``
    and writes nothing, as a fallback that asks is.
    A ``failed_report`` (``fail_attempt``'s, at the cap) charges one attempt on every claimed intention and
    keeps the count; every other outcome resets it to 0.
    ``wrote_memory`` is the turn's evidence for ``progress``. ``arrival_id`` is the id the turn's context
    carried. Does not commit and emits nothing: the runner emits ``intention.arrival_decided`` after it
    commits.

    Two clocks: ``now`` (Python's, unless given) stamps ``result_at``, ``closed_at``, ``delivered_at``,
    ``decided_at`` and ``push_after``; ``claimed_at`` and ``created_at`` are the database's ``now()``. One
    host in prod, and nothing here compares a stamp of one clock with one of the other (the progress
    bound compares two database stamps).
    """
    if resolution.decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}, not {resolution.decision!r}")
    if outcome not in OUTCOMES:  # _close_reason would close a typo as 'resolved'
        raise ValueError(f"outcome must be one of {OUTCOMES}, not {outcome!r}")
    if outcome != OUTCOME_RESOLVED and resolution.decision == "ask":
        raise ValueError(f"a fallback ({outcome}) cannot ask: it writes a REPORT, and no question would be answered")
    if gate_reason is not None and gate_reason not in GATE_REASONS:
        raise ValueError(f"unknown gate reason {gate_reason!r}")
    try:
        async with session.begin_nested():
            return await _commit_arrival(
                session,
                agent_id,
                claim,
                resolution=resolution,
                outcome=outcome,
                gate_reason=gate_reason,
                tokens=tokens,
                brain=brain,
                settings=settings,
                report_text=report_text,
                wrote_memory=wrote_memory,
                arrival_id=arrival_id or uuid.uuid4(),
                now=now or datetime.now(UTC),
            )
    except _FenceLost:
        logger.warning(
            "F099: the claim %s of root %s is no longer live; its decision is discarded",
            claim.claim_token.hex[:8],
            claim.root_id,
        )
        return None


async def _commit_arrival(
    session: AsyncSession,
    agent_id: str,
    claim: Claim,
    *,
    resolution: Resolution,
    outcome: str,
    gate_reason: str | None,
    tokens: tuple[int, int],
    brain: Any,
    settings: Any,
    report_text: str | None,
    wrote_memory: bool,
    arrival_id: UUID,
    now: datetime,
) -> ArrivalCommit:
    ids = sorted(i.id for i in claim.intentions)
    root_id, deepest = claim.root_id, claim.deepest
    await _lock_claimed(session, agent_id, root_id, ids)

    # The owner-facing row and its channel, settled before anything moves. An ask with nowhere to ask is refused
    # (raised inside the SAVEPOINT, so nothing is written): committed, it would wait in awaiting_owner on a
    # QUESTION that was never written, which reads as answered, and the next sweep would wake it to no rows.
    if outcome in (OUTCOME_FALLBACK, OUTCOME_FAILED):
        kind: str | None = MSG_REPORT  # contract 4.14 item 5: a fallback reports unconditionally
    else:
        kind = {"ask": MSG_QUESTION, "report": MSG_REPORT}.get(resolution.decision)
    channel: str | None = None
    if kind is not None:
        channel = await claim_owner_channel(session, agent_id, claim, settings=settings)
    if kind == MSG_QUESTION and channel is None:
        raise ValueError(
            f"root {root_id} has no owner channel (no origin channel, no default chat): an ask has nowhere to ask"
        )

    # Rows that arrived after the claim read them (held, because the intention was deciding): they are
    # not consumed by this arrival, and their intention goes back to result_ready (T11), except after ask.
    shown = [row.id for row in claim.inbox_rows]
    late_query = select(ResultInbox.intention_id).where(
        intention_keyed(agent_id, ids), ResultInbox.delivered_at.is_(None)
    )
    if shown:
        late_query = late_query.where(ResultInbox.id.notin_(shown))
    late = set((await session.execute(late_query.distinct())).scalars().all())

    next_states: dict[UUID, str] = {}
    groups: dict[tuple[str, str | None], list[UUID]] = {}
    for intention_id in ids:
        if gate_reason == "cancelled":
            target: tuple[str, str | None] = (STATE_CANCELLED, CLOSE_CANCELLED)
        elif gate_reason == "expired":
            target = (STATE_EXPIRED, CLOSE_EXPIRED)
        elif resolution.decision == "ask":
            target = (STATE_AWAITING_OWNER, None)
        elif intention_id in late:
            target = (STATE_RESULT_READY, None)
        else:
            target = (STATE_CLOSED, _close_reason(outcome))
        next_states[intention_id] = target[0]
        groups.setdefault(target, []).append(intention_id)
    for (state, reason), group in groups.items():
        values: dict[str, Any] = {"state": state, "claim_token": None, "claimed_at": None, "updated_at": now}
        if outcome != OUTCOME_FAILED:
            values["attempts"] = 0
        else:
            # A failed_report is a failed attempt: it is charged here, in the fenced move, so the claim token in
            # that fence is all that stands between a stale claim and the count. The count is then kept: the close
            # keeps the count that ended it, and a late row's send-back keeps it as a retry below the cap does
            # (2c1-5 ruling), so a chronic failer is not handed a fresh three.
            values["attempts"] = Intention.attempts + 1
        if state in (STATE_CLOSED, STATE_CANCELLED, STATE_EXPIRED):
            values.update(close_reason=reason, closed_at=now)
        if state == STATE_RESULT_READY:
            values["result_at"] = now
        if await _fenced_move(session, agent_id, group, claim.claim_token, values) != set(group):
            raise _FenceLost

    # Delivery is stamped here and only here (spec 4.3 item 1): after the fenced moves above, in the
    # same SAVEPOINT, on exactly the rows the turn was shown. A lost fence rolls this back with them.
    if shown:
        await session.execute(
            update(ResultInbox)
            .where(ResultInbox.agent_id == agent_id, ResultInbox.id.in_(shown), ResultInbox.delivered_at.is_(None))
            .values(delivered_at=now, delivered_session_id=f"{INTENT_SESSION_PREFIX}{root_id}")
            .execution_options(synchronize_session=False)
        )

    n = (
        1
        + (
            await session.execute(
                select(func.coalesce(func.max(IntentionArrival.n), 0)).where(
                    IntentionArrival.agent_id == agent_id, IntentionArrival.root_id == root_id
                )
            )
        ).scalar_one()
    )
    progress = await _verified_progress(
        session,
        agent_id,
        ids,
        # claim_root reloaded the claimed rows after its UPDATE, so this is the claim's own now() (one value
        # for every claimed row). Not re-read: the fenced moves above have already cleared the column.
        since=claim.intentions[0].claimed_at,
        resolution=resolution,
        outcome=outcome,
        gate_reason=gate_reason,
        wrote_memory=wrote_memory,
    )
    decision_record_id = await _record_brain(session, brain, claim=claim, resolution=resolution, ids=ids, n=n)

    report_ids: list[UUID] = []
    if kind is not None:
        if channel is None:  # a REPORT (an ask with no channel was refused above)
            logger.error(
                "F099: arrival %s of root %s has a %s for the owner and no owner channel (no origin channel, no "
                "default chat); it cannot be written",
                n,
                root_id,
                kind,
            )
        else:
            body = resolution.note if kind == MSG_QUESTION else (report_text or resolution.note)
            report_ids.append(
                await insert_report(
                    session,
                    agent_id,
                    kind=kind,
                    title=f"{'Question' if kind == MSG_QUESTION else 'Update'}: {deepest.intent}",
                    body=clip_body(body, settings),
                    channel=channel,
                    intention_id=deepest.id,
                    root_id=root_id,
                    arrival_id=arrival_id,
                    push_after=push_after_for(settings, now),
                )
            )

    session.add(
        IntentionArrival(
            id=arrival_id,
            agent_id=agent_id,
            root_id=root_id,
            n=n,
            intention_ids=ids,
            inbox_ids=shown,
            report_ids=report_ids,
            claim_token=claim.claim_token,
            decision=resolution.decision,
            note=resolution.note,
            progress_claimed=resolution.progress_claimed,
            progress=progress,
            confidence=resolution.confidence,
            gate_reason=gate_reason,
            tokens_in=tokens[0],
            tokens_out=tokens[1],
            decision_record_id=decision_record_id,
            outcome=outcome,
            decided_at=now,
        )
    )
    await session.flush()
    return ArrivalCommit(arrival_id, n, next_states, decision_record_id, tuple(report_ids))


FAIL_RETRY, FAIL_LOST = "retry", "lost"


async def fail_attempt(
    session: AsyncSession,
    agent_id: str,
    claim: Claim,
    *,
    max_attempts: int,
    settings: Any,
    brain: Any = None,
    now: datetime | None = None,
    arrival_id: UUID | None = None,
) -> str:
    """T8 and T12: one claimed attempt failed (the turn raised or timed out, or its lease expired).

    ``attempts`` goes up on every claimed intention (spec 4.5.7). Below ``max_attempts`` the claim is
    released to ``result_ready`` with ``result_at = now``, so the debounce spaces the retry and a
    failing turn cannot spin: returns ``"retry"``. At the cap the raw results become a REPORT and the
    intentions close ``failed_report``: returns ``"failed_report"``. Both fenced on the claim token; a
    claim that is no longer live returns ``"lost"`` and writes nothing. A row that arrived while the claim ran
    sends its intention back to ``result_ready`` with ``attempts`` kept, as a retry below the cap keeps it: the
    late row gets one more attempt, so a lineage whose turns keep failing reports its next result raw after one
    more failure (a successful commit resets the count). Does not commit.

    Each path charges the attempt in its one fenced UPDATE (the retry's move, or the arrival's moves at the
    cap), so the claim token in that UPDATE is the fence and nothing else stands in for it. ``arrival_id`` is the
    id of the cap's arrival row (a new one when not given), as ``commit_arrival`` takes it: the caller can name
    that row in ``intention.arrival_decided``.
    """
    now = now or datetime.now(UTC)
    ids = sorted(i.id for i in claim.intentions)
    try:
        async with session.begin_nested():
            # Kept for the one lock order (root first); not load-bearing: nothing is written before the fenced UPDATE.
            await _lock_claimed(session, agent_id, claim.root_id, ids)
            # A plain read, deliberately not fenced: a fenced read would raise on a stale claim before the
            # fenced UPDATE below could, and so hide that UPDATE's predicate. It only picks the path.
            counts = (
                await session.execute(
                    select(Intention.attempts).where(Intention.agent_id == agent_id, Intention.id.in_(ids))
                )
            ).scalars()
            worst = max(counts) + 1
            if worst < max_attempts:
                released = await _fenced_move(
                    session,
                    agent_id,
                    ids,
                    claim.claim_token,
                    {
                        "state": STATE_RESULT_READY,
                        "claim_token": None,
                        "claimed_at": None,
                        "result_at": now,
                        "updated_at": now,
                        "attempts": Intention.attempts + 1,
                    },
                )
                if released != set(ids):
                    raise _FenceLost
                return FAIL_RETRY
            raw = raw_results_text(claim.inbox_rows)
            body = f"{worst} attempts to act on this work have failed, so this result is passed on as it arrived."
            await _commit_arrival(
                session,
                agent_id,
                claim,
                resolution=Resolution("report", f"Failed after {worst} attempts.", False, 0.0),
                outcome=OUTCOME_FAILED,
                gate_reason=None,
                tokens=(0, 0),
                brain=brain,
                settings=settings,
                report_text=f"{body}\n\n{raw}" if raw else body,
                wrote_memory=False,
                arrival_id=arrival_id or uuid.uuid4(),
                now=now,
            )
            return CLOSE_FAILED_REPORT
    except _FenceLost:
        logger.warning(
            "F099: the failed claim %s of root %s is no longer live", claim.claim_token.hex[:8], claim.root_id
        )
        return FAIL_LOST


async def release_claim(session: AsyncSession, agent_id: str, claim: Claim) -> int:
    """Free a claim deliberately (a shutdown, a cancel) without charging an attempt: the rows go back to
    ``result_ready`` as they were. Fenced: a claim that is no longer live releases nothing. Returns the
    number of intentions released. Does not commit."""
    ids = sorted(i.id for i in claim.intentions)
    now = datetime.now(UTC)
    async with session.begin_nested():
        await _lock_claimed(session, agent_id, claim.root_id, ids)
        moved = await _fenced_move(
            session,
            agent_id,
            ids,
            claim.claim_token,
            {"state": STATE_RESULT_READY, "claim_token": None, "claimed_at": None, "updated_at": now},
        )
    return len(moved)


async def release_stale_claims(
    session: AsyncSession,
    agent_id: str,
    *,
    lease_s: float,
    max_attempts: int,
    settings: Any,
    brain: Any = None,
    now: datetime | None = None,
) -> list[UUID]:
    """T8: at startup and on every sweep, every ``deciding`` row claimed more than ``lease_s`` ago
    counts as a failed attempt (spec 4.5.2 Lease, 4.5.7). Rows are grouped by claim, so a batch fails
    together, and each group goes through ``fail_attempt``: ``attempts + 1`` and back to ``result_ready``,
    or ``failed_report`` at the cap. Returns the ids it took from their stale claim: released to
    ``result_ready``, or closed ``failed_report`` at the cap (or sent back by a row that landed meanwhile);
    a claim that committed while the sweep waited for its rows is not among them. A turn should not
    outlive its lease (the runner's timeout is 60 s under it, less the claim's lock wait: see the
    predicate); the fenced commit is what makes a late one harmless anyway. Does not commit."""
    now = now or datetime.now(UTC)
    root = aliased(Intention)
    stale = (
        (
            await session.execute(
                select(Intention)
                .join(root, and_(root.agent_id == agent_id, root.id == Intention.root_id))
                .where(
                    Intention.agent_id == agent_id,
                    Intention.state == STATE_DECIDING,
                    Intention.claim_token.is_not(None),
                    # claimed_at is the database's now() at the start of the claim's transaction, before
                    # claim_root waited for the root lock, so the lease runs from at or before the claim: it can
                    # end early by that wait, never late (a crashed claim is never held past its lease). The 60 s
                    # between the turn timeout and the lease absorb a short wait; past that, the fence turns the
                    # turn's commit into a lost one and the attempt is charged. ``now`` is taken before this sweep
                    # waits on any lock, so the sweep errs the other way: it releases only what was stale when it
                    # began. It compares the database's clock (claimed_at) with Python's (now), as the debounce
                    # in _CLAIM_SQL and _ELIGIBLE_SQL does (result_at is Python's, now() the database's): one host
                    # in prod.
                    Intention.claimed_at < now - timedelta(seconds=lease_s),
                )
                # The one cross-root lock order, (root.created_at, root.id), in every sweep that locks several
                # roots in one transaction (this one, expire_roots, wake_terminal_arrivals): a released SAVEPOINT
                # keeps its locks, so two sweeps that took two roots in opposite orders could deadlock.
                .order_by(root.created_at, root.id, Intention.claim_token, Intention.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    groups: dict[tuple[UUID, UUID], list[Intention]] = {}
    for member in stale:
        groups.setdefault((member.root_id, member.claim_token), []).append(member)
    released: list[UUID] = []
    for (root_id, token), members in groups.items():
        members.sort(key=lambda m: (-m.depth, m.created_at, m.id))
        ids = [m.id for m in members]
        rows = (
            (
                await session.execute(
                    select(ResultInbox)
                    .where(intention_keyed(agent_id, ids), ResultInbox.delivered_at.is_(None))
                    .order_by(ResultInbox.created_at, ResultInbox.id)
                )
            )
            .scalars()
            .all()
        )
        outcome = await fail_attempt(
            session,
            agent_id,
            Claim(root_id, token, tuple(members), members[0], tuple(rows)),
            max_attempts=max_attempts,
            settings=settings,
            brain=brain,
            now=now,
        )
        if outcome != FAIL_LOST:
            released.extend(ids)
    return released


EXPIRE_BATCH = 10  # roots expired per sweep: a backlog is worked off over several, never in one burst
STRANDED_BATCH = 200  # rows held on closed intentions settled per sweep
# The `source_id` namespace of the row an unanswered question's expiry writes (idempotent per arrival and intention).
_EXPIRY_NAMESPACE = uuid.UUID("0b3c6a52-8f0e-4d0b-9d3b-6a2f4f6a7c11")


@dataclass(frozen=True, slots=True)
class SweepReport:
    """What one runner sweep did (contract section 4.7)."""

    released: int
    expired_roots: int
    expired_proposals: int
    pushed: int
    launched: tuple[UUID, ...]
    next_due: datetime | None


def _ttl_applies(agent_id: str, root_id: Any) -> ColumnElement[bool]:
    """EXISTS: the lineage of ``root_id`` (a value, or a column of the sweep's query) has an open ``continue`` or
    ``report`` intention, what the TTL is about (spec 4.6). One definition: the sweep's ``due`` reads it, and
    ``_expire_root`` reads it again under the root's lock."""
    return exists().where(
        Intention.agent_id == agent_id,
        Intention.root_id == root_id,
        Intention.state.in_(OPEN_STATES),
        Intention.wake_policy.in_((intentions.WAKE_CONTINUE, intentions.WAKE_REPORT)),
    )


async def expire_roots(
    session: AsyncSession,
    agent_id: str,
    *,
    ttl_hours: float,
    settings: Any,
    limit: int = EXPIRE_BATCH,
    now: datetime | None = None,
) -> list[UUID]:
    """T14: expire the roots whose TTL ran out (spec 4.6), at most ``limit``, one SAVEPOINT per root.

    Due: open (neither root marker set), not a container, with an open ``continue`` or ``report``
    intention in its lineage, and ``deadline`` past (a NULL deadline: ``created_at + ttl_hours`` past).
    Then settles the rows held on intentions a gate arrival closed (``_settle_stranded_rows``).
    Does not commit; the runner emits ``intention.root_expired`` for the returned ids.
    """
    now = now or datetime.now(UTC)
    root = aliased(Intention)
    due = (
        select(root.id)
        .where(
            root.agent_id == agent_id,
            root.id == root.root_id,
            root.root_cancelled_at.is_(None),
            root.root_expired_at.is_(None),
            root.wake_policy != intentions.WAKE_CONTAINER,
            _ttl_applies(agent_id, root.id),
            or_(
                root.deadline <= now,
                and_(root.deadline.is_(None), root.created_at <= now - timedelta(hours=ttl_hours)),
            ),
        )
        .order_by(root.created_at, root.id)  # the one cross-root lock order (see release_stale_claims)
        .limit(limit)
    )
    expired: list[UUID] = []
    for root_id in (await session.execute(due)).scalars().all():
        try:
            async with session.begin_nested():
                if await _expire_root(session, agent_id, root_id, ttl_hours=ttl_hours, settings=settings, now=now):
                    expired.append(root_id)
        except Exception:
            logger.warning("F099: could not expire root %s; retried at the next sweep", root_id, exc_info=True)
    try:
        async with session.begin_nested():
            await _settle_stranded_rows(session, agent_id, settings=settings, now=now)
    except Exception:
        logger.warning(
            "F099: could not settle the rows held on closed intentions; retried at the next sweep", exc_info=True
        )
    return expired


async def _settle_stranded_rows(session: AsyncSession, agent_id: str, *, settings: Any, now: datetime) -> int:
    """Rows held on an intention a gate arrival closed (``cancelled`` or ``expired``): they landed after the
    claim read its rows and before the root's marker, so the arrival did not consume them, and nothing claims
    a closed intention. Each root's are stamped delivered and reported raw in one REPORT, as ``record_result``
    reports a result that lands after the close. The expiry never strands one (it reads after it closes).
    Writes no intention row, so it takes no intention lock. Returns the number of rows settled."""
    stranded = (
        await session.execute(
            select(ResultInbox.id, Intention.root_id)
            .join(Intention, and_(Intention.agent_id == agent_id, Intention.id == ResultInbox.intention_id))
            .where(
                ResultInbox.agent_id == agent_id,
                ResultInbox.channel.is_(None),
                ResultInbox.session_id.is_(None),
                ResultInbox.delivered_at.is_(None),
                Intention.state.in_((STATE_CANCELLED, STATE_EXPIRED)),
            )
            .order_by(ResultInbox.created_at, ResultInbox.id)
            .limit(STRANDED_BATCH)
        )
    ).all()
    by_root: dict[UUID, list[UUID]] = {}
    for row_id, root_id in stranded:
        by_root.setdefault(root_id, []).append(row_id)
    settled = 0
    for root_id, row_ids in by_root.items():
        # Stamp and read in one statement: only the rows this call moved are reported, so a row is reported once.
        rows = sorted(
            await session.execute(
                update(ResultInbox)
                .where(ResultInbox.id.in_(row_ids), ResultInbox.delivered_at.is_(None))
                .values(delivered_at=now, delivered_session_id=f"{INTENT_SESSION_PREFIX}{root_id}")
                .returning(ResultInbox.id, ResultInbox.title, ResultInbox.body, ResultInbox.created_at)
                .execution_options(synchronize_session=False)
            ),
            key=lambda r: (r.created_at, r.id),
        )
        if not rows:
            continue
        root = (
            await session.execute(
                select(Intention.intent, Intention.origin_channel).where(
                    Intention.agent_id == agent_id, Intention.id == root_id
                )
            )
        ).one()
        channel = owner_channel(settings, root.origin_channel)
        if channel is None:
            logger.warning(
                "F099: %d result(s) held on the closed lineage of root %s have no owner channel; they stay on their "
                "work rows",
                len(rows),
                root_id,
            )
        else:
            head = f"These results arrived as this work was closed, so nothing acted on them: {root.intent}"
            await insert_report(
                session,
                agent_id,
                kind=MSG_REPORT,
                title=f"Late results: {root.intent}",
                body=clip_body(f"{head}\n\n{raw_results_text(rows)}", settings),
                channel=channel,
                intention_id=root_id,
                root_id=root_id,
                push_after=push_after_for(settings, now),
            )
        settled += len(rows)
    if settled:
        # A backstop: today only a gate arrival's late rows land here, so a count is how a path that writes a root
        # marker without closing its lineage (2e's cancel_root, say) shows up in the log.
        logger.warning(
            "F099: settled %d result(s) held on closed intentions (left by a gate arrival or a cancel)", settled
        )
    return settled


async def _expire_root(
    session: AsyncSession, agent_id: str, root_id: UUID, *, ttl_hours: float, settings: Any, now: datetime
) -> bool:
    row = (
        await session.execute(
            select(Intention)
            .where(Intention.agent_id == agent_id, Intention.id == root_id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None or row.root_cancelled_at is not None or row.root_expired_at is not None:
        return False  # a cancel or another sweep got there first
    # `due` was read before the sweep waited for this root, and a turn that resolved the root meanwhile (it held
    # the root first) may have left nothing the TTL is about: a pending `remember` child outlives its parent's
    # turn by design and never made the root due. So `due`'s own predicate again, under the lock; every resolver
    # takes the root first, so the answer holds until this transaction ends.
    if not (await session.execute(select(_ttl_applies(agent_id, root_id)))).scalar_one():
        return False
    # Close the open intentions FIRST: this UPDATE takes their row locks, so a record_result that is mid-flight
    # (it holds its intention FOR UPDATE and reads the root unlocked) either committed before it, and its row is
    # visible below, or waits for us and then finds its intention expired and writes a raw REPORT. Reading the
    # unread rows before this statement would orphan a row committed in the gap. (2e's cancel_root: same order.)
    # Accepted residual: a T6 reopen (record_result on a `closed` continue intention) holds only that row and
    # reads the root unlocked; this UPDATE skips the `closed` row it sees without waiting, so a reopen committing
    # alongside ends `result_ready` under an expired root, and the next claim's gate drops it (`expired`).
    closed_ids = (
        (
            await session.execute(
                update(Intention)
                .where(
                    Intention.agent_id == agent_id,
                    Intention.root_id == root_id,
                    Intention.state.in_(OPEN_STATES),
                )
                .values(
                    state=STATE_EXPIRED,
                    close_reason=CLOSE_EXPIRED,
                    closed_at=now,
                    claim_token=None,
                    claimed_at=None,
                    updated_at=now,
                )
                .returning(Intention.id)
                .execution_options(synchronize_session=False)
            )
        )
        .scalars()
        .all()
    )
    if not closed_ids:
        return False
    await session.execute(
        update(Intention)
        .where(Intention.agent_id == agent_id, Intention.id == root_id)
        .values(root_expired_at=now, updated_at=now)
    )
    unread = (
        (
            await session.execute(
                select(ResultInbox)
                .where(intention_keyed(agent_id, closed_ids), ResultInbox.delivered_at.is_(None))
                .order_by(ResultInbox.created_at, ResultInbox.id)
            )
        )
        .scalars()
        .all()
    )
    if unread:
        await session.execute(
            update(ResultInbox)
            .where(ResultInbox.id.in_([r.id for r in unread]), ResultInbox.delivered_at.is_(None))
            .values(delivered_at=now, delivered_session_id=f"{INTENT_SESSION_PREFIX}{root_id}")
            .execution_options(synchronize_session=False)
        )
    # Report iff there is something to show (rows the owner never saw) or the root has a deadline (it was
    # written by continuation). A root with no deadline (Phase 1) and nothing unread closes without a report,
    # so the first sweep after the flag flips cannot flood the owner. A later result reaches the owner raw.
    if unread or row.deadline is not None:
        channel = owner_channel(settings, row.origin_channel)
        if channel is None:
            logger.warning("F099: root %s expired with no owner channel; nothing was reported", root_id)
        else:
            head = f"I closed this because it did not finish within {ttl_hours:g} hours: {row.intent}"
            raw = raw_results_text(unread)
            await insert_report(
                session,
                agent_id,
                kind=MSG_REPORT,
                title=f"Closed after {ttl_hours:g} hours: {row.intent}",
                body=clip_body(f"{head}\n\nWhat I had so far:\n{raw}" if raw else head, settings),
                channel=channel,
                intention_id=root_id,
                root_id=root_id,
                push_after=push_after_for(settings, now),
            )
    return True


async def _question_state(
    session: AsyncSession, agent_id: str, arrival_id: UUID, *, settings: Any, now: datetime
) -> tuple[bool, bool, list[ResultInbox]]:
    """``(terminal, answered, questions)`` for an arrival (conflict C9)."""
    base = (
        ResultInbox.agent_id == agent_id,
        ResultInbox.source_kind == SOURCE_INTENTION_REPORT,
        ResultInbox.arrival_id == arrival_id,
    )
    questions = list(
        (await session.execute(select(ResultInbox).where(*base, ResultInbox.msg_type == MSG_QUESTION))).scalars().all()
    )
    if not questions:
        return True, True, questions
    newest_answer = (
        await session.execute(select(func.max(ResultInbox.created_at)).where(*base, ResultInbox.msg_type == "INFORM"))
    ).scalar_one()
    ttl = timedelta(hours=float(settings.intention_proposal_ttl_hours)) if settings is not None else None
    answered = [newest_answer is not None and newest_answer >= q.created_at for q in questions]
    expired = [ttl is not None and q.created_at <= now - ttl for q in questions]
    terminal = all(a or e for a, e in zip(answered, expired, strict=True))
    return terminal, all(answered), questions


async def arrival_is_terminal(
    session: AsyncSession, agent_id: str, arrival_id: UUID, *, settings: Any = None, now: datetime | None = None
) -> bool:
    """True when every QUESTION of the arrival is answered or past its deadline (spec 4.4 item 6; 2d adds
    its proposals). Without ``settings`` a question never expires."""
    terminal, _answered, _questions = await _question_state(
        session, agent_id, arrival_id, settings=settings, now=now or datetime.now(UTC)
    )
    return terminal


async def wake_arrival(
    session: AsyncSession, agent_id: str, arrival_id: UUID, *, now: datetime | None = None
) -> list[UUID]:
    """T5: every intention of the arrival still ``awaiting_owner`` goes back to ``result_ready``, so the
    rows held meanwhile (answers, late results) are one batch at the next claim. Returns their ids.

    Takes the root first (the one lock order): a caller that writes to the arrival's intentions before
    waking it must lock the root before those writes."""
    now = now or datetime.now(UTC)
    arrival = (
        await session.execute(
            select(IntentionArrival).where(IntentionArrival.agent_id == agent_id, IntentionArrival.id == arrival_id)
        )
    ).scalar_one_or_none()
    if arrival is None:
        return []
    # The expiry holds the root while it closes the lineage: an UPDATE that locked an intention of the arrival
    # and then waited for the root would close a cycle with it.
    await session.execute(
        select(Intention.id)
        .where(Intention.agent_id == agent_id, Intention.id == arrival.root_id)
        .with_for_update(key_share=True)
    )
    moved = await session.execute(
        update(Intention)
        .where(
            Intention.agent_id == agent_id,
            Intention.id.in_(list(arrival.intention_ids)),
            Intention.state == STATE_AWAITING_OWNER,
            Intention.wake_policy == intentions.WAKE_CONTINUE,
        )
        .values(state=STATE_RESULT_READY, result_at=now, updated_at=now)
        .returning(Intention.id)
        .execution_options(synchronize_session=False)
    )
    return list(moved.scalars().all())


async def wake_terminal_arrivals(
    session: AsyncSession, agent_id: str, *, settings: Any, now: datetime | None = None, limit: int = 50
) -> list[UUID]:
    """The sweep's backstop for T5: wake every ``ask`` arrival that is terminal and still has an
    intention ``awaiting_owner``. An arrival that expired unanswered gets one ``INFORM`` row per
    intention it woke saying so (``source_id`` derived from the arrival and the intention: idempotent),
    so the woken claim has something to show. Returns the woken intention ids. Does not commit."""
    now = now or datetime.now(UTC)
    root = aliased(Intention)
    waiting = exists().where(
        Intention.id == any_(IntentionArrival.intention_ids), Intention.state == STATE_AWAITING_OWNER
    )
    arrivals = (
        (
            await session.execute(
                select(IntentionArrival)
                .join(root, and_(root.agent_id == agent_id, root.id == IntentionArrival.root_id))
                .where(IntentionArrival.agent_id == agent_id, IntentionArrival.decision == "ask", waiting)
                # The one cross-root lock order (see release_stale_claims); a root's arrivals in decision order.
                .order_by(root.created_at, root.id, IntentionArrival.decided_at, IntentionArrival.id)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    woken: list[UUID] = []
    for arrival in arrivals:
        # Read before the SAVEPOINT: its rollback expires a row it changed, and an async session cannot reload one.
        arrival_id = arrival.id
        try:
            async with session.begin_nested():
                terminal, answered, questions = await _question_state(
                    session, agent_id, arrival_id, settings=settings, now=now
                )
                if not terminal:
                    continue
                # Wake first: wake_arrival takes the root before any intention, and the rows below go only to the
                # intentions it moved. One the expiry closed meanwhile gets none: on a closed intention
                # record_result would turn the row into a report telling the owner that the owner did not answer.
                moved = await wake_arrival(session, agent_id, arrival_id, now=now)
                if not answered:
                    for intention_id in moved:
                        await record_result(
                            session,
                            agent_id,
                            intention_id=intention_id,
                            source_kind=SOURCE_INTENTION_REPORT,
                            source_id=uuid.uuid5(_EXPIRY_NAMESPACE, f"{arrival_id}:{intention_id}"),
                            msg_type="INFORM",
                            title="The owner did not answer",
                            body=(
                                "I asked the owner a question and the owner did not answer within "
                                f"{float(settings.intention_proposal_ttl_hours):g} hours: {questions[0].body[:500]}"
                            ),
                            arrival_id=arrival_id,
                            settings=settings,
                        )
                woken.extend(moved)
        except Exception:
            logger.warning(
                "F099: could not wake arrival %s; it is retried at the next sweep", arrival_id, exc_info=True
            )
    return woken


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
    the row lock, so a miss is a bug, not a race: it raises and the caller's transaction rolls back.
    A reopen also clears the previous arrival's claim, so 2c's lease starts fresh. ``attempts`` stays as
    the last arrival left it (2c1-5 ruling): 0 after a success, the kept count after a ``failed_report``,
    so a chronic failer's new result gets one more attempt whether it lands during the claim or after."""
    values: dict[str, Any] = {"state": STATE_RESULT_READY, "result_at": now, "updated_at": now}
    if from_state == STATE_CLOSED:
        values.update(close_reason=None, closed_at=None, claim_token=None, claimed_at=None)
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
        # every tick for good. NULL-keyed and delivered, no chat turn can claim it, so it keeps the work's
        # created_at; the REPORT below, which chat claims, is stamped now.
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
            push_after=push_after_for(settings, now),  # F099 2c: pushed to Telegram too (R2), at the end of quiet hours
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
RAW_PUSH_CHARS = 3900
# delivered_session_id of a row the rollback sent by Telegram instead of routing.
ROLLBACK_SESSION_ID = "rollback"


async def rollback_at_startup(
    database: Any, settings: Any, *, telegram_push: Callable[[str], Awaitable[bool]] | None
) -> RollbackReport:
    """Spec 4.3 item 6, T15: take the continuation out of the loop at startup (flag off).

    Open ``continue`` intentions in ``result_ready``, ``deciding`` or ``awaiting_owner`` have their
    undelivered intention-keyed inbox rows re-routed to ``owner_channel`` (so F098's chat turn shows
    them; a re-routed row is stamped ``created_at`` now, so the claim window starts when chat can see
    it), their ``staged`` and ``pending`` proposals expired (a later tap is refused), and are closed
    as ``legacy`` with the claim cleared; so is every ``pending`` intention whose source is already
    terminal (work that finished while the flags were off: task-1.9 carry-over 2). It runs whenever
    ``brain.intentions`` exists, ``NOUS_INTENTIONS_ENABLED`` off included, and does nothing when the
    continuation flag is on. If the inbox is off too, a re-routed row would be invisible: each row is
    sent by ``telegram_push`` instead and stamped delivered, and an intention whose push failed stays
    open for the next start. With the inbox off and no Telegram configured there is no channel to
    deliver to: the intention closes with a WARNING and the result stays on its work row (the subtask
    or DAG).

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
                    if await telegram_push(f"{row.title}\n\n{row.body}"[:RAW_PUSH_CHARS]):
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
                    # created_at is when chat can see the row, so F098's claim window starts now and
                    # not at the age of the work (a result that waited past it would never be shown).
                    moved = await session.execute(
                        update(ResultInbox)
                        .where(ResultInbox.id.in_(row_ids), ResultInbox.delivered_at.is_(None))
                        .values(channel=channel, reply_to=channel, created_at=now)
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
