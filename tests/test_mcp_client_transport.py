"""F112 MCP client PR 1: the address guard of the client's HTTP connections. No network: the resolver is replaced
where a host name is resolved, and IP literals resolve locally."""

from __future__ import annotations

import asyncio
import inspect
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from nous.mcp_client import transport
from nous.mcp_client.bridge import ServerErrorReply
from nous.mcp_client.transport import (
    BlockedAddress,
    ResponseTooLarge,
    _GuardedBackend,
    _LimitedTransport,
    check_url,
    guarded_client,
    is_public_address,
)

NONE: frozenset[str] = frozenset()
LIMIT = 2_000_000
SECRET_IN_TEXT = "SECRET-d41e"


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.5.4",
        "192.168.1.141",
        "100.64.0.1",  # shared address space (a tailnet)
        "169.254.169.254",  # cloud metadata
        "169.254.1.1",  # link-local
        "0.0.0.0",
        "224.0.0.1",  # multicast
        "240.0.0.1",
        "255.255.255.255",
        "::1",
        "::",
        "fe80::1",
        "fe80::1%eth0",
        "fc00::1",
        "fd00:ec2::254",  # cloud metadata over IPv6
        "ff02::1",
        "::ffff:127.0.0.1",  # IPv4-mapped
        "::ffff:169.254.169.254",
        "::ffff:10.0.0.1",
        "64:ff9b::7f00:1",  # NAT64 of 127.0.0.1
        "2002:7f00:1::",  # 6to4 of 127.0.0.1
        "::7f00:1",
        "not-an-address",
    ],
)
def test_addresses_that_are_not_public_are_refused(ip):
    assert not is_public_address(ip)


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111", "::ffff:8.8.8.8"])
def test_public_addresses_are_allowed(ip):
    assert is_public_address(ip)


def _resolver(addresses: list[str]):
    async def resolve(host: str, port: int) -> list[str]:
        return addresses

    return resolve


@pytest.mark.parametrize(
    "url",
    [
        "http://mcp.example.com/mcp",
        "https://mcp.example.com/mcp?key=x",
        "https://user:pw@mcp.example.com/mcp",
        "https://127.0.0.1/mcp",
        "https://[::1]/mcp",
        "https://169.254.169.254/latest/meta-data",
    ],
)
async def test_check_url_refuses(url):
    with pytest.raises(BlockedAddress):
        await check_url(url, NONE)


async def test_check_url_resolves_the_host_and_refuses_any_private_answer(monkeypatch):
    monkeypatch.setattr(transport, "_resolve", _resolver(["93.184.216.34"]))
    await check_url("https://mcp.example.com/mcp", NONE)
    monkeypatch.setattr(transport, "_resolve", _resolver(["93.184.216.34", "10.0.0.5"]))
    with pytest.raises(BlockedAddress):
        await check_url("https://mcp.example.com/mcp", NONE)


async def test_an_allowed_private_host_is_not_resolved(monkeypatch):
    monkeypatch.setattr(transport, "_resolve", _resolver(["10.0.0.5"]))
    await check_url("https://Intranet.Lan/mcp", frozenset({"intranet.lan"}))
    with pytest.raises(BlockedAddress):
        await check_url("https://other.lan/mcp", frozenset({"intranet.lan"}))


async def test_a_redirect_hop_to_a_private_address_is_refused(monkeypatch):
    # The client follows no redirect (a 3xx fails the request); a hop the SDK or a later PR followed would be judged
    # by check_url, and its connection by the backend below.
    client = guarded_client(NONE, LIMIT)
    assert client.follow_redirects is False and client.trust_env is False
    monkeypatch.setattr(transport, "_resolve", _resolver(["192.168.1.141"]))
    with pytest.raises(BlockedAddress):
        await check_url("https://rebind.example.com/next", NONE)
    await client.aclose()


class _Inner:
    def __init__(self) -> None:
        self.dialed: list[str] = []

    async def connect_tcp(self, host: str, port: int, **_kwargs: Any) -> str:
        self.dialed.append(host)
        return f"stream to {host}"


async def test_the_backend_dials_the_address_it_checked(monkeypatch):
    inner = _Inner()
    backend = _GuardedBackend(inner, frozenset({"intranet.lan"}))  # type: ignore[arg-type]
    monkeypatch.setattr(transport, "_resolve", _resolver(["93.184.216.34"]))
    assert await backend.connect_tcp("mcp.example.com", 443) == "stream to 93.184.216.34"
    monkeypatch.setattr(transport, "_resolve", _resolver(["10.1.1.1"]))
    with pytest.raises(BlockedAddress):
        await backend.connect_tcp("rebind.example.com", 443)
    assert await backend.connect_tcp("intranet.lan", 443) == "stream to intranet.lan"
    with pytest.raises(BlockedAddress):
        await backend.connect_unix_socket("/var/run/docker.sock")
    assert inner.dialed == ["93.184.216.34", "intranet.lan"]


