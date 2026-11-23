"""F118 PR 1: migration 121, nous_system.judge_verdicts, the outside judge's verdicts.

The database test runs the migration twice inside a transaction that is rolled back, so its rows never outlive the
test. One row per subject, judge name and judge version; the verdicts allowed depend on the kind of subject.
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
MIGRATION = ROOT / "sql" / "migrations" / "121_judge_verdicts.sql"

COLUMNS = {
    "id": "uuid",
    "agent_id": "text",
    "subject_kind": "text",
    "subject_id": "uuid",
    "judge_name": "text",
    "judge_version": "text",
    "model": "text",
    "status": "text",
    "verdict": "text",
    "confidence": "real",
    "labels": "jsonb",
    "reason": "text",
    "against_grader": "text",
    "against_grade": "text",
    "claim_at": "timestamp with time zone",
    "evidence": "jsonb",
    "tokens_in": "integer",
    "tokens_out": "integer",
    "latency_ms": "integer",
    "calls": "smallint",
    "attempts": "smallint",
    "judged_at": "timestamp with time zone",
}


def test_the_migration_creates_one_table_and_one_index_and_grants_nothing():
    sql = MIGRATION.read_text(encoding="utf-8")
    statements = _split_sql_statements(sql)
    assert [s.split("(")[0].split(" ON ")[0].strip() for s in statements] == [
        "CREATE TABLE IF NOT EXISTS nous_system.judge_verdicts",
        "CREATE INDEX IF NOT EXISTS idx_judge_verdicts_agent_judged",
    ]
    code = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    keywords = re.sub(r"'[^']*'", "''", code)
    for word in ("GRANT", "REVOKE", "OWNER", "ROLE", "ALTER", "DROP", "AUTHORIZATION", "REFERENCES"):
        assert not re.search(rf"\b{word}\b", keywords.upper()), word
    # The migrator splits on semicolons: none may sit in a comment.
    assert all(";" not in line for line in sql.splitlines() if line.lstrip().startswith("--"))


CLAIM_AT = datetime(2026, 11, 22, 18, 0, tzinfo=UTC)
ROW = {
    "subject_kind": "decision",
    "judge_name": "background_model",
    "judge_version": "dj-1",
    "status": "ok",
    "verdict": "success",
    "claim_at": CLAIM_AT,
}


async def _insert(conn, agent: str, subject_id: uuid.UUID | None, **over) -> None:
    values = {**ROW, **over, "agent_id": agent, "subject_id": subject_id}
    columns = ", ".join(values)
    params = ", ".join(f"CAST(:{name} AS jsonb)" if name in ("labels", "evidence") else f":{name}" for name in values)
    await conn.execute(text(f"INSERT INTO nous_system.judge_verdicts ({columns}) VALUES ({params})"), values)


async def _refused(conn, agent: str, subject_id: uuid.UUID | None, **over) -> None:
    savepoint = await conn.begin_nested()
    with pytest.raises(IntegrityError):
        await _insert(conn, agent, subject_id, **over)
    await savepoint.rollback()


@pytest.mark.postgres_only
async def test_the_migration_applies_twice_and_its_constraints_hold(db):
    agent = f"test-f118-{uuid.uuid4().hex[:8]}"
    async with db.engine.connect() as conn:
        trans = await conn.begin()
        try:
            for _ in range(2):
                for statement in _split_sql_statements(MIGRATION.read_text(encoding="utf-8")):
                    await conn.execute(text(statement))

            types = dict(
                (
                    await conn.execute(
                        text(
                            "SELECT column_name, data_type FROM information_schema.columns "
                            "WHERE table_schema = 'nous_system' AND table_name = 'judge_verdicts'"
                        )
                    )
                ).all()
            )
            assert types == COLUMNS

            subject = uuid.uuid4()
            await _insert(conn, agent, subject)
            row = (
                await conn.execute(
                    text(
                        "SELECT labels, evidence, attempts, calls, judged_at IS NOT NULL, confidence, reason, claim_at "
                        "FROM nous_system.judge_verdicts WHERE agent_id = :a"
                    ),
                    {"a": agent},
                )
            ).one()
            assert tuple(row) == ([], {}, 1, 0, True, None, None, CLAIM_AT)

            # One verdict per subject, judge name and version; another version, judge or agent is another row.
            await _refused(conn, agent, subject, verdict="failure")
            await _insert(conn, agent, subject, judge_version="dj-2")
            await _insert(conn, agent, subject, judge_name="another_judge")
            await _insert(conn, f"{agent}-other", subject)

            # Every listed value is accepted.
            for verdict in ("partial", "failure", "cannot_tell"):
                await _insert(conn, agent, uuid.uuid4(), verdict=verdict)
            for verdict in ("supported", "not_supported", "cannot_tell"):
                await _insert(conn, agent, uuid.uuid4(), subject_kind="task_report", verdict=verdict)
            for status in ("skipped", "error", "timeout"):
                await _insert(conn, agent, uuid.uuid4(), status=status, verdict="cannot_tell")
            await _insert(conn, agent, uuid.uuid4(), confidence=0.0, labels='["evidence_missing"]')
            await _insert(conn, agent, uuid.uuid4(), confidence=1.0, evidence='{"link": "none"}')
            await _insert(conn, agent, uuid.uuid4(), reason="y" * 200, calls=2, attempts=3)

            # Anything else is refused.
            await _refused(conn, agent, uuid.uuid4(), subject_kind="goal")
            await _refused(conn, agent, uuid.uuid4(), status="done")
            await _refused(conn, agent, uuid.uuid4(), verdict="supported")  # a decision's verdict set
            await _refused(conn, agent, uuid.uuid4(), subject_kind="task_report", verdict="success")
            await _refused(conn, agent, uuid.uuid4(), verdict="Success")
            await _refused(conn, agent, uuid.uuid4(), confidence=1.5)
            await _refused(conn, agent, uuid.uuid4(), confidence=-0.1)
            await _refused(conn, agent, uuid.uuid4(), reason="x" * 201)  # one sentence, at most 200 characters
            await _refused(conn, agent, uuid.uuid4(), calls=-1)
            await _refused(conn, agent, uuid.uuid4(), attempts=0)
            await _refused(conn, agent, uuid.uuid4(), judge_version=None)
            await _refused(conn, agent, None)
            # Every row says when its claim was fixed: there is no default for it.
            await _refused(conn, agent, uuid.uuid4(), claim_at=None)
            default = await conn.scalar(
                text(
                    "SELECT column_default FROM information_schema.columns WHERE table_schema = 'nous_system' "
                    "AND table_name = 'judge_verdicts' AND column_name = 'claim_at'"
                )
            )
            assert default is None

            count = await conn.scalar(
                text("SELECT count(*) FROM nous_system.judge_verdicts WHERE agent_id = :a"), {"a": agent}
            )
            assert count == 15
        finally:
            await trans.rollback()


@pytest.mark.postgres_only
async def test_the_reader_role_has_no_privilege_on_the_table(db):
    """Where the reader role exists (the DB roles job), it holds no privilege on the table. Elsewhere the role is
    absent and the migration names no role at all (the test above)."""
    async with db.engine.connect() as conn:
        exists = await conn.scalar(text("SELECT count(*) FROM pg_roles WHERE rolname = 'nous_reader'"))
        if not exists:
            pytest.skip("no nous_reader role in this database")
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            granted = await conn.scalar(
                text("SELECT has_table_privilege('nous_reader', 'nous_system.judge_verdicts', :p)"), {"p": privilege}
            )
            assert granted is False, privilege
