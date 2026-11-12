"""F111 PR B1: the promise_gate_log retention sweep (plan §7). Postgres: the tables are migration 107's."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from nous.brain.commitments import CommitmentStore
from nous.observability.work_view_retention import sweep_promise_gate_log


@pytest.fixture
async def agent(db):
    agent_id = f"pg-ret-{uuid.uuid4().hex[:8]}"
    yield agent_id
    async with db.session() as s:
        for table in ("nous_system.promise_gate_log", "brain.commitments"):
            await s.execute(text(f"DELETE FROM {table} WHERE agent_id = :a"), {"a": agent_id})
        await s.commit()


async def _log(db, agent_id: str, age_days: float) -> None:
    async with db.session() as s:
        await s.execute(
            text(
                "INSERT INTO nous_system.promise_gate_log (agent_id, path, mode, detected, backed, unbacked, outcome, "
                "created_at) VALUES (:a, 'stream', 'shadow', 0, 0, 0, 'clean', now() - make_interval(secs => :s))"
            ),
            {"a": agent_id, "s": age_days * 86400},
        )
        await s.commit()


async def _count(db, agent_id: str) -> int:
    async with db.session() as s:
        result = await s.execute(
            text("SELECT count(*) FROM nous_system.promise_gate_log WHERE agent_id = :a"), {"a": agent_id}
        )
        return int(result.scalar())


@pytest.mark.postgres_only
async def test_old_gate_log_rows_go_newer_stay_and_commitments_are_untouched(db, agent):
    store = CommitmentStore(db, agent)
    row = await store.create_idempotent(
        source_kind="chat_stream",
        detected_by="gate",
        promise_text="I'll relaunch the chain at 18:15 UTC.",
        what="I'll relaunch the chain at 18:15 UTC.",
        due_at=datetime.now(UTC) + timedelta(hours=1),
        turn_id="t-ret",
    )
    async with db.session() as s:  # an old commitment: retention never touches the ledger
        await s.execute(
            text("UPDATE brain.commitments SET created_at = now() - interval '400 days' WHERE id = :i"), {"i": row.id}
        )
        await s.commit()
    before = await store.get(row.short_id)
    for age in (45, 31, 29, 1):
        await _log(db, agent, age)
    other = f"{agent}-other"
    await _log(db, other, 45)
    try:
        deleted = await sweep_promise_gate_log(db, agent, 30, batch=1)
        assert deleted == {"promise_gate_log": 2}
        assert await _count(db, agent) == 2
        assert await _count(db, other) == 1  # agent-scoped
        assert await store.get(row.short_id) == before
    finally:
        async with db.session() as s:
            await s.execute(text("DELETE FROM nous_system.promise_gate_log WHERE agent_id = :a"), {"a": other})
            await s.commit()


async def test_zero_days_is_refused():
    with pytest.raises(ValueError):
        await sweep_promise_gate_log(object(), "a", 0)
