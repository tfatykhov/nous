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


def _record(
    access: str = OLD, refresh: str | None = REFRESH, expires_in: int | None = 3600, lifetime: int | None = None
) -> OAuthRecord:
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
        lifetime=lifetime,
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
        self.script: list[str] = []  # the next refreshes' answers: a status, "timeout" or "ok" (a normal answer)
        self.expires_in = 3600

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
        scripted = self.script.pop(0) if self.script else None
        if scripted == "timeout":
            raise httpx.ConnectTimeout("timed out")
        if scripted not in (None, "ok"):  # "ok": a normal answer
            return httpx.Response(int(scripted))
        if self.fail or form.get("refresh_token") != self.refresh_token:
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "secret detail"})
        self.refresh_token = ROTATED
        return httpx.Response(
            200,
            json={"access_token": NEW, "token_type": "bearer", "expires_in": self.expires_in, "refresh_token": ROTATED},
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


@pytest.mark.parametrize("seconds_left,refreshes", [(30, 1), (120, 0)])
async def test_a_token_is_refreshed_within_a_minute_of_its_expiry_and_not_before(tmp_path, seconds_left, refreshes):
    rows, provider = Rows(_record(expires_in=seconds_left)), Provider()
    manager = await _manager(tmp_path, rows, provider)
    seen: list[str | None] = []
    auth = manager._servers["alpha"].auth_for(None)
    async with httpx.AsyncClient(transport=_mcp(seen), auth=auth) as client:
        assert (await client.post(ALPHA)).status_code == 200
    assert provider.refreshes == refreshes and seen == [f"Bearer {NEW if refreshes else OLD}"]
    await manager.close()


# --- a provider that cannot be reached: held, never a new sign-in (review P2 1) -------------------------------------


@pytest.mark.parametrize("answer", ["503", "429", "timeout"])
async def test_a_provider_that_cannot_be_reached_is_held_30_s_then_one_request_recovers(tmp_path, answer):
    rows, provider = Rows(_record(expires_in=-10)), Provider()
    provider.script = [answer]
    manager = await _manager(tmp_path, rows, provider)
    clock = [1000.0]
    manager._clock = lambda: clock[0]
    secret_id = rows.secrets["alpha"].id
    seen: list[str | None] = []
    auth = manager._servers["alpha"].auth_for(None)
    async with httpx.AsyncClient(transport=_mcp(seen), auth=auth) as client:
        for _ in range(3):  # the first asks the provider; the next two are inside the hold and ask nothing
            with pytest.raises(Exception) as caught:
                await client.post(ALPHA)
            assert type(caught.value).__name__ == "ProviderUnreachable"
        assert provider.refreshes == 1 and seen == []
        assert manager.provider_unreachable("alpha", secret_id) and not manager.needs_sign_in("alpha", secret_id)
        clock[0] += 29
        with pytest.raises(Exception):
            await client.post(ALPHA)
        assert provider.refreshes == 1  # still held
        clock[0] += 2
        assert (await client.post(ALPHA)).status_code == 200  # the provider is back: one request, and it works
    assert provider.refreshes == 2 and seen == [f"Bearer {NEW}"]
    assert not manager.provider_unreachable("alpha", secret_id) and not manager.needs_sign_in("alpha", secret_id)
    await manager.close()


async def test_an_invalid_grant_is_definitive_and_is_not_held(tmp_path):
    rows, provider = Rows(_record(expires_in=-10)), Provider()
    provider.fail = True
    manager = await _manager(tmp_path, rows, provider)
    secret_id = rows.secrets["alpha"].id
    auth = manager._servers["alpha"].auth_for(None)
    async with httpx.AsyncClient(transport=_mcp([]), auth=auth) as client:
        with pytest.raises(Exception) as caught:
            await client.post(ALPHA)
    assert type(caught.value).__name__ == "SignInNeeded"
    assert manager.needs_sign_in("alpha", secret_id) and not manager.provider_unreachable("alpha", secret_id)
    await manager.close()


UNREACHABLE = (
    'Tool error: the sign-in provider of the MCP server "alpha" could not be reached: the call can be tried again in '
    "a moment."
)


async def test_a_call_while_the_provider_is_unreachable_says_so_and_asks_nothing_during_the_hold(tmp_path):
    gate, rows, provider = TokenGate(), Rows(_record(expires_in=-10), INPROC), Provider()
    provider.script = ["503"]
    async with _in_process(gate) as (connect, connects):
        manager = await _manager(tmp_path, rows, provider, connect)
        d = manager._registrar
        loop = asyncio.get_running_loop()
        started = loop.time()
        assert await d.dispatch("mcp__alpha__echo", {"text": "x"}) == (UNREACHABLE, True)
        assert await d.dispatch("mcp__alpha__echo", {"text": "x"}) == (UNREACHABLE, True)
        assert loop.time() - started < 2
        assert gate.seen == [] and provider.refreshes == 1
        assert not manager.needs_sign_in("alpha", rows.secrets["alpha"].id)
        await manager.close()


# --- refreshes are bounded (review P2 2) ----------------------------------------------------------------------------


async def test_a_server_that_refuses_every_token_gets_one_refresh_then_needs_a_new_sign_in(tmp_path):
    gate, rows, provider = TokenGate(), Rows(_record(), INPROC), Provider()
    async with _in_process(gate) as (connect, connects):
        manager = await _manager(tmp_path, rows, provider, connect)
        gate.refused = {OLD, NEW}  # every token is refused
        d = manager._registrar
        for _ in range(5):
            assert await d.dispatch("mcp__alpha__echo", {"text": "x"}) == (SIGN_IN, True)
        assert provider.refreshes == 1  # never a second rotation
        secret_id = rows.secrets["alpha"].id
        assert not manager.needs_sign_in("alpha", secret_id)  # one refusal of the fresh token so far (then backoff)
        await asyncio.sleep(1.2)  # past the connect backoff: the next call's first request is refused too
        assert await d.dispatch("mcp__alpha__echo", {"text": "x"}) == (SIGN_IN, True)
        assert provider.refreshes == 1 and manager.needs_sign_in("alpha", secret_id)  # the second refusal: sign in
        seen = len(gate.seen)
        assert await d.dispatch("mcp__alpha__echo", {"text": "x"}) == (SIGN_IN, True)
        assert len(gate.seen) == seen  # fails fast
        await manager.close()


# Short lifetimes make one refresh for requests that arrive together (an expires_in of 0 or less is "no expiry given").
@pytest.mark.parametrize("lifetime", [30, 10, 2])
async def test_six_concurrent_requests_with_a_short_lived_token_make_one_refresh(tmp_path, lifetime):
    rows, provider = Rows(_record(expires_in=-10, lifetime=lifetime)), Provider()
    provider.expires_in = lifetime
    provider.hold = asyncio.Event()
    manager = await _manager(tmp_path, rows, provider)
    seen: list[str | None] = []
    auth = manager._servers["alpha"].auth_for(None)
    async with httpx.AsyncClient(transport=_mcp(seen), auth=auth) as client:
        requests = [asyncio.create_task(client.post(ALPHA)) for _ in range(6)]
        await _until(lambda: provider.refreshes == 1)
        await asyncio.sleep(0.05)
        provider.hold.set()
        answers = await asyncio.gather(*requests)
        assert [a.status_code for a in answers] == [200] * 6
        assert (await client.post(ALPHA)).status_code == 200  # and the next one does not refresh either
    assert provider.refreshes == 1 and seen == [f"Bearer {NEW}"] * 7
    await manager.close()


def test_the_refresh_margin_is_at_most_half_the_tokens_lifetime():
    from nous.mcp_client.oauth import expiring

    assert expiring(_record(expires_in=50)) and not expiring(_record(expires_in=70))  # no lifetime: 60 s
    assert not expiring(_record(expires_in=20, lifetime=30)) and expiring(_record(expires_in=14, lifetime=30))
    assert not expiring(_record(expires_in=None))


# --- a call's own token stays in its scrub; a refresh in flight at close is stored (Codex) --------------------------


async def test_a_call_held_open_across_two_refreshes_still_withholds_the_token_it_was_sent(tmp_path):
    from mcp.types import CallToolResult, TextContent

    called, release = asyncio.Event(), asyncio.Event()

    class Held:
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            called.set()
            await release.wait()
            return CallToolResult(content=[TextContent(type="text", text=f"echo {OLD} and {REFRESH}")])

        async def list_tools(self) -> list[Any]:
            return []

    @asynccontextmanager
    async def connect(url: str, allow: frozenset[str], *, auth: Any = None):
        yield Held()

    rows, provider = Rows(_record()), Provider()
    manager = await _manager(tmp_path, rows, provider, connect)
    server = manager._servers["alpha"]
    call = asyncio.create_task(manager._registrar.dispatch("mcp__alpha__echo", {"text": "x"}))
    await called.wait()
    third = "test-token-third-5555555555"
    server.oauth.adopt(_record(access=NEW, refresh=ROTATED))
    server.oauth.adopt(_record(access=third, refresh="test-token-refresh-6666666666"))
    assert OLD not in server.oauth.values()  # the server no longer holds the call's token
    release.set()
    text, is_error = await call
    assert not is_error and OLD not in text and REFRESH not in text and text.endswith("echo [withheld] and [withheld]")
    await manager.close()


async def test_a_refresh_in_flight_at_close_still_stores_the_rotated_tokens(tmp_path, monkeypatch):
    from nous.mcp_client import manager as manager_module

    monkeypatch.setattr(manager_module, "_CLOSE_WAIT_SECONDS", 0.05)  # the connections' deadline, made short
    rows, provider = Rows(_record(expires_in=-10)), Provider()
    provider.hold = asyncio.Event()
    manager = await _manager(tmp_path, rows, provider)
    seen: list[str | None] = []
    auth = manager._servers["alpha"].auth_for(None)
    async with httpx.AsyncClient(transport=_mcp(seen), auth=auth) as client:
        request = asyncio.create_task(client.post(ALPHA))
        await _until(lambda: provider.refreshes == 1)
        closing = asyncio.create_task(manager.close())
        await asyncio.sleep(0.3)  # past the connections' deadline: the refresh is still waited for
        assert not closing.done()
        provider.hold.set()  # the provider rotated the refresh token: its answer must be stored
        await closing
        assert (await request).status_code == 200
    stored = _opened(rows.secrets["alpha"])
    assert (stored.access_token, stored.refresh_token, rows.updates) == (NEW, ROTATED, 1)


# --- round 2: short-lived tokens, a valid token while the provider is down, after close, repeated failures --------


class Numbered:
    """A token endpoint that gives a new access token and rotates the refresh token on every refresh, each living
    ``lifetime`` seconds."""

    def __init__(self, lifetime: int | None = None) -> None:
        self.lifetime = lifetime
        self.refresh_token = REFRESH
        self.refreshes = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if form.get("refresh_token") != self.refresh_token:
            return httpx.Response(400, json={"error": "invalid_grant"})
        self.refreshes += 1
        self.refresh_token = f"test-token-refresh-{self.refreshes:010d}"
        answer = {
            "access_token": self.access(self.refreshes),
            "token_type": "bearer",
            "refresh_token": self.refresh_token,
        }
        if self.lifetime is not None:
            answer["expires_in"] = self.lifetime
        return httpx.Response(200, json=answer)

    @staticmethod
    def access(n: int) -> str:
        return f"test-token-access-{n:010d}"

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))


