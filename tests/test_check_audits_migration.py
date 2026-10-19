"""Migration 090 (nous_system.check_audits): the file the migrator will run matches the ORM model the SQLite tests
build, and the table keeps one audit per subtask."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from nous.storage.migrator import _split_sql_statements
from nous.storage.models import CheckAudit

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "sql" / "migrations" / "090_check_audits.sql"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _code() -> str:
    return "\n".join(ln for ln in _sql().splitlines() if not ln.lstrip().startswith("--"))


def test_the_migration_splits_into_a_table_and_an_index():
    statements = _split_sql_statements(_sql())
    assert len(statements) == 2
    assert statements[0].startswith("CREATE TABLE IF NOT EXISTS nous_system.check_audits")
    assert statements[1].startswith("CREATE INDEX IF NOT EXISTS idx_check_audits_agent_kind_answered")
    assert "ON nous_system.check_audits (agent_id, check_kind, answered_at)" in statements[1]


def test_the_migration_creates_only_the_table():
    # Grants belong to scripts/ops/db_roles.sql: the migrator runs as the application role.
    code = _code().upper()
    for word in ("GRANT", "REVOKE", "OWNER", "ROLE", "ALTER", "DROP", "AUTHORIZATION"):
        assert not re.search(rf"\b{word}\b", code), word
    assert not re.search(r"scoring\.|brain\.|heart\.", _code(), re.I)


def test_the_migration_has_nothing_the_splitter_or_text_would_mangle():
    sql = _sql()
    assert "/*" not in sql and "$$" not in sql
    assert not re.search(r"(?<!:):[A-Za-z_]", _code())  # text() reads :name as a bind parameter
    assert all(";" not in ln for ln in sql.splitlines() if ln.lstrip().startswith("--"))


def test_the_columns_and_constraints_match_the_orm_model():
    body = _split_sql_statements(_sql())[0]
    sql_columns = set(re.findall(r"^\s{4}([a-z_]+)\s+(?:UUID|TEXT|TIMESTAMPTZ)", body, re.M))
    assert sql_columns == {c.name for c in CheckAudit.__table__.columns}
    assert set(re.findall(r"CONSTRAINT (\w+)", body)) == {c.name for c in CheckAudit.__table__.constraints if c.name}
    assert "UNIQUE (agent_id, task_id)" in body
    assert "CHECK (verdict IN ('right', 'wrong'))" in body
    assert CheckAudit.__table__.fullname == "nous_system.check_audits"
    (index,) = CheckAudit.__table__.indexes
    assert index.name == "idx_check_audits_agent_kind_answered"
    assert [c.name for c in index.columns] == ["agent_id", "check_kind", "answered_at"]


def _row(agent: str, task_id: uuid.UUID, **kw) -> CheckAudit:
    return CheckAudit(agent_id=agent, task_id=task_id, node_id=uuid.uuid4(), check_kind="file_exists", **kw)


async def test_a_subtask_has_at_most_one_audit_per_agent(db):
    agent = f"test-audit-{uuid.uuid4().hex[:8]}"
    task_id = uuid.uuid4()
    async with db.session() as s:
        s.add(_row(agent, task_id))
        await s.commit()
    with pytest.raises(IntegrityError):
        async with db.session() as s:
            s.add(_row(agent, task_id))
            await s.commit()
    async with db.session() as s:  # another agent's audit of the same id is its own row
        s.add(_row(f"{agent}-b", task_id))
        await s.commit()


async def test_a_verdict_is_right_or_wrong(db):
    agent = f"test-audit-{uuid.uuid4().hex[:8]}"
    with pytest.raises(IntegrityError):
        async with db.session() as s:
            s.add(_row(agent, uuid.uuid4(), verdict="maybe"))
            await s.commit()


@pytest.mark.postgres_only
async def test_the_migration_is_idempotent(db):
    async with db.engine.begin() as conn:
        for _ in range(2):
            for statement in _split_sql_statements(_sql()):
                await conn.execute(text(statement))
