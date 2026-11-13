"""F112 MCP client PR 2d: using and refreshing OAuth tokens. Two requests with an expired token make one refresh; a
refresh that loses its ``UPDATE ... WHERE id`` is discarded; a cancelled caller leaves the refresh to finish; a 401
refreshes only when the stored token is the one that was sent, and retries once; a failed refresh makes the server need
a new sign-in, fails fast, and never starts a login.

A fake provider on ``httpx.MockTransport`` and, where the session matters, the SDK's FastMCP in process (no port, no
network). Every token here is made up."""

from __future__ import annotations

import asyncio
import base64
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from nous.api import tool_classes
from nous.api.tools import ToolDispatcher
from nous.config import Settings
from nous.mcp_client import McpClientManager, transport
from nous.mcp_client.config import ServerConfig, ToolConfig
from nous.mcp_client.secrets import (
    OAuthRecord,
    SealedSecret,
    SecretBox,
    StoredSecret,
    aad_fields,
    open_credential,
    seal_oauth,
)
from nous.mcp_client.transport import _LimitedTransport

AGENT = "mcp-oauth-refresh"
KEY = bytes(range(32))
ALPHA = "https://alpha.example.com/mcp"
INPROC = "https://127.0.0.1:8000/mcp"  # the in-process server checks the Host header
TOKEN_URL = "https://auth.example.com/token"
OLD = "test-token-old-0000000000"
NEW = "test-token-new-1111111111"
REFRESH = "test-token-refresh-2222222222"
ROTATED = "test-token-refresh-3333333333"
ECHO = {"type": "object", "properties": {"text": {"type": "string"}}}
SIGN_IN = 'Tool error: the MCP server "alpha" needs the owner to sign in again on the Integrations screen.'


@pytest.fixture(autouse=True)
def _restore_mcp_tools():
    saved = dict(tool_classes._MCP_TOOLS)
    yield
    tool_classes._MCP_TOOLS.clear()
    tool_classes._MCP_TOOLS.update(saved)


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """Every host resolves to a public address (no network), as the address guard needs."""

    async def resolve(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(transport, "_resolve", resolve)


def _record(access: str = OLD, refresh: str | None = REFRESH, expires_in: int | None = 3600) -> OAuthRecord:
    return OAuthRecord(
        issuer="https://auth.example.com",
        token_endpoint=TOKEN_URL,
        client_id="test-client-0000",
        client_secret=None,
        token_auth_method="none",
        resource=ALPHA,
        scope="read",
        access_token=access,
        refresh_token=refresh,
        expires_at=None if expires_in is None else int(time.time()) + expires_in,
    )


def _sealed(record: OAuthRecord, url: str = ALPHA) -> SealedSecret:
    return seal_oauth(SecretBox(KEY), aad_fields(AGENT, "alpha", url, "oauth"), record)


def _stored(sealed: SealedSecret, secret_id: uuid.UUID | None = None) -> StoredSecret:
    return StoredSecret(
        secret_id or uuid.uuid4(), sealed.kind, sealed.key_id, sealed.nonce, sealed.ciphertext, (), None
    )


def _opened(secret: StoredSecret) -> OAuthRecord:
    record, problem = open_credential(SecretBox(KEY), aad_fields(AGENT, "alpha", ALPHA, "oauth"), secret)
    assert problem is None and isinstance(record, OAuthRecord)
    return record


class Rows:
    """The registry's reads and the in-place update, over a dict the test changes."""

    def __init__(self, record: OAuthRecord, url: str = ALPHA) -> None:
        self.rows = [
            (
                ServerConfig(name="alpha", url=url, call_timeout_seconds=20),
                [ToolConfig(server="alpha", tool="echo", input_schema=ECHO, permission="allow")],
            )
        ]
        self.secrets = {"alpha": _stored(_sealed(record, url))}
        self.updates = 0

    async def list_enabled(self):
        return self.rows

    async def list_secrets(self) -> dict[str, StoredSecret]:
        return dict(self.secrets)

    async def update_secret_in_place(self, secret_id: uuid.UUID, secret: SealedSecret) -> bool:
        current = self.secrets.get("alpha")
        if current is None or current.id != secret_id:
            return False
        self.updates += 1
        self.secrets["alpha"] = _stored(secret, secret_id)
        return True


class Provider:
    """A fake token endpoint (and the other endpoints of a provider, counted so a test can say none was reached).
    A refresh with the current refresh token answers ``NEW`` and rotates the refresh token to ``ROTATED``; ``hold``
    keeps the answer back until it is set; ``fail`` answers ``invalid_grant``."""

    def __init__(self) -> None:
        self.refreshes = 0
        self.hits: list[str] = []
        self.forms: list[dict[str, str]] = []
        self.hold: asyncio.Event | None = None
        self.fail = False
        self.refresh_token = REFRESH

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.hits.append(request.url.path)
        if request.url.path != "/token":
            return httpx.Response(404)
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.forms.append(form)
        if form.get("grant_type") != "refresh_token":
            return httpx.Response(400, json={"error": "unsupported_grant_type"})
        self.refreshes += 1
        if self.hold is not None:
            await self.hold.wait()
        if self.fail or form.get("refresh_token") != self.refresh_token:
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "secret detail"})
        self.refresh_token = ROTATED
        return httpx.Response(
            200,
            json={"access_token": NEW, "token_type": "bearer", "expires_in": 3600, "refresh_token": ROTATED},
        )

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))


