"""F112 MCP client PR 2c: stored credentials at work. The headers on every request of a session and on no other origin,
a 401 in the middle of a session, a missing key, the reload's credential version and the result scrub.

A real MCP server in process (the SDK's FastMCP over ASGI: no port, no network) where the transport matters, fakes
elsewhere. Every credential-looking value here is made up."""

from __future__ import annotations

import asyncio
import base64
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from nous.api import tool_classes
from nous.api.tools import ToolDispatcher
from nous.config import Settings
from nous.mcp_client import McpClientManager, transport
from nous.mcp_client.config import ServerConfig, ToolConfig
from nous.mcp_client.secrets import SecretBox, StoredSecret, aad_fields, seal_headers
from nous.mcp_client.transport import _LimitedTransport

AGENT = "mcp-credentials"
KEY = bytes(range(32))
URL = "https://127.0.0.1:8000/mcp"
VALUE = "Bearer test-key-0000000000000000"
LIMIT = 2_000_000
ECHO = {"type": "object", "properties": {"text": {"type": "string"}}}
REFUSED = (
    'Tool error: the MCP server "inproc" refused the stored credentials. The owner can replace them on the '
    "Integrations screen."
)


@pytest.fixture(autouse=True)
def _restore_mcp_tools():
    saved = dict(tool_classes._MCP_TOOLS)
    yield
    tool_classes._MCP_TOOLS.clear()
    tool_classes._MCP_TOOLS.update(saved)


def _key_file(tmp_path) -> str:
    path = tmp_path / "mcp-client.key"
    path.write_bytes(base64.b64encode(KEY) + b"\n")
    return str(path)


def _secret(name: str, url: str, headers: dict[str, str], secret_id: uuid.UUID | None = None) -> StoredSecret:
    sealed = seal_headers(SecretBox(KEY), aad_fields(AGENT, name, url, "headers"), headers)
    return StoredSecret(
        secret_id or uuid.uuid4(),
        sealed.kind,
        sealed.key_id,
        sealed.nonce,
        sealed.ciphertext,
        sealed.header_names,
        None,
    )


class Rows:
    """The registry's two reads, from lists the test changes."""

    def __init__(self, rows: list[tuple[ServerConfig, list[ToolConfig]]], secrets: dict[str, StoredSecret]) -> None:
        self.rows = rows
        self.secrets = secrets

    async def list_enabled(self) -> list[tuple[ServerConfig, list[ToolConfig]]]:
        return self.rows

    async def list_secrets(self) -> dict[str, StoredSecret]:
        return self.secrets


class Gate:
    """Wraps the in-process app: records every request's method and headers; once ``status`` is set, every new POST
    is answered with it (with no body)."""

    def __init__(self) -> None:
        self.status: int | None = None
        self.seen: list[tuple[str, dict[str, str]]] = []

    def __call__(self, app: Any) -> Any:
        async def served(scope: Any, receive: Any, send: Any) -> None:
            if scope["type"] == "http":
                headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
                self.seen.append((scope["method"], headers))
                if self.status is not None and scope["method"] == "POST":
                    await send({"type": "http.response.start", "status": self.status, "headers": []})
                    await send({"type": "http.response.body", "body": b""})
                    return
            await app(scope, receive, send)

        return served


@asynccontextmanager
async def _in_process(gate: Gate):
    """A connector like ``transport.open_session`` (its session part) to the SDK's FastMCP server in this process,
    with the manager's ``auth`` on the client, as ``open_session`` sets it. Yields the connector."""
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("inproc", stateless_http=True, json_response=True)

    @server.tool()
    def echo(text: str) -> str:
        """Repeat the text."""
        return text

    app = server.streamable_http_app()

    @asynccontextmanager
    async def connect(url: str, allow: frozenset[str], *, auth: httpx.Auth | None = None):
        client = httpx.AsyncClient(transport=_LimitedTransport(httpx.ASGITransport(app=gate(app)), LIMIT), auth=auth)
        async with client, transport._session_over(client, url) as session:
            yield session

    async with server.session_manager.run():
        yield connect


