"""F118 PR 3: migration 122, nous_system.judge_second_settings, the owner's settings of the second judge.

The database test runs the migration twice inside a transaction that is rolled back, so its rows never outlive the
test. One row per agent and judge name; each setting is NULL for the server's value; the sealed key's four columns
are all set or all NULL.
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
MIGRATION = ROOT / "sql" / "migrations" / "122_judge_second_settings.sql"

COLUMNS = {
    "agent_id": "text",
    "judge_name": "text",
    "enabled": "boolean",
    "model": "text",
    "daily_calls": "integer",
    "key_id": "text",
    "nonce": "bytea",
    "ciphertext": "bytea",
    "key_set_at": "timestamp with time zone",
    "updated_at": "timestamp with time zone",
    "updated_by": "text",
}
SET_AT = datetime(2026, 11, 23, 9, 0, tzinfo=UTC)
SEALED = {"key_id": "0123456789abcdef", "nonce": b"\x00" * 12, "ciphertext": b"\x01" * 40, "key_set_at": SET_AT}


def test_the_migration_creates_one_table_and_grants_nothing():
    sql = MIGRATION.read_text(encoding="utf-8")
    statements = _split_sql_statements(sql)
    assert [s.split("(")[0].strip() for s in statements] == [
        "CREATE TABLE IF NOT EXISTS nous_system.judge_second_settings",
    ]
    code = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    keywords = re.sub(r"'[^']*'", "''", code)
    for word in ("GRANT", "REVOKE", "OWNER", "ROLE", "ALTER", "DROP", "AUTHORIZATION", "REFERENCES"):
        assert not re.search(rf"\b{word}\b", keywords.upper()), word
    # The migrator splits on semicolons: none may sit in a comment.
    assert all(";" not in line for line in sql.splitlines() if line.lstrip().startswith("--"))
    # No column for the key's text, its length or a part of it.
    assert not re.search(r"\b(plain|key_text|key_len|last4|suffix|prefix)\w*", code, re.I)


async def _insert(conn, agent: str, **over) -> None:
    values = {"agent_id": agent, "judge_name": "jev", "updated_by": "owner", **over}
    columns = ", ".join(values)
    params = ", ".join(f":{name}" for name in values)
    await conn.execute(text(f"INSERT INTO nous_system.judge_second_settings ({columns}) VALUES ({params})"), values)


async def _refused(conn, agent: str, **over) -> None:
    savepoint = await conn.begin_nested()
    with pytest.raises(IntegrityError):
        await _insert(conn, agent, **over)
    await savepoint.rollback()


@pytest.mark.postgres_only
async def test_the_migration_applies_twice_and_its_constraints_hold(db):
    agent = f"test-f118-settings-{uuid.uuid4().hex[:8]}"
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
                            "WHERE table_schema = 'nous_system' AND table_name = 'judge_second_settings'"
                        )
                    )
                ).all()
            )
            assert types == COLUMNS

            # Every setting NULL: the server's values. updated_at is filled.
            await _insert(conn, agent)
            row = (
                await conn.execute(
                    text(
                        "SELECT enabled, model, daily_calls, key_id, updated_at IS NOT NULL, updated_by "
                        "FROM nous_system.judge_second_settings WHERE agent_id = :a"
                    ),
                    {"a": agent},
                )
            ).one()
            assert tuple(row) == (None, None, None, None, True, "owner")

            # One row per agent and judge name.
            await _refused(conn, agent)
            other = f"{agent}-other"
            await _insert(conn, other, enabled=True, model="jev-1.14.0", daily_calls=0, updated_by="proxy", **SEALED)
            await _insert(conn, f"{agent}-a", model="jev-12.0.3", daily_calls=5000)

            # Anything else is refused.
            await _refused(conn, f"{agent}-b", judge_name="other")
            await _refused(conn, f"{agent}-b", updated_by="agent")
            await _refused(conn, f"{agent}-b", updated_by=None)
            for model in ("jev-latest", "jev-1.14", "jev-1.14.0-rc1", "Jev-1.14.0", "jev-1.14.0\n", " jev-1.14.0", ""):
                await _refused(conn, f"{agent}-b", model=model)
            for calls in (-1, 5001):
                await _refused(conn, f"{agent}-b", daily_calls=calls)
            # The sealed key's columns: all four or none.
            for missing in SEALED:
                await _refused(conn, f"{agent}-b", **{**SEALED, missing: None})
            await _refused(conn, f"{agent}-b", key_id="0123456789abcdef")

            count = await conn.scalar(
                text("SELECT count(*) FROM nous_system.judge_second_settings WHERE agent_id LIKE :a"),
                {"a": f"{agent}%"},
            )
            assert count == 3
        finally:
            await trans.rollback()


@pytest.mark.postgres_only
async def test_the_reader_role_has_no_privilege_on_the_table_and_the_app_role_has(db):
    """Where the reader role exists (the DB roles job), it holds no privilege on the table. Elsewhere the role is
    absent and the migration names no role at all (the first test). The login the tests run as reads and writes it."""
    async with db.engine.connect() as conn:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            mine = await conn.scalar(
                text("SELECT has_table_privilege(current_user, 'nous_system.judge_second_settings', :p)"),
                {"p": privilege},
            )
            assert mine is True, privilege
        exists = await conn.scalar(text("SELECT count(*) FROM pg_roles WHERE rolname = 'nous_reader'"))
        if not exists:
            pytest.skip("no nous_reader role in this database")
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            granted = await conn.scalar(
                text("SELECT has_table_privilege('nous_reader', 'nous_system.judge_second_settings', :p)"),
                {"p": privilege},
            )
            assert granted is False, privilege