def _key_file(tmp_path) -> str:
    path = tmp_path / "mcp-client.key"
    path.write_bytes(base64.b64encode(KEY) + b"\n")
    return str(path)


def _settings(tmp_path) -> Settings:
    return Settings(_env_file=None, ANTHROPIC_API_KEY="k", agent_id=AGENT, mcp_client_key_file=_key_file(tmp_path))


class NoConnect:
    """A connector the tests that drive ``_ServerAuth`` directly never use."""

    @asynccontextmanager
    async def connect(self, url: str, allow: frozenset[str], *, auth: Any = None):
        raise AssertionError("no session in this test")
        yield


async def _manager(tmp_path, rows: Rows, provider: Provider, connect: Any = None) -> McpClientManager:
    manager = McpClientManager(
        ToolDispatcher(),
        rows,
        _settings(tmp_path),
        connect=connect or NoConnect().connect,
        oauth_http=provider.client,
    )
    await manager.start()
    return manager


def _mcp(seen: list[str | None], refuse: set[str] | None = None) -> httpx.AsyncBaseTransport:
    """A fake MCP server: records each request's Authorization, answers 401 to a token in ``refuse``."""

    def handle(request: httpx.Request) -> httpx.Response:
        sent = request.headers.get("authorization")
        seen.append(sent)
        return httpx.Response(401 if refuse and sent in {f"Bearer {t}" for t in refuse} else 200)

    return httpx.MockTransport(handle)


async def _until(check: Any, seconds: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not check():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.01)


# --- the refresh race (written first) --------------------------------------------------------------------------------


async def test_two_requests_with_an_expired_token_make_one_refresh_and_both_send_the_new_token(tmp_path):
    rows, provider = Rows(_record(expires_in=-10)), Provider()
    provider.hold = asyncio.Event()
    manager = await _manager(tmp_path, rows, provider)
    seen: list[str | None] = []
    auth = manager._servers["alpha"].auth_for(None)
    async with httpx.AsyncClient(transport=_mcp(seen), auth=auth) as client:
        first = asyncio.create_task(client.post(ALPHA))
        second = asyncio.create_task(client.post(ALPHA))
        await _until(lambda: provider.refreshes == 1)
        await asyncio.sleep(0.05)  # both requests are inside the auth flow: one at the provider, one at the lock
        assert provider.refreshes == 1 and seen == []
        provider.hold.set()
        answers = await asyncio.gather(first, second)
    assert [a.status_code for a in answers] == [200, 200]
    assert provider.refreshes == 1
    assert seen == [f"Bearer {NEW}", f"Bearer {NEW}"]
    stored = _opened(rows.secrets["alpha"])
    assert (stored.access_token, stored.refresh_token, rows.updates) == (NEW, ROTATED, 1)
    await manager.close()


