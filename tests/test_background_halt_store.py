"""Goals PR 0c: the stored halt (migration 114), its audit rows, and the load at start."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from nous import background_halt_store
from nous.background_halt import BackgroundHalt
from nous.background_halt_store import BackgroundHaltStore

pytestmark = pytest.mark.postgres_only

MIGRATION = Path(__file__).resolve().parents[1] / "sql" / "migrations" / "114_background_halt.sql"


def _agent() -> str:
    return f"halt-{uuid.uuid4().hex[:10]}"


async def _state(db, agent: str):
    async with db.session() as session:
        return (
            await session.execute(text("SELECT * FROM nous_system.background_halt WHERE agent_id = :a"), {"a": agent})
        ).first()


async def _log(db, agent: str) -> list[str]:
    async with db.session() as session:
        rows = await session.execute(
            text("SELECT action FROM nous_system.background_halt_log WHERE agent_id = :a ORDER BY id"), {"a": agent}
        )
        return [r.action for r in rows]


def _halt(db, agent: str, *, host_hold: bool = False, retry: float = 5.0) -> BackgroundHalt:
    return BackgroundHalt(host_hold=host_hold, store=BackgroundHaltStore(db, agent), retry_seconds=retry)


async def test_halt_writes_state_and_audit_in_one_transaction(db, monkeypatch):
    agent = _agent()
    halt = _halt(db, agent)
    outcome = await halt.halt(actor="dashboard", surface="dashboard", reason="stop")
    assert outcome.persisted
    row = await _state(db, agent)
    assert row.halted and row.halted_by == "dashboard" and row.halted_via == "dashboard" and row.halted_at
    assert await _log(db, agent) == ["halt"]

    other = _agent()
    broken = _halt(db, other)
    original = BackgroundHaltStore._audit_params

    def bad_action(self, entry):
        return {**original(self, entry), "action": "not-an-action"}  # the CHECK refuses it

    monkeypatch.setattr(BackgroundHaltStore, "_audit_params", bad_action)
    outcome = await broken.halt(actor="t", surface="dashboard")
    assert not outcome.persisted and broken.halted  # memory first
    assert await _state(db, other) is None  # the audit insert failed, so the state row did not land
    await broken.close()


async def test_restart_while_halted_stays_halted(db):
    agent = _agent()
    await _halt(db, agent).halt(actor="telegram:7", surface="telegram", verified=True)
    again = _halt(db, agent)
    await again.load()
    snap = again.snapshot()
    assert again.halted and snap["source"] == "stored" and snap["by"] == "telegram:7" and snap["since"]
    assert snap["verified"] is True and snap["surface"] == "telegram"


async def test_unreadable_state_starts_halted(db, monkeypatch):
    agent = _agent()
    monkeypatch.setattr(background_halt_store, "_READ", text("SELECT halted FROM nous_system.no_such_table"))
    halt = _halt(db, agent)
    await halt.load()
    assert halt.halted and halt.source == "unreadable"
    monkeypatch.undo()
    outcome = await halt.resume(actor="owner", via="dashboard")
    assert outcome.status == "resumed" and not halt.halted
    assert (await _state(db, agent)).halted is False


async def test_resume_persists_before_flipping(db, monkeypatch):
    agent = _agent()
    halt = _halt(db, agent)
    await halt.halt(actor="t", surface="dashboard")

    async def down(self, row, audit):
        raise RuntimeError("the database is down")

    with monkeypatch.context() as patch:
        patch.setattr(BackgroundHaltStore, "write", down)
        outcome = await halt.resume(actor="owner", via="dashboard")
    assert outcome.status == "failed" and halt.halted
    assert (await _state(db, agent)).halted

    outcome = await halt.resume(actor="owner", via="dashboard")
    assert outcome.status == "resumed" and not halt.halted
    row = await _state(db, agent)
    assert row.halted is False and row.resumed_by == "owner" and row.resumed_at is not None
    assert await _log(db, agent) == ["halt", "resume"]


async def test_a_failed_halt_write_followed_by_a_restart_loses_the_halt_but_a_landed_retry_survives(db, monkeypatch):
    """The price of memory first: a process that dies before its retry lands comes back not halted. The state
    route said ``persisted: false`` meanwhile."""
    agent = _agent()
    real_write = BackgroundHaltStore.write
    failures = {"left": 1}

    async def flaky(self, row, audit):
        if failures["left"]:
            failures["left"] -= 1
            raise RuntimeError("the database blinked")
        await real_write(self, row, audit)

    monkeypatch.setattr(BackgroundHaltStore, "write", flaky)
    lost = _halt(db, agent, retry=60)
    await lost.halt(actor="t", surface="dashboard")
    assert lost.halted and lost.snapshot()["persisted"] is False
    await lost.close()  # the process dies before the retry
    restarted = _halt(db, agent)
    await restarted.load()
    assert not restarted.halted

    failures["left"] = 1
    kept = _halt(db, agent, retry=0.05)
    await kept.halt(actor="t", surface="dashboard")
    for _ in range(100):
        if kept.persisted:
            break
        await asyncio.sleep(0.05)
    assert kept.persisted
    restarted = _halt(db, agent)
    await restarted.load()
    assert restarted.halted


async def test_host_hold_ignores_the_stored_state_and_resume_writes_a_refused_row(db):
    agent = _agent()
    halt = _halt(db, agent, host_hold=True)
    await halt.load()
    assert halt.halted and halt.source == "host"
    outcome = await halt.resume(actor="owner", via="dashboard")
    assert outcome.status == "host_hold" and halt.halted
    assert await _log(db, agent) == ["resume_refused"]
    assert await _state(db, agent) is None


async def test_the_migration_is_idempotent(db):
    sql = MIGRATION.read_text(encoding="utf-8")
    statements = [s for s in sql.split(";") if "CREATE" in s]
    for _ in range(2):
        async with db.engine.begin() as connection:
            for statement in statements:
                await connection.execute(text(statement))
    async with db.session() as session:
        names = {
            r.table_name
            for r in await session.execute(
                text(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'nous_system' "
                    "AND table_name LIKE 'background_halt%'"
                )
            )
        }
    assert names == {"background_halt", "background_halt_log"}