@pytest.mark.parametrize("lifetime", [10, 20])
async def test_a_short_lived_token_past_its_window_is_refreshed_not_counted_as_refused(tmp_path, monkeypatch, lifetime):
    from types import SimpleNamespace

    from nous.mcp_client import oauth as oauth_module

    rows, provider = Rows(_record(expires_in=-10, lifetime=lifetime)), Numbered(lifetime)
    now = [0.0]
    wall = time.time()
    monkeypatch.setattr(oauth_module, "time", SimpleNamespace(time=lambda: wall + now[0], monotonic=time.monotonic))
    manager = McpClientManager(
        ToolDispatcher(), rows, _settings(tmp_path), connect=NoConnect().connect, oauth_http=provider.client
    )
    manager._clock = lambda: 1000.0 + now[0]
    await manager.start()
    refused: set[str] = set()
    seen: list[str | None] = []
    auth = manager._servers["alpha"].auth_for(None)
    async with httpx.AsyncClient(transport=_mcp(seen, refuse=refused), auth=auth) as client:
        assert (await client.post(ALPHA)).status_code == 200  # t=0: the first refresh
        refused.add(Numbered.access(1))  # the server refuses it from t=12 (expired, or about to)
        for t in (12, 14):
            now[0] = t
            assert (await client.post(ALPHA)).status_code == 200
    assert not manager.needs_sign_in("alpha", rows.secrets["alpha"].id)
    assert provider.refreshes == 2 and seen[-1] == f"Bearer {Numbered.access(2)}"
    await manager.close()


