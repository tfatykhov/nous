"""Migration 123 (nous_system.entity_profile): the file the migrator runs matches the ORM model, creates only the
table and its index, and keeps one row per agent and entity key."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from nous.storage.migrator import _split_sql_statements
from nous.storage.models import EntityProfile

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "sql" / "migrations" / "123_entity_profile.sql"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _code() -> str:
    return "\n".join(ln for ln in _sql().splitlines() if not ln.lstrip().startswith("--"))


def test_the_migration_is_one_create_table_and_one_index():
    table, index = _split_sql_statements(_sql())
    assert table.startswith("CREATE TABLE IF NOT EXISTS nous_system.entity_profile (")
    assert index.startswith("CREATE INDEX IF NOT EXISTS ix_entity_profile_status ON nous_system.entity_profile ")


def test_the_migration_creates_only_the_table_and_its_index():
    # Grants belong to scripts/ops/db_roles.sql: the migrator runs as the application role.
    code = _code().upper()
    for word in ("GRANT", "REVOKE", "OWNER", "ROLE", "ALTER", "DROP", "AUTHORIZATION", "DELETE"):
        assert not re.search(rf"\b{word}\b", code), word
    assert not re.search(r"scoring\.|brain\.|heart\.|public\.", _code(), re.I)


def test_the_migration_has_nothing_the_splitter_or_text_would_mangle():
    sql = _sql()
    assert "/*" not in sql and "$$" not in sql
    assert not re.search(r"(?<!:):[A-Za-z_]", _code())  # text() reads :name as a bind parameter
    assert all(";" not in ln for ln in sql.splitlines() if ln.lstrip().startswith("--"))


def test_the_columns_match_the_orm_model():
    body = _split_sql_statements(_sql())[0]
    sql_columns = set(re.findall(r"^\s{4}([a-z_]+)\s+(?:VARCHAR|JSONB|TEXT|INT|BOOLEAN|TIMESTAMPTZ)", body, re.M))
    assert sql_columns == {c.name for c in EntityProfile.__table__.columns}
    assert EntityProfile.__table__.fullname == "nous_system.entity_profile"
    assert [c.name for c in EntityProfile.__table__.primary_key.columns] == ["agent_id", "entity_key"]


def test_the_reader_role_is_not_given_the_table():
    roles = (ROOT / "scripts" / "ops" / "db_roles.sql").read_text(encoding="utf-8")
    assert "entity_profile" not in roles


@pytest.mark.postgres_only
async def test_the_reader_role_has_no_privilege_on_the_table_and_the_app_role_has(db):
    """Where the reader role exists (the DB roles job), it holds no privilege on the table. The login the tests run as
    reads and writes it."""
    async with db.engine.connect() as conn:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            mine = await conn.scalar(
                text("SELECT has_table_privilege(current_user, 'nous_system.entity_profile', :p)"), {"p": privilege}
            )
            assert mine is True, privilege
        exists = await conn.scalar(text("SELECT count(*) FROM pg_roles WHERE rolname = 'nous_reader'"))
        if not exists:
            pytest.skip("no nous_reader role in this database")
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            granted = await conn.scalar(
                text("SELECT has_table_privilege('nous_reader', 'nous_system.entity_profile', :p)"), {"p": privilege}
            )
            assert granted is False, privilege


@pytest.mark.postgres_only
async def test_one_row_per_agent_and_key_with_the_defaults(db, session):
    agent = f"test-entity-profile-mig-{uuid.uuid4().hex[:8]}"
    insert = text(
        "INSERT INTO nous_system.entity_profile (agent_id, entity_key, status, source_hash)"
        " VALUES (:a, :k, 'failed', '')"
    )
    await session.execute(insert, {"a": agent, "k": "zephyrine"})
    await session.execute(insert, {"a": agent, "k": "quillon"})
    row = (
        await session.execute(
            text(
                "SELECT lines, previous_lines, rejected, tokens, fact_count, source_count, active"
                " FROM nous_system.entity_profile WHERE agent_id = :a AND entity_key = :k"
            ),
            {"a": agent, "k": "zephyrine"},
        )
    ).one()
    assert tuple(row) == ([], [], [], 0, 0, 0, True)
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(insert, {"a": agent, "k": "zephyrine"})
