"""F112 PR 2d: signing in to a remote MCP server with OAuth 2.1 (authorization code with PKCE S256).

Nous runs the steps itself rather than the SDK's ``OAuthClientProvider``: that provider keeps the login's state in one
waiting coroutine and starts a login from any 401, and a tool call must never wait for a person. Only the SDK's pure
parts are used: its metadata models (``mcp.shared.auth``), its discovery URL lists and ``WWW-Authenticate`` readers
(``mcp.client.auth.utils``) and its resource rules (``mcp.shared.auth_utils``). No request is sent by SDK code.

Every request here (discovery, registration, the code exchange, a refresh) goes through its own client (``HttpFactory``;
by default ``transport.guarded_client`` with no ``auth``: public addresses only, no redirect followed, no proxy, a body
cap of ``BODY_MAX_BYTES``), never through a session's client, which carries the server's credential. Every URL a server
or a provider supplies passes ``config.url_problem`` and the address guard (``transport.check_url``) before it is
fetched; the authorization endpoint, which the owner's browser opens and Nous never fetches, may carry a query string.
Their hosts are added to the quiet-log set (``transport.quiet_host``). A failure is an ``OAuthProblem`` with a fixed
sentence: never a provider's text, a URL, a code, a state, a verifier or a token.

Login state lives in memory (``OAuthFlows``, owned by the manager): ten minutes, at most ``FLOWS_PENDING_MAX``
pending, single-use state compared in constant time; a restart loses it and the owner presses Sign in again.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import httpx
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    extract_resource_metadata_from_www_auth,
    extract_scope_from_www_auth,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthMetadata, OAuthToken, ProtectedResourceMetadata
from mcp.shared.auth_utils import check_resource_allowed, resource_url_from_server_url
from mcp.types import LATEST_PROTOCOL_VERSION
from pydantic import BaseModel, ValidationError

from nous.mcp_client.bridge import error_class
from nous.mcp_client.config import BlockedAddress, token_ok, url_problem
from nous.mcp_client.secrets import OAuthRecord
from nous.mcp_client.transport import ResponseTooLarge, check_url, guarded_client, quiet_host

logger = logging.getLogger(__name__)

CALLBACK_PATH = "/integrations/mcp/oauth/callback"  # registered with providers: fixed
CLIENT_NAME = "Nous"
FLOW_SECONDS = 600
FLOWS_PENDING_MAX = 8
HTTP_SECONDS = 10.0
BODY_MAX_BYTES = 100_000
REFRESH_MARGIN_SECONDS = 60
_CLIENT_ID = re.compile(r"[\x21-\x7e]{1,512}")
# RFC 6749 scope-tokens separated by single spaces.
_SCOPE = re.compile(r"[\x21\x23-\x5b\x5d-\x7e]+(?: [\x21\x23-\x5b\x5d-\x7e]+)*")
_SCOPE_MAX_CHARS = 2048
_CODE = re.compile(r"[\x21-\x7e]{1,4096}")
_SECRET_METHODS = ("client_secret_basic", "client_secret_post")

HttpFactory = Callable[[], httpx.AsyncClient]


class OAuthProblem(Exception):
    """A sign-in or refresh step that failed. ``status`` is the route's answer; the message is a fixed sentence (a
    host name at most)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def redirect_uri(base: str) -> str | None:
    """``<base>/integrations/mcp/oauth/callback`` for the public base URL (``NOUS_A2UI_PUBLIC_BASE_URL``), or None
    when it is not an https address with a host (then OAuth is unavailable)."""
    base = (base or "").strip().rstrip("/")
    try:
        parts = urlsplit(base)
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.hostname or parts.query or parts.fragment or "@" in parts.netloc:
        return None
    return base + CALLBACK_PATH


