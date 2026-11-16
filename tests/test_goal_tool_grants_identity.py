"""Per-goal tool grants, the MCP side: each MCP client tool registers with its identity (the server's row id, the
SHA-256 of the server's full URL and the server's own name for the tool), the registry reads the row id, and a reload
or close leaves no identity behind. A server deleted and added again under its name, or moved to another URL, gives
its tools another identity."""

from __future__ import annotations

import hashlib
import uuid

import pytest
from test_mcp_client_manager import FakeRegistry, FakeServer, _pins, _settings, _tool

from nous.api import tool_classes
from nous.api.tool_classes import McpIdentity, mcp_tool_identity
from nous.api.tools import ToolDispatcher
from nous.mcp_client import McpClientManager
from nous.mcp_client.config import ServerConfig
from nous.mcp_client.registry import McpRegistry

URL = "https://notes.example.com/mcp/private-path"
OTHER_URL = "https://notes.example.com/mcp/other-path"


@pytest.fixture(autouse=True)
def _clean_mcp_registry(monkeypatch):
    monkeypatch.setattr(tool_classes, "_MCP_TOOLS", {})
    monkeypatch.setattr(tool_classes, "_MCP_IDENTITIES", {})


def _digest(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _rows(server_id: uuid.UUID | None, url: str = URL, *tools: str):
    config = ServerConfig(name="notes", url=url, background=True, id=server_id)
    return [(config, [_tool("notes", tool) for tool in tools or ("search",)])]


async def _start(rows):
    fake = FakeServer()
    registry = FakeRegistry(rows)
    manager = McpClientManager(
        ToolDispatcher(),
        registry,
        _settings(mcp_client_pinned_servers=_pins(rows), mcp_client_background_servers="notes"),
        None,
        connect=fake.connect,
    )
    await manager.start()
    return manager, registry


async def test_a_tool_registers_with_its_servers_row_id_url_digest_and_remote_name():
    server_id = uuid.uuid4()
    manager, _registry = await _start(_rows(server_id))
    identity = mcp_tool_identity("mcp__notes__search")
    assert identity == McpIdentity(server_id, _digest(URL), "search")
    assert (identity.server_id, identity.url_sha256, identity.remote_tool) == (server_id, _digest(URL), "search")
    assert (identity.server_name, identity.server_host) == ("notes", "notes.example.com")
    assert URL not in repr(identity) and "private-path" not in repr(identity)
    await manager.close()


async def test_the_digest_is_of_the_whole_url_not_the_host():
    server_id = uuid.uuid4()
    manager, registry = await _start(_rows(server_id))
    before = mcp_tool_identity("mcp__notes__search")
    registry.rows = _rows(server_id, OTHER_URL)
    await manager.reload()
    after = mcp_tool_identity("mcp__notes__search")
    assert before.server_host == after.server_host == "notes.example.com"
    assert after.url_sha256 == _digest(OTHER_URL) and after != before
    await manager.close()


async def test_a_server_added_again_under_its_name_gives_its_tools_another_identity():
    manager, registry = await _start(_rows(uuid.uuid4()))
    before = mcp_tool_identity("mcp__notes__search")
    registry.rows = _rows(uuid.uuid4())
    await manager.reload()
    after = mcp_tool_identity("mcp__notes__search")
    assert after is not None and after != before
    assert (after.url_sha256, after.remote_tool) == (before.url_sha256, before.remote_tool)
    await manager.close()


async def test_a_reload_drops_the_identity_of_a_tool_no_longer_registered_and_close_drops_all():
    server_id = uuid.uuid4()
    manager, registry = await _start(_rows(server_id, URL, "search", "find"))
    assert mcp_tool_identity("mcp__notes__find") is not None
    registry.rows = _rows(server_id, URL, "search")
    await manager.reload()
    assert mcp_tool_identity("mcp__notes__find") is None
    assert mcp_tool_identity("mcp__notes__search") is not None
    await manager.close()
    assert mcp_tool_identity("mcp__notes__search") is None
    assert tool_classes._MCP_IDENTITIES == {}


async def test_a_server_with_no_row_id_registers_its_tools_with_no_identity():
    manager, _registry = await _start(_rows(None))
    assert tool_classes.mcp_tool_background("mcp__notes__search") is True
    assert mcp_tool_identity("mcp__notes__search") is None
    await manager.close()


async def test_the_registry_reads_each_servers_row_id(db):
    registry = McpRegistry(db, f"grant-identity-{uuid.uuid4().hex[:8]}")
    stored = await registry.upsert_server(ServerConfig(name="notes", url=URL, background=True))
    [(config, _tools)] = await registry.list_enabled()
    assert config.id == stored
    assert await registry.delete_server("notes")
    again = await registry.upsert_server(ServerConfig(name="notes", url=URL, background=True))
    [(config, _tools)] = await registry.list_enabled()
    assert config.id == again != stored
