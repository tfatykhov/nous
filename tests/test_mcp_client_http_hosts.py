"""F112 MCP client: an http server address is taken only for a host named in ``NOUS_MCP_CLIENT_ALLOW_PRIVATE_HOSTS``.

The rule is an argument with a closed default: ``url_problem`` and the transport's ``check_url`` refuse http unless
they are handed the hosts, and only the code that opens a server's session hands them over. So a row may hold an http
address, the routes refuse one for a host that is not listed before any connection, a stored one whose host has left
the list is refused when it is opened (and can still be seen and removed), and the sign-in code never takes one.

The routes' app and fakes come from test_mcp_client_routes.py.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette

from nous.api.mcp_client_routes import HTTP_HOST_TEXT, build_mcp_client_routes
from nous.mcp_client import McpClientManager, transport
from nous.mcp_client.config import (
    BlockedAddress,
    ServerConfig,
    ToolConfig,
    is_http,
    parse_host_list,
    row_url_problem,
    url_problem,
)
from nous.mcp_client.registry import McpRegistry
from nous.mcp_client.transport import check_url, open_session
from tests import test_mcp_client_routes as base
from tests.test_mcp_client_routes import OBJ, _add, _dispatcher, _names, _req, _settings, _tool

# The fixtures of the route tests, by name, so pytest finds them in this module.
make_env = base.make_env
_restore_mcp_tools = base._restore_mcp_tools

LAN_HOST = "192.168.1.127"
LAN = "http://192.168.1.127:8000/mcp"
HOSTS = frozenset({LAN_HOST, "mcp.lan"})


# --- the rule --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,accepted",
    [
        (LAN, True),
        ("http://192.168.1.127/mcp", True),  # the list names hosts, not ports
        ("http://MCP.Lan:9000/x", True),  # the host is compared without case
        ("https://192.168.1.127:8000/mcp", True),  # https is not what the list decides
        ("http://192.168.1.128:8000/mcp", False),  # the next address
        ("http://alpha.example.com/mcp", False),
        ("http://192.168.1.127.example.com/mcp", False),  # the listed text as a label of another host
        ("http://mcp.lan.example.com/mcp", False),
        ("http://example.com/192.168.1.127", False),  # the listed text in the path
        ("http://user:pw@192.168.1.127/mcp", False),  # the other rules hold for http too
        ("http://192.168.1.127/mcp?key=x", False),
        ("http:///mcp", False),
        ("HTTP://192.168.1.127:8000/mcp", False),  # the scheme as the table's check compares it: lower case
        ("ftp://192.168.1.127/mcp", False),  # http and https only
        ("ws://mcp.lan/mcp", False),
        ("//192.168.1.127/mcp", False),
    ],
)
def test_an_http_address_is_accepted_only_for_a_listed_host(url, accepted):
    assert (url_problem(url, HOSTS) is None) is accepted


def test_without_the_hosts_no_http_address_is_accepted():
    for url in (LAN, "http://mcp.lan/mcp", "http://alpha.example.com/mcp"):
        assert url_problem(url) == "only https URLs are accepted"
    assert url_problem("https://alpha.example.com/mcp") is None
    assert url_problem("HTTPS://alpha.example.com/mcp") == "only https URLs are accepted"  # the table would refuse it
    for not_text in (None, b"https://alpha.example.com/mcp", 7):
        assert url_problem(not_text, HOSTS) == "not a valid URL" == row_url_problem(not_text)  # type: ignore[arg-type]


def test_the_setting_gives_the_hosts_the_rule_compares():
    assert url_problem("http://Win-MCP.lan:8000/mcp", parse_host_list(" Win-MCP.LAN , 192.168.1.127 ")) is None


def test_a_row_may_hold_an_http_address_of_any_host_and_nothing_else_new():
    for url in (LAN, "http://alpha.example.com/mcp"):
        assert row_url_problem(url) is None and is_http(url)
        assert ServerConfig(name="win", url=url).url == url
    assert not is_http("https://alpha.example.com/mcp") and not is_http("httpx://192.168.1.127/mcp")
    bad = ("ftp://192.168.1.127/mcp", "http://user:pw@192.168.1.127/mcp", "http://192.168.1.127/mcp?k=v", "x")
    for url in (*bad, "HTTP://192.168.1.127/mcp", "Https://alpha.example.com/mcp"):
        assert row_url_problem(url) is not None
        with pytest.raises(ValueError):
            ServerConfig(name="win", url=url)


# --- the transport ---------------------------------------------------------------------------------------------------


async def test_check_url_takes_http_only_when_it_is_handed_the_hosts(monkeypatch):
    async def never(host: str, port: int) -> list[str]:
        raise AssertionError("a listed host is not resolved")

    monkeypatch.setattr(transport, "_resolve", never)
    with pytest.raises(BlockedAddress, match="only https"):
        await check_url(LAN, HOSTS)  # how the sign-in code calls it: the address list alone
    await check_url(LAN, HOSTS, http_hosts=HOSTS)
    with pytest.raises(BlockedAddress, match="only https"):
        await check_url("http://192.168.1.128:8000/mcp", HOSTS, http_hosts=HOSTS)


async def test_http_hosts_do_not_lift_the_address_check(monkeypatch):
    seen: list[tuple[str, int]] = []

    async def resolve(host: str, port: int) -> list[str]:
        seen.append((host, port))
        return ["10.0.0.5"]

    monkeypatch.setattr(transport, "_resolve", resolve)
    with pytest.raises(BlockedAddress, match="not public"):
        await check_url("http://mcp.lan/mcp", frozenset(), http_hosts=HOSTS)
    assert seen == [("mcp.lan", 80)]  # an http address with no port is looked up on 80


async def test_a_session_to_an_http_host_that_is_not_listed_opens_no_connection(monkeypatch):
    opened: list[Any] = []
    monkeypatch.setattr(transport, "guarded_client", lambda *a, **k: opened.append(a))
    for url, allow in ((LAN, frozenset()), ("http://192.168.1.128:8000/mcp", HOSTS)):
        with pytest.raises(BlockedAddress, match="only https"):
            async with open_session(url, allow):
                pass
    assert opened == []


class _Gate:
    """A bearer gate in front of an ASGI app, like the one ``windows-mcp --auth-key`` puts up: 401 without the key.
    Keeps what each request carried."""

    def __init__(self, app: Any, key: str) -> None:
        self.app, self.key = app, key
        self.seen: list[tuple[str, str | None]] = []

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {name.decode().lower(): value.decode() for name, value in scope["headers"]}
        self.seen.append((scope["scheme"], headers.get("authorization")))
        if headers.get("authorization") != f"Bearer {self.key}":
            body = b'{"error":"unauthorized"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


async def test_a_real_plain_http_server_on_a_listed_host_answers_with_the_stored_key():
    """The SDK's own server behind uvicorn on 127.0.0.1, over a real socket and no TLS: the guarded pool, the
    per-origin credential and the session all run as they do against a LAN server."""
    uvicorn = pytest.importorskip("uvicorn")
    from mcp.server.fastmcp import FastMCP

    from nous.mcp_client.manager import _ServerAuth

    key = "test-key-4444444444"
    server = FastMCP("lan", stateless_http=True, json_response=True)

    @server.tool()
    def echo(text: str) -> str:
        """Repeat the text."""
        return text

    gate = _Gate(server.streamable_http_app(), key)
    running = uvicorn.Server(uvicorn.Config(gate, host="127.0.0.1", port=0, log_config=None, lifespan="on"))
    task = asyncio.create_task(running.serve())
    try:
        async with asyncio.timeout(20):
            while not running.started:
                if task.done():
                    task.result()
                await asyncio.sleep(0.02)
        port = running.servers[0].sockets[0].getsockname()[1]
        url = f"http://127.0.0.1:{port}/mcp"
        listed = frozenset({"127.0.0.1"})

        with pytest.raises(BlockedAddress, match="only https"):
            async with open_session(url, frozenset()):
                pass
        assert gate.seen == []  # refused before a socket was opened

        async with asyncio.timeout(30):
            async with open_session(
                url, listed, auth=_ServerAuth(url, lambda: {"Authorization": f"Bearer {key}"})
            ) as s:
                assert [tool.name for tool in await s.list_tools()] == ["echo"]
                result = await s.call_tool("echo", {"text": "over plain http"})
                assert (result.content[0].text, result.isError) == ("over plain http", False)
        assert gate.seen and all(seen == ("http", f"Bearer {key}") for seen in gate.seen)

        gate.seen.clear()
        with pytest.raises(BaseException) as refused:  # no key: the server's 401 ends the handshake
            async with asyncio.timeout(30):
                async with open_session(url, listed) as s:
                    await s.list_tools()
        assert not isinstance(refused.value, (BlockedAddress, AssertionError, TimeoutError))
        assert gate.seen and all(authorization is None for _scheme, authorization in gate.seen)
    finally:
        running.should_exit = True
        await asyncio.wait_for(task, 20)


class _Wire:
    """What would have gone out: stands in for the connection pool, so no socket is opened."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, answer: Any = None) -> None:
        self.sent: list[tuple[str, str, int | None, str, dict[str, str]]] = []
        wire = self

        async def handle(_self: Any, request: httpx.Request) -> httpx.Response:
            url = request.url
            wire.sent.append((url.scheme, url.host, url.port, url.path, dict(request.headers)))
            return answer(request) if answer is not None else httpx.Response(200, request=request)

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)