async def test_every_connection_of_the_real_client_goes_through_the_guard():
    client = guarded_client(NONE, LIMIT)
    assert isinstance(client._transport, _LimitedTransport)
    assert isinstance(client._transport._inner._pool._network_backend, _GuardedBackend)
    with pytest.raises(BlockedAddress):
        await client.get("https://127.0.0.1:9/mcp")  # refused before any socket is opened
    with pytest.raises(BlockedAddress):
        await client.get("https://[::ffff:169.254.169.254]/latest")
    await client.aclose()


async def test_the_session_refuses_a_url_before_connecting():
    with pytest.raises(BlockedAddress):
        async with transport.open_session("https://10.0.0.1/mcp", NONE):
            pass


async def test_a_url_with_a_query_string_is_never_requested(caplog):
    # The HTTP client logs every request URL at INFO: a URL that could carry a credential is refused first.
    caplog.set_level(logging.DEBUG)
    with pytest.raises(BlockedAddress) as refused:
        async with transport.open_session("https://mcp.example.com/mcp?key=SECRET-91c2", NONE):
            pass
    assert "SECRET-91c2" not in str(refused.value)
    assert all("SECRET-91c2" not in record.getMessage() for record in caplog.records)


def test_the_sdk_still_has_what_the_transport_uses():
    # CI installs the newest mcp 1.x the range allows, not the locked 1.26: pin the signatures the transport calls.
    from mcp import types
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    assert "http_client" in inspect.signature(streamable_http_client).parameters
    assert "params" in inspect.signature(ClientSession.list_tools).parameters
    assert "message_handler" in inspect.signature(ClientSession.__init__).parameters
    assert list(inspect.signature(ClientSession.send_request).parameters)[1:3] == ["request", "result_type"]
    assert types.PaginatedRequestParams(cursor="c").cursor == "c"
    request = types.CallToolRequest(params=types.CallToolRequestParams(name="t", arguments={"a": 1}))
    assert types.ClientRequest(request).root.method == "tools/call"
    assert isinstance(types.CONNECTION_CLOSED, int)


# --- the session: no output validation, pagination, server errors ---------------------------------------------------


class _FakeClientSession:
    """Stands in for ClientSession's requests: pages of tools, and one recorded tools/call."""

    def __init__(self, result: Any = None, error: BaseException | None = None) -> None:
        self.requests: list[Any] = []
        self.pages = {None: (["a", "b"], "p2"), "p2": (["c"], None)}
        self.result = result
        self.error = error

    async def send_request(self, request: Any, result_type: Any) -> Any:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.result

    async def list_tools(self, *, params: Any = None) -> Any:
        from mcp import types

        names, cursor = self.pages[params.cursor if params else None]
        tools = [types.Tool(name=n, inputSchema={"type": "object"}) for n in names]
        return types.ListToolsResult(tools=tools, nextCursor=cursor)

    async def call_tool(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("ClientSession.call_tool validates structured content: it must not be used")


async def test_list_tools_reads_every_page():
    session = transport.McpSession(_FakeClientSession())  # type: ignore[arg-type]
    assert [tool.name for tool in await session.list_tools()] == ["a", "b", "c"]


async def test_a_call_is_a_plain_request_and_its_structured_content_is_not_validated():
    from mcp import types
    from mcp.client.session import ClientSession

    # A result whose structured content breaks the tool's output schema. ClientSession.call_tool would check it
    # (and list the tools first): with a real ClientSession, that check raises.
    result = types.CallToolResult(content=[], structuredContent={"count": "not a number"})
    schema = {"type": "object", "properties": {"count": {"type": "integer"}}, "required": ["count"]}
    validate = getattr(ClientSession, "_validate_tool_result", None)  # private: checked while it exists
    if validate is not None:
        real = ClientSession.__new__(ClientSession)
        real._tool_output_schemas = {"t": schema}
        with pytest.raises(RuntimeError):
            await validate(real, "t", result)

    fake = _FakeClientSession(result=result)
    assert await transport.McpSession(fake).call_tool("t", {"x": 1}) is result  # type: ignore[arg-type]
    (request,) = fake.requests
    assert request.root.method == "tools/call"
    assert (request.root.params.name, request.root.params.arguments) == ("t", {"x": 1})


def _mcp_error(code: int, message: str) -> BaseException:
    from mcp.shared.exceptions import McpError
    from mcp.types import ErrorData

    return McpError(ErrorData(code=code, message=message))


def test_which_errors_keep_the_session():
    from mcp.types import CONNECTION_CLOSED, INVALID_PARAMS

    assert transport.is_server_error(_mcp_error(INVALID_PARAMS, "bad arguments"))
    assert transport.is_server_error(_mcp_error(-32600, "invalid request"))
    assert not transport.is_server_error(_mcp_error(CONNECTION_CLOSED, "Connection closed"))
    assert not transport.is_server_error(_mcp_error(32600, "Session terminated"))
    assert not transport.is_server_error(ConnectionResetError("reset"))


async def test_a_server_error_becomes_a_reply_without_the_servers_words():
    fake = _FakeClientSession(error=_mcp_error(-32602, f"bad key {SECRET_IN_TEXT}"))
    with pytest.raises(ServerErrorReply) as raised:
        await transport.McpSession(fake).call_tool("t", {})  # type: ignore[arg-type]
    assert raised.value.code == -32602 and SECRET_IN_TEXT not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__suppress_context__
    closed = _FakeClientSession(error=_mcp_error(-32000, "Connection closed"))
    with pytest.raises(Exception) as raised_closed:
        await transport.McpSession(closed).call_tool("t", {})  # type: ignore[arg-type]
    assert not isinstance(raised_closed.value, ServerErrorReply)


# --- the response limit ---------------------------------------------------------------------------------------------


class _Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


def _limited(handler, limit: int) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=_LimitedTransport(httpx.MockTransport(handler), limit))


