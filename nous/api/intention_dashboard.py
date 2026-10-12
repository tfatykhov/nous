"""F099 Phase 3c: the intentions dashboard (contract 4.9): every root as a tree, and the metrics beside it.

Read-only and agent-scoped. Postgres-only: arrays (``ANY``), ``FILTER``, a raw read of ``nous_system.context_log``
(which has no ORM model). The harness precedent (``harness_dashboard.py``) keeps its queries beside the route in
its own module for the same reason. Model-authored text (``intent``, ``note``, a proposal's ``rationale``,
``arguments`` and ``result``, an owner row's ``title``) is returned RAW, as ``GET /intentions`` returns it: the
view renders it as text nodes only.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, any_, exists, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from nous.api.harness_dashboard import window_start
from nous.brain import continuation
from nous.heart.result_inbox import _percentile
from nous.storage.models import Decision, Intention, IntentionArrival, IntentionProposal, ResultInbox

LIMIT_MAX = 200


def check_query(window: str, state: str, limit: int, now: datetime | None = None) -> datetime:
    """Validate the query and return the start of the window. Raises ``ValueError`` (the route answers 400)."""
    since = window_start(window, now)  # ValueError: window must be one of 24h, 7d, 30d
    if state not in continuation.ROOT_STATES:
        raise ValueError(f"state must be one of {', '.join(continuation.ROOT_STATES)}")
    if not 1 <= limit <= LIMIT_MAX:
        raise ValueError(f"limit must be a whole number from 1 to {LIMIT_MAX}")
    return since


def disabled_payload() -> dict[str, Any]:
    """What a deployment with continuation off gets: no row is read to build it."""
    return {"continuation": False, "roots": [], "metrics": None}


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts is not None else None


async def _root_rows(session: AsyncSession, agent_id: str, *, state: str, limit: int) -> list[Intention]:
    query = select(Intention).where(Intention.agent_id == agent_id, Intention.id == Intention.root_id)
    if state == "open":
        # The predicate of continuation.list_roots, copied rather than hoisted (3a and 3b also edit continuation.py).
        # tests/test_f099_phase3c_tree.py pins the two equal.
        lineage_row = aliased(Intention)
        query = query.where(
            Intention.root_cancelled_at.is_(None),
            Intention.root_expired_at.is_(None),
            exists().where(
                lineage_row.agent_id == agent_id,
                lineage_row.root_id == Intention.id,
                lineage_row.state.in_(continuation.OPEN_STATES),
            ),
        )
    return list(
        (await session.execute(query.order_by(Intention.created_at.desc(), Intention.id).limit(limit))).scalars()
    )


def _child_view(row: Intention) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "parent_id": str(row.parent_id) if row.parent_id is not None else None,
        "depth": row.depth,
        "source_kind": row.source_kind,
        "source_id": row.source_id,
        "state": row.state,
        "close_reason": row.close_reason,
        "intent": row.intent,
        "created_at": _iso(row.created_at),
        "result_at": _iso(row.result_at),
        "closed_at": _iso(row.closed_at),
        "attempts": row.attempts,
        "failed_tokens": row.failed_tokens,
        "children": [],
    }


def _nest(root: Intention, rows: list[Intention]) -> list[dict[str, Any]]:
    """The lineage as a forest under the root. ``rows`` arrive ordered by (depth, created_at, id), so a parent is
    always built before its children; a row whose parent is missing hangs off the root rather than vanishing."""
    nodes = {row.id: _child_view(row) for row in rows if row.id != root.id}
    top: list[dict[str, Any]] = []
    for row in rows:
        if row.id == root.id:
            continue
        parent = nodes.get(row.parent_id) if row.parent_id is not None else None
        (parent["children"] if parent is not None else top).append(nodes[row.id])
    return top


# The result that woke an arrival: the oldest inbox row it claimed (contract 4.9 ``result_to_decision_s``).
_FIRST_RESULT_AT = (
    select(func.min(ResultInbox.created_at))
    .where(ResultInbox.agent_id == IntentionArrival.agent_id, ResultInbox.id == any_(IntentionArrival.inbox_ids))
    .correlate(IntentionArrival)
    .scalar_subquery()
)


def _arrival_view(
    a: IntentionArrival, d_outcome: str | None, d_reviewer: str | None, first: datetime | None
) -> dict[str, Any]:
    latency = (a.decided_at - first).total_seconds() if a.decided_at is not None and first is not None else None
    return {
        "id": str(a.id),
        "n": a.n,
        "intention_ids": [str(i) for i in a.intention_ids],
        "decision": a.decision,
        "note": a.note,
        "progress_claimed": a.progress_claimed,
        "progress": a.progress,
        "confidence": a.confidence,
        "gate_reason": a.gate_reason,
        "outcome": a.outcome,
        "tokens_in": a.tokens_in,
        "tokens_out": a.tokens_out,
        "decided_at": _iso(a.decided_at),
        "decision_record_id": str(a.decision_record_id) if a.decision_record_id is not None else None,
        "decision_outcome": d_outcome,
        "decision_reviewer": d_reviewer,
        "result_to_decision_s": latency,
    }


async def _tree(session: AsyncSession, agent_id: str, roots: list[Intention], *, settings: Any) -> list[dict[str, Any]]:
    ids = [r.id for r in roots]
    lineage: dict[UUID, list[Intention]] = {i: [] for i in ids}
    owner_of: dict[UUID, UUID] = {}  # intention id -> root id, to place an owner row without a join
    for row in (
        await session.execute(
            select(Intention)
            .where(Intention.agent_id == agent_id, Intention.root_id.in_(ids))
            .order_by(Intention.root_id, Intention.depth, Intention.created_at, Intention.id)
        )
    ).scalars():
        lineage[row.root_id].append(row)
        owner_of[row.id] = row.root_id
    arrivals: dict[UUID, list[dict[str, Any]]] = {i: [] for i in ids}
    for a, d_outcome, d_reviewer, first in (
        await session.execute(
            select(IntentionArrival, Decision.outcome, Decision.reviewer, _FIRST_RESULT_AT)
            .outerjoin(
                Decision,
                and_(
                    Decision.agent_id == IntentionArrival.agent_id, Decision.id == IntentionArrival.decision_record_id
                ),
            )
            .where(IntentionArrival.agent_id == agent_id, IntentionArrival.root_id.in_(ids))
            .order_by(IntentionArrival.root_id, IntentionArrival.n)
        )
    ).all():
        arrivals[a.root_id].append(_arrival_view(a, d_outcome, d_reviewer, first))
    proposals: dict[UUID, list[dict[str, Any]]] = {i: [] for i in ids}
    for p in (
        await session.execute(
            select(IntentionProposal)
            .where(
                IntentionProposal.agent_id == agent_id,
                IntentionProposal.root_id.in_(ids),
                IntentionProposal.state != "staged",
            )
            .order_by(IntentionProposal.created_at, IntentionProposal.id)
        )
    ).scalars():
        proposals[p.root_id].append(continuation.proposal_view(p))
    owner_rows: dict[UUID, list[dict[str, Any]]] = {i: [] for i in ids}
    for r in (
        await session.execute(
            select(ResultInbox)
            .where(
                ResultInbox.agent_id == agent_id,
                ResultInbox.source_kind == continuation.SOURCE_INTENTION_REPORT,
                ResultInbox.msg_type.in_(continuation.REPORT_KINDS),
                ResultInbox.channel.is_not(None),  # owner-facing rows; a keyed answer row has no channel
                ResultInbox.intention_id.in_(list(owner_of)),
            )
            .order_by(ResultInbox.created_at, ResultInbox.id)
        )
    ).scalars():
        owner_rows[owner_of[r.intention_id]].append(
            {
                "id": str(r.id),
                "msg_type": r.msg_type,
                "title": r.title,
                "created_at": _iso(r.created_at),
                "push_after": _iso(r.push_after),
                "pushed_at": _iso(r.pushed_at),
                "delivered_at": _iso(r.delivered_at),
            }
        )
    limits = await continuation.root_limits_many(
        session, agent_id, [r.id for r in roots if r.wake_policy != "container"], settings=settings
    )
    views = []
    for root in roots:
        views.append(
            {
                "id": str(root.id),
                "short_id": continuation.short_id(root.id),
                "intent": root.intent,
                "state": root.state,
                "wake_policy": root.wake_policy,
                "authority": root.authority,
                "origin_kind": root.origin_kind,
                "origin_channel": root.origin_channel,
                "created_at": _iso(root.created_at),
                "deadline": _iso(root.deadline),
                "root_cancelled_at": _iso(root.root_cancelled_at),
                "root_expired_at": _iso(root.root_expired_at),
                "closed_at": _iso(root.closed_at),
                "limits": asdict(limits[root.id]) if root.id in limits else None,
                "children": _nest(root, lineage[root.id]),
                "arrivals": arrivals[root.id],
                "proposals": proposals[root.id],
                "owner_rows": owner_rows[root.id],
            }
        )
    return views


# Contract 4.9.3 (+ C4): rows nobody will ever read, and rows still waiting for their turn. One statement so the
# two numbers are disjoint. ``delivered_at IS NULL`` leaves out the rows a cancel stamped (SILENT_SESSION_ID) and the
# rollback stamped (ROLLBACK_UNDELIVERABLE_ID): those are "closed, not lost" (result_inbox.py:454-473).
_RESULTS_STUCK_SQL = text(
    """
    SELECT
      count(*) FILTER (WHERE i.state IN ('closed', 'cancelled', 'expired')
                         OR root.root_cancelled_at IS NOT NULL OR root.root_expired_at IS NOT NULL) AS lost,
      count(*) FILTER (WHERE i.state IN ('result_ready', 'awaiting_owner', 'deciding')
                         AND root.root_cancelled_at IS NULL AND root.root_expired_at IS NULL) AS waiting
    FROM heart.result_inbox r
    JOIN brain.intentions i ON i.agent_id = r.agent_id AND i.id = r.intention_id
    JOIN brain.intentions root ON root.agent_id = i.agent_id AND root.id = i.root_id
    WHERE r.agent_id = :a AND r.channel IS NULL AND r.session_id IS NULL AND r.delivered_at IS NULL
      AND r.created_at < :cutoff
    """
)


async def _results_stuck(session: AsyncSession, agent_id: str, *, settings: Any, now: datetime) -> tuple[int, int, int]:
    grace = int(settings.continuation_lease_seconds) + int(settings.continuation_max_wait_seconds) + 60
    row = (await session.execute(_RESULTS_STUCK_SQL, {"a": agent_id, "cutoff": now - timedelta(seconds=grace)})).one()
    return int(row.lost), int(row.waiting), grace


async def _latency(session: AsyncSession, agent_id: str, *, since: datetime) -> dict[str, Any]:
    rows = (
        await session.execute(
            select(IntentionArrival.decided_at, _FIRST_RESULT_AT).where(
                IntentionArrival.agent_id == agent_id,
                IntentionArrival.decided_at >= since,
                func.cardinality(IntentionArrival.inbox_ids) > 0,
            )
        )
    ).all()
    # ASCENDING: _percentile indexes the list it is given by rank
    values = sorted((decided - first).total_seconds() for decided, first in rows if first is not None)
    if not values:
        return {"p50": None, "p90": None, "max": None, "n": 0}
    return {
        "p50": _percentile(values, 0.50),
        "p90": _percentile(values, 0.90),
        "max": round(values[-1], 1),
        "n": len(values),
    }


async def get_metrics(
    session: AsyncSession, agent_id: str, *, settings: Any, since: datetime, now: datetime
) -> dict[str, Any]:
    """The metrics of contract 4.9, each a read over existing tables; ``since`` is the start of the window."""
    A = IntentionArrival
    decided = (A.agent_id == agent_id, A.decided_at >= since)
    by_outcome = (
        await session.execute(
            select(A.decision, A.outcome, A.gate_reason.is_not(None).label("gated"), func.count())
            .where(*decided)
            .group_by(A.decision, A.outcome, A.gate_reason.is_not(None))
            .order_by(A.decision, A.outcome, A.gate_reason.is_not(None))
        )
    ).all()
    lost, waiting, grace = await _results_stuck(session, agent_id, settings=settings, now=now)
    unpushed = (
        await session.execute(
            select(func.count()).where(
                ResultInbox.agent_id == agent_id,
                ResultInbox.source_kind == continuation.SOURCE_INTENTION_REPORT,
                ResultInbox.pushed_at.is_(None),
                ResultInbox.push_after < now - timedelta(hours=1),
                ResultInbox.delivered_at.is_(None),
                ResultInbox.channel.like("telegram:%"),
                ResultInbox.created_at >= since,  # C7
            )
        )
    ).scalar_one()
    proposals = dict(
        (
            await session.execute(
                select(IntentionProposal.state, func.count())
                .where(
                    IntentionProposal.agent_id == agent_id,
                    IntentionProposal.state != "staged",
                    IntentionProposal.created_at >= since,
                )
                .group_by(IntentionProposal.state)
            )
        ).all()
    )
    reasons = {r: 0 for r in continuation.GATE_REASONS}
    for reason, n in (
        await session.execute(
            select(A.gate_reason, func.count()).where(*decided, A.gate_reason.is_not(None)).group_by(A.gate_reason)
        )
    ).all():
        reasons[reason] = int(n)
    expired_roots = (
        await session.execute(
            select(func.count()).where(
                Intention.agent_id == agent_id, Intention.id == Intention.root_id, Intention.root_expired_at >= since
            )
        )
    ).scalar_one()
    stalls = (
        await session.execute(
            select(
                func.count().filter(A.progress.is_(False), A.gate_reason.is_(None)),
                func.count().filter(A.progress_claimed.is_(True), A.progress.is_(False)),
                func.count().filter(A.gate_reason == "budget_stall"),
                func.count().filter(A.progress_claimed.is_(True)),
                func.count().filter(A.progress_claimed.is_(True), A.progress.is_(True)),
            ).where(*decided)
        )
    ).one()
    ungradable = (
        await session.execute(select(func.count()).where(*decided, A.decision_record_id.is_(None)))
    ).scalar_one()
    return {
        "arrivals_by_decision_outcome": [
            {"decision": d, "outcome": o, "gated": bool(g), "count": int(n)} for d, o, g, n in by_outcome
        ],
        "result_to_decision_latency_s": await _latency(session, agent_id, since=since),
        "results_lost": lost,
        "results_waiting": waiting,
        "results_lost_grace_s": grace,
        "owner_rows_unpushed": int(unpushed),
        "proposals_by_state": {
            "counts": {s: int(n) for s, n in proposals.items()},
            "approved_total": sum(int(proposals.get(s, 0)) for s in ("approved", "executing", "executed", "failed")),
        },
        "escalations_by_cause": {"by_reason": reasons, "expired_roots": int(expired_roots)},
        "stalls": {
            "verified_no_progress": int(stalls[0]),
            "claimed_not_credited": int(stalls[1]),
            "stall_escalations": int(stalls[2]),
            "progress_claimed": int(stalls[3]),
            "progress_credited": int(stalls[4]),
        },
        "arrivals_ungradable": int(ungradable),
    }


async def get_intentions_data(
    session: AsyncSession,
    agent_id: str,
    *,
    settings: Any,
    window: str = "7d",
    state: str = "all",
    limit: int = 50,
    now: datetime | None = None,
) -> dict[str, Any]:
    """``GET /dashboard/intentions``. Raises ``ValueError`` for a bad ``window``, ``state`` or ``limit`` (checked
    before the flag, so the answer to a bad query does not depend on the deployment). With continuation off it
    returns ``disabled_payload()`` and reads no row. ``window`` selects the metrics' period; the tree is the
    newest ``limit`` roots whatever their age."""
    now = now or datetime.now(UTC)
    since = check_query(window, state, limit, now)
    if not continuation.enabled(settings):
        return disabled_payload()
    roots = await _root_rows(session, agent_id, state=state, limit=limit)
    return {
        "continuation": True,
        "window": window,
        "roots": await _tree(session, agent_id, roots, settings=settings) if roots else [],
        "metrics": await get_metrics(session, agent_id, settings=settings, since=since, now=now),
    }