@asynccontextmanager
async def _manager(tmp_path, gate: Gate, *, headers: dict[str, str] | None = None, timeout: int = 20):
    """A started manager whose one server, inproc, is the in-process server, with ``headers`` stored for it. Yields
    the dispatcher and the manager."""
    config = ServerConfig(name="inproc", url=URL, call_timeout_seconds=timeout)
    secrets = {"inproc": _secret("inproc", URL, headers)} if headers else {}
    rows = Rows([(config, [ToolConfig(server="inproc", tool="echo", input_schema=ECHO, permission="allow")])], secrets)
    settings = Settings(_env_file=None, ANTHROPIC_API_KEY="k", agent_id=AGENT, mcp_client_key_file=_key_file(tmp_path))
    async with _in_process(gate) as connect:
        d = ToolDispatcher()
        manager = McpClientManager(d, rows, settings, connect=connect)
        await manager.start()
        try:
            yield d, manager
        finally:
            await manager.close()


# --- a 401 in the middle of a session (written first: it picks the mechanism) ---------------------------------------


async def test_a_401_mid_session_fails_the_call_at_once_with_the_fixed_message(tmp_path):
    gate = Gate()
    async with _manager(tmp_path, gate, headers={"Authorization": VALUE}, timeout=20) as (d, manager):
        assert (await d.dispatch("mcp__inproc__echo", {"text": "warm"}))[1] is False
        assert manager.credentials_refused() == set()
        gate.status = 401
        loop = asyncio.get_running_loop()
        started = loop.time()
        text, is_error = await d.dispatch("mcp__inproc__echo", {"text": "x"})
        assert loop.time() - started < 2  # not the call's 20 s timeout
        assert (text, is_error) == (REFUSED, True)
        assert manager.credentials_refused() == {"inproc"}
        # The next call connects again and is refused at initialize; the one after waits out the connect backoff.
        for _ in range(2):
            assert await d.dispatch("mcp__inproc__echo", {"text": "y"}) == (REFUSED, True)
        gate.status = None
        await asyncio.sleep(1.2)  # past the first backoff
        text, is_error = await d.dispatch("mcp__inproc__echo", {"text": "again"})
        assert not is_error and text.endswith("\nagain")
        assert manager.credentials_refused() == set()  # a successful call clears it


async def test_a_401_at_the_first_connect_is_the_same_message(tmp_path):
    gate = Gate()
    gate.status = 401
    async with _manager(tmp_path, gate, headers={"Authorization": VALUE}) as (d, manager):
        loop = asyncio.get_running_loop()
        started = loop.time()
        assert await d.dispatch("mcp__inproc__echo", {"text": "x"}) == (REFUSED, True)
        assert await d.dispatch("mcp__inproc__echo", {"text": "x"}) == (REFUSED, True)  # inside the backoff
        assert loop.time() - started < 2


async def test_a_401_to_a_server_without_credentials_says_it_asks_for_them(tmp_path):
    gate = Gate()
    async with _manager(tmp_path, gate) as (d, manager):
        assert (await d.dispatch("mcp__inproc__echo", {"text": "warm"}))[1] is False
        gate.status = 401
        text, is_error = await d.dispatch("mcp__inproc__echo", {"text": "x"})
        assert is_error and text == (
            'Tool error: the MCP server "inproc" asks for credentials. The owner can add them on the Integrations '
            "screen."
        )


# --- the headers: every request of a session, and no other origin ----------------------------------------------------


async def test_the_headers_reach_every_request_of_a_session_and_of_a_probe(tmp_path):
    gate = Gate()
    headers = {"Authorization": VALUE, "X-Api-Key": "test-key-1111111111"}
    async with _manager(tmp_path, gate, headers=headers) as (d, manager):
        assert (await d.dispatch("mcp__inproc__echo", {"text": "hi"}))[1] is False
        listed = await manager.probe("inproc", URL, headers)
        assert [tool.tool for tool in listed] == ["echo"]
        assert len([method for method, _h in gate.seen if method == "POST"]) >= 5  # initialize, initialized, call...
        for _method, seen in gate.seen:
            assert (seen.get("authorization"), seen.get("x-api-key")) == (VALUE, "test-key-1111111111")
        gate.seen.clear()
        await manager.probe("inproc", URL)  # a probe never attaches the stored credential
        assert gate.seen and all("authorization" not in seen for _m, seen in gate.seen)


