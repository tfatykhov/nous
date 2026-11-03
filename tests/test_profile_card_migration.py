"""Migration 096 (nous_system.profile_card): the file the migrator runs matches the ORM model, creates only the table
and keeps one card per agent."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from nous.storage.migrator import _split_sql_statements
from nous.storage.models import ProfileCard

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "sql" / "migrations" / "096_profile_card.sql"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _code() -> str:
    return "\n".join(ln for ln in _sql().splitlines() if not ln.lstrip().startswith("--"))


def test_the_migration_is_one_create_table():
    (statement,) = _split_sql_statements(_sql())
    assert statement.startswith("CREATE TABLE IF NOT EXISTS nous_system.profile_card")


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


def test_the_columns_match_the_orm_model():
    body = _split_sql_statements(_sql())[0]
    sql_columns = set(re.findall(r"^\s{4}([a-z_]+)\s+(?:VARCHAR|JSONB|TEXT|INT|TIMESTAMPTZ)", body, re.M))
    assert sql_columns == {c.name for c in ProfileCard.__table__.columns}
    assert ProfileCard.__table__.fullname == "nous_system.profile_card"
    assert [c.name for c in ProfileCard.__table__.primary_key.columns] == ["agent_id"]


@pytest.mark.postgres_only
async def test_one_card_per_agent(db, session):
    agent = f"test-profile-card-mig-{uuid.uuid4().hex[:8]}"
    insert = text("INSERT INTO nous_system.profile_card (agent_id, source_hash) VALUES (:a, 'h')")
    await session.execute(insert, {"a": agent})
    row = (
        await session.execute(
            text(
                "SELECT entries, previous_entries, rejected, tokens FROM nous_system.profile_card WHERE agent_id = :a"
            ),
            {"a": agent},
        )
    ).one()
    assert (row.entries, row.previous_entries, row.rejected, row.tokens) == ([], [], [], 0)
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(insert, {"a": agent})
