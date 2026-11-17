"""A goal that may run any command: what migration 118 holds by itself (a mode is 'contained' or 'commands', the hours
chosen go with 'commands' and only with it, an end time never past the deadline), that no check names the earlier mode
value, that the file applies twice, and that the ORM names the same checks."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import CheckConstraint, text
from test_goals_schema import _agent, _insert_goal, _refused

from nous.storage.migrator import _split_sql_statements
from nous.storage.models import Goal

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "sql" / "migrations" / "118_goal_commands_mode.sql"
MODE_CHECKS = {"chk_goals_mode_value", "chk_goals_mode", "chk_goals_mode_end", "goals_mode_hours_check"}


def test_the_migration_changes_brain_goals_only_and_grants_nothing():
    sql = MIGRATION.read_text(encoding="utf-8")
    statements = _split_sql_statements(sql)
    assert all(statement.startswith("ALTER TABLE brain.goals ") for statement in statements)
    code = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    for word in ("GRANT", "REVOKE", "OWNER", "ROLE", "AUTHORIZATION", "CREATE", "UPDATE", "DELETE", "INSERT"):
        assert not re.search(rf"\b{word}\b", code.upper()), word
    assert all(";" not in line for line in sql.splitlines() if line.lstrip().startswith("--"))
    added = re.findall(r"ADD CONSTRAINT (\w+)", code)
    dropped = re.findall(r"DROP CONSTRAINT IF EXISTS (\w+)", code)
    assert added and set(added) <= set(dropped)  # each one is dropped before it is added: the file applies twice


@pytest.mark.postgres_only
async def test_no_check_on_brain_goals_names_the_earlier_mode_value(db):
    async with db.session() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = 'brain.goals'::regclass AND contype = 'c'"
                )
            )
        ).all()
    assert rows
    assert not [name for name, definition in rows if "full_autonomy" in definition]
    names = {name for name, _definition in rows}
    assert MODE_CHECKS <= names and "goals_mode_check" not in names


@pytest.mark.postgres_only
async def test_the_orm_names_the_checks_the_database_has(db):
    orm = {c.name for c in Goal.__table__.constraints if isinstance(c, CheckConstraint)}
    async with db.session() as s:
        database = set(
            (
                await s.execute(
                    text("SELECT conname FROM pg_constraint WHERE conrelid = 'brain.goals'::regclass AND contype = 'c'")
                )
            ).scalars()
        )
    assert MODE_CHECKS <= orm
    assert {name for name in orm if "mode" in name} == {name for name in database if "mode" in name}
    assert "goals_mode_check" not in orm


@pytest.mark.postgres_only
async def test_a_mode_is_contained_or_commands(db):
    agent = _agent()
    await _refused(db, agent, mode="full_autonomy", mode_hours=2, mode_until=datetime.now(UTC) + timedelta(hours=1))
    await _refused(db, agent, mode="anything", mode_hours=2)
    await _insert_goal(db, agent, mode="commands", mode_hours=2)


@pytest.mark.postgres_only
async def test_the_hours_go_with_commands_and_only_with_it(db):
    agent = _agent()
    await _refused(db, agent, mode="commands")
    await _refused(db, agent, mode="contained", mode_hours=2)
    await _refused(db, agent, mode="contained", mode_until=datetime.now(UTC) + timedelta(hours=1))
    for hours in (0, 2161, -1):
        await _refused(db, agent, mode="commands", mode_hours=hours)
    for hours in (1, 2160):
        await _insert_goal(db, agent, mode="commands", mode_hours=hours)


@pytest.mark.postgres_only
async def test_an_end_time_is_never_past_the_deadline(db):
    agent = _agent()
    now = datetime.now(UTC)
    active = {"state": "active", "approved_at": now, "deadline_at": now + timedelta(hours=10)}
    await _refused(db, agent, mode="commands", mode_hours=24, mode_until=now + timedelta(hours=11), **active)
    await _insert_goal(db, agent, mode="commands", mode_hours=24, mode_until=now + timedelta(hours=10), **active)
    await _insert_goal(db, agent, mode="commands", mode_hours=24)  # a draft's choice: no end until it is approved


@pytest.mark.postgres_only
async def test_the_migration_applies_again_with_no_change(db):
    agent = _agent()
    goal_id = await _insert_goal(db, agent, mode="commands", mode_hours=5)
    async with db.session() as s:
        for statement in _split_sql_statements(MIGRATION.read_text(encoding="utf-8")):
            await s.execute(text(statement))
        await s.commit()
    async with db.session() as s:
        row = (await s.execute(text("SELECT mode, mode_hours FROM brain.goals WHERE id = :g"), {"g": goal_id})).one()
    assert tuple(row) == ("commands", 5)