async def test_a_still_valid_token_is_sent_while_the_provider_cannot_be_reached(tmp_path):
    rows, provider = Rows(_record(expires_in=30)), Provider()  # inside the 60 s margin, not expired
    provider.script = ["503"]
    manager = await _manager(tmp_path, rows, provider)
    seen: list[str | None] = []
    auth = manager._servers["alpha"].auth_for(None)
    async with httpx.AsyncClient(transport=_mcp(seen), auth=auth) as client:
        for _ in range(2):  # the second is inside the hold: no request to the provider, the token still sent
            assert (await client.post(ALPHA)).status_code == 200
    assert seen == [f"Bearer {OLD}", f"Bearer {OLD}"] and provider.refreshes == 1
    assert manager.provider_unreachable("alpha", rows.secrets["alpha"].id)
    await manager.close()


async def test_no_refresh_is_asked_after_close(tmp_path):
    rows, provider = Rows(_record(expires_in=-10)), Provider()
    manager = await _manager(tmp_path, rows, provider)
    await manager.close()
    assert await manager._refresh("alpha", ALPHA, rows.secrets["alpha"].id, None) is None
    assert provider.hits == []


async def test_five_failed_refreshes_in_a_row_that_might_pass_need_a_new_sign_in(tmp_path):
    rows, provider = Rows(_record(expires_in=-10)), Provider()
    provider.script = ["404"] * 5  # a provider that answers 404 for good
    manager = await _manager(tmp_path, rows, provider)
    clock = [0.0]
    manager._clock = lambda: clock[0]
    secret_id = rows.secrets["alpha"].id
    auth = manager._servers["alpha"].auth_for(None)
    kinds = []
    async with httpx.AsyncClient(transport=_mcp([]), auth=auth) as client:
        for _ in range(5):
            with pytest.raises(Exception) as caught:
                await client.post(ALPHA)
            kinds.append(type(caught.value).__name__)
            clock[0] += 31  # past the hold
    assert kinds == ["ProviderUnreachable"] * 4 + ["SignInNeeded"]
    assert provider.refreshes == 5 and manager.needs_sign_in("alpha", secret_id)
    await manager.close()


