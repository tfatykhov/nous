"""F112 MCP client PR 1: migration 109 and the registry of servers and tools (SQLite by default, Postgres in CI)."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from nous.mcp_client.config import ServerConfig, ToolConfig
from nous.mcp_client.registry import McpRegistry
from nous.storage.migrator import _split_sql_statements
from nous.storage.models import McpServer, McpServerTool

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "sql" / "migrations" / "109_mcp_client.sql"
SCHEMA = {"type": "object", "properties": {"q": {"type": "string"}}}


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _code() -> str:
    return "\n".join(line for line in _sql().splitlines() if not line.lstrip().startswith("--"))


def test_the_migration_is_two_create_tables():
    servers, tools = _split_sql_statements(_sql())
    assert servers.startswith("CREATE TABLE IF NOT EXISTS nous_system.mcp_servers ")
    assert tools.startswith("CREATE TABLE IF NOT EXISTS nous_system.mcp_server_tools ")
    assert "UNIQUE (agent_id, name)" in servers and "UNIQUE (agent_id, server_id, tool)" in tools
    assert "CHECK (permission IN ('allow', 'deny', 'ask'))" in tools


def test_the_migration_creates_only_the_tables_and_holds_no_secret():
    code = _code()
    for word in ("GRANT", "REVOKE", "OWNER", "ROLE", "ALTER", "DROP", "AUTHORIZATION"):
        assert not re.search(rf"\b{word}\b", code.upper()), word
    assert not re.search(r"scoring\.|brain\.|heart\.|public\.", code, re.I)
    assert not re.search(r"secret|token|password|header", code, re.I)


def test_the_migration_has_nothing_the_splitter_or_text_would_mangle():
    sql = _sql()
    assert "/*" not in sql and "$$" not in sql and "%" not in _code()
    assert not re.search(r"(?<!:):[A-Za-z_]", _code())  # text() reads :name as a bind parameter
    assert all(";" not in line for line in sql.splitlines() if line.lstrip().startswith("--"))


def test_the_columns_match_the_orm_models():
    for statement, model in zip(_split_sql_statements(_sql()), (McpServer, McpServerTool), strict=True):
        sql_columns = set(re.findall(r"^\s{4}([a-z_]+)\s+(?:UUID|TEXT|BOOLEAN|INT|JSONB|TIMESTAMPTZ)", statement, re.M))
        assert sql_columns == {c.name for c in model.__table__.columns}, model.__tablename__
    assert McpServer.__table__.fullname == "nous_system.mcp_servers"
    assert McpServerTool.__table__.fullname == "nous_system.mcp_server_tools"


@pytest.mark.postgres_only
async def test_the_checks_hold_on_postgres(session):
    agent = _agent()
    insert = text("INSERT INTO nous_system.mcp_servers (agent_id, name, url) VALUES (:a, :n, :u)")
    await session.execute(insert, {"a": agent, "n": "alpha", "u": "https://a.example.com/mcp"})
    for name, url in (("a__b", "https://a.example.com/mcp"), ("Alpha", "https://a.example.com/mcp"),
                      ("plain", "http://a.example.com/mcp"), ("alpha", "https://b.example.com/mcp")):
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                await session.execute(insert, {"a": agent, "n": name, "u": url})
    tool = text(
        "INSERT INTO nous_system.mcp_server_tools (agent_id, server_id, tool, input_schema, permission) "
        "SELECT :a, id, :t, CAST('{}' AS jsonb), :p FROM nous_system.mcp_servers WHERE agent_id = :a"
    )
    await session.execute(tool, {"a": agent, "t": "one", "p": "allow"})
    for name, permission in (("two", "maybe"), ("one", "deny")):  # an unknown permission; the same tool twice
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                await session.execute(tool, {"a": agent, "t": name, "p": permission})


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
