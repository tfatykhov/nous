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


def _added_later(table: str) -> set[str]:
    """The columns migrations after 109 add to ``table`` (``ALTER TABLE <table> ADD COLUMN [IF NOT EXISTS] <name>``)."""
    added: set[str] = set()
    pattern = rf"ALTER TABLE {re.escape(table)}\s+ADD COLUMN(?:\s+IF NOT EXISTS)?\s+([a-z_]+)"
    for path in sorted(MIGRATION.parent.glob("*.sql")):
        if int(path.name.split("_", 1)[0]) <= 109:
            continue
        for statement in _split_sql_statements(path.read_text(encoding="utf-8")):
            added.update(re.findall(pattern, statement, re.I))
    return added


def test_the_columns_match_the_orm_models():
    # 109 creates both tables; 112 adds mcp_servers.images. What they make together is what the models hold.
    assert _added_later("nous_system.mcp_servers") == {"images"}
    assert _added_later("nous_system.mcp_server_tools") == set()
    for statement, model in zip(_split_sql_statements(_sql()), (McpServer, McpServerTool), strict=True):
        sql_columns = set(re.findall(r"^\s{4}([a-z_]+)\s+(?:UUID|TEXT|BOOLEAN|INT|JSONB|TIMESTAMPTZ)", statement, re.M))
        sql_columns |= _added_later(model.__table__.fullname)
        assert sql_columns == {c.name for c in model.__table__.columns}, model.__tablename__
    assert McpServer.__table__.fullname == "nous_system.mcp_servers"
    assert McpServerTool.__table__.fullname == "nous_system.mcp_server_tools"


# --- migration 112: the images switch ---------------------------------------------------------------------------------

IMAGES_MIGRATION = ROOT / "sql" / "migrations" / "112_mcp_server_images.sql"
IMAGES_STATEMENT = "ALTER TABLE nous_system.mcp_servers ADD COLUMN IF NOT EXISTS images BOOLEAN NOT NULL DEFAULT FALSE"


def _images_sql() -> str:
    return IMAGES_MIGRATION.read_text(encoding="utf-8")


def _images_code() -> str:
    return "\n".join(line for line in _images_sql().splitlines() if not line.lstrip().startswith("--"))


def test_migration_112_is_one_idempotent_add_column():
    (statement,) = _split_sql_statements(_images_sql())
    assert statement == IMAGES_STATEMENT


def test_migration_112_adds_only_the_column():
    code = _images_code()
    for word in ("GRANT", "REVOKE", "OWNER", "ROLE", "DROP", "AUTHORIZATION", "CREATE", "UPDATE", "DELETE"):
        assert not re.search(rf"\b{word}\b", code.upper()), word
    assert not re.search(r"scoring\.|brain\.|heart\.|public\.", code, re.I)


def test_migration_112_has_nothing_the_splitter_or_text_would_mangle():
    sql = _images_sql()
    assert "/*" not in sql and "$$" not in sql and "%" not in _images_code()
    assert not re.search(r"(?<!:):[A-Za-z_]", _images_code())  # text() reads :name as a bind parameter
    assert all(";" not in line for line in sql.splitlines() if line.lstrip().startswith("--"))


@pytest.mark.postgres_only
async def test_migration_112_column_on_postgres(session):
    column = (
        await session.execute(
            text(
                "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
                "WHERE table_schema = 'nous_system' AND table_name = 'mcp_servers' AND column_name = 'images'"
            )
        )
    ).one()
    assert (column.data_type, column.is_nullable, column.column_default) == ("boolean", "NO", "false")
    agent = _agent()
    insert = text("INSERT INTO nous_system.mcp_servers (agent_id, name, url) VALUES (:a, 'alpha', :u) RETURNING images")
    assert (await session.execute(insert, {"a": agent, "u": "https://a.example.com/mcp"})).scalar_one() is False
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                text("UPDATE nous_system.mcp_servers SET images = NULL WHERE agent_id = :a"), {"a": agent}
            )
    for _ in range(2):  # the migration's statement again, twice: the column is there, nothing changes
        await session.execute(text(IMAGES_STATEMENT))
    count = text(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_schema = 'nous_system' AND table_name = 'mcp_servers' AND column_name = 'images'"
    )
    assert (await session.execute(count)).scalar_one() == 1


@pytest.mark.postgres_only
async def test_the_checks_hold_on_postgres(session):
    agent = _agent()
    insert = text("INSERT INTO nous_system.mcp_servers (agent_id, name, url) VALUES (:a, :n, :u)")
    await session.execute(insert, {"a": agent, "n": "alpha", "u": "https://a.example.com/mcp"})
    for name, url in (("a__b", "https://a.example.com/mcp"), ("Alpha", "https://a.example.com/mcp"),
                      ("plain", "ftp://a.example.com/mcp"), ("alpha", "https://b.example.com/mcp")):
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


TOOLS = [ToolConfig(server="alpha", tool="search", description="d", input_schema=SCHEMA, permission="allow")]


async def _images(registry: McpRegistry) -> dict[str, bool]:
    return {config.name: config.images for config, _tools in await registry.list_all()}


async def test_the_images_switch_is_off_on_insert_and_set_only_by_its_own_field(db):
    registry = McpRegistry(db, _agent())
    await registry.upsert_server(ServerConfig(name="alpha", url="https://alpha.example.com/mcp"))
    await registry.replace_server(ServerConfig(name="beta", url="https://beta.example.com/mcp"), [])
    assert await _images(registry) == {"alpha": False, "beta": False}
    assert await registry.update_server("alpha", images=True) is True
    assert await _images(registry) == {"alpha": True, "beta": False}
    ((config, _tools),) = [row for row in await registry.list_enabled() if row[0].name == "alpha"]
    assert config.images is True and config.background is False and config.enabled is True
    # Other fields, images=None: left as it is.
    assert await registry.update_server("alpha", background=True, call_timeout_seconds=9, images=None) is True
    assert await registry.update_server("alpha", enabled=False) is True
    ((config, _tools),) = [row for row in await registry.list_all() if row[0].name == "alpha"]
    assert (config.images, config.background, config.call_timeout_seconds, config.enabled) == (True, True, 9, False)
    assert await registry.update_server("alpha", images=False) is True
    assert await _images(registry) == {"alpha": False, "beta": False}
    assert await registry.update_server("zeta", images=True) is False


async def test_replacing_or_upserting_a_server_writes_its_configs_switch(db):
    registry = McpRegistry(db, _agent())
    url = "https://alpha.example.com/mcp"
    await registry.replace_server(ServerConfig(name="alpha", url=url), TOOLS)
    await registry.update_server("alpha", images=True)
    # A replace with a config that does not set the switch (as the routes build one) turns it off.
    created, _deleted = await registry.replace_server(ServerConfig(name="alpha", url=url, background=True), TOOLS)
    assert created is False and await _images(registry) == {"alpha": False}
    await registry.replace_server(ServerConfig(name="alpha", url=url, images=True), TOOLS)
    assert await _images(registry) == {"alpha": True}
    await registry.upsert_server(ServerConfig(name="alpha", url="https://alpha.example.com/v2"))
    assert await _images(registry) == {"alpha": False}


async def test_rows_are_per_agent(db):
    a, b = McpRegistry(db, _agent()), McpRegistry(db, _agent())
    await a.upsert_server(ServerConfig(name="alpha", url="https://alpha.example.com/mcp"))
    assert [config.name for config, _tools in await a.list_enabled()] == ["alpha"]
    assert await b.list_enabled() == []
