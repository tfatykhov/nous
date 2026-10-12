"""Builders for the F099 Phase 3d tests: arrival-linked Brain decisions on a real root."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import select, update

from nous.brain import Brain
from nous.storage.models import Decision, Intention, IntentionArrival, IntentionProposal

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def brain_of(env) -> Brain:
    return Brain(database=env.db, settings=env.settings)


async def make_decision(
    env,
    *,
    session_id: str | None = None,
    outcome: str = "pending",
    reviewer: str | None = None,
    description: str = "F099 arrival 1 on 'check the snow': continue",
) -> UUID:
    """A Brain decision row. ``outcome`` other than ``pending`` also stamps ``reviewed_at`` (a reviewed row)."""
    async with env.db.session() as s:
        row = Decision(
            agent_id=env.agent,
            description=description,
            confidence=0.7,
            category="process",
            stakes="low",
            session_id=session_id,
            outcome=outcome,
            reviewer=reviewer,
            reviewed_at=None if outcome == "pending" else datetime.now(UTC),
        )
        s.add(row)
        await s.commit()
        return row.id


async def arrival(
    env,
    root,
    n: int,
    *,
    linked: bool = True,
    decision_kw: dict | None = None,
    decision: str = "continue",
    outcome: str = "resolved",
    gate_reason: str | None = None,
) -> SimpleNamespace:
    """An arrival row of ``root`` as a commit leaves it. ``linked`` also writes its Brain decision
    (session ``intent-<root>``) and sets ``decision_record_id``. ``decided_at`` rises with ``n``."""
    decision_id = None
    if linked:
        decision_id = await make_decision(
            env,
            session_id=f"intent-{root.id}",
            description=f"F099 arrival {n} on 'check the snow': {decision}",
            **(decision_kw or {}),
        )
    arrival_id = uuid.uuid4()
    async with env.db.session() as s:
        s.add(
            IntentionArrival(
                id=arrival_id,
                agent_id=env.agent,
                root_id=root.id,
                n=n,
                intention_ids=[root.id],
                claim_token=uuid.uuid4(),
                decision=decision,
                progress_claimed=False,
                progress=False,
                gate_reason=gate_reason,
                decision_record_id=decision_id,
                outcome=outcome,
                decided_at=T0 + timedelta(minutes=n),
            )
        )
        await s.commit()
    return SimpleNamespace(id=arrival_id, decision_id=decision_id, n=n)


async def proposal(env, root, arr, *, state: str, decided_by: str | None = "owner") -> UUID:
    async with env.db.session() as s:
        row = IntentionProposal(
            agent_id=env.agent,
            intention_id=root.id,
            root_id=root.id,
            arrival_id=arr.id,
            tool="send_email",
            arguments={"to": "friend@example.com"},
            rationale="Share the snow report.",
            state=state,
            claim_token=uuid.uuid4(),
            decided_by=decided_by,
        )
        s.add(row)
        await s.commit()
        return row.id


async def close_root(env, root, *, cancelled: bool = False, expired: bool = False, child_expired: bool = False):
    """End the root the way the runner would: closed, cancelled, or expired (``child_expired`` adds the
    ``expired`` intention the TTL sweep leaves)."""
    now = datetime.now(UTC)
    values: dict = {"state": "closed", "close_reason": "delivered", "closed_at": now}
    if cancelled:
        values = {"state": "cancelled", "close_reason": "cancelled", "closed_at": now, "root_cancelled_at": now}
    if expired:
        values = {"state": "expired", "close_reason": "expired", "closed_at": now, "root_expired_at": now}
        if not child_expired:  # the hanging sweep writes only the marker
            values = {"state": "closed", "close_reason": "delivered", "closed_at": now, "root_expired_at": now}
    async with env.db.session() as s:
        await s.execute(update(Intention).where(Intention.id == root.id).values(**values))
        await s.commit()


async def outcome_of(env, decision_id: UUID) -> SimpleNamespace:
    async with env.db.session() as s:
        row = (await s.execute(select(Decision).where(Decision.id == decision_id))).scalar_one()
    return SimpleNamespace(
        outcome=row.outcome, reviewer=row.reviewer, result=row.outcome_result, reviewed_at=row.reviewed_at
    )