@pytest.mark.parametrize(
    "server,other",
    [
        (LAN, "https://192.168.1.127:8000/mcp"),  # the same host and port over https
        (LAN, "http://192.168.1.127:8001/mcp"),  # another port
        (LAN, "http://192.168.1.127/mcp"),
        (LAN, "http://192.168.1.128:8000/mcp"),  # another host
        (LAN, "http://mcp.lan:8000/mcp"),  # another listed host
        ("http://mcp.lan/mcp", "https://mcp.lan/mcp"),  # the upgrade the SDK counts as the same origin
        ("http://mcp.lan:80/mcp", "https://mcp.lan:443/mcp"),
        ("https://alpha.example.com/mcp", "http://alpha.example.com/mcp"),  # an https server is not moved to http
        ("https://alpha.example.com/mcp", "https://alpha.example.com:8443/mcp"),
    ],
)
async def test_a_sessions_client_sends_to_the_servers_own_scheme_host_and_port_only(monkeypatch, server, other):
    wire = _Wire(monkeypatch)
    async with transport.guarded_client(HOSTS, 10_000, origin=server) as client:
        with pytest.raises(BlockedAddress, match="another address"):
            await client.post(other, headers={"X-Api-Key": "test-key-4444444444"})
        assert wire.sent == []
        own = httpx.URL(server)
        assert (await client.post(str(own.copy_with(path="/other/path")))).status_code == 200
    assert [(scheme, host, port) for scheme, host, port, _path, _headers in wire.sent] == [
        (own.scheme, own.host, own.port)
    ]