async def test_a_json_body_past_the_limit_fails_and_one_under_it_reads():
    client = _limited(lambda request: httpx.Response(200, json={"x": "y" * 500}), 300)
    with pytest.raises(ResponseTooLarge):
        await client.post("https://mcp.example.com/mcp", json={})
    ok = _limited(lambda request: httpx.Response(200, json={"x": "y"}), 300)
    assert (await ok.post("https://mcp.example.com/mcp", json={})).json() == {"x": "y"}


async def test_an_event_stream_past_the_limit_fails_while_it_is_read():
    events = [b"event: message\ndata: " + b"z" * 100 + b"\n\n"] * 10
    client = _limited(
        lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_Chunks(events)), 500
    )
    seen = 0
    with pytest.raises(ResponseTooLarge):
        async with client.stream("GET", "https://mcp.example.com/mcp") as response:
            async for chunk in response.aiter_bytes():
                seen += len(chunk)
    assert seen <= 500


async def test_a_compressed_body_is_refused():
    client = _limited(lambda request: httpx.Response(200, headers={"content-encoding": "gzip"}, content=b"x"), 300)
    with pytest.raises(ResponseTooLarge):
        await client.get("https://mcp.example.com/mcp")


def test_the_guarded_client_asks_for_uncompressed_bodies():
    client = guarded_client(NONE, LIMIT)
    assert client.headers["accept-encoding"] == "identity"


# --- logging --------------------------------------------------------------------------------------------------------


def test_the_request_line_of_a_client_host_is_dropped_and_others_are_kept(monkeypatch):
    monkeypatch.setattr(transport, "_QUIET_HOSTS", {"mcp.example.com"})
    (quiet,) = [f for f in logging.getLogger("httpx").filters if isinstance(f, transport._QuietMcpRequests)]

    def record(url: str) -> logging.LogRecord:
        return logging.LogRecord(
            "httpx", logging.INFO, __file__, 1, 'HTTP Request: %s %s "%s %d %s"',
            ("POST", httpx.URL(url), "HTTP/1.1", 200, "OK"), None,
        )

    assert not quiet.filter(record("https://MCP.example.com/mcp/secret-path"))
    assert quiet.filter(record("https://api.telegram.org/bot1/sendMessage"))
    assert logging.getLogger("mcp.client.streamable_http").level == logging.WARNING


