"""Migration 087 (scoring.task_scores): the file the migrator will run matches the ORM model the SQLite tests build."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import text

from nous.storage.migrator import _split_sql_statements
from nous.storage.models import TaskScore

pytestmark = pytest.mark.scoring_writer

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "sql" / "migrations" / "087_task_scores.sql"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _code() -> str:
    return "\n".join(ln for ln in _sql().splitlines() if not ln.lstrip().startswith("--"))


def test_the_migration_splits_into_a_schema_a_table_and_an_index():
    statements = _split_sql_statements(_sql())
    assert len(statements) == 3
    assert statements[0] == "CREATE SCHEMA IF NOT EXISTS scoring"
    assert statements[1].startswith("CREATE TABLE IF NOT EXISTS scoring.task_scores")
    assert statements[2].startswith("CREATE INDEX IF NOT EXISTS idx_task_scores_agent_version_finished")
    assert "ON scoring.task_scores" in statements[2]


def test_the_migration_creates_only_the_schema_and_the_table():
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
    body = _split_sql_statements(_sql())[1]
    sql_columns = set(
        re.findall(r"^\s{4}([a-z_]+)\s+(?:UUID|TEXT|VARCHAR|TIMESTAMPTZ|INTEGER|DOUBLE|BOOLEAN|JSONB)", body, re.M)
    )
    assert sql_columns == {c.name for c in TaskScore.__table__.columns}


def test_the_constraints_match_the_orm_model():
    body = _split_sql_statements(_sql())[1]
    named = set(re.findall(r"CONSTRAINT (\w+)", body))
    assert named == {c.name for c in TaskScore.__table__.constraints if c.name}


def test_the_table_lives_in_the_scoring_schema():
    assert TaskScore.__table__.schema == "scoring"
    assert TaskScore.__table__.fullname == "scoring.task_scores"


def test_the_index_matches_the_scoreboard_predicate_and_the_orm_model():
    # GET /dashboard/autonomy filters on agent_id, scorer_version and finished_at
    sql = _split_sql_statements(_sql())[2]
    assert "(agent_id, scorer_version, finished_at DESC)" in sql
    (index,) = TaskScore.__table__.indexes
    assert index.name == "idx_task_scores_agent_version_finished"
    assert [c.name for c in index.columns] == ["agent_id", "scorer_version", "finished_at"]


@pytest.mark.postgres_only
async def test_the_migration_is_idempotent(db):
    async with db.engine.begin() as conn:
        for _ in range(2):  # the second run is what a restart after a partly applied deploy does
            for statement in _split_sql_statements(_sql()):
                await conn.execute(text(statement))