@pytest.mark.parametrize(
    "url,sent",
    [
        ("https://mcp.example.com/mcp", True),
        ("https://mcp.example.com:443/other/path", True),
        ("https://MCP.Example.com/mcp", True),
        ("https://mcp.example.com:8443/mcp", False),
        ("http://mcp.example.com/mcp", False),
        ("https://evil.example.net/mcp", False),
        ("https://mcp.example.com.evil.net/mcp", False),
    ],
)
async def test_the_credential_goes_to_the_servers_own_origin_only(url, sent):
    from nous.mcp_client.manager import _ServerAuth

    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        return httpx.Response(200)

    auth = _ServerAuth("https://mcp.example.com/mcp", lambda: {"Authorization": VALUE})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), auth=auth) as client:
        await client.post(url)
    assert (seen[0].get("authorization") == VALUE) is sent


async def test_a_401_or_403_is_kept_until_a_2xx_and_only_for_a_post():
    from nous.mcp_client.manager import _ServerAuth

    statuses = iter([401, 200, 403, 401, 204])
    dropped: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(next(statuses))

    auth = _ServerAuth("https://mcp.example.com/mcp", dict, on_refused=lambda: dropped.append(1))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), auth=auth) as client:
        await client.post("https://mcp.example.com/mcp")
        assert auth.refused == 401 and dropped == [1]
        await client.post("https://mcp.example.com/mcp")
        assert auth.refused is None
        await client.post("https://mcp.example.com/mcp")
        assert auth.refused == 403 and dropped == [1]  # a 403 does not drop the connection
        await client.get("https://mcp.example.com/mcp")  # the event stream's answers do not count
        assert auth.refused == 403
        await client.post("https://mcp.example.com/mcp")
        assert auth.refused is None


# --- fakes for the rest ----------------------------------------------------------------------------------------------


class Fake:
    """A fake connector: records each connection's URL and auth; a call returns ``result``, a probe lists ``tools``."""

    def __init__(self) -> None:
        self.connects: list[tuple[str, Any]] = []
        self.closed = 0
        self.result: Any = None
        self.tools: list[Any] = []

    @asynccontextmanager
    async def connect(self, url: str, allow: frozenset[str], *, auth: Any = None):
        self.connects.append((url, auth))
        try:
            yield _FakeSession(self)
        finally:
            self.closed += 1


class _FakeSession:
    def __init__(self, fake: Fake) -> None:
        self.fake = fake

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        from mcp.types import CallToolResult, TextContent

        return self.fake.result or CallToolResult(content=[TextContent(type="text", text="hello")])

    async def list_tools(self) -> list[Any]:
        return list(self.fake.tools)


async def _sent_by(auth: Any, url: str = "https://alpha.example.com/mcp") -> dict[str, str]:
    """The credential headers ``auth`` sets on a request to ``url``."""
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), auth=auth) as client:
        await client.post(url)
    return {k: v for k, v in seen[0].items() if k in ("authorization", "x-api-key")}


ALPHA = "https://alpha.example.com/mcp"
BETA = "https://beta.example.com/mcp"


def _row(name: str, url: str) -> tuple[ServerConfig, list[ToolConfig]]:
    return ServerConfig(name=name, url=url), [
        ToolConfig(server=name, tool="echo", input_schema=ECHO, permission="allow")
    ]


def _settings(key_file: str = "") -> Settings:
    return Settings(_env_file=None, ANTHROPIC_API_KEY="k", agent_id=AGENT, mcp_client_key_file=key_file)


