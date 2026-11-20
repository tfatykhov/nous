"""F112 MCP client: signing in to a server where sign-in is optional. Such a server answers an unauthenticated
``initialize`` with 2xx and publishes its protected resource metadata (RFC 9728) at a well-known address: discovery
reads it there and goes on exactly as for a 401, with every check of that path. With none published, the server does
not ask for sign-in (409). A 401 server is discovered as before. After the sign-in, the stored token is sent on every
request of the server, its first ``initialize`` included; after sign-out, none is.

The fake provider and the route fixtures come from test_mcp_client_oauth.py (the same SDK floor skips them); the
end-to-end tests run the SDK's FastMCP server in process. No network. Every value here is made up."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette

from nous.api.mcp_client_routes import SIGNED_IN_PAGE, build_mcp_client_routes
from nous.api.tools import ToolDispatcher
from nous.config import Settings
from nous.mcp_client import McpClientManager, oauth, transport
from nous.mcp_client.oauth import Discovery, OAuthProblem
from nous.mcp_client.registry import McpRegistry
from tests import test_mcp_client_oauth as base
from tests.test_mcp_client_oauth import ALPHA, BASE, ISSUER, PROXY, _callback, _req, _start

pytestmark = base.pytestmark

# The fixtures of the OAuth route tests, by name, so pytest finds them in this module.
make_env = base.make_env
_dns = base._dns
_restore_mcp_tools = base._restore_mcp_tools

ORIGIN = "https://alpha.example.com"
PATH_PRM = f"{ORIGIN}/.well-known/oauth-protected-resource/mcp"
ROOT_PRM = f"{ORIGIN}/.well-known/oauth-protected-resource"
AS_META = f"{ISSUER}/.well-known/oauth-authorization-server"
PUBLISHED = {"resource": ALPHA, "authorization_servers": [ISSUER]}
NO_SIGN_IN = "this server does not ask for sign-in"


class OptionalWorld(base.World):
    """``base.World`` with a server that answers an unauthenticated ``initialize`` with 200 (and, as there, a
    ``WWW-Authenticate`` header while ``www_auth`` is on, which a 2xx must not be read for). Its resource metadata is
    ``documents``, by address: a JSON document, raw bytes, or an HTTP status (a 3xx with a ``Location``)."""

    def __init__(self) -> None:
        super().__init__()
        self.mcp_status = 200
        self.www_auth = False
        self.documents: dict[str, Any] = {PATH_PRM: dict(PUBLISHED)}

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        where = f"{url.scheme}://{url.host}{url.path}"
        if request.method == "GET" and where in (PATH_PRM, ROOT_PRM):
            self.hits.append(("GET", where))
            document = self.documents.get(where, 404)
            if isinstance(document, int):
                return httpx.Response(document, headers={"Location": "https://elsewhere.example.com/"})
            if isinstance(document, bytes):
                return httpx.Response(200, content=document)
            return httpx.Response(200, json=document)
        return super().handle(request)


async def _discover(world: base.World, scope: str | None = None) -> Discovery:
    return await oauth.discover(ALPHA, world.client, frozenset(), scope)


# --- a 401 server: discovered as before (pinned on the code before the change) ---------------------------------------

THE_401_DISCOVERY = Discovery(
    issuer=ISSUER,
    authorization_endpoint=f"{ISSUER}/authorize",
    token_endpoint=f"{ISSUER}/token",
    registration_endpoint=f"{ISSUER}/register",
    resource=ALPHA,
    scope="read",
    token_auth_methods=("none", "client_secret_post"),
    iss_supported=False,
    grant_types=("authorization_code", "refresh_token"),
    client_id_metadata=False,
)
THE_401_HITS = [("POST", ALPHA), ("GET", PATH_PRM), ("GET", AS_META)]


async def test_a_401_server_named_by_its_header_is_discovered_as_before():
    world = base.World()
    assert await _discover(world) == THE_401_DISCOVERY
    assert world.hits == THE_401_HITS


async def test_a_401_server_found_at_the_well_known_address_is_discovered_as_before():
    world = base.World()
    world.www_auth = False
    assert await _discover(world) == dataclasses.replace(THE_401_DISCOVERY, scope=None)
    assert world.hits == THE_401_HITS


async def test_a_401_server_with_no_resource_metadata_still_takes_its_origin_as_the_provider():
    world = base.World()
    world.www_auth, world.prm = False, False
    world.as_meta.update(
        issuer=ORIGIN,
        authorization_endpoint=f"{ORIGIN}/authorize",
        token_endpoint=f"{ORIGIN}/token",
        registration_endpoint=f"{ORIGIN}/register",
    )
    found = await _discover(world)
    assert (found.issuer, found.resource, found.token_endpoint) == (ORIGIN, ALPHA, f"{ORIGIN}/token")
    assert world.hits == [
        ("POST", ALPHA),
        ("GET", PATH_PRM),
        ("GET", ROOT_PRM),
        ("GET", f"{ORIGIN}/.well-known/oauth-authorization-server"),
    ]


# --- a 2xx server: its metadata at a well-known address, or no sign-in ------------------------------------------------


async def test_a_2xx_server_with_metadata_gives_the_discovery_a_401_server_gives_for_the_same_metadata():
    world = OptionalWorld()
    assert await _discover(world) == dataclasses.replace(THE_401_DISCOVERY, scope=None)  # no 401: no scope hint
    assert world.hits == THE_401_HITS


@pytest.mark.parametrize("status", [200, 202, 204])
async def test_any_2xx_to_initialize_looks_for_the_metadata(status):
    world = OptionalWorld()
    world.mcp_status = status
    assert (await _discover(world)).issuer == ISSUER


async def test_a_2xx_server_with_metadata_only_at_the_root_address():
    world = OptionalWorld()
    world.documents = {ROOT_PRM: dict(PUBLISHED)}  # the path-based address answers 404
    assert await _discover(world) == dataclasses.replace(THE_401_DISCOVERY, scope=None)
    assert world.hits == [("POST", ALPHA), ("GET", PATH_PRM), ("GET", ROOT_PRM), ("GET", AS_META)]


async def test_a_2xx_answer_is_not_read_for_a_metadata_address_or_a_scope():
    world = OptionalWorld()
    world.www_auth = True  # the 200 names a metadata address on a private host, and the scope "read"
    world.www_auth_url = "https://meta.private.example.com/prm"
    found = await _discover(world)
    assert found == dataclasses.replace(THE_401_DISCOVERY, scope=None)
    assert all("private" not in where for _method, where in world.hits)


async def test_a_2xx_server_takes_the_owners_scope_else_the_metadatas():
    world = OptionalWorld()
    world.documents[PATH_PRM]["scopes_supported"] = ["read", "write"]
    assert (await _discover(world)).scope == "read write"
    assert (await _discover(OptionalWorld(), "mine")).scope == "mine"


async def test_a_2xx_server_with_no_metadata_does_not_ask_for_sign_in():
    world = OptionalWorld()
    world.documents = {}
    # A provider at the origin would answer: a 401 server with no metadata uses it, a 2xx server never does.
    world.as_meta.update(issuer=ORIGIN, authorization_endpoint=f"{ORIGIN}/authorize", token_endpoint=f"{ORIGIN}/token")
    with pytest.raises(OAuthProblem) as caught:
        await _discover(world)
    assert (caught.value.status, str(caught.value)) == (409, NO_SIGN_IN)
    assert world.hits == [("POST", ALPHA), ("GET", PATH_PRM), ("GET", ROOT_PRM)]


@pytest.mark.parametrize(
    "document",
    [
        b"not json",
        ["a", "list"],
        {"resource": ALPHA},  # the SDK's model requires one authorization server at least
        {"resource": ALPHA, "authorization_servers": []},
        {"resource": ALPHA, "authorization_servers": ["not an address"]},
        {"authorization_servers": [ISSUER]},  # and a resource
    ],
)
async def test_a_2xx_server_whose_metadata_cannot_be_used_does_not_ask_for_sign_in(document):
    """A document the metadata model does not take is no document (as for a 401 server, the next address is tried):
    with none usable at either address, the 2xx server does not ask for sign-in."""
    world = OptionalWorld()
    world.documents = {PATH_PRM: document, ROOT_PRM: document}
    with pytest.raises(OAuthProblem) as caught:
        await _discover(world)
    assert (caught.value.status, str(caught.value)) == (409, NO_SIGN_IN)
    assert world.hits == [("POST", ALPHA), ("GET", PATH_PRM), ("GET", ROOT_PRM)]


def _another_path(w: OptionalWorld) -> None:
    w.documents[PATH_PRM]["resource"] = f"{ORIGIN}/other"


def _another_origin(w: OptionalWorld) -> None:
    w.documents[PATH_PRM]["resource"] = "https://beta.example.com/mcp"


def _issuer_mismatch(w: OptionalWorld) -> None:
    w.as_meta["issuer"] = "https://other.example.com"


def _no_s256(w: OptionalWorld) -> None:
    w.as_meta["code_challenge_methods_supported"] = ["plain"]


def _no_code_flow(w: OptionalWorld) -> None:
    w.as_meta["response_types_supported"] = ["token"]


def _no_code_grant(w: OptionalWorld) -> None:
    w.as_meta["grant_types_supported"] = ["client_credentials"]


def _no_provider_metadata(w: OptionalWorld) -> None:
    w.documents[PATH_PRM]["authorization_servers"] = ["https://nothing.example.com"]  # it publishes nothing


def _private_authorization_server(w: OptionalWorld) -> None:
    w.documents[PATH_PRM]["authorization_servers"] = ["https://as.private.example.com"]


def _http_authorization_server(w: OptionalWorld) -> None:
    w.documents[PATH_PRM]["authorization_servers"] = ["http://auth.example.com"]


def _private_token(w: OptionalWorld) -> None:
    w.as_meta["token_endpoint"] = "https://token.private.example.com/token"


def _too_large(w: OptionalWorld) -> None:
    w.documents[PATH_PRM] = b" " * (oauth.BODY_MAX_BYTES + 1)


def _redirecting(w: OptionalWorld) -> None:
    w.documents[PATH_PRM] = 302


def _unavailable(w: OptionalWorld) -> None:
    w.documents[PATH_PRM] = 503


@pytest.mark.parametrize(
    "spoil,words",
    [
        (_another_path, "the server's resource metadata names another resource"),
        (_another_origin, "the server's resource metadata names another resource"),
        (_issuer_mismatch, "names another issuer"),
        (_no_s256, "PKCE with S256"),
        (_no_code_flow, "does not support the authorization code flow"),
        (_no_code_grant, "does not support the authorization code grant"),
        (_no_provider_metadata, "publishes no authorization server metadata"),
        (_private_authorization_server, "address guard refused"),
        (_http_authorization_server, "only https URLs are accepted"),
        (_private_token, "address guard refused"),
        (_too_large, f"answered past {oauth.BODY_MAX_BYTES} bytes"),
        # A redirect, a 429 or a 5xx at a well-known address ends discovery, as for a 401 server.
        (_redirecting, "redirects are not followed"),
        (_unavailable, "answered HTTP 503"),
    ],
)
async def test_a_2xx_server_keeps_every_check_of_the_401_path(spoil, words):
    world = OptionalWorld()
    spoil(world)
    with pytest.raises(OAuthProblem) as caught:
        await _discover(world)
    assert caught.value.status == 502 and words in str(caught.value), str(caught.value)
    assert not any(where.endswith(("/token", "/register")) for _method, where in world.hits)


async def test_a_2xx_servers_metadata_address_is_refused_when_its_host_turns_private(monkeypatch):
    """The address guard runs before each request: a host that answered the ``initialize`` from a public address and
    resolves to a private one for the metadata is refused there."""
    answers = iter([["93.184.216.34"], ["10.0.0.7"]])

    async def resolve(host: str, port: int) -> list[str]:
        return next(answers)

    monkeypatch.setattr(transport, "_resolve", resolve)
    world = OptionalWorld()
    with pytest.raises(OAuthProblem) as caught:
        await _discover(world)
    assert caught.value.status == 502 and "the address guard refused it" in str(caught.value)
    assert world.hits == [("POST", ALPHA)]


# --- the routes: the sign-in, the next requests, sign-out ------------------------------------------------------------


async def test_the_start_route_refuses_a_2xx_server_that_publishes_nothing_with_the_same_sentence(make_env):
    env = await make_env()
    env.world.mcp_status, env.world.prm = 200, False
    started = await _start(env)
    assert (started.status_code, started.json()) == (409, {"error": NO_SIGN_IN})
    assert env.manager.flows._flows == {}


# The provider's metadata as an optional-sign-in server's provider was seen to publish it: dynamic registration, S256,
# public clients only, no client ID metadata document, nothing else listed.
OBSERVED_PROVIDER = {
    "issuer": ISSUER,
    "authorization_endpoint": f"{ISSUER}/authorize",
    "token_endpoint": f"{ISSUER}/token",
    "registration_endpoint": f"{ISSUER}/register",
    "code_challenge_methods_supported": ["S256"],
    "token_endpoint_auth_methods_supported": ["none"],
}


class Gate:
    """In front of the in-process server: records each request's method, its JSON-RPC method (for a POST) and its
    ``Authorization``, and answers every request, with a token or without one."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str | None, str | None]] = []

    def __call__(self, app: Any) -> Any:
        async def served(scope: Any, receive: Any, send: Any) -> None:
            if scope["type"] != "http":
                await app(scope, receive, send)
                return
            headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
            messages = [await receive()]
            while messages[-1]["type"] == "http.request" and messages[-1].get("more_body"):
                messages.append(await receive())
            body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.request")
            rpc = None
            if body:
                parsed = json.loads(body)
                rpc = parsed.get("method") if isinstance(parsed, dict) else None
            self.seen.append((scope["method"], rpc, headers.get("authorization")))
            queued = list(messages)

            async def replay() -> Any:
                return queued.pop(0) if queued else await receive()

            await app(scope, replay, send)

        return served