def oauth_client(allow_private_hosts: frozenset[str]) -> httpx.AsyncClient:
    """The default ``HttpFactory``: a guarded client with no ``auth``, ``HTTP_SECONDS`` per request and a body cap of
    ``BODY_MAX_BYTES``."""
    return guarded_client(allow_private_hosts, BODY_MAX_BYTES, timeout=HTTP_SECONDS)


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def expiring(record: OAuthRecord) -> bool:
    """Whether the access token expires within ``REFRESH_MARGIN_SECONDS`` (never, without a lifetime)."""
    return record.expires_at is not None and record.expires_at - time.time() <= REFRESH_MARGIN_SECONDS


@dataclass(frozen=True)
class Discovery:
    """What discovery found. ``issuer`` and ``resource`` are the provider's and the server's own strings (not the SDK
    models' normalised URLs). ``iss_supported``: the provider says its answer carries ``iss`` (RFC 9207)."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None
    resource: str
    scope: str | None
    token_auth_methods: tuple[str, ...] | None
    iss_supported: bool


@dataclass(frozen=True)
class Client:
    """The client Nous uses at the provider. ``registration``: ``dynamic``, ``provided`` (by the owner) or ``reused``
    (the stored one, the issuer unchanged)."""

    client_id: str
    client_secret: str | None = field(repr=False)
    token_auth_method: str
    registration: str


async def _request(
    client: httpx.AsyncClient,
    allow: frozenset[str],
    method: str,
    url: Any,
    what: str,
    *,
    read: bool = True,
    **kwargs: Any,
) -> tuple[int, bytes, httpx.Headers]:
    """One request through ``client`` after the address checks: ``(status, body, headers)``, the body read only with
    ``read`` and cut at ``BODY_MAX_BYTES`` (refused past it)."""
    if not isinstance(url, str):
        raise OAuthProblem(502, f"{what}: its address is not text")
    try:
        await check_url(url, allow)
    except BlockedAddress as exc:
        raise OAuthProblem(502, f"{what}: the address guard refused it ({exc})") from None
    except Exception as exc:  # a name that does not resolve
        raise OAuthProblem(502, f"{what} could not be reached ({error_class(exc)})") from None
    quiet_host(url)
    try:
        async with asyncio.timeout(HTTP_SECONDS):
            async with client.stream(method, url, **kwargs) as response:
                body = b""
                if read:
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > BODY_MAX_BYTES:
                            raise ResponseTooLarge(f"a response passed {BODY_MAX_BYTES} bytes")
                        chunks.append(chunk)
                    body = b"".join(chunks)
                return response.status_code, body, response.headers
    except Exception as exc:  # the class only: an HTTP error's text carries the URL
        logger.warning("MCP client: OAuth: %s failed (%s)", what, error_class(exc))
        raise OAuthProblem(502, f"{what} could not be reached ({error_class(exc)})") from None


async def _metadata(
    client: httpx.AsyncClient, allow: frozenset[str], urls: list[str], model: type[BaseModel], what: str
) -> tuple[Any, dict[str, Any]] | None:
    """The first of ``urls`` that answers 200 with a body ``model`` parses, as ``(model, raw JSON)``; None when none
    does. Another 4xx is "not here" (the next URL); a redirect, a 429 or a 5xx ends discovery."""
    for candidate in urls:
        status, body, _headers = await _request(client, allow, "GET", candidate, what)
        if status == 200:
            try:
                parsed = model.model_validate_json(body)
                raw = json.loads(body)
            except (ValidationError, ValueError):
                continue
            if isinstance(raw, dict):
                return parsed, raw
            continue
        if 300 <= status < 400:
            raise OAuthProblem(502, f"{what} redirects to another address, and redirects are not followed")
        if status == 429 or status >= 500:
            raise OAuthProblem(502, f"{what} answered HTTP {status}")
    return None


def _endpoint(value: Any, what: str, *, query_ok: bool = False) -> str:
    """A provider's endpoint, checked as a server address is (``url_problem``); the authorization endpoint may carry a
    query string (the owner's browser opens it, Nous never fetches it)."""
    if not isinstance(value, str):
        raise OAuthProblem(502, f"the provider's {what} is missing")
    try:
        checked = urlsplit(value)._replace(query="").geturl() if query_ok else value
    except ValueError:
        checked = ""
    problem = url_problem(checked)
    if problem is not None:
        raise OAuthProblem(502, f"the provider's {what} cannot be used: {problem}")
    return value


def _scope(value: str | None) -> str | None:
    """A scope Nous may send, or None (a server's list that breaks the rule is not sent: the scope is optional)."""
    if value is None or len(value) > _SCOPE_MAX_CHARS or not _SCOPE.fullmatch(value):
        return None
    return value


def owner_scope(value: Any) -> str | None:
    """The owner's scope from a request, or None when absent. Raises ``OAuthProblem`` (400) when it breaks the rule."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or _scope(value) is None:
        raise OAuthProblem(
            400, f"a scope is up to {_SCOPE_MAX_CHARS} printable characters, words separated by one space"
        )
    return value


_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": LATEST_PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": CLIENT_NAME, "version": "1"},
    },
}


async def discover(url: str, http: HttpFactory, allow: frozenset[str], scope: str | None = None) -> Discovery:
    """The provider of the MCP server at ``url``: an unauthenticated ``initialize`` must be answered 401; then the
    protected resource metadata (the URL its ``WWW-Authenticate`` names, else the well-known ones), whose ``resource``
    must cover ``url``; then the authorization server's metadata, whose issuer must be the one it was looked up for.
    Without resource metadata, the authorization server is ``url``'s origin. The provider must support S256, the
    ``code`` response type and the ``authorization_code`` grant (the last two when listed). ``scope``: the owner's, else
    the 401's, else the metadata's. Raises ``OAuthProblem``."""
    # The issuer check is in the SDK from 1.30, the floor pyproject.toml requires.
    from mcp.client.auth.utils import validate_metadata_issuer

    async with http() as client:
        status, _body, headers = await _request(
            client,
            allow,
            "POST",
            url,
            "the server",
            read=False,
            json=_INITIALIZE,
            headers={"Accept": "application/json, text/event-stream"},
        )
        if 200 <= status < 300:
            raise OAuthProblem(409, "this server does not ask for sign-in")
        if status != 401:
            raise OAuthProblem(502, f"the server answered HTTP {status} to a request without credentials, not 401")
        answer = httpx.Response(401, headers=headers)
        found = await _metadata(
            client,
            allow,
            build_protected_resource_metadata_discovery_urls(extract_resource_metadata_from_www_auth(answer), url),
            ProtectedResourceMetadata,
            "the server's resource metadata",
        )
        auth_server: str | None = None
        prm_scopes: list[str] | None = None
        resource = resource_url_from_server_url(url)
        if found is not None:
            prm, raw_prm = found
            raw_resource = raw_prm.get("resource")
            if not isinstance(raw_resource, str) or not check_resource_allowed(url, raw_resource):
                raise OAuthProblem(502, "the server's resource metadata names another resource")
            resource = raw_resource
            servers = raw_prm.get("authorization_servers")
            auth_server = servers[0] if isinstance(servers, list) and servers and isinstance(servers[0], str) else None
            if auth_server is None:
                raise OAuthProblem(502, "the server's resource metadata names no authorization server")
            prm_scopes = prm.scopes_supported
        parts = urlsplit(url)
        expected_issuer = auth_server or f"{parts.scheme}://{parts.netloc}"
        found_as = await _metadata(
            client,
            allow,
            build_oauth_authorization_server_metadata_discovery_urls(auth_server, url),
            OAuthMetadata,
            "the provider's metadata",
        )
    if found_as is None:
        raise OAuthProblem(502, "the provider publishes no authorization server metadata")
    metadata, raw = found_as
    try:
        validate_metadata_issuer(metadata, expected_issuer)
    except Exception:  # its text holds both issuers
        raise OAuthProblem(
            502, "the provider's metadata names another issuer than the one it was looked up for"
        ) from None
    if "S256" not in (metadata.code_challenge_methods_supported or []):
        raise OAuthProblem(502, "the provider does not support PKCE with S256")
    if "response_types_supported" in raw and "code" not in metadata.response_types_supported:
        raise OAuthProblem(502, "the provider does not support the authorization code flow")
    if metadata.grant_types_supported is not None and "authorization_code" not in metadata.grant_types_supported:
        raise OAuthProblem(502, "the provider does not support the authorization code grant")
    issuer = raw.get("issuer")
    if not isinstance(issuer, str):
        raise OAuthProblem(502, "the provider's metadata names no issuer")
    authorization_endpoint = _endpoint(raw.get("authorization_endpoint"), "authorization endpoint", query_ok=True)
    token_endpoint = _endpoint(raw.get("token_endpoint"), "token endpoint")
    registration = raw.get("registration_endpoint")
    registration_endpoint = None if registration is None else _endpoint(registration, "registration endpoint")
    for endpoint, what in ((token_endpoint, "token endpoint"), (registration_endpoint, "registration endpoint")):
        if endpoint is not None:  # refused now rather than at the exchange: Nous posts to both
            try:
                await check_url(endpoint, allow)
            except BlockedAddress as exc:
                raise OAuthProblem(502, f"the provider's {what}: the address guard refused it ({exc})") from None
            except Exception as exc:
                raise OAuthProblem(502, f"the provider's {what} could not be resolved ({error_class(exc)})") from None
    for endpoint in (authorization_endpoint, token_endpoint, registration_endpoint):
        if endpoint is not None:
            quiet_host(endpoint)
    hint = extract_scope_from_www_auth(answer)
    listed = prm_scopes if prm_scopes is not None else metadata.scopes_supported
    chosen = scope or _scope(hint) or (_scope(" ".join(listed)) if listed else None)
    methods = metadata.token_endpoint_auth_methods_supported
    return Discovery(
        issuer=issuer,
        authorization_endpoint=authorization_endpoint,
        token_endpoint=token_endpoint,
        registration_endpoint=registration_endpoint,
        resource=resource,
        scope=chosen,
        token_auth_methods=tuple(methods) if methods is not None else None,
        iss_supported=raw.get("authorization_response_iss_parameter_supported") is True,
    )


def _auth_method(secret: str | None, discovery: Discovery, declared: str | None = None) -> str:
    """How the client authenticates at the token endpoint: ``none`` without a secret, else what the registration
    declared, else the first of basic and post the provider lists (basic when it lists none, its default)."""
    if secret is None:
        return "none"
    if declared in _SECRET_METHODS:
        return declared
    supported = discovery.token_auth_methods or ("client_secret_basic",)
    for method in _SECRET_METHODS:
        if method in supported:
            return method
    raise OAuthProblem(502, "the provider accepts no client authentication Nous can use")


def owner_client(discovery: Discovery, client_id: Any, client_secret: Any) -> Client:
    """The client id (and secret) the owner registered with the provider. Raises ``OAuthProblem`` (400) for a value
    that breaks its rule; the refusal never repeats it."""
    if not isinstance(client_id, str) or not _CLIENT_ID.fullmatch(client_id):
        raise OAuthProblem(400, "a client id is 1 to 512 printable ASCII characters with no space")
    if client_secret is not None and client_secret != "" and not token_ok(client_secret):
        raise OAuthProblem(
            400,
            'a client secret is 8 to 8192 printable ASCII characters with no space, " or \\, '
            "8 of them letters or digits",
        )
    secret = client_secret or None
    return Client(client_id, secret, _auth_method(secret, discovery), "provided")


async def register(discovery: Discovery, redirect: str, http: HttpFactory, allow: frozenset[str]) -> Client:
    """Dynamic client registration (RFC 7591) as ``Nous`` with ``redirect`` as its one redirect address. 409 when the
    provider offers none: the owner must register an app and give its client id."""
    if discovery.registration_endpoint is None:
        raise OAuthProblem(
            409,
            "this server needs a client id: register an OAuth app with the provider using the redirect address "
            f"{redirect} and enter its client id",
        )
    body = {
        "client_name": CLIENT_NAME,
        "redirect_uris": [redirect],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    async with http() as client:
        status, answer, _headers = await _request(
            client, allow, "POST", discovery.registration_endpoint, "the client registration", json=body
        )
    if status not in (200, 201):
        raise OAuthProblem(502, f"the provider refused the client registration (HTTP {status})")
    try:
        info = OAuthClientInformationFull.model_validate_json(answer)
    except ValidationError:
        raise OAuthProblem(502, "the provider's registration answer cannot be read") from None
    if info.client_id is None or not _CLIENT_ID.fullmatch(info.client_id):
        raise OAuthProblem(502, "the provider's registration answer carries no usable client id")
    if info.client_secret is not None and not token_ok(info.client_secret):
        raise OAuthProblem(502, "the provider's client secret breaks the rule for a header value")
    method = _auth_method(info.client_secret, discovery, info.token_endpoint_auth_method)
    return Client(info.client_id, info.client_secret, method, "dynamic")


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


@dataclass
class Flow:
    """One sign-in in progress (or just finished). The state and the verifier never leave the process. A completed
    flow for a server that does not exist yet holds its ``record`` until ``POST /servers`` consumes it."""

    flow_id: str
    server: str
    url: str
    discovery: Discovery
    client: Client
    redirect: str
    expires_at: float
    state: str = field(repr=False)
    verifier: str = field(repr=False)
    status: str = "pending"  # pending, complete or failed ("expired" is read from the clock)
    reason: str | None = None
    record: OAuthRecord | None = field(default=None, repr=False)
    state_used: bool = False

    def authorization_url(self) -> str:
        """The address the owner's browser opens: the code flow with PKCE (S256), the resource and the scope."""
        params = {
            "response_type": "code",
            "client_id": self.client.client_id,
            "redirect_uri": self.redirect,
            "state": self.state,
            "code_challenge": _challenge(self.verifier),
            "code_challenge_method": "S256",
            "resource": self.discovery.resource,
        }
        if self.discovery.scope:
            params["scope"] = self.discovery.scope
        endpoint = self.discovery.authorization_endpoint
        return f"{endpoint}{'&' if urlsplit(endpoint).query else '?'}{urlencode(params, quote_via=quote)}"


class OAuthFlows:
    """The sign-ins in progress, in memory: each lives ``FLOW_SECONDS`` (a completed one gets a fresh
    ``FLOW_SECONDS``: the owner still has to check and approve tools), at most ``FLOWS_PENDING_MAX`` pending at once.
    A flow is forgotten ``FLOW_SECONDS`` after it expired."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._flows: dict[str, Flow] = {}
        self._clock = clock

    def start(self, server: str, url: str, discovery: Discovery, client: Client, redirect: str) -> Flow:
        self._purge()
        if sum(1 for flow in self._flows.values() if self.status(flow) == "pending") >= FLOWS_PENDING_MAX:
            raise OAuthProblem(429, f"{FLOWS_PENDING_MAX} sign-ins are already in progress: finish one or wait")
        flow = Flow(
            flow_id=secrets.token_urlsafe(32),
            server=server,
            url=url,
            discovery=discovery,
            client=client,
            redirect=redirect,
            expires_at=self._clock() + FLOW_SECONDS,
            state=secrets.token_urlsafe(32),
            verifier=secrets.token_urlsafe(64),
        )
        self._flows[flow.flow_id] = flow
        return flow

    def status(self, flow: Flow) -> str:
        if flow.status in ("pending", "complete") and self._clock() > flow.expires_at:
            return "expired"
        return flow.status

    def get(self, flow_id: Any) -> Flow | None:
        self._purge()
        return self._flows.get(flow_id) if isinstance(flow_id, str) else None

    def take_by_state(self, state: Any) -> Flow | None:
        """The pending flow whose state is ``state`` (compared in constant time with every flow's), marked used so a
        second answer with the same state finds nothing; None when there is none."""
        if not isinstance(state, str):
            return None
        given = state.encode("utf-8")
        found = None
        for flow in self._flows.values():
            if hmac.compare_digest(flow.state.encode("ascii"), given):
                found = flow
        return self._use(found)

    def take(self, flow_id: Any, state: Any) -> Flow | None:
        """The pending flow ``flow_id`` when ``state`` is its state (constant time), marked used; else None."""
        flow = self.get(flow_id)
        if flow is None or not isinstance(state, str):
            return None
        return self._use(flow if hmac.compare_digest(flow.state.encode("ascii"), state.encode("utf-8")) else None)

    def _use(self, flow: Flow | None) -> Flow | None:
        if flow is None or flow.state_used or self.status(flow) != "pending":
            return None
        flow.state_used = True
        return flow

    def complete(self, flow: Flow, record: OAuthRecord | None) -> None:
        """The sign-in finished: ``record`` is held until a create consumes it (None when it was stored at once)."""
        flow.status, flow.record, flow.reason = "complete", record, None
        flow.expires_at = self._clock() + FLOW_SECONDS

    def fail(self, flow: Flow, reason: str) -> None:
        flow.status, flow.record, flow.reason = "failed", None, reason

    def completed_for(self, flow_id: Any, server: str, url: str) -> Flow:
        """The completed flow ``flow_id`` holding a record for ``server`` at ``url``; 409 (a fixed sentence) when it is
        unknown, not complete, expired, already used, or bound to another name or address."""
        flow = self.get(flow_id)
        if flow is None or self.status(flow) != "complete" or flow.record is None:
            raise OAuthProblem(409, "that sign-in is not complete, has expired or was already used: sign in again")
        if flow.server != server or flow.url != url:
            raise OAuthProblem(409, "that sign-in was made for another server name or address: sign in again")
        return flow

    def consume(self, flow: Flow) -> None:
        self._flows.pop(flow.flow_id, None)

    def _purge(self) -> None:
        now = self._clock()
        for flow_id in [key for key, flow in self._flows.items() if now > flow.expires_at + FLOW_SECONDS]:
            del self._flows[flow_id]


def callback_params(callback_url: Any, redirect: str) -> dict[str, str]:
    """The parameters of the address the provider sent the owner's browser back to (the paste fallback): it must be
    the redirect address. The first value of each; the refusal never repeats the address."""
    if not isinstance(callback_url, str) or callback_url.split("?", 1)[0].split("#", 1)[0] != redirect:
        raise OAuthProblem(
            400, "this is not the sign-in's return address: paste the whole address from the address bar"
        )
    query = urlsplit(callback_url).query
    return {key: values[0] for key, values in parse_qs(query, keep_blank_values=True).items() if values}


def _check_answer(flow: Flow, params: Mapping[str, Any]) -> str:
    """The code of the provider's answer, after its ``error`` and ``iss`` (RFC 9207: checked whenever present, and
    required when the provider says it sends it). Raises ``OAuthProblem`` with a fixed reason."""
    error = params.get("error")
    if error is not None:
        if error == "access_denied":
            raise OAuthProblem(400, "the provider said the request was denied")
        raise OAuthProblem(400, "the provider reported an error")
    iss = params.get("iss")
    if iss is not None and iss != flow.discovery.issuer:
        raise OAuthProblem(400, "the provider's answer names another issuer")
    if iss is None and flow.discovery.iss_supported:
        raise OAuthProblem(400, "the provider's answer does not name its issuer")
    code = params.get("code")
    if not isinstance(code, str) or not _CODE.fullmatch(code):
        raise OAuthProblem(400, "the provider's answer carries no usable code")
    return code


async def _token(
    token_endpoint: str,
    client_id: str,
    client_secret: str | None,
    method: str,
    form: dict[str, str],
    what: str,
    http: HttpFactory,
    allow: frozenset[str],
) -> OAuthToken:
    """One token request (form-encoded, with the client's authentication), its answer parsed and its tokens checked
    against the header value rule."""
    data = dict(form)
    headers = {"Accept": "application/json"}
    if method == "client_secret_basic" and client_secret is not None:
        pair = f"{quote(client_id, safe='')}:{quote(client_secret, safe='')}".encode("ascii")
        headers["Authorization"] = f"Basic {base64.b64encode(pair).decode('ascii')}"
    else:
        data["client_id"] = client_id
        if method == "client_secret_post" and client_secret is not None:
            data["client_secret"] = client_secret
    async with http() as client:
        status, body, _headers = await _request(client, allow, "POST", token_endpoint, what, data=data, headers=headers)
    if status != 200:
        raise OAuthProblem(502, f"the provider refused {what} (HTTP {status})")
    try:
        token = OAuthToken.model_validate_json(body)  # token_type must be bearer (any case)
    except ValidationError:
        raise OAuthProblem(502, f"the provider's answer to {what} cannot be read") from None
    if not token_ok(token.access_token) or (token.refresh_token is not None and not token_ok(token.refresh_token)):
        raise OAuthProblem(502, f"the tokens of {what} break the rule for a header value")
    return token


def _expires_at(token: OAuthToken) -> int | None:
    return None if token.expires_in is None else int(time.time()) + max(0, token.expires_in)


async def exchange_code(flow: Flow, params: Mapping[str, Any], http: HttpFactory, allow: frozenset[str]) -> OAuthRecord:
    """The token record for the provider's answer ``params`` (its ``error``, ``iss`` and ``code`` checked), from the
    code exchange with the flow's verifier, redirect address and resource."""
    code = _check_answer(flow, params)
    client, discovery = flow.client, flow.discovery
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": flow.redirect,
        "code_verifier": flow.verifier,
        "resource": discovery.resource,
    }
    token = await _token(
        discovery.token_endpoint,
        client.client_id,
        client.client_secret,
        client.token_auth_method,
        form,
        "the code exchange",
        http,
        allow,
    )
    return OAuthRecord(
        issuer=discovery.issuer,
        token_endpoint=discovery.token_endpoint,
        client_id=client.client_id,
        client_secret=client.client_secret,
        token_auth_method=client.token_auth_method,
        resource=discovery.resource,
        scope=token.scope or discovery.scope,
        access_token=token.access_token,
        refresh_token=token.refresh_token,
        expires_at=_expires_at(token),
    )


async def refresh_tokens(record: OAuthRecord, http: HttpFactory, allow: frozenset[str]) -> OAuthRecord:
    """``record`` with the tokens of a refresh at its stored token endpoint (never discovered again): a new access
    token, the new refresh token when the answer carries one (else the old one). One attempt, ``HTTP_SECONDS``."""
    if not record.refresh_token:
        raise OAuthProblem(409, "there is no refresh token")
    form = {"grant_type": "refresh_token", "refresh_token": record.refresh_token, "resource": record.resource}
    if record.scope:
        form["scope"] = record.scope
    token = await _token(
        record.token_endpoint,
        record.client_id,
        record.client_secret,
        record.token_auth_method,
        form,
        "the token refresh",
        http,
        allow,
    )
    return replace(
        record,
        access_token=token.access_token,
        refresh_token=token.refresh_token or record.refresh_token,
        expires_at=_expires_at(token),
        scope=token.scope or record.scope,
    )