# --- a credential that cannot be opened ------------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["no key", "another key", "another url"])
async def test_a_server_whose_credential_cannot_be_opened_is_skipped_and_others_register(tmp_path, caplog, state):
    key_file = _key_file(tmp_path)
    secret = _secret("alpha", ALPHA, {"Authorization": VALUE})
    expected = "credentials unavailable: cannot be decrypted"
    if state == "no key":
        key_file, expected = "", "credentials unavailable: no key file"
    elif state == "another key":
        (tmp_path / "mcp-client.key").write_bytes(base64.b64encode(bytes(range(1, 33))))
        expected = "credentials unavailable: stored with another key"
    else:
        secret = _secret("alpha", "https://alpha.example.com/old", {"Authorization": VALUE})
    rows = Rows([_row("alpha", ALPHA), _row("beta", BETA)], {"alpha": secret})
    d, fake = ToolDispatcher(), Fake()
    caplog.set_level(logging.DEBUG)
    manager = McpClientManager(d, rows, _settings(key_file), connect=fake.connect)
    await manager.start()
    assert [t["name"] for t in d.tool_definitions()] == ["mcp__beta__echo"]
    alpha, beta = manager.plan(rows.rows, rows.secrets)
    assert alpha.reason == expected and alpha.tools[0].skip == "credentials" and alpha.tools[0].reason == expected
    assert beta.reason is None and beta.credentials is None
    assert any("'alpha'" in r.getMessage() and expected in r.getMessage() for r in caplog.records)
    assert all("test-key" not in r.getMessage() for r in caplog.records)
    await manager.close()


# --- the reload: the credential version ------------------------------------------------------------------------------


async def test_a_reload_keeps_an_unchanged_server_and_drops_one_whose_credential_changed(tmp_path):
    secrets = {
        "alpha": _secret("alpha", ALPHA, {"Authorization": VALUE}),
        "beta": _secret("beta", BETA, {"X-Api-Key": "test-key-1111"}),
    }
    rows = Rows([_row("alpha", ALPHA), _row("beta", BETA)], secrets)
    d, fake = ToolDispatcher(), Fake()
    manager = McpClientManager(d, rows, _settings(_key_file(tmp_path)), connect=fake.connect)
    await manager.start()
    for tool in ("mcp__alpha__echo", "mcp__beta__echo"):
        assert (await d.dispatch(tool, {"text": "x"}))[1] is False
    assert len(fake.connects) == 2
    assert await _sent_by(fake.connects[0][1]) == {"authorization": VALUE}

    await manager.reload()  # nothing changed: both connections kept
    for tool in ("mcp__alpha__echo", "mcp__beta__echo"):
        assert (await d.dispatch(tool, {"text": "x"}))[1] is False
    assert (len(fake.connects), fake.closed) == (2, 0)

    old_auth = manager._servers["beta"].auth
    rows.secrets["beta"] = _secret("beta", BETA, {"X-Api-Key": "test-key-2222"})  # replaced: a new row id
    await manager.reload()
    assert fake.closed == 1  # beta's connection, not alpha's
    assert (await d.dispatch("mcp__beta__echo", {"text": "x"}))[1] is False
    assert fake.connects[-1][0] == BETA
    assert await _sent_by(fake.connects[-1][1], BETA) == {"x-api-key": "test-key-2222"}
    # The retiring server keeps what it was built with: it never re-reads by name.
    assert await _sent_by(old_auth, BETA) == {"x-api-key": "test-key-1111"}

    del rows.secrets["alpha"]  # removed
    await manager.reload()
    assert fake.closed == 2
    assert (await d.dispatch("mcp__alpha__echo", {"text": "x"}))[1] is False
    assert await _sent_by(fake.connects[-1][1]) == {}
    await manager.close()


async def test_an_in_place_update_of_the_same_row_keeps_the_connection(tmp_path):
    secret_id = uuid.uuid4()
    rows = Rows([_row("alpha", ALPHA)], {"alpha": _secret("alpha", ALPHA, {"Authorization": VALUE}, secret_id)})
    d, fake = ToolDispatcher(), Fake()
    manager = McpClientManager(d, rows, _settings(_key_file(tmp_path)), connect=fake.connect)
    await manager.start()
    assert (await d.dispatch("mcp__alpha__echo", {"text": "x"}))[1] is False
    rows.secrets["alpha"] = _secret("alpha", ALPHA, {"Authorization": "Bearer test-key-3333333333"}, secret_id)
    await manager.reload()  # the same version (a later token refresh): the connection stays
    assert (await d.dispatch("mcp__alpha__echo", {"text": "x"}))[1] is False
    assert (len(fake.connects), fake.closed) == (1, 0)
    await manager.close()


