"""F099 Phase 3c: the intentions dashboard (contract 4.9): every root as a tree, and the metrics beside it.

Read-only and agent-scoped. Postgres-only: arrays (``ANY``), ``FILTER``, a raw read of ``nous_system.context_log``
(which has no ORM model). The harness precedent (``harness_dashboard.py``) keeps its queries beside the route in
its own module for the same reason. Model-authored text (``intent``, ``note``, a proposal's ``rationale``,
``arguments`` and ``result``, an owner row's ``title``) is returned RAW, as ``GET /intentions`` returns it: the
view renders it as text nodes only.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, any_, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from nous.api.harness_dashboard import window_start
from nous.brain import continuation
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


async def get_metrics(
    session: AsyncSession, agent_id: str, *, settings: Any, since: datetime, now: datetime
) -> dict[str, Any]:
    return {}  # Task 3c.3 and 3c.4


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
