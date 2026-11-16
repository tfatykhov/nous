"""Per-goal tool grants: what migration 117 holds by itself (the MCP identity columns both ways, the revoked pair, one
active grant of a tool per goal, the goal it belongs to) and that it applies again with no change."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from test_goals_schema import _agent, _insert_goal

from nous.storage.migrator import _split_sql_statements

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "sql" / "migrations" / "117_goal_tool_grants.sql"
DIGEST = "ab" * 32
MCP = {
    "server_name": "notes",
    "server_host": "notes.example.com",
    "server_id": "00000000-0000-4000-8000-000000000001",
    "url_sha256": DIGEST,
    "remote_tool": "search",
}
NO_MCP = dict.fromkeys(MCP)


async def _grant(db, agent: str, goal_id: uuid.UUID, tool: str, **over) -> uuid.UUID:
    values = {**NO_MCP, "granted_by": "owner", **over, "agent_id": agent, "goal_id": goal_id, "tool": tool}
    columns = ", ".join(values)
    params = ", ".join(f":{name}" for name in values)
    async with db.session() as s:
        grant_id = (
            await s.execute(
                text(f"INSERT INTO brain.goal_tool_grants ({columns}) VALUES ({params}) RETURNING id"), values
            )
        ).scalar_one()
        await s.commit()
    return grant_id


async def _refused(db, agent: str, goal_id: uuid.UUID, tool: str, **over) -> None:
    with pytest.raises(IntegrityError):
        await _grant(db, agent, goal_id, tool, **over)


async def _revoke(db, grant_id: uuid.UUID) -> None:
    async with db.session() as s:
        await s.execute(
            text("UPDATE brain.goal_tool_grants SET revoked_by = 'proxy', revoked_at = now() WHERE id = :id"),
            {"id": grant_id},
        )
        await s.commit()


def test_the_migration_creates_one_table_and_two_indexes_and_grants_nothing():
    sql = MIGRATION.read_text(encoding="utf-8")
    statements = _split_sql_statements(sql)
    assert [statement.split("(")[0].strip() for statement in statements] == [
        "CREATE TABLE IF NOT EXISTS brain.goal_tool_grants",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_goal_tool_grants_active\n    ON brain.goal_tool_grants",
        "CREATE INDEX IF NOT EXISTS idx_goal_tool_grants_goal ON brain.goal_tool_grants",
    ]
    code = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    for word in ("GRANT", "REVOKE", "OWNER", "ROLE", "ALTER", "DROP", "AUTHORIZATION"):
        assert not re.search(rf"\b{word}\b", code.upper()), word
    assert all(";" not in line for line in sql.splitlines() if line.lstrip().startswith("--"))
    assert not re.search(r"(?<!:):[A-Za-z_]", code)


@pytest.mark.postgres_only
async def test_an_mcp_tool_needs_all_five_identity_columns_and_any_other_tool_none(db):
    agent = _agent()
    goal_id = await _insert_goal(db, agent)
    await _grant(db, agent, goal_id, "mcp__notes__search", **MCP)
    await _grant(db, agent, goal_id, "send_email")
    for column in MCP:
        await _refused(db, agent, goal_id, "mcp__notes__find", **{**MCP, column: None})
        await _refused(db, agent, goal_id, "send_file", **{column: MCP[column]})


@pytest.mark.postgres_only
async def test_the_names_and_values_are_bounded(db):
    agent = _agent()
    goal_id = await _insert_goal(db, agent)
    await _refused(db, agent, goal_id, "")
    await _refused(db, agent, goal_id, "send email")
    await _refused(db, agent, goal_id, "x" * 201)
    await _refused(db, agent, goal_id, "mcp__notes__a", **{**MCP, "url_sha256": "AB" * 32})
    await _refused(db, agent, goal_id, "mcp__notes__b", **{**MCP, "url_sha256": "ab"})
    await _refused(db, agent, goal_id, "mcp__notes__c", **{**MCP, "server_host": ""})
    await _refused(db, agent, goal_id, "mcp__notes__d", **{**MCP, "remote_tool": ""})
    await _refused(db, agent, goal_id, "send_email", granted_by="")
    await _refused(db, agent, goal_id, "send_email", granted_by="x" * 41)


@pytest.mark.postgres_only
async def test_revoked_by_and_revoked_at_move_together(db):
    agent = _agent()
    goal_id = await _insert_goal(db, agent)
    await _refused(db, agent, goal_id, "send_email", revoked_by="owner")
    async with db.session() as s:
        with pytest.raises(IntegrityError):
            await s.execute(
                text(
                    "INSERT INTO brain.goal_tool_grants (agent_id, goal_id, tool, granted_by, revoked_at) "
                    "VALUES (:a, :g, 'send_email', 'owner', now())"
                ),
                {"a": agent, "g": goal_id},
            )


@pytest.mark.postgres_only
async def test_one_active_grant_of_a_tool_per_goal_and_a_new_one_after_a_revoke(db):
    agent = _agent()
    goal_id = await _insert_goal(db, agent)
    other = await _insert_goal(db, agent)
    first = await _grant(db, agent, goal_id, "send_email")
    await _refused(db, agent, goal_id, "send_email")
    await _grant(db, agent, other, "send_email")  # another goal's grant of the same tool
    await _revoke(db, first)
    second = await _grant(db, agent, goal_id, "send_email")
    await _revoke(db, second)
    await _grant(db, agent, goal_id, "send_email")
    async with db.session() as s:
        rows = (
            await s.execute(
                text("SELECT count(*), count(revoked_at) FROM brain.goal_tool_grants WHERE goal_id = :g"),
                {"g": goal_id},
            )
        ).one()
    assert tuple(rows) == (3, 2)


@pytest.mark.postgres_only
async def test_a_grant_belongs_to_a_goal_that_exists(db):
    await _refused(db, _agent(), uuid.uuid4(), "send_email")


@pytest.mark.postgres_only
async def test_the_migration_applies_again_with_no_change(db):
    agent = _agent()
    goal_id = await _insert_goal(db, agent)
    await _grant(db, agent, goal_id, "send_email")
    async with db.session() as s:
        for statement in _split_sql_statements(MIGRATION.read_text(encoding="utf-8")):
            await s.execute(text(statement))
        await s.commit()
    async with db.session() as s:
        count = await s.scalar(text("SELECT count(*) FROM brain.goal_tool_grants WHERE goal_id = :g"), {"g": goal_id})
    assert count == 1