async def test_a_refresh_that_loses_its_where_id_update_is_never_used(tmp_path):
    rows, provider = Rows(_record(expires_in=-10)), Provider()
    provider.hold = asyncio.Event()
    manager = await _manager(tmp_path, rows, provider)
    replacement = _record(access="test-token-owner-4444444444", expires_in=3600)
    seen: list[str | None] = []
    auth = manager._servers["alpha"].auth_for(None)
    async with httpx.AsyncClient(transport=_mcp(seen), auth=auth) as client:
        request = asyncio.create_task(client.post(ALPHA))
        await _until(lambda: provider.refreshes == 1)
        rows.secrets["alpha"] = _stored(_sealed(replacement))  # the owner signed in again meanwhile: a new row id
        provider.hold.set()
        with pytest.raises(Exception) as caught:
            await request
        assert type(caught.value).__name__ == "SignInNeeded"
        with pytest.raises(Exception):
            await client.post(ALPHA)  # this retiring server's row is gone: nothing to send, the provider not asked
    assert seen == []  # the refreshed token was never sent
    assert (provider.refreshes, rows.updates) == (1, 0)
    assert _opened(rows.secrets["alpha"]).access_token == "test-token-owner-4444444444"
    assert not manager.needs_sign_in("alpha", rows.secrets["alpha"].id)
    await manager.close()


async def test_a_cancelled_caller_leaves_the_refresh_to_finish_and_store_the_rotated_token(tmp_path):
    rows, provider = Rows(_record(expires_in=-10)), Provider()
    provider.hold = asyncio.Event()
    manager = await _manager(tmp_path, rows, provider)
    seen: list[str | None] = []
    auth = manager._servers["alpha"].auth_for(None)
    async with httpx.AsyncClient(transport=_mcp(seen), auth=auth) as client:
        request = asyncio.create_task(client.post(ALPHA))
        await _until(lambda: provider.refreshes == 1)
        request.cancel()  # the runner's tool timeout, say
        with pytest.raises(asyncio.CancelledError):
            await request
        provider.hold.set()
        await _until(lambda: rows.updates == 1)
        stored = _opened(rows.secrets["alpha"])
        assert (stored.access_token, stored.refresh_token) == (NEW, ROTATED)
        await client.post(ALPHA)  # the next request uses what was stored, with no second refresh
    assert seen == [f"Bearer {NEW}"] and provider.refreshes == 1
    await manager.close()


# --- a 401 ------------------------------------------------------------------------------------------------------------


async def test_a_401_refreshes_once_and_retries_with_the_new_token(tmp_path):
    rows, provider = Rows(_record()), Provider()
    manager = await _manager(tmp_path, rows, provider)
    seen: list[str | None] = []
    auth = manager._servers["alpha"].auth_for(None)
    async with httpx.AsyncClient(transport=_mcp(seen, refuse={OLD}), auth=auth) as client:
        answer = await client.post(ALPHA)
    assert answer.status_code == 200
    assert seen == [f"Bearer {OLD}", f"Bearer {NEW}"]
    assert provider.refreshes == 1 and auth.refused is None  # only the last answer counts
    assert provider.forms[0] == {
        "grant_type": "refresh_token",
        "refresh_token": REFRESH,
        "resource": ALPHA,
        "scope": "read",
        "client_id": "test-client-0000",
    }
    await manager.close()


async def test_a_401_to_a_token_already_replaced_in_the_row_retries_with_the_stored_one_without_a_refresh(tmp_path):
    rows, provider = Rows(_record()), Provider()
    manager = await _manager(tmp_path, rows, provider)
    secret_id = rows.secrets["alpha"].id
    rows.secrets["alpha"] = _stored(_sealed(_record(access=NEW)), secret_id)  # another caller refreshed it
    seen: list[str | None] = []
    auth = manager._servers["alpha"].auth_for(None)
    async with httpx.AsyncClient(transport=_mcp(seen, refuse={OLD}), auth=auth) as client:
        assert (await client.post(ALPHA)).status_code == 200
    assert seen == [f"Bearer {OLD}", f"Bearer {NEW}"] and provider.refreshes == 0
    await manager.close()


async def test_a_401_after_the_retry_is_the_refusal_and_is_retried_only_once(tmp_path):
    rows, provider = Rows(_record()), Provider()
    manager = await _manager(tmp_path, rows, provider)
    seen: list[str | None] = []
    refusals: list[int] = []
    server = manager._servers["alpha"]
    from nous.mcp_client.manager import _ServerAuth

    auth = _ServerAuth(ALPHA, dict, on_refused=refusals.append, oauth=server.oauth)
    async with httpx.AsyncClient(transport=_mcp(seen, refuse={OLD, NEW}), auth=auth) as client:
        assert (await client.post(ALPHA)).status_code == 401
    assert seen == [f"Bearer {OLD}", f"Bearer {NEW}"] and refusals == [401] and provider.refreshes == 1
    await manager.close()


