"""F112 MCP client PR 1: the owner's seed command, over a fake session (no network) and the tests' database."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any

import pytest
from mcp.types import Tool

from nous.config import Settings
from nous.mcp_client import seed, transport
from nous.mcp_client.bridge import TOOLS_PER_SERVER, prepare_tool, serialize_schema
from nous.mcp_client.registry import McpRegistry

URL = "https://alpha.example.com/mcp"
SEARCH = {"type": "object", "properties": {"q": {"type": "string", "description": "the\nquery​"}}}


class _Session:
    def __init__(self, tools: list[Tool]) -> None:
        self.tools = tools

    async def list_tools(self) -> list[Tool]:
        return self.tools


class _Db:
    """The tests' database, with connect and disconnect left to the fixture."""

    def __init__(self, db: Any) -> None:
        self._db = db

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    def session(self) -> Any:
        return self._db.session()


@pytest.fixture
def listing(monkeypatch, db):
    """Set the tools the fake server lists; the seed's database is the tests' one."""
    current: list[Tool] = []

    @asynccontextmanager
    async def open_session(url: str, allow: frozenset[str], *, max_response_bytes: int):
        assert url == URL
        yield _Session(list(current))

    monkeypatch.setattr(transport, "open_session", open_session)
    monkeypatch.setattr(seed, "Database", lambda settings: _Db(db))

    def set_tools(*tools: Tool) -> None:
        current[:] = tools

    return set_tools


def _settings() -> Settings:
    return Settings(_env_file=None, ANTHROPIC_API_KEY="k", agent_id=f"mcp-seed-{uuid.uuid4().hex[:8]}")


def _args(*extra: str) -> Any:
    return seed._parse_args(["--name", "alpha", "--url", URL, *extra])


def _tool(name: str, description: str = "Finds things.\nFast.", schema: dict | None = None) -> Tool:
    return Tool(name=name, description=description, inputSchema=schema or SEARCH)


async def test_a_dry_run_prints_what_the_model_gets_and_writes_nothing(listing, db, capsys):
    listing(_tool("search"), _tool("delete", "Deletes."), _tool("_bad", schema={"type": "array"}))
    settings = _settings()
    assert await seed.seed(_args("--allow", "search"), settings) == 0
    out = capsys.readouterr().out
    expected = prepare_tool("alpha", "search", "Finds things.\nFast.", SEARCH).schema
    assert expected is not None
    assert "allow  mcp__alpha__search\n" in out
    assert f"       description: {expected['description']}\n" in out  # "[alpha] Finds things. Fast."
    assert f"       input schema: {serialize_schema(expected)}\n" in out
    assert "deny   mcp__alpha__delete\n" in out
    assert "mcp__alpha___bad  (skipped at startup: its input schema is not one the model API accepts)" in out
    assert "Pin entry, for NOUS_MCP_CLIENT_PINNED_SERVERS on the host" in out and out.rstrip().endswith(
        "Nothing written: pass --write to store the server and these permissions."
    )
    assert ": alpha=alpha.example.com;search\n" in out  # the host and the tools that would register
    assert await McpRegistry(db, settings.agent_id).list_enabled() == []


async def test_write_stores_the_server_and_the_permissions_and_a_reseed_replaces_them(listing, db, capsys):
    settings = _settings()
    registry = McpRegistry(db, settings.agent_id)
    listing(_tool("search"), _tool("fetch"), _tool("delete"))
    assert await seed.seed(_args("--allow", "search,fetch", "--background", "--write"), settings) == 0
    assert "Stored server 'alpha': 2 tool(s) will register, 1 denied." in capsys.readouterr().out
    ((config, tools),) = await registry.list_enabled()
    assert config.background is True
    permissions = sorted((t.tool, t.permission) for t in tools)
    assert permissions == [("delete", "deny"), ("fetch", "allow"), ("search", "allow")]

    # The server stops listing fetch; the owner re-seeds without --background.
    listing(_tool("search"), _tool("delete"))
    assert await seed.seed(_args("--allow", "search", "--write"), settings) == 0
    ((config, tools),) = await registry.list_enabled()
    assert config.background is False and config.call_timeout_seconds == 30
    assert sorted((t.tool, t.permission) for t in tools) == [("delete", "deny"), ("fetch", "deny"), ("search", "allow")]


async def test_an_unknown_allowed_tool_writes_nothing(listing, db, capsys):
    settings = _settings()
    listing(_tool("search"))
    assert await seed.seed(_args("--allow", "search,nope", "--write"), settings) == 2
    assert "--allow names tools the server does not list: nope" in capsys.readouterr().err
    assert await McpRegistry(db, settings.agent_id).list_enabled() == []


async def test_the_seed_says_which_allowed_tools_pass_the_cap(listing, capsys):
    names = [f"t{i:03d}" for i in range(TOOLS_PER_SERVER + 1)]
    listing(*(_tool(name) for name in names))
    assert await seed.seed(_args("--allow", ",".join(names)), _settings()) == 0
    out = capsys.readouterr().out
    assert f"allow  mcp__alpha__{names[-2]}\n" in out
    assert f"allow  mcp__alpha__{names[-1]}  (skipped at startup: over the {TOOLS_PER_SERVER}-tool cap)" in out


async def test_a_refused_url_is_reported_without_its_query_string(capsys):
    args = seed._parse_args(["--name", "alpha", "--url", "https://alpha.example.com/mcp?key=SECRET-77"])
    assert await seed.seed(args, _settings()) == 2
    assert "SECRET-77" not in capsys.readouterr().err


async def test_the_pin_entry_names_the_port_and_the_background_entry_is_printed(listing, monkeypatch, capsys):
    @asynccontextmanager
    async def open_session(url: str, allow: frozenset[str], *, max_response_bytes: int):
        yield _Session([_tool("search"), _tool("fetch")])

    monkeypatch.setattr(transport, "open_session", open_session)
    args = seed._parse_args(
        ["--name", "alpha", "--url", "https://alpha.example.com:8443/mcp", "--allow", "search,fetch", "--background"]
    )
    assert await seed.seed(args, _settings()) == 0
    out = capsys.readouterr().out
    assert ": alpha=alpha.example.com:8443;fetch|search\n" in out
    assert (
        "Background entry, for NOUS_MCP_CLIENT_BACKGROUND_SERVERS on the host when that optional list is used "
        "(a comma list): alpha\n"
    ) in out


async def test_a_tool_name_that_cannot_be_pinned_cannot_be_allowed(listing, capsys):
    listing(_tool("a|b"), _tool("c"))
    assert await seed.seed(_args("--allow", "a|b,c"), _settings()) == 2
    assert "these tool names cannot be pinned: a|b" in capsys.readouterr().err


async def test_a_hostile_schema_is_reported_not_raised(listing, capsys):
    hostile = {"type": "object", "properties": {"q": {"type": "string", "pattern": "a{4294967296}"}}}
    listing(_tool("search", schema=hostile), _tool("fetch", schema={"type": "object", "$schema": 5}), _tool("ok"))
    assert await seed.seed(_args("--allow", "search,fetch,ok"), _settings()) == 0
    out = capsys.readouterr().out
    assert "mcp__alpha__search  (skipped at startup: its input schema cannot be checked)" in out
    assert "mcp__alpha__fetch  (skipped at startup: its input schema cannot be checked)" in out
    assert ": alpha=alpha.example.com;ok\n" in out
