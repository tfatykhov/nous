"""Migration 088 (scoring.scorer_heartbeat): the file the migrator runs matches the ORM model the SQLite tests build."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from nous.observability import task_score_job as job
from nous.storage.migrator import _split_sql_statements
from nous.storage.models import ScorerHeartbeat

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "sql" / "migrations" / "088_scorer_heartbeat.sql"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _code() -> str:
    return "\n".join(ln for ln in _sql().splitlines() if not ln.lstrip().startswith("--"))


def test_the_migration_is_one_table():
    statements = _split_sql_statements(_sql())
    assert len(statements) == 1
    assert statements[0].startswith("CREATE TABLE IF NOT EXISTS scoring.scorer_heartbeat")


def test_the_migration_creates_only_the_table():
    # Ownership and grants are not this migration's job: the migrator runs as the app role.
    code = _code().upper()
    for word in ("GRANT", "REVOKE", "OWNER", "ROLE", "ALTER", "DROP", "AUTHORIZATION"):
        assert not re.search(rf"\b{word}\b", code), word
    assert not re.search(r"nous_system|brain\.|heart\.", code, re.I)  # nothing is created in an app schema


def test_the_migration_has_nothing_the_splitter_or_text_would_mangle():
    sql = _sql()
    assert "/*" not in sql and "$$" not in sql
    # text() reads :name as a bind parameter
    assert not re.search(r"(?<!:):[A-Za-z_]", _code())
    # a ';' inside a comment line is dropped with the line, but one after code on the same line is not
    assert all(";" not in ln for ln in sql.splitlines() if ln.lstrip().startswith("--"))


def test_the_columns_match_the_orm_model():
    body = _split_sql_statements(_sql())[0]
    sql_columns = set(re.findall(r"^\s{4}([a-z_]+)\s+(?:TEXT|VARCHAR|TIMESTAMPTZ|INTEGER)", body, re.M))
    assert sql_columns == {c.name for c in ScorerHeartbeat.__table__.columns}


def test_the_error_width_is_the_same_in_the_sql_the_model_and_the_job():
    # the job truncates the class name to the column: a wider name would fail the error heartbeat's write
    (width,) = re.findall(r"^\s{4}last_error\s+VARCHAR\((\d+)\)", _split_sql_statements(_sql())[0], re.M)
    assert int(width) == ScorerHeartbeat.__table__.c.last_error.type.length == job.ERROR_CHARS


def test_the_constraints_match_the_orm_model():
    body = _split_sql_statements(_sql())[0]
    named = set(re.findall(r"CONSTRAINT (\w+)", body))
    assert named == {c.name for c in ScorerHeartbeat.__table__.constraints if c.name}


def test_the_table_lives_in_the_scoring_schema_with_one_row_per_agent():
    assert ScorerHeartbeat.__table__.fullname == "scoring.scorer_heartbeat"
    assert [c.name for c in ScorerHeartbeat.__table__.primary_key.columns] == ["agent_id"]
    assert re.search(r"^\s{4}agent_id\s+TEXT\s+PRIMARY KEY,$", _split_sql_statements(_sql())[0], re.M)


@pytest.mark.parametrize(
    "bad",
    [
        dict(last_status="bogus", last_error=None),  # not a status
        dict(last_status="error", last_error=None),  # an error names its class
        dict(last_status="ok", last_error="RuntimeError"),  # an ok pass has no error
    ],
)
async def test_the_check_constraints_refuse_what_the_scorer_never_writes(db, bad):
    row = ScorerHeartbeat(
        agent_id=f"test-heartbeat-{uuid.uuid4().hex[:8]}", scorer_version=1, last_run_at=datetime.now(UTC), **bad
    )
    with pytest.raises(IntegrityError):
        async with db.session() as s:
            s.add(row)
            await s.commit()


@pytest.mark.postgres_only
async def test_the_migration_is_idempotent(db):
    async with db.engine.begin() as conn:
        for _ in range(2):  # the second run is what a restart after a partly applied deploy does
            for statement in _split_sql_statements(_sql()):
                await conn.execute(text(statement))