# --- a failed refresh ------------------------------------------------------------------------------------------------


async def test_a_failed_refresh_needs_a_new_sign_in_and_later_requests_do_not_ask_the_provider(tmp_path, caplog):
    rows, provider = Rows(_record(expires_in=-10)), Provider()
    provider.fail = True
    caplog.set_level(logging.DEBUG)
    manager = await _manager(tmp_path, rows, provider)
    seen: list[str | None] = []
    auth = manager._servers["alpha"].auth_for(None)
    async with httpx.AsyncClient(transport=_mcp(seen), auth=auth) as client:
        for _ in range(3):
            with pytest.raises(Exception) as caught:
                await client.post(ALPHA)
            assert type(caught.value).__name__ == "SignInNeeded"
    assert seen == [] and provider.refreshes == 1
    assert manager.needs_sign_in("alpha", rows.secrets["alpha"].id)
    assert all(path == "/token" for path in provider.hits)  # no discovery, no registration: no login
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "test-token" not in text and "secret detail" not in text
    await manager.close()


async def test_no_refresh_token_needs_a_new_sign_in_at_once(tmp_path):
    rows, provider = Rows(_record(refresh=None, expires_in=-10)), Provider()
    manager = await _manager(tmp_path, rows, provider)
    auth = manager._servers["alpha"].auth_for(None)
    async with httpx.AsyncClient(transport=_mcp([]), auth=auth) as client:
        with pytest.raises(Exception):
            await client.post(ALPHA)
    assert provider.hits == [] and manager.needs_sign_in("alpha", rows.secrets["alpha"].id)
    await manager.close()


async def test_a_refresh_answer_whose_token_breaks_the_header_rule_is_a_failure(tmp_path):
    rows, provider = Rows(_record(expires_in=-10)), Provider()

    async def bad(request: httpx.Request) -> httpx.Response:
        provider.refreshes += 1
        return httpx.Response(200, json={"access_token": 'test token "0000"', "token_type": "Bearer"})

    manager = McpClientManager(
        ToolDispatcher(),
        rows,
        _settings(tmp_path),
        connect=NoConnect().connect,
        oauth_http=lambda: httpx.AsyncClient(transport=httpx.MockTransport(bad)),
    )
    await manager.start()
    auth = manager._servers["alpha"].auth_for(None)
    async with httpx.AsyncClient(transport=_mcp([]), auth=auth) as client:
        with pytest.raises(Exception):
            await client.post(ALPHA)
    assert provider.refreshes == 1 and rows.updates == 0
    await manager.close()


# --- in a real session (the SDK's FastMCP in process) ----------------------------------------------------------------


class TokenGate:
    """Wraps the in-process app: records every request's Authorization; a POST carrying a token in ``refused`` is
    answered 401."""

    def __init__(self) -> None:
        self.refused: set[str] = set()
        self.seen: list[tuple[str, str | None]] = []

    def __call__(self, app: Any) -> Any:
        async def served(scope: Any, receive: Any, send: Any) -> None:
            if scope["type"] == "http":
                headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
                sent = headers.get("authorization")
                self.seen.append((scope["method"], sent))
                if scope["method"] == "POST" and sent in {f"Bearer {t}" for t in self.refused}:
                    await send({"type": "http.response.start", "status": 401, "headers": []})
                    await send({"type": "http.response.body", "body": b""})
                    return
            await app(scope, receive, send)

        return served


@asynccontextmanager
async def _in_process(gate: TokenGate):
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("inproc", stateless_http=True, json_response=True)

    @server.tool()
    def echo(text: str) -> str:
        """Repeat the text."""
        return text

    app = server.streamable_http_app()
    connects: list[int] = []

    @asynccontextmanager
    async def connect(url: str, allow: frozenset[str], *, auth: httpx.Auth | None = None):
        connects.append(1)
        limited = _LimitedTransport(httpx.ASGITransport(app=gate(app)), 2_000_000)
        client = httpx.AsyncClient(transport=limited, auth=auth)
        async with client, transport._session_over(client, url) as session:
            yield session

    async with server.session_manager.run():
        yield connect, connects


