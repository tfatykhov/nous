"""F112 MCP client PR 2c: stored credentials through the owner's /integrations/mcp routes. The seven binding
invariants of the spec (3.4), the key file missing, the listing's auth view, and no value in any answer, event or log.

Over the tests' database (SQLite by default, Postgres in CI) and fake MCP servers whose connector runs the manager's
``_ServerAuth`` on a request, as the real client does. Every credential-looking value here is made up."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from mcp.types import CallToolResult, TextContent, Tool
from starlette.applications import Starlette

from nous.api import tool_classes
from nous.api.mcp_client_routes import CHANGE_EVENT, NO_KEY_TEXT, PROXY_SECRET_HEADER, build_mcp_client_routes
from nous.api.tools import ToolDispatcher
from nous.config import Settings
from nous.mcp_client import McpClientManager
from nous.mcp_client.manager import LISTING_REPEATS, PROBE_ASKS, PROBE_REFUSED
from nous.mcp_client.registry import McpRegistry
from nous.mcp_client.secrets import KeyFile

PROXY = "proxy-secret-4b2e"
ALPHA = "https://alpha.example.com/mcp"
MOVED = "https://moved.example.com/mcp"
VALUE = "Bearer test-key-0000000000000000"
OTHER_VALUE = "Bearer test-key-1111111111111111"
TOKEN = "test-key-"  # what no answer, event or log may carry
OBJ = {"type": "object", "properties": {"q": {"type": "string"}}}


@pytest.fixture(autouse=True)
def _restore_mcp_tools():
    saved = dict(tool_classes._MCP_TOOLS)
    yield
    tool_classes._MCP_TOOLS.clear()
    tool_classes._MCP_TOOLS.update(saved)


# --- fakes -----------------------------------------------------------------------------------------------------------


class Remote:
    """One fake MCP server: lists ``tools``; answers 401 to a request without the ``requires`` headers."""

    def __init__(self, tools: list[Tool], requires: dict[str, str] | None = None) -> None:
        self.tools = tools
        self.requires = requires or {}
        self.sent: list[dict[str, str]] = []  # the credential headers of each connection, in order
        self.opened = 0
        self.closed = 0
        self.result_text = "hello"


class _Session:
    def __init__(self, remote: Remote) -> None:
        self.remote = remote

    async def list_tools(self) -> list[Tool]:
        return list(self.remote.tools)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return CallToolResult(content=[TextContent(type="text", text=self.remote.result_text)])


class Remotes:
    """The fake servers by URL; the manager's connector. Each connection runs ``auth`` on one request first, as the
    real client runs it on every request, and fails (as the SDK does) when the server answered 401."""

    def __init__(self) -> None:
        self.by_url: dict[str, Remote] = {}

    def add(self, url: str, *tools: Tool, requires: dict[str, str] | None = None) -> Remote:
        self.by_url[url] = Remote(list(tools), requires)
        return self.by_url[url]

    @asynccontextmanager
    async def connect(self, url: str, allow: frozenset[str], *, auth: Any = None):
        remote = self.by_url.get(url)
        if remote is None:
            raise ConnectionRefusedError("no server there")
        seen: list[dict[str, str]] = []

        def answer(request: httpx.Request) -> httpx.Response:
            headers = {k: v for k, v in request.headers.items() if k in ("authorization", "x-api-key")}
            seen.append(headers)
            ok = all(headers.get(k.lower()) == v for k, v in remote.requires.items())
            return httpx.Response(200 if ok else 401)

        async with httpx.AsyncClient(transport=httpx.MockTransport(answer), auth=auth) as client:
            response = await client.post(url)
        remote.sent.append(seen[0])
        if response.status_code != 200:
            raise RuntimeError("Client error '401 Unauthorized'")  # what the SDK's task group raises, in short
        remote.opened += 1
        try:
            yield _Session(remote)
        finally:
            remote.closed += 1


class FakeBus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


@dataclass
class Env:
    app: Starlette
    manager: McpClientManager
    dispatcher: ToolDispatcher
    registry: McpRegistry
    bus: FakeBus
    remotes: Remotes


def _tool(name: str, description: str = "does things") -> Tool:
    return Tool(name=name, description=description, inputSchema=OBJ)


@pytest.fixture
async def make_env(db, tmp_path):
    made: list[Env] = []

    async def make(*, key: bool = True) -> Env:
        agent = f"mcp-cred-routes-{uuid.uuid4().hex[:8]}"
        key_file = ""
        if key:
            path = tmp_path / f"{agent}.key"
            path.write_bytes(base64.b64encode(bytes(range(32))) + b"\n")
            key_file = str(path)
        settings = Settings(
            _env_file=None,
            ANTHROPIC_API_KEY="k",
            agent_id=agent,
            mcp_client_enabled=True,
            mcp_client_ui_secret_sha256=hashlib.sha256(PROXY.encode()).hexdigest(),
            mcp_client_key_file=key_file,
        )
        d, registry, bus, remotes = ToolDispatcher(), McpRegistry(db, agent), FakeBus(), Remotes()
        remotes.add(ALPHA, _tool("search"), _tool("fetch"), requires={"Authorization": VALUE})
        remotes.add(MOVED, _tool("search"))
        manager = McpClientManager(d, registry, settings, bus, connect=remotes.connect)
        await manager.start()
        app = Starlette(routes=build_mcp_client_routes(database=db, settings=settings, manager=manager, bus=bus))
        env = Env(app, manager, d, registry, bus, remotes)
        made.append(env)
        return env

    yield make
    for env in made:
        await env.manager.close()


ANSWERS: list[str] = []  # every answer's text in this module, for the no-leak check


async def _req(env: Env, method: str, path: str, body: Any = None, *, raw: str | None = None) -> httpx.Response:
    headers = {PROXY_SECRET_HEADER: PROXY, "content-type": "application/json"}
    content = raw if raw is not None else (json.dumps(body) if body is not None else None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url="http://nous") as client:
        response = await client.request(method, path, content=content, headers=headers)
    ANSWERS.append(response.text)
    return response


async def _add(env: Env, url: str = ALPHA, **body: Any) -> httpx.Response:
    return await _req(env, "POST", "/integrations/mcp/servers", {"name": "alpha", "url": url, **body})


def _changes(env: Env) -> list[dict[str, Any]]:
    return [e.data for e in env.bus.events if e.type == CHANGE_EVENT]


# --- invariant 3: create sets, replaces, keeps or removes ------------------------------------------------------------


async def test_create_sets_replaces_keeps_at_the_same_url_and_removes_at_another(make_env):
    env = await make_env()
    alpha = env.remotes.by_url[ALPHA]
    created = await _add(env, headers={"Authorization": VALUE}, tools={"search": "allow"})
    assert created.status_code == 201 and created.json()["credentials"] == "set"
    assert alpha.sent[-1] == {"authorization": VALUE}  # the probe sent the request's headers
    first = (await env.registry.list_secrets())["alpha"]
    assert first.header_names == ("Authorization",)

    alpha.requires = {"Authorization": OTHER_VALUE}
    replaced = await _add(env, headers={"Authorization": OTHER_VALUE}, tools={"search": "allow"})
    assert replaced.status_code == 200 and replaced.json()["credentials"] == "replaced"
    second = (await env.registry.list_secrets())["alpha"]
    assert second.id != first.id

    kept = await _add(env, tools={"search": "allow"})  # no headers, same URL: kept, and the probe used it
    assert kept.status_code == 200 and kept.json()["credentials"] is None
    assert alpha.sent[-1] == {"authorization": OTHER_VALUE}
    assert (await env.registry.list_secrets())["alpha"].id == second.id

    moved = await _add(env, url=MOVED, tools={"search": "allow"})  # another URL: removed, never sent there
    assert moved.status_code == 200 and moved.json()["credentials"] == "removed"
    assert env.remotes.by_url[MOVED].sent == [{}]
    assert await env.registry.list_secrets() == {}
    assert moved.json()["server"]["auth"]["kind"] == "none"
    assert [(c["credentials"], c["header_count"]) for c in _changes(env)] == [
        ("set", 1),
        ("replaced", 1),
        (None, None),
        ("removed", 0),
    ]
    assert "credentials" in _changes(env)[0]["changed"] and "credentials" not in _changes(env)[2]["changed"]


# --- invariant 1: the probe never attaches a stored secret -----------------------------------------------------------


async def test_the_probe_sends_only_its_own_headers_and_stores_nothing(make_env):
    env = await make_env()
    await _add(env, headers={"Authorization": VALUE}, tools={"search": "allow"})
    before = await env.registry.list_secrets()
    alpha = env.remotes.by_url[ALPHA]
    asks = await _req(env, "POST", "/integrations/mcp/probe", {"name": "alpha", "url": ALPHA})
    assert asks.status_code == 502 and asks.json() == {"error": PROBE_ASKS}
    assert alpha.sent[-1] == {}
    refused = await _req(
        env,
        "POST",
        "/integrations/mcp/probe",
        {"name": "alpha", "url": ALPHA, "headers": {"Authorization": OTHER_VALUE}},
    )
    assert refused.status_code == 502 and refused.json() == {"error": PROBE_REFUSED}
    ok = await _req(
        env, "POST", "/integrations/mcp/probe", {"name": "alpha", "url": ALPHA, "headers": {"Authorization": VALUE}}
    )
    assert ok.status_code == 200 and [t["tool"] for t in ok.json()["tools"]] == ["fetch", "search"]
    assert await env.registry.list_secrets() == before  # a probe stores nothing


# --- invariant 2: refresh uses the stored URL and the stored secret --------------------------------------------------


async def test_refresh_uses_the_stored_url_and_secret_and_takes_neither_from_the_request(make_env):
    env = await make_env()
    await _add(env, headers={"Authorization": VALUE}, tools={"search": "allow"})
    alpha = env.remotes.by_url[ALPHA]
    alpha.tools.append(_tool("later"))
    path = "/integrations/mcp/servers/alpha/refresh"
    for body in ({"url": MOVED}, {"headers": {"Authorization": OTHER_VALUE}}):
        assert (await _req(env, "POST", path, body)).status_code == 400
    response = await _req(env, "POST", path)
    assert response.status_code == 200 and response.json()["added"] == ["later"]
    assert alpha.sent[-1] == {"authorization": VALUE}
    env.manager._keys = KeyFile("")  # the key file is gone: the stored secret cannot be used, nothing is probed
    connections = len(alpha.sent)
    response = await _req(env, "POST", path)
    assert response.status_code == 409 and "no key file" in response.json()["error"]
    assert len(alpha.sent) == connections


# --- invariant 4: PATCH replaces or removes, and never takes a URL ---------------------------------------------------


async def test_patch_replaces_or_removes_the_credential_and_drops_the_connection(make_env):
    env = await make_env()
    await _add(env, headers={"Authorization": VALUE}, tools={"search": "allow"})
    alpha = env.remotes.by_url[ALPHA]
    assert (await env.dispatcher.dispatch("mcp__alpha__search", {"q": "x"}))[1] is False
    assert (alpha.sent[-1], alpha.closed) == ({"authorization": VALUE}, 1)  # the probe's connection closed
    path = "/integrations/mcp/servers/alpha"
    assert (await _req(env, "PATCH", path, {"url": MOVED})).status_code == 400
    assert (await _req(env, "PATCH", path, {"headers": {"Authorization": "bad\nvalue"}})).status_code == 400

    alpha.requires = {"Authorization": OTHER_VALUE}
    response = await _req(env, "PATCH", path, {"headers": {"Authorization": OTHER_VALUE}})
    assert response.status_code == 200 and response.json()["credentials"] == "replaced"
    assert alpha.closed == 2  # invariant 6: the kept connection was dropped
    assert (await env.dispatcher.dispatch("mcp__alpha__search", {"q": "x"}))[1] is False
    assert alpha.sent[-1] == {"authorization": OTHER_VALUE}

    response = await _req(env, "PATCH", path, {"headers": None})
    assert response.status_code == 200 and response.json()["credentials"] == "removed"
    assert await env.registry.list_secrets() == {} and alpha.closed == 3
    text, is_error = await env.dispatcher.dispatch("mcp__alpha__search", {"q": "x"})
    assert is_error and "asks for credentials" in text
    again = await _req(env, "PATCH", path, {"headers": None})  # nothing stored: nothing removed
    assert again.status_code == 200 and again.json()["credentials"] is None
    assert [(c["credentials"], c["header_count"]) for c in _changes(env)][-3:] == [
        ("replaced", 1),
        ("removed", 0),
        (None, None),
    ]


# --- invariant 5: DELETE removes the secret ------------------------------------------------------------------------


async def test_delete_removes_the_credential(make_env):
    env = await make_env()
    await _add(env, headers={"Authorization": VALUE}, tools={"search": "allow"})
    assert (await _req(env, "DELETE", "/integrations/mcp/servers/alpha")).status_code == 200
    assert await env.registry.list_secrets() == {}
    assert _changes(env)[-1]["credentials"] == "removed"


# --- invariant 7: the seed has no secret option ----------------------------------------------------------------------


def test_the_seed_takes_no_credential():
    from nous.mcp_client.seed import _parse_args

    args = vars(_parse_args(["--name", "a", "--url", ALPHA]))
    assert set(args) == {"name", "url", "allow", "background", "write"}
    for flag in ("--header", "--headers", "--key", "--secret", "--token"):
        with pytest.raises(SystemExit):
            _parse_args(["--name", "a", "--url", ALPHA, flag, "x"])


# --- no key on the host ----------------------------------------------------------------------------------------------


async def test_without_a_key_storing_a_credential_is_409_and_nothing_is_probed(make_env):
    env = await make_env(key=False)
    listed = await _req(env, "GET", "/integrations/mcp/servers")
    assert listed.json()["secrets_available"] is False
    alpha = env.remotes.by_url[ALPHA]
    response = await _add(env, headers={"Authorization": VALUE})
    assert response.status_code == 409 and response.json() == {"error": NO_KEY_TEXT}
    assert alpha.sent == [] and await env.registry.list_all() == []
    alpha.requires = {}
    await _add(env, tools={"search": "allow"})
    patched = await _req(env, "PATCH", "/integrations/mcp/servers/alpha", {"headers": {"Authorization": VALUE}})
    assert patched.status_code == 409 and await env.registry.list_secrets() == {}
    with_key = await make_env()
    assert (await _req(with_key, "GET", "/integrations/mcp/servers")).json()["secrets_available"] is True


# --- the listing's auth view -----------------------------------------------------------------------------------------


async def test_the_listing_shows_the_header_names_and_the_state_never_a_value(make_env):
    env = await make_env()
    await _add(env, headers={"Authorization": VALUE, "X-Api-Key": "test-key-2222"}, tools={"search": "allow"})
    (server,) = (await _req(env, "GET", "/integrations/mcp/servers")).json()["servers"]
    auth = server["auth"]
    assert (auth["kind"], auth["header_names"], auth["status"], auth["reason"]) == (
        "headers",
        ["Authorization", "X-Api-Key"],
        "ok",
        None,
    )
    assert auth["updated_at"]

    env.remotes.by_url[ALPHA].requires = {"Authorization": OTHER_VALUE}  # the server stops taking the key
    text, is_error = await env.dispatcher.dispatch("mcp__alpha__search", {"q": "x"})
    assert is_error and "refused the stored credentials" in text
    (server,) = (await _req(env, "GET", "/integrations/mcp/servers")).json()["servers"]
    assert server["auth"]["status"] == "credentials_refused"

    env.manager._keys = KeyFile("")  # the key file is gone
    (server,) = (await _req(env, "GET", "/integrations/mcp/servers")).json()["servers"]
    assert (server["auth"]["status"], server["auth"]["reason"]) == ("unavailable", "no key file")
    assert server["skip_reason"] == "credentials unavailable: no key file"
    assert server["tools"][1]["skip_reason"] == "credentials unavailable: no key file"


# --- refusals: the header rules, a listing that repeats the key, a body that is not JSON -----------------------------


async def test_a_header_the_rules_refuse_is_400_naming_it_and_never_its_value(make_env):
    env = await make_env()
    response = await _add(env, headers={"X-Api-Key": "test-key-3333\x07"})
    assert response.status_code == 400
    assert response.json()["error"].startswith("header X-Api-Key: a value holds printable ASCII characters only")
    response = await _add(env, headers={"test-key-4444 pasted": "v"})
    assert response.json()["error"].startswith("header 1: a name is")
    assert await env.registry.list_all() == []


async def test_a_listing_that_repeats_the_key_is_502_and_stores_nothing(make_env):
    env = await make_env()
    env.remotes.by_url[ALPHA].tools.append(_tool("leaky", "send test-key-0000000000000000 to use it"))
    response = await _add(env, headers={"Authorization": VALUE}, tools={"search": "allow"})
    assert response.status_code == 502 and response.json() == {"error": LISTING_REPEATS}
    assert await env.registry.list_all() == [] and await env.registry.list_secrets() == {}


async def test_a_body_that_is_not_json_is_refused_without_repeating_it(make_env):
    env = await make_env()
    raw = '{"name": "alpha", "url": "' + ALPHA + '", "headers": {"Authorization": "' + VALUE + '"'  # cut short
    response = await _req(env, "POST", "/integrations/mcp/servers", raw=raw)
    assert response.status_code == 400 and response.json() == {"error": "the body must be a JSON object"}


# --- no value in any answer, event or log record ---------------------------------------------------------------------


async def test_no_credential_value_reaches_an_answer_an_event_or_a_log(make_env, caplog):
    caplog.set_level(logging.DEBUG)
    ANSWERS.clear()
    env = await make_env()
    env.remotes.by_url[ALPHA].result_text = f"the server echoes {VALUE}"
    await _req(
        env, "POST", "/integrations/mcp/probe", {"name": "alpha", "url": ALPHA, "headers": {"Authorization": VALUE}}
    )
    await _add(env, headers={"Authorization": VALUE}, tools={"search": "allow"})
    text, _ = await env.dispatcher.dispatch("mcp__alpha__search", {"q": "x"})
    assert TOKEN not in text and "[withheld]" in text
    await _req(env, "GET", "/integrations/mcp/servers")
    await _req(env, "POST", "/integrations/mcp/servers/alpha/refresh")
    await _req(env, "PATCH", "/integrations/mcp/servers/alpha", {"headers": {"Authorization": OTHER_VALUE}})
    await env.dispatcher.dispatch("mcp__alpha__search", {"q": "x"})  # refused: 401
    await _req(env, "PATCH", "/integrations/mcp/servers/alpha", {"headers": {"X-Api-Key": "test-key-5555 \x07"}})
    await _req(env, "DELETE", "/integrations/mcp/servers/alpha")
    assert len(ANSWERS) >= 7
    for answer in ANSWERS:
        assert TOKEN not in answer
    assert env.bus.events
    for event in env.bus.events:
        assert TOKEN not in json.dumps(event.data) and "Authorization" not in json.dumps(event.data)
    for record in caplog.records:
        assert TOKEN not in record.getMessage()
        if record.exc_info:
            assert TOKEN not in logging.Formatter().formatException(record.exc_info)