async def _failing(tmp_path, answers: list[str]) -> tuple[McpClientManager, Rows, Provider, list[str]]:
    """A manager whose provider answers ``answers`` to the next refreshes; one request each, past the hold every time.
    Returns what each request raised (its class name), or "ok"."""
    rows, provider = Rows(_record(expires_in=-10)), Provider()
    provider.script = list(answers)
    manager = await _manager(tmp_path, rows, provider)
    clock = [0.0]
    manager._clock = lambda: clock[0]
    auth = manager._servers["alpha"].auth_for(None)
    kinds = []
    async with httpx.AsyncClient(transport=_mcp([]), auth=auth) as client:
        for _ in answers:
            try:
                await client.post(ALPHA)
                kinds.append("ok")
            except Exception as exc:
                kinds.append(type(exc).__name__)
            clock[0] += 31
    return manager, rows, provider, kinds


async def test_ten_failures_with_no_answer_never_need_a_sign_in_and_the_server_recovers(tmp_path):
    answers = ["timeout", "503", "429", "timeout", "500", "timeout", "429", "timeout", "502", "timeout", "ok"]
    manager, rows, provider, kinds = await _failing(tmp_path, answers)
    assert kinds == ["ProviderUnreachable"] * 10 + ["ok"]  # the network is back: the stored refresh token works
    assert not manager.needs_sign_in("alpha", rows.secrets["alpha"].id) and provider.refreshes == 11
    await manager.close()


async def test_five_403s_in_a_row_need_a_new_sign_in(tmp_path):
    manager, rows, provider, kinds = await _failing(tmp_path, ["403"] * 5)
    assert kinds == ["ProviderUnreachable"] * 4 + ["SignInNeeded"]
    assert manager.needs_sign_in("alpha", rows.secrets["alpha"].id)
    await manager.close()


async def test_only_the_answered_4xx_count_toward_the_five(tmp_path):
    answers = ["403", "timeout", "403", "503", "404", "timeout", "429", "403", "timeout", "403"]
    manager, rows, provider, kinds = await _failing(tmp_path, answers)
    assert kinds == ["ProviderUnreachable"] * 9 + ["SignInNeeded"]  # the fifth answered 4xx, at the tenth try
    assert manager.needs_sign_in("alpha", rows.secrets["alpha"].id)
    await manager.close()


async def test_a_lifetime_of_zero_or_less_is_no_expiry_given(tmp_path):
    for expires_in in (0, -5):
        rows, provider = Rows(_record(expires_in=-10)), Provider()
        provider.expires_in = expires_in
        manager = await _manager(tmp_path, rows, provider)
        seen: list[str | None] = []
        auth = manager._servers["alpha"].auth_for(None)
        async with httpx.AsyncClient(transport=_mcp(seen), auth=auth) as client:
            for _ in range(20):
                assert (await client.post(ALPHA)).status_code == 200
        stored = _opened(rows.secrets["alpha"])
        assert (stored.expires_at, stored.lifetime) == (None, None)
        assert provider.refreshes == 1 and seen == [f"Bearer {NEW}"] * 20
        assert not manager.needs_sign_in("alpha", rows.secrets["alpha"].id)
        await manager.close()