async def test_the_sign_in_client_sends_to_https_only(monkeypatch):
    wire = _Wire(monkeypatch)
    async with transport.guarded_client(HOSTS, 10_000) as client:
        for url in (LAN, "http://mcp.lan/token", "http://auth.example.com/token"):
            with pytest.raises(BlockedAddress, match="only https"):
                await client.post(url)
        assert wire.sent == []
        assert (await client.get("https://auth.example.com/token")).status_code == 200
    assert [sent[:2] for sent in wire.sent] == [("https", "auth.example.com")]


@pytest.mark.parametrize("header", ["Authorization", "X-Api-Key"])
@pytest.mark.parametrize(
    "server,location",
    [
        ("http://mcp.lan/mcp", "https://mcp.lan/mcp"),  # port 80 to 443: the SDK follows it as the same origin
        ("http://mcp.lan:80/mcp", "https://mcp.lan:443/other"),
        ("http://mcp.lan:8000/mcp", "https://mcp.lan:8000/mcp"),
        ("http://mcp.lan/mcp", "https://other.lan/mcp"),
        ("http://mcp.lan/mcp", "http://mcp.lan:8001/mcp"),
    ],
)
async def test_a_redirect_carries_the_stored_key_nowhere_but_the_servers_own_address(
    monkeypatch, server, location, header
):
    """The real session, SDK and credential class; the server answers every request with a redirect."""
    from nous.mcp_client.manager import _ServerAuth

    key = "test-key-4444444444"
    wire = _Wire(monkeypatch, lambda request: httpx.Response(307, headers={"Location": location}, request=request))
    with pytest.raises(BaseException) as failed:  # the session cannot start: its one address only redirects
        async with asyncio.timeout(20):
            async with open_session(server, HOSTS, auth=_ServerAuth(server, lambda: {header: key})) as s:
                await s.list_tools()
    assert not isinstance(failed.value, (AssertionError, TimeoutError))
    own = httpx.URL(server)
    assert wire.sent, "the server's own address was asked"
    for scheme, host, port, _path, headers in wire.sent:
        assert (scheme, host, port) == (own.scheme, own.host, own.port)  # nothing left for the redirect's target
        assert headers.get(header.lower()) == key


# --- the routes ------------------------------------------------------------------------------------------------------


