"""F099 Phase 2b: migration 084 — the arrivals and proposals tables, the widened inbox."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from nous.storage.models import Intention, IntentionArrival, IntentionProposal, ResultInbox

# The tables must come from migration 084, not from the ORM (contract risk 11), and the SQLite lane's
# list type cannot bind UUIDs. CI (Postgres) is the gate.
pytestmark = pytest.mark.postgres_only


def _agent() -> str:
    return f"f099-sch-{uuid.uuid4().hex[:8]}"


async def _root(db, agent: str) -> uuid.UUID:
    root_id = uuid.uuid4()
    async with db.session() as s:
        s.add(
            Intention(
                id=root_id,
                agent_id=agent,
                root_id=root_id,
                source_kind="subtask",
                source_id=str(uuid.uuid4()),
                intent="x",
                origin_kind="interactive",
                wake_policy="continue",
            )
        )
        await s.commit()
    return root_id


def _arrival(agent: str, root_id: uuid.UUID, n: int = 1, **over) -> IntentionArrival:
    values = dict(
        agent_id=agent,
        root_id=root_id,
        n=n,
        intention_ids=[root_id],
        claim_token=uuid.uuid4(),
        outcome="resolved",
    )
    values.update(over)
    return IntentionArrival(**values)


async def test_an_arrival_row_round_trips_with_its_defaults(db):
    agent = _agent()
    root_id = await _root(db, agent)
    async with db.session() as s:
        s.add(_arrival(agent, root_id, decision="ask", gate_reason="budget_turns"))
        await s.commit()
    async with db.session() as s:
        row = (await s.execute(select(IntentionArrival).where(IntentionArrival.agent_id == agent))).scalar_one()
    assert (row.decision, row.gate_reason, row.tokens_in, row.tokens_out) == ("ask", "budget_turns", 0, 0)
    assert list(row.inbox_ids) == [] and list(row.report_ids) == []


@pytest.mark.parametrize(
    "bad",
    [{"outcome": "bogus"}, {"decision": "bogus"}, {"gate_reason": "bogus"}],
    ids=["outcome", "decision", "gate_reason"],
)
async def test_an_arrival_check_constraint_rejects_a_foreign_value(db, bad):
    agent = _agent()
    root_id = await _root(db, agent)
    with pytest.raises(IntegrityError):
        async with db.session() as s:
            s.add(_arrival(agent, root_id, **bad))
            await s.commit()


async def test_arrival_numbers_are_unique_per_root(db):
    agent = _agent()
    root_id = await _root(db, agent)
    async with db.session() as s:
        s.add(_arrival(agent, root_id, n=1))
        await s.commit()
    with pytest.raises(IntegrityError):
        async with db.session() as s:
            s.add(_arrival(agent, root_id, n=1))
            await s.commit()


async def test_a_proposal_row_defaults_to_staged_and_rejects_a_foreign_state(db):
    agent = _agent()
    root_id = await _root(db, agent)
    values = dict(
        agent_id=agent,
        intention_id=root_id,
        root_id=root_id,
        tool="send_email",
        arguments={"to": "a@example.com"},
        rationale="the owner asked",
        claim_token=uuid.uuid4(),
    )
    async with db.session() as s:
        s.add(IntentionProposal(**values))
        await s.commit()
    async with db.session() as s:
        row = (await s.execute(select(IntentionProposal).where(IntentionProposal.agent_id == agent))).scalar_one()
    assert row.state == "staged" and row.arrival_id is None
    with pytest.raises(IntegrityError):
        async with db.session() as s:
            s.add(IntentionProposal(**values, state="bogus"))
            await s.commit()


def _inbox(agent: str, **over) -> ResultInbox:
    values = dict(
        agent_id=agent,
        channel="telegram:8080",
        source_kind="intention_report",
        source_id=uuid.uuid4(),
        msg_type="QUESTION",
        title="t",
        body="b",
    )
    values.update(over)
    return ResultInbox(**values)


async def test_the_inbox_accepts_an_owner_facing_row_with_the_new_columns(db):
    agent = _agent()
    async with db.session() as s:
        s.add(_inbox(agent, arrival_id=uuid.uuid4(), proposal_id=uuid.uuid4(), push_message_id=2**40))
        await s.commit()
    async with db.session() as s:
        row = (await s.execute(select(ResultInbox).where(ResultInbox.agent_id == agent))).scalar_one()
    assert row.push_message_id == 2**40 and row.push_after is None and row.pushed_at is None


@pytest.mark.parametrize("bad", [{"source_kind": "bogus"}, {"msg_type": "bogus"}], ids=["source_kind", "msg_type"])
async def test_the_inbox_still_rejects_a_foreign_kind(db, bad):
    with pytest.raises(IntegrityError):
        async with db.session() as s:
            s.add(_inbox(_agent(), **bad))
            await s.commit()


async def test_two_agents_may_share_a_source_key_but_one_agent_may_not(db):
    source_id = uuid.uuid4()
    a, b = _agent(), _agent()
    async with db.session() as s:
        s.add(_inbox(a, source_id=source_id))
        s.add(_inbox(b, source_id=source_id))
        await s.commit()
    with pytest.raises(IntegrityError):
        async with db.session() as s:
            s.add(_inbox(a, source_id=source_id))
            await s.commit()


@pytest.mark.postgres_only  # pg_constraint introspection
async def test_the_inbox_unique_key_carries_the_agent_last(db):
    """agent_id goes LAST so the reconciler's has_row lookups, which prefix on
    (source_kind, source_id), keep using the index (contract section 4.2)."""
    async with db.engine.connect() as conn:
        cols = (
            (
                await conn.execute(
                    text(
                        "SELECT a.attname FROM pg_constraint c "
                        "JOIN pg_class t ON t.oid = c.conrelid "
                        "JOIN pg_namespace n ON n.oid = t.relnamespace "
                        "JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true "
                        "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum "
                        "WHERE n.nspname = 'heart' AND t.relname = 'result_inbox' "
                        "AND c.conname = 'uq_result_inbox_source' "
                        "ORDER BY k.ord"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert list(cols) == ["source_kind", "source_id", "source_generation", "agent_id"]


@pytest.mark.postgres_only  # pg_indexes
async def test_the_partial_indexes_exist(db):
    async with db.engine.connect() as conn:
        names = set(
            (
                await conn.execute(
                    text(
                        "SELECT indexname FROM pg_indexes WHERE schemaname IN ('brain', 'heart') AND indexname IN "
                        "('idx_intention_arrivals_root', 'idx_intention_proposals_open', "
                        "'idx_intention_proposals_arrival', 'idx_result_inbox_intention_undelivered', "
                        "'idx_result_inbox_push_due')"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(names) == 5
