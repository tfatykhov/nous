"""Row builders for the F099 Phase 3c dashboard tests: they write brain/heart rows directly. The pool is built
with expire_on_commit=False (storage/database.py), so a returned row can be read after its commit."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from nous.storage.models import Decision, Intention, IntentionArrival, IntentionProposal, ResultInbox


def utcnow() -> datetime:
    return datetime.now(UTC)


async def put_intention(
    env, *, parent: Intention | None = None, intent: str = "watch the snow", state: str = "pending", **over: Any
) -> Intention:
    """An intention row: a root when ``parent`` is None, else a child of it (depth + 1, same root)."""
    row_id = uuid.uuid4()
    fields: dict[str, Any] = dict(
        id=row_id,
        agent_id=env.agent,
        root_id=parent.root_id if parent is not None else row_id,
        parent_id=parent.id if parent is not None else None,
        depth=(parent.depth + 1) if parent is not None else 0,
        source_kind="subtask",
        source_id=str(uuid.uuid4()),  # no Subtask row: nothing reads it here
        intent=intent,
        origin_kind="interactive" if parent is None else "continuation",
        wake_policy="continue",
        authority="owner" if parent is None else "internal_only",
        state=state,
    )
    row = Intention(**{**fields, **over})  # a keyword argument replaces the default of the same name
    async with env.db.session() as s:
        s.add(row)
        await s.commit()
    return row


async def put_arrivals(env, root: Intention, specs: list[dict[str, Any]]) -> list[IntentionArrival]:
    """Arrival rows n = 1.. in one transaction. A spec may set decision, outcome, progress, progress_claimed,
    gate_reason, tokens (in, out), decided_ago (seconds, default 60), inbox_ids, decision_record_id, note,
    confidence, intention_ids."""
    rows = []
    for n, spec in enumerate(specs, start=1):
        rows.append(
            IntentionArrival(
                agent_id=env.agent,
                root_id=root.id,
                n=spec.get("n", n),
                intention_ids=list(spec.get("intention_ids") or [root.id]),
                inbox_ids=list(spec.get("inbox_ids") or []),
                claim_token=uuid.uuid4(),
                decision=spec.get("decision", "continue"),
                note=spec.get("note"),
                progress_claimed=spec.get("progress_claimed"),
                progress=spec.get("progress"),
                confidence=spec.get("confidence"),
                gate_reason=spec.get("gate_reason"),
                tokens_in=spec.get("tokens", (0, 0))[0],
                tokens_out=spec.get("tokens", (0, 0))[1],
                decision_record_id=spec.get("decision_record_id"),
                outcome=spec.get("outcome", "resolved"),
                decided_at=spec.get("decided_at", utcnow() - timedelta(seconds=spec.get("decided_ago", 60))),
            )
        )
    async with env.db.session() as s:
        s.add_all(rows)
        await s.commit()
    return rows


async def put_inbox(
    env,
    intention: Intention | None = None,
    *,
    created_ago: float = 600,
    delivered: bool = False,
    delivered_session_id: str | None = None,
    created_at: datetime | None = None,
    **over: Any,
) -> ResultInbox:
    """A result_inbox row. The default is an intention-keyed row (channel and session NULL, the continuation's)."""
    row = ResultInbox(
        agent_id=env.agent,
        channel=over.pop("channel", None),
        session_id=over.pop("session_id", None),
        source_kind=over.pop("source_kind", "subtask"),
        source_id=uuid.uuid4(),
        msg_type=over.pop("msg_type", "INFORM"),
        title=over.pop("title", "Snow report"),
        body=over.pop("body", "40cm overnight"),
        created_at=created_at or utcnow() - timedelta(seconds=created_ago),
        delivered_at=utcnow() if delivered else None,
        delivered_session_id=delivered_session_id,
        intention_id=intention.id if intention is not None else None,
        **over,
    )
    async with env.db.session() as s:
        s.add(row)
        await s.commit()
    return row


async def put_decision(env, *, outcome: str = "noise", reviewer: str | None = "agent-background") -> uuid.UUID:
    row = Decision(
        agent_id=env.agent,
        description="F099 arrival 1 on 'watch the snow': continue",
        confidence=0.7,
        category="process",
        stakes="low",
        outcome=outcome,
        reviewer=reviewer,
    )
    async with env.db.session() as s:
        s.add(row)
        await s.commit()
        return row.id


async def put_proposal(env, root: Intention, *, state: str = "pending", tool: str = "send_email") -> IntentionProposal:
    row = IntentionProposal(
        agent_id=env.agent,
        intention_id=root.id,
        root_id=root.root_id,
        tool=tool,
        arguments={"to": "a@example.test"},
        rationale="the user asked for it",
        state=state,
        claim_token=uuid.uuid4(),
    )
    async with env.db.session() as s:
        s.add(row)
        await s.commit()
    return row


async def put_context_log(
    env,
    session_id: str,
    *,
    output: int,
    inp: int = 10,
    cw: int = 0,
    cr: int = 0,
    ago: float = 60,
    agent: str | None = None,
) -> None:
    """A nous_system.context_log row (no ORM model): one API call of ``session_id``."""
    async with env.db.session() as s:
        await s.execute(
            text(
                "INSERT INTO nous_system.context_log (id, agent_id, session_id, turn_number, timestamp, call_type, "
                "model, input_tokens_actual, output_tokens, cache_creation, cache_read) VALUES "
                "(:id, :a, :sid, 1, :ts, 'chat', 'test-model', :i, :o, :cw, :cr)"
            ),
            {
                "id": uuid.uuid4().hex,
                "a": agent or env.agent,
                "sid": session_id,
                "ts": utcnow() - timedelta(seconds=ago),
                "i": inp,
                "o": output,
                "cw": cw,
                "cr": cr,
            },
        )
        await s.commit()