def test_no_proxy_from_the_environment(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://10.0.0.9:3128")
    client = guarded_client(NONE, LIMIT)
    # A proxy mount would carry requests past the guarded transport; with its own transport and trust_env off, the
    # client builds none.
    assert client.trust_env is False and not client._mounts
    assert isinstance(client._transport._inner, httpx.AsyncHTTPTransport)


# --- a real MCP server in process (the SDK's own server over ASGI: no port, no network) -----------------------------


@asynccontextmanager
async def _in_process_server(limit: int, *, json_response: bool = True, gate: Any = None):
    """A connector like transport.open_session (its session part, ``_session_over``), to the SDK's FastMCP server in
    this process, through the response limit. The address guard is not in the path (there is no socket)."""
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("inproc", stateless_http=True, json_response=json_response)

    @server.tool()
    def echo(text: str, times: int = 1) -> str:
        """Repeat the text."""
        return text * times

    @server.tool()
    async def slow(seconds: float) -> str:
        """Answer late."""
        await asyncio.sleep(seconds)
        return "late"

    @asynccontextmanager
    async def connect(url: str, allow: frozenset[str]):
        served = app if gate is None else gate(app)
        client = httpx.AsyncClient(transport=_LimitedTransport(httpx.ASGITransport(app=served), limit))
        async with client, transport._session_over(client, url) as session:
            yield session

    app = server.streamable_http_app()
    async with server.session_manager.run():
        yield connect


async def test_a_real_server_lists_and_answers_through_the_session():
    async with _in_process_server(LIMIT) as connect:
        async with connect("http://127.0.0.1:8000/mcp", NONE) as session:
            assert [tool.name for tool in await session.list_tools()] == ["echo", "slow"]
            result = await session.call_tool("echo", {"text": "hi", "times": 2})
            assert (result.content[0].text, result.isError) == ("hihi", False)


@asynccontextmanager
async def _in_process_manager(limit: int, *, timeout: int = 30, json_response: bool = True, gate: Any = None):
    """A started manager whose one pinned server, inproc, is the in-process FastMCP server. Yields the dispatcher
    and a list that gets one entry per connection opened."""
    from nous.api.tools import ToolDispatcher
    from nous.config import Settings
    from nous.mcp_client import McpClientManager
    from nous.mcp_client.config import ServerConfig, ToolConfig

    echo = {"type": "object", "properties": {"text": {"type": "string"}, "times": {"type": "integer"}}}
    slow = {"type": "object", "properties": {"seconds": {"type": "number"}}}

    class Rows:
        async def list_enabled(self):
            config = ServerConfig(name="inproc", url="https://127.0.0.1:8000/mcp", call_timeout_seconds=timeout)
            return [
                (
                    config,
                    [
                        ToolConfig(server="inproc", tool="echo", input_schema=echo, permission="allow"),
                        ToolConfig(server="inproc", tool="slow", input_schema=slow, permission="allow"),
                    ],
                )
            ]

    connects: list[int] = []
    async with _in_process_server(limit, json_response=json_response, gate=gate) as connect:

        @asynccontextmanager
        async def plain(url: str, allow: frozenset[str]):  # the stored URL is https; the in-process app is http
            connects.append(1)
            async with connect(url.replace("https://", "http://"), allow) as session:
                yield session

        settings = Settings(
            _env_file=None, ANTHROPIC_API_KEY="k", mcp_client_pinned_servers="inproc=127.0.0.1:8000;echo|slow"
        )
        d = ToolDispatcher()
        manager = McpClientManager(d, Rows(), settings, connect=plain)
        await manager.start()
        try:
            yield d, connects
        finally:
            await manager.close()


async def test_the_manager_over_a_real_server_fails_a_call_past_the_limit_and_recovers():
    async with _in_process_manager(1200) as (d, connects):
        text, is_error = await d.dispatch("mcp__inproc__echo", {"text": "ok"})
        assert not is_error and text.endswith("\nok")
        text, is_error = await d.dispatch("mcp__inproc__echo", {"text": "z", "times": 5000})
        assert is_error and text.startswith('Tool error: the call to the MCP server "inproc" failed')
        text, is_error = await d.dispatch("mcp__inproc__echo", {"text": "again"})
        assert not is_error and text.endswith("\nagain")
        assert len(connects) == 2  # the over-limit JSON response ended the session at once; the next call reconnected


async def test_a_late_reply_to_a_timed_out_call_does_not_end_the_session():
    async with _in_process_manager(LIMIT, timeout=1) as (d, connects):
        text, is_error = await d.dispatch("mcp__inproc__slow", {"seconds": 1.6})
        assert is_error and "timed out after 1 s" in text
        await asyncio.sleep(1.0)  # the reply arrives for a request nobody waits on any more
        text, is_error = await d.dispatch("mcp__inproc__echo", {"text": "still"})
        assert not is_error and text.endswith("\nstill")
        assert len(connects) == 1


async def test_an_event_stream_past_the_limit_fails_the_call_at_its_timeout():
    # The SDK ends an event-stream response quietly when reading it fails: the call ends at its timeout, and the
    # session stays usable.
    async with _in_process_manager(1200, timeout=2, json_response=False) as (d, connects):
        text, is_error = await d.dispatch("mcp__inproc__echo", {"text": "z", "times": 5000})
        assert is_error and "timed out after 2 s" in text
        text, is_error = await d.dispatch("mcp__inproc__echo", {"text": "small"})
        assert not is_error and text.endswith("\nsmall")
        assert len(connects) == 1


def _quiet_filter() -> logging.Filter:
    (quiet,) = [f for f in logging.getLogger("httpx").filters if isinstance(f, transport._QuietMcpRequests)]
    return quiet


def _request_line(url: str) -> logging.LogRecord:
    return logging.LogRecord(
        "httpx", logging.INFO, __file__, 1, 'HTTP Request: %s %s "%s %d %s"',
        ("POST", httpx.URL(url), "HTTP/1.1", 200, "OK"), None,
    )


def test_a_punycode_host_is_quiet_whichever_form_the_url_was_stored_in(monkeypatch):
    monkeypatch.setattr(transport, "_QUIET_HOSTS", {"xn--bcher-kva.example"})
    assert not _quiet_filter().filter(_request_line("https://xn--bcher-kva.example/mcp"))
    monkeypatch.setattr(transport, "_QUIET_HOSTS", {"bücher.example"})
    assert not _quiet_filter().filter(_request_line("https://xn--bcher-kva.example/mcp"))


@pytest.mark.parametrize(
    ("logger_name", "module_file", "message"),
    [
        ("", "shared/session.py", "Failed to validate notification: bad. Message was: %s"),  # the root logger
        ("mcp.client.streamable_http", "client/streamable_http.py", "Unknown SSE event: %s"),
        ("client", "client/session.py", "Tool %s not listed by server, cannot validate any structured content"),
    ],
)
def test_the_sdks_warning_lines_lose_what_the_server_sent(caplog, logger_name, module_file, message):
    import mcp as mcp_package

    path = str(Path(mcp_package.__file__).parent / module_file)
    try:
        raise ValueError("SERVER-TEXT-91f0 in a traceback")
    except ValueError:
        exc_info = sys.exc_info()
    name = logger_name or "root"
    record = logging.LogRecord(name, logging.WARNING, path, 7, message, ("SERVER-TEXT-91f0",), exc_info)
    with caplog.at_level(logging.WARNING):
        logging.getLogger(logger_name).handle(record)
    (seen,) = [r for r in caplog.records if r is record]
    assert "SERVER-TEXT-91f0" not in seen.getMessage() and "withheld" in seen.getMessage()
    assert seen.exc_info is None


def test_other_warning_lines_pass_unchanged(caplog):
    record = logging.LogRecord("root", logging.WARNING, __file__, 7, "nous says %s", ("hello",), None)
    with caplog.at_level(logging.WARNING):
        logging.getLogger().handle(record)
    assert record.getMessage() == "nous says hello"


class _BadGateway:
    """Wraps the in-process app: once ``failing`` is set, every new request is answered 502."""

    def __init__(self) -> None:
        self.failing = False

    def __call__(self, app: Any) -> Any:
        async def served(scope: Any, receive: Any, send: Any) -> None:
            if self.failing and scope["type"] == "http":
                await send({"type": "http.response.start", "status": 502, "headers": []})
                await send({"type": "http.response.body", "body": b""})
                return
            await app(scope, receive, send)

        return served


async def test_a_mid_session_502_ends_the_waiting_calls_well_inside_their_timeout():
    gateway = _BadGateway()
    async with _in_process_manager(LIMIT, timeout=20, gate=gateway) as (d, _connects):
        assert (await d.dispatch("mcp__inproc__echo", {"text": "warm"}))[1] is False
        slow = asyncio.create_task(d.dispatch("mcp__inproc__slow", {"seconds": 15}))
        await asyncio.sleep(0.3)
        gateway.failing = True
        loop = asyncio.get_running_loop()
        started = loop.time()
        text, is_error = await asyncio.wait_for(d.dispatch("mcp__inproc__echo", {"text": "x"}), 10)
        assert is_error
        text, is_error = await asyncio.wait_for(slow, 10)
        assert is_error
        assert loop.time() - started < 10  # the 502 ended both calls, not their 20 s timeout