@asynccontextmanager
async def _in_process(gate: Gate):
    """The SDK's FastMCP server in this process, at any host (``ALPHA``), behind ``gate``: the connector the manager
    opens its sessions with, and the list of connections it opened."""
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings

    server = FastMCP(
        "optional",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @server.tool()
    def echo(text: str) -> str:
        """Repeat the text."""
        return text

    app = server.streamable_http_app()
    connects: list[int] = []

    @asynccontextmanager
    async def connect(url: str, allow: frozenset[str], *, auth: httpx.Auth | None = None):
        connects.append(1)
        limited = transport._LimitedTransport(httpx.ASGITransport(app=gate(app)), 2_000_000)
        client = httpx.AsyncClient(transport=limited, auth=auth)
        async with client, transport._session_over(client, url) as session:
            yield session

    async with server.session_manager.run():
        yield connect, connects


@asynccontextmanager
async def _optional_env(db: Any, tmp_path: Any):
    """The routes over a manager whose sessions go to the in-process server (which needs no token) and whose OAuth
    requests go to ``OptionalWorld`` (the server's unauthenticated 200 and its metadata, the observed provider)."""
    gate = Gate()
    async with _in_process(gate) as (connect, connects):
        agent = f"mcp-oauth-opt-{uuid.uuid4().hex[:8]}"
        key = tmp_path / f"{agent}.key"
        key.write_bytes(base64.b64encode(bytes(range(32))) + b"\n")
        settings = Settings(
            _env_file=None,
            ANTHROPIC_API_KEY="k",
            agent_id=agent,
            mcp_client_enabled=True,
            mcp_client_ui_secret_sha256=hashlib.sha256(PROXY.encode()).hexdigest(),
            mcp_client_key_file=str(key),
            a2ui_public_base_url=BASE,
        )
        world = OptionalWorld()
        world.as_meta = dict(OBSERVED_PROVIDER)
        d, registry, bus = ToolDispatcher(), McpRegistry(db, agent), base.FakeBus()
        manager = McpClientManager(d, registry, settings, bus, connect=connect, oauth_http=world.client)
        await manager.start()
        app = Starlette(routes=build_mcp_client_routes(database=db, settings=settings, manager=manager, bus=bus))
        env = base.Env(app, manager, d, registry, bus, world, base.Remotes(world), [], settings)
        try:
            yield env, gate, connects
        finally:
            await manager.close()


async def _call(env: base.Env, text: str) -> str:
    answer, is_error = await env.dispatcher.dispatch("mcp__alpha__echo", {"text": text})
    assert not is_error, answer
    assert answer.endswith(f"\n{text}")
    return answer


async def _auth(env: base.Env) -> dict[str, Any]:
    listing = (await _req(env, "GET", "/integrations/mcp/servers")).json()
    return next(server["auth"] for server in listing["servers"] if server["name"] == "alpha")


async def test_a_stored_server_signs_in_from_its_card_sends_the_token_on_every_request_then_signs_out(db, tmp_path):
    async with _optional_env(db, tmp_path) as (env, gate, connects):
        # Stored with no credential: the server lists its tools and answers calls without a token.
        created = await _req(
            env, "POST", "/integrations/mcp/servers", {"name": "alpha", "url": ALPHA, "tools": {"echo": "allow"}}
        )
        assert created.status_code == 201, created.text
        assert (await _auth(env))["kind"] == "none"
        await _call(env, "anonymous")
        assert gate.seen and all(sent is None for _m, _rpc, sent in gate.seen)
        assert env.manager._servers["alpha"].connected

        # Sign in from the card: discovered by the well-known address, registered dynamically, stored at once.
        started = await _start(env)
        assert started.status_code == 200, started.text
        answer = started.json()
        assert (answer["registration"], answer["scope"]) == ("dynamic", None)
        assert (answer["authorization_host"], answer["token_host"]) == ("auth.example.com", "auth.example.com")
        assert ("GET", PATH_PRM) in env.world.hits
        assert env.world.registrations and env.world.registrations[0]["token_endpoint_auth_method"] == "none"
        page = await _callback(env, env.world.authorize(answer["authorization_url"]))
        assert page.content == SIGNED_IN_PAGE
        flow = (await _req(env, "GET", f"/integrations/mcp/oauth/flows/{answer['flow_id']}")).json()
        assert flow == {"status": "complete", "reason": None, "stored": True}
        assert (
            env.world.forms[-1]["client_id"] == "test-client-dynamic-0001"
            and "client_secret" not in env.world.forms[-1]
        )
        auth = await _auth(env)
        assert (auth["kind"], auth["status"]) == ("oauth", "ok")
        (token,) = env.world.valid

        # The next call opens a new connection, and every request of it carries the token, its initialize first.
        mark, opened = len(gate.seen), len(connects)
        await _call(env, "signed in")
        signed = gate.seen[mark:]
        assert len(connects) == opened + 1
        assert signed[0] == ("POST", "initialize", f"Bearer {token}")
        assert ("POST", "tools/call", f"Bearer {token}") in signed
        assert all(sent == f"Bearer {token}" for _m, _rpc, sent in signed)
        mark = len(gate.seen)
        await _call(env, "again")  # the same session: still sent
        assert [sent for _m, _rpc, sent in gate.seen[mark:]] == [f"Bearer {token}"] * len(gate.seen[mark:])

        # Sign out: back to anonymous use on a new connection, with no Authorization anywhere.
        out = await _req(env, "POST", "/integrations/mcp/servers/alpha/sign-out")
        assert out.status_code == 200 and out.json()["credentials"] == "removed"
        assert out.json()["server"]["auth"]["kind"] == "none" and out.json()["server"]["auth"]["status"] == "ok"
        mark, opened = len(gate.seen), len(connects)
        await _call(env, "signed out")
        unsigned = gate.seen[mark:]
        assert len(connects) == opened + 1
        assert unsigned[0] == ("POST", "initialize", None)
        assert ("POST", "tools/call", None) in unsigned
        assert all(sent is None for _m, _rpc, sent in unsigned)
        assert await env.registry.list_secrets() == {}


async def test_a_new_server_where_sign_in_is_optional_is_checked_and_added_with_the_sign_in(db, tmp_path):
    async with _optional_env(db, tmp_path) as (env, gate, connects):
        started = await _start(env)
        assert started.status_code == 200, started.text
        flow_id = started.json()["flow_id"]
        assert (
            await _callback(env, env.world.authorize(started.json()["authorization_url"]))
        ).content == SIGNED_IN_PAGE
        (token,) = env.world.valid
        probed = await _req(
            env, "POST", "/integrations/mcp/probe", {"name": "alpha", "url": ALPHA, "oauth_flow_id": flow_id}
        )
        assert probed.status_code == 200 and [t["tool"] for t in probed.json()["tools"]] == ["echo"]
        assert gate.seen and all(sent == f"Bearer {token}" for _m, _rpc, sent in gate.seen)  # the check sent it
        body = {"name": "alpha", "url": ALPHA, "tools": {"echo": "allow"}, "oauth_flow_id": flow_id}
        created = await _req(env, "POST", "/integrations/mcp/servers", body)
        assert created.status_code == 201 and created.json()["server"]["auth"]["kind"] == "oauth", created.text
        mark = len(gate.seen)
        await _call(env, "x")
        assert gate.seen[mark][1:] == ("initialize", f"Bearer {token}")
        assert all(sent == f"Bearer {token}" for _m, _rpc, sent in gate.seen[mark:])