async def test_an_http_server_on_a_listed_host_is_checked_added_listed_and_called(make_env):
    env = await make_env(mcp_client_allow_private_hosts=LAN_HOST)
    env.remotes.add(LAN, _tool("click"))
    probed = await _req(env, "POST", "/integrations/mcp/probe", {"name": "win", "url": LAN})
    assert probed.status_code == 200 and [tool["tool"] for tool in probed.json()["tools"]] == ["click"]
    assert (await _add(env, "win", LAN, tools={"click": "allow"})).status_code == 201
    assert "mcp__win__click" in _names(env.dispatcher)
    text, is_error = await env.dispatcher.dispatch("mcp__win__click", {"q": "x"})
    assert not is_error and text.endswith("hello")
    listing = (await _req(env, "GET", "/integrations/mcp/servers")).json()["servers"]
    assert [(server["name"], server["url"]) for server in listing] == [("win", LAN)]
    assert [config.url for config, _tools in await env.registry.list_all()] == [LAN]


@pytest.mark.parametrize("path", ["/integrations/mcp/probe", "/integrations/mcp/servers"])
@pytest.mark.parametrize("listed", ["", LAN_HOST])
async def test_an_http_address_of_a_host_that_is_not_listed_is_400_before_any_connection(make_env, path, listed):
    env = await make_env(mcp_client_allow_private_hosts=listed)
    urls = ["http://192.168.1.128:8000/mcp", "http://alpha.example.com/mcp", "http://192.168.1.127.example.com/mcp"]
    for url in urls + ([LAN] if not listed else []):
        response = await _req(env, "POST", path, {"name": "win", "url": url})
        assert (response.status_code, response.json()) == (400, {"error": HTTP_HOST_TEXT}), url
    assert env.remotes.connects == [] and await env.registry.list_all() == []
    assert "192.168" not in HTTP_HOST_TEXT  # the answer names the setting, never the address


async def test_a_stored_http_server_whose_host_left_the_list_is_shown_refused_and_removable(db, monkeypatch):
    opened: list[Any] = []
    monkeypatch.setattr(transport, "guarded_client", lambda *a, **k: opened.append(a))
    settings = _settings(f"mcp-http-{uuid.uuid4().hex[:8]}")  # the list is empty now
    registry = McpRegistry(db, settings.agent_id)
    await registry.upsert_server(ServerConfig(name="win", url=LAN))  # stored while the host was listed
    await registry.upsert_tools("win", [ToolConfig(server="win", tool="click", input_schema=OBJ, permission="allow")])
    dispatcher = _dispatcher()
    manager = McpClientManager(dispatcher, registry, settings)  # the real transport
    await manager.start()
    try:
        app = Starlette(routes=build_mcp_client_routes(database=db, settings=settings, manager=manager, bus=None))
        listing = (await _req(app, "GET", "/integrations/mcp/servers")).json()["servers"]
        assert [(server["name"], server["url"]) for server in listing] == [("win", LAN)]  # the owner still sees it
        assert listing[0]["blocked_by_ceiling"] is True and listing[0]["connected"] is False
        assert (
            listing[0]["skip_reason"]
            == "its address is http and its host is not in NOUS_MCP_CLIENT_ALLOW_PRIVATE_HOSTS"
        )
        assert "mcp__win__click" not in _names(dispatcher)  # the allowed tool is not offered to the model
        refreshed = await _req(app, "POST", "/integrations/mcp/servers/win/refresh")
        assert refreshed.status_code == 400 and refreshed.json()["error"].startswith("the address guard refused")
        assert opened == []  # nothing was sent to it
        assert (await _req(app, "DELETE", "/integrations/mcp/servers/win")).status_code == 200
        assert await registry.list_all() == []
    finally:
        await manager.close()


@pytest.mark.postgres_only
async def test_the_table_takes_http_and_https_and_no_other_scheme(session):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    agent = f"mcp-http-{uuid.uuid4().hex[:8]}"
    insert = text("INSERT INTO nous_system.mcp_servers (agent_id, name, url) VALUES (:a, :n, :u)")
    await session.execute(insert, {"a": agent, "n": "win", "u": LAN})
    await session.execute(insert, {"a": agent, "n": "alpha", "u": "https://alpha.example.com/mcp"})
    for name, url in (
        ("f", "ftp://192.168.1.127/mcp"),
        ("w", "ws://mcp.lan/mcp"),
        ("n", "192.168.1.127:8000/mcp"),
        ("h", "httpx://192.168.1.127/mcp"),
    ):
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                await session.execute(insert, {"a": agent, "n": name, "u": url})
