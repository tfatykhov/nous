"""F116 PR 1: migration 119, heart.pending_resolutions (feature doc §5.1, §7.8).

A headless job may not write under sql/migrations/, so the DDL is recorded in the F116 doc's appendix and the owner adds
the file. These tests read the DDL from there: once the file exists it must be that block exactly. The database tests
run the DDL inside a transaction that is rolled back, so the table never outlives the test (test_database's exact
table set is not touched until the migration itself lands).
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from nous.storage.migrator import _split_sql_statements

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "sql" / "migrations" / "119_pending_resolutions.sql"
DOC = ROOT / "docs" / "features" / "F116-stale-pending-findings.md"


def _ddl() -> str:
    doc = DOC.read_text(encoding="utf-8")
    appendix = doc.split("## Appendix: the DDL of migration 119", 1)[1]
    return appendix.split("```sql\n", 1)[1].split("```", 1)[0]


def test_the_ddl_creates_one_table_and_grants_nothing():
    ddl = _ddl()
    statements = _split_sql_statements(ddl)
    assert [s.split("(")[0].strip() for s in statements] == ["CREATE TABLE IF NOT EXISTS heart.pending_resolutions"]
    code = "\n".join(line for line in ddl.splitlines() if not line.lstrip().startswith("--"))
    keywords = re.sub(r"'[^']*'", "''", code)  # 'owner' is a created_by value, not a keyword
    for word in ("GRANT", "REVOKE", "OWNER", "ROLE", "ALTER", "DROP", "AUTHORIZATION", "REFERENCES"):
        assert not re.search(rf"\b{word}\b", keywords.upper()), word
    assert all(";" not in line for line in ddl.splitlines() if line.lstrip().startswith("--"))
    assert not re.search(r"(?<!:):[A-Za-z_]", code)


def test_the_migration_file_is_the_ddl_the_doc_records():
    if not MIGRATION.exists():
        pytest.skip("the owner adds sql/migrations/119_pending_resolutions.sql (F116 PR 1)")
    assert MIGRATION.read_text(encoding="utf-8") == _ddl()


ROW = {
    "subject_kind": "fact",
    "reason": "referents_terminal",
    "created_by": "heartbeat",
    "review_by": None,
}


async def _insert(conn, agent: str, subject_id: uuid.UUID, **over) -> None:
    values = {**ROW, **over, "agent_id": agent, "subject_id": subject_id}
    columns = ", ".join(values)
    params = ", ".join(f":{name}" for name in values)
    await conn.execute(text(f"INSERT INTO heart.pending_resolutions ({columns}) VALUES ({params})"), values)


async def _refused(conn, agent: str, subject_id: uuid.UUID, **over) -> None:
    savepoint = await conn.begin_nested()
    with pytest.raises(IntegrityError):
        await _insert(conn, agent, subject_id, **over)
    await savepoint.rollback()


@pytest.mark.postgres_only
async def test_the_ddl_applies_twice_and_its_constraints_hold(db):
    agent = f"test-f116-{uuid.uuid4().hex[:8]}"
    async with db.engine.connect() as conn:
        trans = await conn.begin()
        try:
            for _ in range(2):
                for statement in _split_sql_statements(_ddl()):
                    await conn.execute(text(statement))

            fact_id = uuid.uuid4()
            await _insert(conn, agent, fact_id)
            row = (
                await conn.execute(
                    text(
                        "SELECT evidence, created_at IS NOT NULL, review_by, reopened_at "
                        "FROM heart.pending_resolutions WHERE agent_id = :a"
                    ),
                    {"a": agent},
                )
            ).one()
            assert tuple(row) == ({}, True, None, None)

            # One resolution per item and agent; the same id as an episode, or for another agent, is another item.
            await _refused(conn, agent, fact_id, reason="age_cap")
            await _insert(conn, agent, fact_id, subject_kind="episode")
            await _insert(conn, f"{agent}-other", fact_id)

            # Every listed value is accepted.
            for reason in ("superseded", "age_cap", "owner_closed"):
                await _insert(conn, agent, uuid.uuid4(), reason=reason)
            for created_by in ("tool", "owner"):
                await _insert(conn, agent, uuid.uuid4(), created_by=created_by)
            await _insert(
                conn,
                agent,
                uuid.uuid4(),
                reason="suppressed_by_note",
                created_by="tool",
                review_by=datetime(2027, 1, 20, tzinfo=UTC),
            )

            # Anything else is refused.
            await _refused(conn, agent, uuid.uuid4(), subject_kind="decision")
            await _refused(conn, agent, uuid.uuid4(), reason="stale")
            await _refused(conn, agent, uuid.uuid4(), created_by="Heartbeat")
            await _refused(conn, agent, uuid.uuid4(), reason="suppressed_by_note")  # a note's close must expire
            await _refused(conn, agent, uuid.uuid4(), subject_kind=None)
            await _refused(conn, agent, None)

            count = await conn.scalar(
                text("SELECT count(*) FROM heart.pending_resolutions WHERE agent_id = :a"), {"a": agent}
            )
            assert count == 8
        finally:
            await trans.rollback()
