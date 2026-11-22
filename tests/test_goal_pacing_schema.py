"""Goal pacing: what migration 120 holds by itself (``brain.goals.daily_at_minute``, NULL or a minute of the day),
that the file changes that table only and grants nothing, that it applies twice, and that the ORM names its check."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import CheckConstraint, text
from test_goals_schema import _agent, _insert_goal

from nous.storage.migrator import _split_sql_statements
from nous.storage.models import Goal

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "sql" / "migrations" / "120_goal_pacing.sql"
CHECK = "chk_goals_daily_at_minute"


def test_the_migration_changes_brain_goals_only_and_grants_nothing():
    sql = MIGRATION.read_text(encoding="utf-8")
    statements = _split_sql_statements(sql)
    assert statements and all(statement.startswith("ALTER TABLE brain.goals ") for statement in statements)
    code = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    for word in ("GRANT", "REVOKE", "OWNER", "ROLE", "AUTHORIZATION", "CREATE", "UPDATE", "DELETE", "INSERT"):
        assert not re.search(rf"\b{word}\b", code.upper()), word
    assert all(";" not in line for line in sql.splitlines() if line.lstrip().startswith("--"))
    added = re.findall(r"ADD CONSTRAINT (\w+)", code)
    dropped = re.findall(r"DROP CONSTRAINT IF EXISTS (\w+)", code)
    assert added == [CHECK] and set(added) <= set(dropped)  # dropped before it is added: the file applies twice
    assert "ADD COLUMN IF NOT EXISTS daily_at_minute INTEGER" in code


def test_the_model_has_the_column_and_its_check():
    column = Goal.__table__.c.daily_at_minute
    assert column.nullable and column.server_default is None
    checks = {c.name: str(c.sqltext) for c in Goal.__table__.constraints if isinstance(c, CheckConstraint)}
    assert checks[CHECK] == "daily_at_minute IS NULL OR daily_at_minute BETWEEN 0 AND 1439"


@pytest.mark.postgres_only
async def test_the_database_has_the_check_the_orm_names(db):
    async with db.session() as s:
        found = (
            await s.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = 'brain.goals'::regclass AND conname = :name"
                ),
                {"name": CHECK},
            )
        ).scalar_one()
    assert "daily_at_minute" in found and "1439" in found


@pytest.mark.postgres_only
async def test_the_migration_applies_again_with_no_change(db):
    agent = _agent()
    paced = await _insert_goal(db, agent, daily_at_minute=540)
    plain = await _insert_goal(db, agent)
    async with db.session() as s:
        for statement in _split_sql_statements(MIGRATION.read_text(encoding="utf-8")):
            await s.execute(text(statement))
        await s.commit()
    async with db.session() as s:
        rows = dict(
            (
                await s.execute(
                    text("SELECT id, daily_at_minute FROM brain.goals WHERE id IN (:a, :b)"), {"a": paced, "b": plain}
                )
            ).all()
        )
    assert rows == {paced: 540, plain: None}