async def test_a_successful_refresh_resets_the_count_of_failures(tmp_path):
    rows, provider = Rows(_record(expires_in=-10)), Provider()
    provider.script = ["403"] * 4
    manager = await _manager(tmp_path, rows, provider)
    clock = [0.0]
    manager._clock = lambda: clock[0]
    auth = manager._servers["alpha"].auth_for(None)
    async with httpx.AsyncClient(transport=_mcp([]), auth=auth) as client:
        for _ in range(4):
            with pytest.raises(Exception):
                await client.post(ALPHA)
            clock[0] += 31
        assert (await client.post(ALPHA)).status_code == 200  # the fifth works
    assert manager._transient_failures == {} and not manager.needs_sign_in("alpha", rows.secrets["alpha"].id)
    await manager.close()


def test_a_record_sealed_without_its_lifetime_opens():
    import json as _json

    box = SecretBox(KEY)
    fields = aad_fields(AGENT, "alpha", ALPHA, "oauth")
    record = {
        "v": 1,
        "issuer": "https://auth.example.com",
        "token_endpoint": TOKEN_URL,
        "client_id": "test-client-0000",
        "client_secret": None,
        "token_auth_method": "none",
        "resource": ALPHA,
        "scope": None,
        "access_token": OLD,
        "refresh_token": REFRESH,
        "expires_at": None,
    }
    key_id, nonce, ciphertext = box.seal(fields, _json.dumps(record).encode())
    opened, problem = open_credential(
        box, fields, StoredSecret(uuid.uuid4(), "oauth", key_id, nonce, ciphertext, (), None)
    )
    assert problem is None and isinstance(opened, OAuthRecord)
    assert (opened.access_token, opened.lifetime) == (OLD, None)


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


def _reflecting(kind: str):
    """A token endpoint that repeats the refresh and access tokens it was sent: in an error answer, in a body that does
    not parse as a token answer (a parser's error text quotes its input), or in the text of a transport error."""

    async def handle(request: httpx.Request) -> httpx.Response:
        echo = f"unknown {REFRESH} after {OLD}"
        if kind == "error answer":
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": echo})
        if kind == "unreadable answer":
            return httpx.Response(200, json={"access_token": {"echo": echo}, "token_type": "bearer"})
        if kind == "not json":
            return httpx.Response(200, text=echo)
        raise httpx.ConnectError(echo)

    return handle


@pytest.mark.parametrize("kind", ["error answer", "unreadable answer", "not json", "transport error"])
async def test_a_token_the_provider_reflects_in_an_error_reaches_no_log_record(tmp_path, caplog, kind):
    rows = Rows(_record(expires_in=-10))
    caplog.set_level(logging.DEBUG)
    manager = McpClientManager(
        ToolDispatcher(),
        rows,
        _settings(tmp_path),
        connect=NoConnect().connect,
        oauth_http=lambda: httpx.AsyncClient(transport=httpx.MockTransport(_reflecting(kind))),
    )
    await manager.start()
    auth = manager._servers["alpha"].auth_for(None)
    async with httpx.AsyncClient(transport=_mcp([]), auth=auth) as client:
        with pytest.raises(Exception):
            await client.post(ALPHA)
    secret_id = rows.secrets["alpha"].id
    if kind == "transport error":  # no answer: the provider is held, the sign-in stands
        assert manager.provider_unreachable("alpha", secret_id) and not manager.needs_sign_in("alpha", secret_id)
    else:  # an answer, and not a usable one: definitive
        assert manager.needs_sign_in("alpha", secret_id) and not manager.provider_unreachable("alpha", secret_id)
    await manager.close()
    assert caplog.records  # the failure is logged, by its class
    for record in caplog.records:
        text = logging.Formatter().format(record)  # the message, its arguments and any traceback
        assert REFRESH not in text and OLD not in text, text


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


async def test_a_result_that_repeats_the_rotated_tokens_after_a_refresh_is_scrubbed(tmp_path):
    gate, rows, provider = TokenGate(), Rows(_record(), INPROC), Provider()
    gate.refused = {OLD}  # the first request is answered 401: the tokens are refreshed and rotated
    async with _in_process(gate) as (connect, connects):
        manager = await _manager(tmp_path, rows, provider, connect)
        d = manager._registrar
        text, is_error = await d.dispatch("mcp__alpha__echo", {"text": f"{NEW} {ROTATED} {OLD}"})
        assert not is_error and provider.refreshes == 1
        assert NEW not in text and ROTATED not in text and OLD not in text
        assert text.endswith("\n[withheld] [withheld] [withheld]")
        await manager.close()