# --- the result scrub ------------------------------------------------------------------------------------------------


async def test_a_result_that_repeats_the_credential_is_scrubbed(tmp_path):
    from mcp.types import CallToolResult, TextContent

    headers = {"Authorization": VALUE, "X-Short": "abc1234"}  # under 8 characters: not looked for
    rows = Rows([_row("alpha", ALPHA)], {"alpha": _secret("alpha", ALPHA, headers)})
    d, fake = ToolDispatcher(), Fake()
    manager = McpClientManager(d, rows, _settings(_key_file(tmp_path)), connect=fake.connect)
    await manager.start()
    fake.result = CallToolResult(
        content=[
            TextContent(type="text", text=f"you sent {VALUE}"),
            TextContent(type="text", text="token test-key-0000000000000000 and abc1234"),
            TextContent(type="text", text="split test-key-0000​000000000000"),  # the cleaning joins it
        ]
    )
    text, is_error = await d.dispatch("mcp__alpha__echo", {"text": "x"})
    assert not is_error and "test-key-0000" not in text
    assert text.endswith("you sent [withheld]\ntoken [withheld] and abc1234\nsplit [withheld]")
    structured = {"echo": {"auth": VALUE}, "list": ["test-key-0000000000000000"]}
    fake.result = CallToolResult(content=[], structuredContent=structured)
    text, _ = await d.dispatch("mcp__alpha__echo", {"text": "x"})
    assert "test-key-0000" not in text and '"auth": "[withheld]"' in text
    await manager.close()


def test_the_needles_and_the_scrub_before_the_cut():
    from mcp.types import CallToolResult, TextContent

    from nous.mcp_client.bridge import credential_needles, flatten_result

    values = ["Bearer test-key-0000000000000000", "short", "Token  test-key-1111", "basic dGVzdDp0ZXN0"]
    assert credential_needles(values) == (
        "Bearer test-key-0000000000000000",
        "test-key-0000000000000000",
        "Token  test-key-1111",
        "basic dGVzdDp0ZXN0",
        "test-key-1111",
        "dGVzdDp0ZXN0",
    )
    raw = CallToolResult(content=[TextContent(type="text", text="x" * 5 + VALUE)])
    text, _ = flatten_result(raw, 12, credential_needles([VALUE]))
    assert text.startswith("xxxxx[withhe") and "test-key" not in text  # scrubbed before the cut, not after


# --- the probe: credentials refused, and a listing that repeats them ------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
async def test_a_probe_refused_by_the_server_says_whether_credentials_were_sent(tmp_path, status):
    from nous.mcp_client.manager import PROBE_ASKS, PROBE_REFUSED, ProbeCredentials

    gate = Gate()
    gate.status = status
    async with _manager(tmp_path, gate) as (_d, manager):
        with pytest.raises(ProbeCredentials) as with_headers:
            await manager.probe("inproc", URL, {"Authorization": VALUE})
        assert str(with_headers.value) == PROBE_REFUSED
        with pytest.raises(ProbeCredentials) as without:
            await manager.probe("inproc", URL)
        assert str(without.value) == PROBE_ASKS


@pytest.mark.parametrize(
    "where",
    [
        {"name": "lookup", "description": f"Send {VALUE} to call it"},
        {"name": "lookup", "description": "Uses key test-key-0000000000000000"},
        {"name": "lookup", "description": "split test-key-0000​000000000000"},  # what prepare_tool makes of it
        {
            "name": "lookup",
            "description": "d",
            "inputSchema": {"type": "object", "default": {"k": "test-key-0000000000000000"}},
        },
        {"name": "test-key-0000000000000000", "description": "d"},
    ],
)
async def test_a_listing_that_repeats_the_credential_it_was_sent_is_refused(where):
    from mcp.types import Tool

    from nous.mcp_client.manager import LISTING_REPEATS, ProbeCredentials

    fake = Fake()
    fake.tools = [Tool(**{"inputSchema": ECHO, **where})]
    manager = McpClientManager(ToolDispatcher(), Rows([], {}), _settings(), connect=fake.connect)
    with pytest.raises(ProbeCredentials) as refused:
        await manager.probe("alpha", ALPHA, {"Authorization": VALUE})
    assert str(refused.value) == LISTING_REPEATS
    assert [tool.tool for tool in await manager.probe("alpha", ALPHA)] == [where["name"]]  # nothing sent, no check