async def test_a_401_in_the_middle_of_a_session_refreshes_retries_and_keeps_the_session(tmp_path):
    gate, rows, provider = TokenGate(), Rows(_record(), INPROC), Provider()
    async with _in_process(gate) as (connect, connects):
        manager = await _manager(tmp_path, rows, provider, connect)
        d = manager._registrar
        assert (await d.dispatch("mcp__alpha__echo", {"text": "warm"}))[1] is False
        conn = manager._servers["alpha"].current()
        gate.refused = {OLD}  # the provider revoked the old token
        text, is_error = await d.dispatch("mcp__alpha__echo", {"text": "after"})
        assert not is_error and text.endswith("\nafter")
        assert manager._servers["alpha"].current() is conn and len(connects) == 1  # the same session
        assert provider.refreshes == 1 and manager.credentials_refused() == {}
        posts = [sent for method, sent in gate.seen if method == "POST"]
        assert posts[-2:] == [f"Bearer {OLD}", f"Bearer {NEW}"]
        await manager.close()


async def test_a_failed_refresh_mid_session_fails_the_call_at_once_and_later_calls_fast(tmp_path):
    gate, rows, provider = TokenGate(), Rows(_record(), INPROC), Provider()
    provider.fail = True
    async with _in_process(gate) as (connect, connects):
        manager = await _manager(tmp_path, rows, provider, connect)
        d = manager._registrar
        assert (await d.dispatch("mcp__alpha__echo", {"text": "warm"}))[1] is False
        gate.refused = {OLD}
        loop = asyncio.get_running_loop()
        started = loop.time()
        assert await d.dispatch("mcp__alpha__echo", {"text": "x"}) == (SIGN_IN, True)
        assert loop.time() - started < 2  # not the call's 20 s timeout
        before = len(gate.seen)
        for _ in range(2):
            assert await d.dispatch("mcp__alpha__echo", {"text": "y"}) == (SIGN_IN, True)
        assert len(gate.seen) == before and provider.refreshes == 1  # fail fast: neither the server nor the provider
        assert all(path == "/token" for path in provider.hits)
        await manager.close()


async def test_an_expired_token_at_connect_with_a_failed_refresh_fails_fast(tmp_path):
    gate, rows, provider = TokenGate(), Rows(_record(expires_in=-10), INPROC), Provider()
    provider.fail = True
    async with _in_process(gate) as (connect, connects):
        manager = await _manager(tmp_path, rows, provider, connect)
        d = manager._registrar
        loop = asyncio.get_running_loop()
        started = loop.time()
        assert await d.dispatch("mcp__alpha__echo", {"text": "x"}) == (SIGN_IN, True)
        assert await d.dispatch("mcp__alpha__echo", {"text": "x"}) == (SIGN_IN, True)
        assert loop.time() - started < 2
        assert gate.seen == [] and provider.refreshes == 1
        await manager.close()


async def test_a_refresh_survives_a_reload_and_the_reloaded_server_sends_the_new_token(tmp_path):
    gate, rows, provider = TokenGate(), Rows(_record(expires_in=-10), INPROC), Provider()
    provider.hold = asyncio.Event()
    async with _in_process(gate) as (connect, connects):
        manager = await _manager(tmp_path, rows, provider, connect)
        d = manager._registrar
        call = asyncio.create_task(d.dispatch("mcp__alpha__echo", {"text": "x"}))
        await _until(lambda: provider.refreshes == 1)
        await manager.reload()  # the row's id is unchanged: the server and its connect are kept
        provider.hold.set()
        text, is_error = await call
        assert not is_error and text.endswith("\nx")
        await manager.reload()  # the in-place update kept the id: still the same server
        assert (await d.dispatch("mcp__alpha__echo", {"text": "y"}))[1] is False
        assert {sent for _m, sent in gate.seen} == {f"Bearer {NEW}"} and provider.refreshes == 1
        assert len(connects) == 1
        await manager.close()


async def test_a_result_that_repeats_a_token_is_scrubbed(tmp_path):
    gate, rows, provider = TokenGate(), Rows(_record(), INPROC), Provider()
    async with _in_process(gate) as (connect, connects):
        manager = await _manager(tmp_path, rows, provider, connect)
        d = manager._registrar
        text, is_error = await d.dispatch("mcp__alpha__echo", {"text": f"a {OLD} b {REFRESH} c"})
        assert not is_error and OLD not in text and REFRESH not in text and "a [withheld] b [withheld] c" in text
        await manager.close()
