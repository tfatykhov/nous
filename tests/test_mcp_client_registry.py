"""F112 MCP client PR 1: migration 109 and the registry of servers and tools (SQLite by default, Postgres in CI)."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from nous.mcp_client.config import ServerConfig, ToolConfig
from nous.mcp_client.registry import McpRegistry
from nous.storage.models import McpServer, McpServerTool

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "sql" / "migrations" / "109_mcp_client.sql"
SCHEMA = {"type": "object", "properties": {"q": {"type": "string"}}}


def test_the_migration_creates_the_two_tables_and_nothing_else():
    sql = MIGRATION.read_text(encoding="utf-8")
    body = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    assert re.findall(r"CREATE TABLE IF NOT EXISTS (\S+)", body) == [
        "nous_system.mcp_servers",
        "nous_system.mcp_server_tools",
    ]
    assert "UNIQUE (agent_id, name)" in body and "UNIQUE (agent_id, server_id, tool)" in body
    assert "CHECK (permission IN ('allow', 'deny', 'ask'))" in body
    assert not re.search(r"\b(GRANT|REVOKE|OWNER TO)\b", body)
    assert not re.search(r"secret|token|password|header", body, re.IGNORECASE)
    for line in sql.splitlines():  # the migrator strips comment lines; a ';' in one has split statements before
        if line.lstrip().startswith("--"):
            assert ";" not in line, line


def test_the_models_match_the_migration():
    assert McpServer.__table__.fullname == "nous_system.mcp_servers"
    assert McpServerTool.__table__.fullname == "nous_system.mcp_server_tools"
    assert {c.name for c in McpServer.__table__.columns} == {
        "id", "agent_id", "name", "url", "transport", "enabled", "background", "call_timeout_seconds",
        "created_at", "updated_at",
    }
    assert {c.name for c in McpServerTool.__table__.columns} == {
        "id", "agent_id", "server_id", "tool", "description", "input_schema", "permission", "created_at",
        "updated_at",
    }


def _agent() -> str:
    return f"mcp-registry-{uuid.uuid4().hex[:8]}"


async def test_upsert_and_list_enabled(db):
    registry = McpRegistry(db, _agent())
    await registry.upsert_server(ServerConfig(name="alpha", url="https://alpha.example.com/mcp", background=True))
    await registry.upsert_tools(
        "alpha",
        [
            ToolConfig(server="alpha", tool="search", description="d", input_schema=SCHEMA, permission="allow"),
            ToolConfig(server="alpha", tool="write", description="w", input_schema=SCHEMA, permission="deny"),
        ],
    )
    await registry.upsert_server(ServerConfig(name="off", url="https://off.example.com/mcp", enabled=False))
    rows = await registry.list_enabled()
    assert [config.name for config, _tools in rows] == ["alpha"]
    config, tools = rows[0]
    assert config.background is True and config.url == "https://alpha.example.com/mcp"
    assert sorted((t.tool, t.permission) for t in tools) == [("search", "allow"), ("write", "deny")]
    assert tools[0].input_schema == SCHEMA

    # an update keeps one row per name and per tool
    await registry.upsert_server(ServerConfig(name="alpha", url="https://alpha.example.com/v2", call_timeout_seconds=9))
    assert await registry.set_permission("alpha", "write", "allow") is True
    assert await registry.set_permission("alpha", "missing", "allow") is False
    config, tools = (await registry.list_enabled())[0]
    assert (config.url, config.call_timeout_seconds, config.background) == ("https://alpha.example.com/v2", 9, False)
    assert sorted((t.tool, t.permission) for t in tools) == [("search", "allow"), ("write", "allow")]


async def test_a_row_that_no_longer_validates_is_skipped(db, caplog):
    agent = _agent()
    async with db.session() as session:
        session.add(McpServer(agent_id=agent, name="leaky", url="https://leaky.example.com/mcp?key=SECRET-55"))
        await session.commit()
    assert await McpRegistry(db, agent).list_enabled() == []
    assert all("SECRET-55" not in record.getMessage() for record in caplog.records)


async def test_rows_are_per_agent(db):
    a, b = McpRegistry(db, _agent()), McpRegistry(db, _agent())
    await a.upsert_server(ServerConfig(name="alpha", url="https://alpha.example.com/mcp"))
    assert [config.name for config, _tools in await a.list_enabled()] == ["alpha"]
    assert await b.list_enabled() == []