# --- nothing of the credential in a log, an event or an error --------------------------------------------------------


async def test_no_credential_value_reaches_a_log_record_or_an_event(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    gate = Gate()
    events: list[Any] = []

    class Bus:
        async def emit(self, event: Any) -> None:
            events.append(event)

    async with _manager(tmp_path, gate, headers={"Authorization": VALUE}) as (d, manager):
        manager._bus = Bus()
        await d.dispatch("mcp__inproc__echo", {"text": "hi"})
        await manager.probe("inproc", URL, {"Authorization": VALUE})
        gate.status = 401
        await d.dispatch("mcp__inproc__echo", {"text": "x"})
        with pytest.raises(Exception) as refused:
            await manager.probe("inproc", URL, {"Authorization": VALUE})
        assert "test-key" not in str(refused.value)
        await manager.reload()
    assert events
    for event in events:
        assert "test-key" not in repr(event.data)
    for record in caplog.records:
        assert "test-key" not in record.getMessage()
        if record.exc_info:
            assert "test-key" not in logging.Formatter().formatException(record.exc_info)


# --- 8.1 item 10: what the tool-call log and the execution ledger receive --------------------------------------------


async def test_the_recorder_and_the_ledger_get_the_scrubbed_text(tmp_path):
    from mcp.types import CallToolResult, TextContent
    from test_runner_authorization import _run_loop
    from test_runner_authorization import _settings as _runner_settings
    from test_runner_background import _MockBrain, _MockCognitive, _MockHeart

    from nous.api.execution_context import ExecutionContext
    from nous.api.models import ApiResponse
    from nous.api.runner import AgentRunner

    rows = Rows([_row("alpha", ALPHA)], {"alpha": _secret("alpha", ALPHA, {"Authorization": VALUE})})
    d, fake = ToolDispatcher(), Fake()
    fake.result = CallToolResult(content=[TextContent(type="text", text=f"echo {VALUE}")])
    manager = McpClientManager(d, rows, _settings(_key_file(tmp_path)), connect=fake.connect)
    await manager.start()
    runner = AgentRunner(_MockCognitive(), _MockBrain(), _MockHeart(), _runner_settings())
    runner.set_dispatcher(d)
    recorded: list[Any] = []
    closed: list[Any] = []

    class Recorder:
        def record(self, record: Any) -> None:
            recorded.append(record)

    async def ledger_close(entry_id: Any, status: str, result_summary: str | None, *args: Any, **kwargs: Any) -> None:
        closed.append(result_summary)

    runner.set_tool_call_recorder(Recorder())  # type: ignore[arg-type]
    runner._ledger_close = ledger_close  # type: ignore[method-assign]
    calls = 0

    async def call_api(system_prompt, messages, tools=None, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            use = {"type": "tool_use", "id": "t1", "name": "mcp__alpha__echo", "input": {"text": "hi"}}
            return ApiResponse(content=[use], stop_reason="tool_use")
        return ApiResponse(content=[{"type": "text", "text": "done"}], stop_reason="end_turn")

    runner._call_api = call_api
    context = ExecutionContext(kind="interactive", session_id="s1")
    _text, results, _usage, _thinking = await _run_loop(runner, context=context)
    assert (results[0].result or "").endswith("echo [withheld]")
    assert recorded and all("test-key" not in (r.result_preview + (r.error_text or "")) for r in recorded)
    assert "echo [withheld]" in recorded[0].result_preview
    assert closed and all("test-key" not in (summary or "") for summary in closed)
    await manager.close()
