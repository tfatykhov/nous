"""F112 PR 2a: the owner's routes for the MCP client's servers, under ``/integrations/mcp``.

Mounted by ``rest.py`` only when ``NOUS_MCP_CLIENT_ENABLED`` is on. Approval is a request to these routes: a server
added with ``POST /integrations/mcp/servers`` or a permission set with ``PATCH`` takes effect at once, through the
client's ``reload``. There is no other approval step.

Authentication: the owner's proxy adds ``X-Nous-Proxy-Secret`` to the requests it has authenticated, and this process
holds only the secret's SHA-256 (``NOUS_MCP_CLIENT_UI_SECRET_SHA256``), compared in constant time. Without a valid
digest, or with no client running, every route answers 503; without a matching header, 403. Every POST, PATCH and
DELETE must be sent with the media type exactly ``application/json`` (415 otherwise; ``text/plain; application/json``
is a type a cross-site page sends with no preflight, and the proxy would add the secret). The header is never logged.

What this protects against: a request from the LAN that did not come through the proxy. Not code running in the Nous
process: the agent's ``bash`` and ``run_python`` run here, so they can call these routes on loopback (every proxied
request, header included, reaches this process) or write the rows themselves. The host's two optional ceilings
(``NOUS_MCP_CLIENT_PINNED_SERVERS``, ``NOUS_MCP_CLIENT_BACKGROUND_SERVERS``) are the hard limit for an owner who
wants one.

A tool's description and schema are stored as the server gave them, from a probe this process makes itself (never
from the request), and shown as ``prepare_tool`` makes them, which is what the model is offered. Only ``allow`` and
``deny`` are accepted: ``ask`` is not offered yet. A create or refresh probes again, so the server could answer
differently from what the owner was shown: ``definitions_sha256`` (``/probe``'s answer) passed back as
``expected_definitions_sha256`` makes a different listing a 409 with nothing stored.

Every mutating route emits one ``mcp_client_server_changed`` event: the action, the server's name, the names of the
fields that changed, the old and new ``enabled`` and ``background``, the tools whose permission changed with the
old and new permission, and what happened to the stored credential (``credentials``: ``set``, ``replaced``,
``removed`` or null, and ``header_count``). Never a URL, a description, a header name or a value: the agent's reader
can read the events table. A tool's name is the server's text, so in an event it keeps ``[A-Za-z0-9_.-]`` only
(anything else becomes ``?``), is cut to 80 characters, and a list names at most 60 tools (``more`` counts the rest).

Stored credentials (PR 2c): ``headers`` (``{name: value}``, the rules of ``config.validate_headers``) on a create or a
``PATCH`` become the server's credential, sealed with the host's key (``NOUS_MCP_CLIENT_KEY_FILE``; without one, 409).
No route ever returns a value: the listing names the headers only. A credential never follows a server to another
address: ``/probe`` uses only the headers in its own request (never stored), a create without headers probes with the
stored credential opened as bound to the request's URL (so another URL cannot open it) and, at another URL, deletes
it, and ``/refresh`` uses the stored URL and credential. A probe the server answers 401 or 403, or whose listing
repeats a credential it was sent or the server's stored one, is 502 with a fixed sentence and stores nothing.

OAuth sign-in (PR 2d, ``nous/mcp_client/oauth.py``): ``POST /oauth/start`` discovers the server's provider, registers
a client (or takes the owner's client id, or reuses the stored one while the issuer is unchanged) and answers with the
address the owner's browser opens and the authorization and token hosts. The provider sends the browser back to
``<NOUS_A2UI_PUBLIC_BASE_URL>/integrations/mcp/oauth/callback`` (a GET through the same proxy, so it carries the proxy
secret too), which exchanges the code and answers with one of two fixed pages that reflect nothing of the request
(``default-src 'none'``, no script); ``POST /oauth/complete`` takes the address pasted from the browser's address bar
instead. ``GET /oauth/flows/{id}`` says how a sign-in stands. For a stored server at the same address the tokens are
stored at once (a new credential row: the connection is dropped); for a new one the finished sign-in is held in memory
until ``POST /servers`` with its ``oauth_flow_id`` stores it with the server (``POST /probe`` takes it too). A sign-in
is bound to its server name and address. ``POST /servers/{name}/sign-out`` deletes the stored tokens (the provider's
grant is not revoked). No answer, log line or event carries a code, a state, a verifier or a token; the uvicorn access
line of the callback (its query string holds the code and the state) is dropped.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from nous.api.commitment_routes import owner_secret_matches
from nous.events import Event
from nous.mcp_client.bridge import serialize_schema
from nous.mcp_client.config import ServerConfig, ToolConfig, validate_headers
from nous.mcp_client.manager import (
    ProbeCredentials,
    ProbedTool,
    ProbeFailed,
    ProbeRefused,
    ServerVerdict,
    ToolVerdict,
)
from nous.mcp_client.oauth import (
    CALLBACK_PATH,
    FLOW_SECONDS,
    Client,
    Flow,
    OAuthProblem,
    callback_params,
    discover,
    exchange_code,
    host_of,
    owner_client,
    owner_scope,
    redirect_uri,
    refresh_due,
    refresh_tokens,
    register,
)
from nous.mcp_client.registry import McpRegistry
from nous.mcp_client.secrets import OAuthRecord, StoredSecret

logger = logging.getLogger(__name__)

PROXY_SECRET_HEADER = "X-Nous-Proxy-Secret"
CHANGE_EVENT = "mcp_client_server_changed"
_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}")
# A tool's name is the server's text, and the agent's reader can read the events table: in an event it keeps these
# characters only (any other becomes "?"), is cut to 80, and one list names at most 60 tools (the rest are counted).
_EVENT_TOOL_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")
_EVENT_TOOL_CHARS = 80
_EVENT_TOOLS_MAX = 60
_SERVER_FIELDS = ("url", "enabled", "background", "call_timeout_seconds")
NO_KEY_TEXT = "no key file is configured on the host (NOUS_MCP_CLIENT_KEY_FILE)"
OAUTH_UNAVAILABLE_TEXT = "sign-in is not available: NOUS_A2UI_PUBLIC_BASE_URL is not an https address"

Handler = Callable[[Request], Awaitable[Response]]

# The callback's two pages: fixed bytes, nothing of the request in them, no script, nothing loaded.
_PAGE = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1"><title>Nous sign-in</title></head>'
    "<body><p>{}</p></body></html>"
)
SIGNED_IN_PAGE = _PAGE.format("Signed in. You can close this tab.").encode("utf-8")
NOT_SIGNED_IN_PAGE = _PAGE.format("Sign-in did not complete. Go back to Nous and try again.").encode("utf-8")
_PAGE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


class _Superseded(Exception):
    """A sign-in whose code exchange returned after it stopped being the current one (a newer sign-in replaced it, or
    it expired): it stores nothing and stays as it is."""


class _QuietOAuthCallback(logging.Filter):
    """Drops uvicorn's access line for the OAuth callback: it prints the request's query string, which holds the
    authorization code and the state. Other lines pass."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            return CALLBACK_PATH not in record.getMessage()
        except Exception:  # a record whose arguments do not format: not this one
            return True


_ACCESS_LOG = logging.getLogger("uvicorn.access")
if not any(isinstance(f, _QuietOAuthCallback) for f in _ACCESS_LOG.filters):
    _ACCESS_LOG.addFilter(_QuietOAuthCallback())


class _Refused(Exception):
    """A request the route answers with ``status`` and ``message`` (fixed text, or what the caller sent), plus
    ``extra`` fields in the answer."""

    def __init__(self, status: int, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.extra = extra


def _is_json(request: Request) -> bool:
    """Whether the request's media type is exactly ``application/json`` (parameters such as a charset aside). A
    substring match would pass ``text/plain; application/json``, a type a cross-site page sends with no preflight."""
    media_type = (request.headers.get("content-type", "") or "").split(";")[0]
    return media_type.strip().lower() == "application/json"


def definitions_sha256(probed: list[ProbedTool]) -> str:
    """The SHA-256 of what a listing stores: canonical JSON (sorted keys, compact separators, UTF-8, non-ASCII kept)
    of ``[{tool, description, input_schema}]`` as the server gave them, sorted by tool name."""
    items = [
        {"tool": tool.tool, "description": tool.description, "input_schema": tool.input_schema}
        for tool in sorted(probed, key=lambda tool: tool.tool)
    ]
    canonical = json.dumps(items, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _digest(raw: Any) -> str:
    """The setting as a lowercase hex SHA-256, or "" when it is not one (fail closed). The first field only:
    ``sha256sum`` prints the digest followed by "  -"."""
    fields = raw.split() if isinstance(raw, str) else []
    digest = fields[0].lower() if fields else ""
    return digest if _SHA256_HEX_RE.fullmatch(digest) else ""


async def _body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise _Refused(400, "the body must be a JSON object")
    return body


def _only(body: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise _Refused(400, f"unknown field(s): {', '.join(unknown)}")


def _flag(body: dict[str, Any], key: str) -> bool | None:
    """``body[key]`` when it is a JSON boolean, None when absent; anything else is refused (``"false"`` is not)."""
    if key not in body:
        return None
    if not isinstance(body[key], bool):
        raise _Refused(400, f"{key} must be true or false")
    return body[key]


def _permissions(value: Any) -> dict[str, str]:
    """A ``{tool: "allow" | "deny"}`` object. ``ask`` is refused: it is not offered yet."""
    if not isinstance(value, dict):
        raise _Refused(400, 'tools must be an object of tool name to "allow" or "deny"')
    for permission in value.values():
        if permission == "ask":
            raise _Refused(400, '"ask" is not offered yet: use "allow" or "deny"')
        if permission not in ("allow", "deny"):
            raise _Refused(400, 'a tool\'s permission must be "allow" or "deny"')
    return dict(value)


def _headers(value: Any) -> dict[str, str]:
    """A credential's headers (``config.validate_headers``); the refusal names the header and the rule, never a
    value."""
    try:
        return validate_headers(value)
    except ValueError as exc:
        raise _Refused(400, str(exc)) from None


def _server_config(name: Any, url: Any, background: bool) -> ServerConfig:
    """The server's name and URL, validated as a row holds them (``url_problem``). The error names the field and the
    rule, never the value."""
    if not isinstance(name, str) or not isinstance(url, str):
        raise _Refused(400, "name and url must be strings")
    try:
        return ServerConfig(name=name, url=url, background=background)
    except ValidationError as exc:
        raise _Refused(400, "; ".join(f"{error['loc'][0]}: {error['msg']}" for error in exc.errors())) from None


def _count(credentials: str | None, headers: dict[str, str] | None) -> int | None:
    """The event's ``header_count``: the headers stored now when the credential changed (0 once removed)."""
    if credentials is None:
        return None
    return len(headers) if headers is not None else 0


def _event_tool(tool: str) -> str:
    return _EVENT_TOOL_UNSAFE.sub("?", tool[:_EVENT_TOOL_CHARS])


def _capped(items: list[Any]) -> tuple[list[Any], int]:
    """At most ``_EVENT_TOOLS_MAX`` items of an event's tool list, and how many were left out."""
    return items[:_EVENT_TOOLS_MAX], max(0, len(items) - _EVENT_TOOLS_MAX)


def _change(
    action: str,
    server: str,
    old: ServerConfig | None,
    new: ServerConfig | None,
    old_permissions: dict[str, str],
    new_permissions: dict[str, str],
    *,
    credentials: str | None = None,
    header_count: int | None = None,
) -> dict[str, Any]:
    """The ``mcp_client_server_changed`` event's data: names, switches and permissions only. ``more`` counts the
    tools each list left out past its cap. ``credentials``: ``set``, ``replaced`` or ``removed`` when the stored
    credential changed, with ``header_count`` its number of headers now (never the names: a key pasted into a name
    would land here)."""
    changed = [field for field in _SERVER_FIELDS if getattr(old, field, None) != getattr(new, field, None)]
    tools = [
        {"tool": _event_tool(tool), "old": old_permissions.get(tool), "new": new_permissions.get(tool)}
        for tool in sorted(set(old_permissions) | set(new_permissions))
        if old_permissions.get(tool) != new_permissions.get(tool)
    ]
    if tools:
        changed.append("tools")
    if credentials is not None:
        changed.append("credentials")
    shown, more = _capped(tools)
    return {
        "action": action,
        "server": server,
        "changed": changed,
        "enabled": {"old": old.enabled if old else None, "new": new.enabled if new else None},
        "background": {"old": old.background if old else None, "new": new.background if new else None},
        "tools": shown,
        "more": {"tools": more},
        "credentials": credentials,
        "header_count": header_count,
    }


def _probe_view(probed: ProbedTool) -> dict[str, Any]:
    """One probed tool as the model would be offered it (what the seed prints), or why it would not be."""
    prepared = probed.prepared
    return {
        "tool": probed.tool,
        "name": prepared.name,
        "description": prepared.schema["description"] if prepared.schema else None,
        "schema": serialize_schema(prepared.schema) if prepared.schema else None,
        "skip_reason": prepared.problem,
    }


def _rows_from_probe(server: str, probed: list[ProbedTool], permissions: dict[str, str]) -> list[ToolConfig]:
    """The tool rows a probe gives: description and schema as the server listed them, ``deny`` unless named."""
    return [
        ToolConfig(
            server=server,
            tool=tool.tool,
            description=tool.description,
            input_schema=tool.input_schema,
            permission=permissions.get(tool.tool, "deny"),  # type: ignore[arg-type]
        )
        for tool in probed
    ]


def build_mcp_client_routes(*, database: Any, settings: Any, manager: Any, bus: Any) -> list[Route]:
    """The ``/integrations/mcp`` routes over the MCP client ``manager`` and a registry for ``settings.agent_id``.
    ``manager`` and ``bus`` may be lazy components: a falsy ``manager`` answers 503."""
    registry = McpRegistry(database, settings.agent_id)

    def redirect_address() -> str | None:
        """The OAuth redirect address, read when a request needs it (never at build: a settings object that is not
        text there must not fail the app); None: OAuth is unavailable here."""
        return redirect_uri(getattr(settings, "a2ui_public_base_url", ""))

    digest = _digest(settings.mcp_client_ui_secret_sha256)
    if not digest:
        logger.warning(
            "F112: NOUS_MCP_CLIENT_UI_SECRET_SHA256 is not set to a SHA-256 digest, so every /integrations/mcp route "
            "answers 503"
        )
    changes = asyncio.Lock()  # one change at a time: read the old row, write, emit, reload

    def endpoint(handler: Handler, *, mutating: bool) -> Handler:
        async def run(request: Request) -> Response:
            if not digest:
                return JSONResponse({"error": "the MCP client routes are not configured"}, status_code=503)
            if not owner_secret_matches(request.headers.get(PROXY_SECRET_HEADER), digest):
                logger.warning("F112: refused an /integrations/mcp request: no valid %s", PROXY_SECRET_HEADER)
                return JSONResponse({"error": "forbidden"}, status_code=403)
            if mutating and not _is_json(request):
                return JSONResponse({"error": "Content-Type must be application/json"}, status_code=415)
            if not manager or manager.closed:
                return JSONResponse({"error": "no MCP client is running"}, status_code=503)
            try:
                return await handler(request)
            except _Refused as exc:
                return JSONResponse({"error": str(exc), **exc.extra}, status_code=exc.status)
            except Exception as exc:
                # The class only, never a traceback: a database error's text can carry the URL, and an error raised
                # from what a server or a provider sent can repeat a credential it was sent.
                logger.warning("F112: %s failed (%s)", handler.__name__, type(exc).__name__)
                return JSONResponse({"error": "the request could not be processed"}, status_code=500)

        return run

    async def stored(name: str) -> tuple[ServerConfig, list[ToolConfig]] | None:
        for config, tools in await registry.list_all():
            if config.name == name:
                return config, tools
        return None

    async def secret_of(name: str) -> StoredSecret | None:
        return (await registry.list_secrets()).get(name)

    async def stored_values(name: str) -> list[str]:
        """The values of the server's stored credential (header values, or an OAuth record's tokens and client
        secret), opened with its stored URL (none when it cannot be opened): a listing that repeats one is refused
        even when other headers, or none, were sent."""
        secret, current = await secret_of(name), await stored(name)
        if secret is None or current is None:
            return []
        return manager.credential_values(name, current[0].url, secret)

    async def probe_tools(
        name: str, url: str, headers: dict[str, str] | None = None, record: OAuthRecord | None = None
    ) -> list[ProbedTool]:
        """The probe, with ``headers`` sent, or a finished sign-in's access token (``record``); a listing that repeats
        the server's stored credential or the sign-in's tokens is refused."""
        withheld = await stored_values(name)
        if record is not None:
            headers = {"Authorization": f"Bearer {record.access_token}"}
            withheld += record.values()
        try:
            return await manager.probe(name, url, headers, withheld=withheld)
        except ProbeRefused as exc:
            raise _Refused(400, f"the address guard refused the URL: {exc}") from None
        except ProbeCredentials as exc:
            raise _Refused(502, str(exc)) from None
        except ProbeFailed as exc:
            raise _Refused(502, f"could not list the server's tools: {exc}") from None

    def sealed_for(name: str, url: str, headers: dict[str, str]) -> Any:
        """``headers`` sealed for the server ``name`` at ``url``; 409 when the host has no key."""
        sealed = manager.seal_credentials(name, url, headers)
        if sealed is None:
            raise _Refused(409, NO_KEY_TEXT)
        return sealed

    async def audit(data: dict[str, Any]) -> None:
        if not bus:
            return
        try:
            await bus.emit(Event(type=CHANGE_EVENT, agent_id=settings.agent_id, data=data))
        except Exception:
            logger.debug("F112: the change event was not emitted", exc_info=True)

    async def apply() -> None:
        try:
            await manager.reload()
        except Exception as exc:
            logger.warning(
                "F112: a change to the MCP servers was stored, but the reload failed (%s)", type(exc).__name__
            )
            raise _Refused(500, "the change was stored, but applying it failed: see the log") from None
        if manager.closed:  # closed while the change was written: the reload did nothing
            raise _Refused(503, "the change was stored, but no MCP client is running to apply it")

    def expect(body: dict[str, Any], probed: list[ProbedTool]) -> str:
        """The listing's ``definitions_sha256``; 409 (nothing stored) when the request's
        ``expected_definitions_sha256`` names another: the server answered differently from what the owner saw."""
        expected = body.get("expected_definitions_sha256")
        if expected is not None and not isinstance(expected, str):
            raise _Refused(400, "expected_definitions_sha256 must be a string")
        actual = definitions_sha256(probed)
        if expected is not None and expected.lower() != actual:
            raise _Refused(
                409, "the server's tool definitions changed since they were shown: review them again",
                definitions_sha256=actual,
            )
        return actual

    def tool_view(verdict: ToolVerdict, server: str) -> dict[str, Any]:
        prepared = verdict.prepared
        return {
            "tool": verdict.tool,
            "name": prepared.name,
            "permission": verdict.permission,
            # This row's own tool is the one registered under the name, not another that maps to it (a.b and a_b).
            "registered": prepared.name is not None
            and manager.registered_source(prepared.name) == (server, verdict.tool),
            "skip_reason": verdict.reason,
            "blocked_by_ceiling": verdict.ceiling,
            "description": prepared.schema["description"] if prepared.schema else None,
            "schema": serialize_schema(prepared.schema) if prepared.schema else None,
        }

    def auth_view(verdict: ServerVerdict, secret: StoredSecret | None, refused: int | None) -> dict[str, Any]:
        """The server's stored credential, without its values: the kind, the header names, when it was stored, and
        whether it works (``unavailable``: it cannot be opened here, ``reason`` says why; ``needs_sign_in``: its OAuth
        tokens can no longer be refreshed; ``provider_unreachable``: a refresh found the sign-in provider unreachable
        less than 30 s ago; ``credentials_refused``: the server refused a request with a 401, or with a 403
        while a credential is stored, ``reason`` saying so for a 403)."""
        reason = verdict.credentials
        if verdict.credentials is not None:
            status = "unavailable"
        elif secret is not None and manager.needs_sign_in(verdict.config.name, secret.id):
            status = "needs_sign_in"
        elif secret is not None and manager.provider_unreachable(verdict.config.name, secret.id):
            status = "provider_unreachable"
        elif refused is not None:
            status = "credentials_refused"
            reason = "the server answered 403 (forbidden)" if refused == 403 else None
        else:
            status = "ok"
        return {
            "kind": secret.kind if secret is not None else "none",
            "header_names": list(secret.header_names) if secret is not None else [],
            "updated_at": secret.updated_at.isoformat() if secret is not None and secret.updated_at else None,
            "status": status,
            "reason": reason,
        }

    def server_view(
        verdict: ServerVerdict,
        connected: dict[str, bool],
        secret: StoredSecret | None = None,
        refused: int | None = None,
    ) -> dict[str, Any]:
        config = verdict.config
        return {
            "name": config.name,
            "url": config.url,
            "enabled": config.enabled,
            "background": config.background,
            "background_in_effect": verdict.background,
            "background_blocked_by_ceiling": verdict.background_ceiling,
            "call_timeout_seconds": config.call_timeout_seconds,
            "connected": connected.get(config.name, False),
            "blocked_by_ceiling": verdict.ceiling,
            "skip_reason": verdict.reason,
            "tools": [tool_view(tool, config.name) for tool in verdict.tools],
            "auth": auth_view(verdict, secret, refused),
        }

    async def views() -> list[dict[str, Any]]:
        connected = {server["name"]: server["connected"] for server in manager.status()["servers"]}
        refused = manager.credentials_refused()
        secrets = await registry.list_secrets()
        return [
            server_view(verdict, connected, secrets.get(verdict.config.name), refused.get(verdict.config.name))
            for verdict in manager.plan(await registry.list_all(), secrets)
        ]

    async def view(name: str) -> dict[str, Any] | None:
        return next((server for server in await views() if server["name"] == name), None)

    async def list_servers(request: Request) -> JSONResponse:
        """GET /integrations/mcp/servers: every stored server with its tools, what registers and why not, and
        whether a credential can be stored on this host (``secrets_available``)."""
        return JSONResponse(
            {
                "ceilings": manager.ceilings(),
                "secrets_available": manager.secrets_available(),
                "oauth_available": redirect_address() is not None,
                "servers": await views(),
            }
        )

    def finished_flow(body: dict[str, Any], config: ServerConfig) -> Flow | None:
        """The finished sign-in ``oauth_flow_id`` names, bound to the request's name and URL (409 otherwise); None when
        the request names none. ``headers`` and ``oauth_flow_id`` together are refused."""
        if "oauth_flow_id" not in body:
            return None
        if "headers" in body:
            raise _Refused(400, "give headers or oauth_flow_id, not both")
        try:
            return manager.flows.completed_for(body["oauth_flow_id"], config.name, config.url)
        except OAuthProblem as exc:
            raise _Refused(exc.status, str(exc)) from None

    async def held_record(flow: Flow | None) -> OAuthRecord | None:
        """The finished sign-in's record (renewed first when about to expire), read ONCE: the request uses this copy
        for its probe and its write, whatever happens to the flow meanwhile (its expiry clears the held record).
        None without a flow; 409 when a flow was named and holds no record any more."""
        if flow is None:
            return None
        await renew(flow)
        record = flow.record
        if record is None:
            raise _Refused(409, "that sign-in is not complete, has expired or was already used: sign in again")
        return record

    async def renew(flow: Flow | None) -> None:
        """A finished sign-in's tokens, refreshed first when they are about to expire (a sign-in waits up to 10 minutes
        for its create; a token can live less): the refreshed record replaces the held one, so the probe sends it and
        the create stores it."""
        if flow is None:
            return
        async with flow.renewing:  # a concurrent probe and create make one token request
            # Without a refresh token the record is used as it is until it has expired.
            if flow.record is None or not refresh_due(flow.record):
                return
            try:
                flow.record = await refresh_tokens(flow.record, manager.oauth_http, manager.allow_private_hosts)
            except OAuthProblem as exc:
                if exc.transient:
                    raise _Refused(502, "the sign-in provider could not be reached: try again in a moment") from None
                raise _Refused(409, "the sign-in's tokens expired and could not be renewed: sign in again") from None

    async def probe(request: Request) -> JSONResponse:
        """POST /integrations/mcp/probe ``{name, url, headers? | oauth_flow_id?}``: the server's tools as they would be
        registered, with ``headers`` sent on its requests, or a finished sign-in's token (never stored; a stored
        credential is never attached). No write."""
        body = await _body(request)
        _only(body, {"name", "url", "headers", "oauth_flow_id"})
        config = _server_config(body.get("name"), body.get("url"), False)
        flow = finished_flow(body, config)
        headers = _headers(body["headers"]) if "headers" in body else None
        record = await held_record(flow)
        probed = await probe_tools(config.name, config.url, headers, record)
        return JSONResponse(
            {"tools": [_probe_view(tool) for tool in probed], "definitions_sha256": definitions_sha256(probed)}
        )

    async def create_server(request: Request) -> JSONResponse:
        """POST /integrations/mcp/servers ``{name, url, background?, tools?, expected_definitions_sha256?,
        headers? | oauth_flow_id?}``: add or replace a server, its tools stored from a probe made here, with the
        request's permissions (a tool not named is ``deny``). Replacing enables the server and sets its call timeout to
        30 s, as the seed does. ``headers``, or the tokens of the finished sign-in ``oauth_flow_id`` (bound to this
        name and URL, and consumed here), become its credential (409 without a key on the host). Without either, the
        probe uses the stored credential opened as bound to the request's URL, and a replace at another URL removes it
        (``credentials: "removed"``)."""
        body = await _body(request)
        _only(body, {"name", "url", "background", "tools", "expected_definitions_sha256", "headers", "oauth_flow_id"})
        config = _server_config(body.get("name"), body.get("url"), bool(_flag(body, "background")))
        wanted = _permissions(body.get("tools", {}))
        flow = finished_flow(body, config)
        headers = _headers(body["headers"]) if "headers" in body else None
        if (headers is not None or flow is not None) and not manager.secrets_available():
            raise _Refused(409, NO_KEY_TEXT)
        record = await held_record(flow)  # this copy is probed with and stored, whatever the flow becomes meanwhile
        sent = headers
        stored_secret = await secret_of(config.name)
        if sent is None and flow is None and stored_secret is not None:
            # Opened as bound to the request's URL: at another URL it does not open, and nothing is sent.
            sent, _problem = await manager.credential_headers(config.name, config.url, stored_secret)
        probed = await probe_tools(config.name, config.url, sent, record)
        digest_stored = expect(body, probed)
        unknown = sorted(set(wanted) - {tool.tool for tool in probed})
        if unknown:
            raise _Refused(400, f"the server does not list these tools: {', '.join(unknown)}")
        rows = _rows_from_probe(config.name, probed, wanted)
        sealed = sealed_for(config.name, config.url, headers) if headers is not None else None
        if flow is not None:
            # A request that names a sign-in never creates the server without its tokens.
            sealed = manager.seal_oauth_record(config.name, config.url, record) if record is not None else None
            if sealed is None:
                raise _Refused(
                    409, NO_KEY_TEXT if record is not None else "that sign-in holds no tokens: sign in again"
                )
        async with changes:
            old = await stored(config.name)
            old_secret = await secret_of(config.name)
            created, secret_deleted = await registry.replace_server(config, rows, secret=sealed)
            if flow is not None:
                manager.flows.consume(flow)  # a finished sign-in is stored once
            if sealed is not None:
                credentials: str | None = "replaced" if old_secret is not None else "set"
            else:
                # What the registry did with the URL change: a row that no longer validates is not read by stored().
                credentials = "removed" if secret_deleted else None
            old_permissions = {tool.tool: tool.permission for tool in old[1]} if old else {}
            # A stored tool the server no longer lists is set to deny (its row is kept).
            new_permissions = {**{tool: "deny" for tool in old_permissions}, **{r.tool: r.permission for r in rows}}
            action = "create" if created else "replace"
            data = _change(
                action,
                config.name,
                old[0] if old else None,
                config,
                old_permissions,
                new_permissions,
                credentials=credentials,
                header_count=_count(credentials, headers),
            )
            await audit(data)
            await apply()
            server = await view(config.name)
        return JSONResponse(
            {"created": created, "definitions_sha256": digest_stored, "credentials": credentials, "server": server},
            status_code=201 if created else 200,
        )

    async def update_server(request: Request) -> JSONResponse:
        """PATCH /integrations/mcp/servers/{name} ``{enabled?, background?, call_timeout_seconds?, tools?,
        headers?}``: change the server's switches, its call timeout, the permissions of tools it already has stored,
        and its credential (``headers``: replace it, 409 without a key on the host; ``headers: null``: remove it).
        The URL cannot be changed here."""
        name = request.path_params["name"]
        body = await _body(request)
        _only(body, {"enabled", "background", "call_timeout_seconds", "tools", "headers"})
        if not body:
            raise _Refused(400, "nothing to change")
        enabled, background = _flag(body, "enabled"), _flag(body, "background")
        timeout = body.get("call_timeout_seconds")
        if "call_timeout_seconds" in body and (type(timeout) is not int or not 1 <= timeout <= 600):
            raise _Refused(400, "call_timeout_seconds must be a whole number from 1 to 600")
        wanted = _permissions(body["tools"]) if "tools" in body else {}
        headers = _headers(body["headers"]) if body.get("headers") is not None else None
        remove = "headers" in body and body["headers"] is None
        if headers is not None and not manager.secrets_available():
            raise _Refused(409, NO_KEY_TEXT)
        async with changes:
            current = await stored(name)
            if current is None:
                raise _Refused(404, "no such server")
            old, tools = current
            old_permissions = {tool.tool: tool.permission for tool in tools}
            unknown = sorted(set(wanted) - set(old_permissions))
            if unknown:
                raise _Refused(400, f"the server has no stored tools named: {', '.join(unknown)}")
            old_secret = await secret_of(name)
            sealed = sealed_for(name, old.url, headers) if headers is not None else None
            await registry.update_server(
                name,
                enabled=enabled,
                background=background,
                call_timeout_seconds=timeout,
                permissions=wanted,  # type: ignore[arg-type]
                secret=sealed,
                remove_secret=remove,
            )
            if headers is not None:
                credentials: str | None = "replaced" if old_secret is not None else "set"
            else:
                credentials = "removed" if remove and old_secret is not None else None
            fields = {"enabled": enabled, "background": background, "call_timeout_seconds": timeout}
            new = old.model_copy(update={key: value for key, value in fields.items() if value is not None})
            data = _change(
                "update",
                name,
                old,
                new,
                old_permissions,
                {**old_permissions, **wanted},
                credentials=credentials,
                header_count=_count(credentials, headers),
            )
            await audit(data)
            await apply()
            server = await view(name)
        return JSONResponse({"credentials": credentials, "server": server})

    async def refresh_server(request: Request) -> JSONResponse:
        """POST /integrations/mcp/servers/{name}/refresh ``{expected_definitions_sha256?}`` (the body may be empty):
        list the server's tools again. A new tool is stored as ``deny``; a tool it no longer lists is set to ``deny``;
        a tool whose description or schema changed keeps its permission and is reported in ``changed``."""
        name = request.path_params["name"]
        body = await _body(request) if await request.body() else {}
        _only(body, {"expected_definitions_sha256"})
        before = await stored(name)
        if before is None:
            raise _Refused(404, "no such server")
        stored_secret = await secret_of(name)
        headers: dict[str, str] | None = None
        if stored_secret is not None:  # the stored URL and the stored credential, nothing from the request
            headers, problem = await manager.credential_headers(name, before[0].url, stored_secret)
            if problem is not None:
                raise _Refused(409, f"the stored credentials cannot be used here: {problem}")
        probed = await probe_tools(name, before[0].url, headers)
        digest_stored = expect(body, probed)
        async with changes:
            current = await stored(name)
            if current is None:
                raise _Refused(404, "no such server")
            config, tools = current
            if config.url != before[0].url:
                raise _Refused(409, "the server's URL changed during the refresh: try again")
            by_tool = {tool.tool: tool for tool in tools}
            listed = {tool.tool for tool in probed}
            added = sorted(listed - set(by_tool))
            removed = sorted(set(by_tool) - listed)
            redefined = sorted(
                tool.tool
                for tool in probed
                if tool.tool in by_tool
                and (
                    by_tool[tool.tool].description != tool.description
                    or by_tool[tool.tool].input_schema != tool.input_schema
                )
            )
            kept = {tool: by_tool[tool].permission for tool in listed if tool in by_tool}
            rows = _rows_from_probe(name, probed, kept)
            await registry.upsert_tools(name, rows)
            old_permissions = {tool: row.permission for tool, row in by_tool.items()}
            new_permissions = {**old_permissions, **{tool: "deny" for tool in removed}}
            new_permissions.update({row.tool: row.permission for row in rows})
            data = _change("refresh", name, config, config, old_permissions, new_permissions)
            if (added or removed or redefined) and "tools" not in data["changed"]:
                data["changed"].append("tools")
            for key, names in (("added", added), ("removed", removed), ("redefined", redefined)):
                data[key], data["more"][key] = _capped([_event_tool(tool) for tool in names])
            await audit(data)
            await apply()
            server = await view(name)
        return JSONResponse(
            {
                "added": added,
                "removed": removed,
                "changed": redefined,
                "definitions_sha256": digest_stored,
                "server": server,
            }
        )

    async def delete_server(request: Request) -> JSONResponse:
        """DELETE /integrations/mcp/servers/{name}: remove the server, its tools and its stored credential."""
        name = request.path_params["name"]
        async with changes:
            current = await stored(name)  # None for a row that no longer validates: it is still deleted
            old_secret = await secret_of(name)
            if not await registry.delete_server(name):
                raise _Refused(404, "no such server")
            old, tools = current if current else (None, [])
            credentials = "removed" if old_secret is not None else None
            data = _change(
                "delete",
                name,
                old,
                None,
                {tool.tool: tool.permission for tool in tools},
                {},
                credentials=credentials,
                header_count=_count(credentials, None),
            )
            await audit(data)
            await apply()
        return JSONResponse({"deleted": name})

    # --- OAuth sign-in (PR 2d) ---------------------------------------------------------------------------------------

    def oauth_problem(exc: OAuthProblem) -> _Refused:
        return _Refused(exc.status, str(exc))

    async def oauth_start(request: Request) -> JSONResponse:
        """POST /integrations/mcp/oauth/start ``{name, url, client_id?, client_secret?, scope?}``: discover the
        server's provider, register a client (or take the owner's ``client_id``, or reuse the stored client while the
        issuer is unchanged) and start a sign-in: ``{flow_id, authorization_url, authorization_host, token_host,
        registration, scope, expires_in}``. For a stored server the URL must be its stored one. A sign-in pending for
        the same name and URL is replaced (its client reused while the issuer is unchanged). Nothing is stored."""
        body = await _body(request)
        _only(body, {"name", "url", "client_id", "client_secret", "scope"})
        redirect = redirect_address()
        if redirect is None:
            raise _Refused(409, OAUTH_UNAVAILABLE_TEXT)
        if not manager.secrets_available():
            raise _Refused(409, NO_KEY_TEXT)
        config = _server_config(body.get("name"), body.get("url"), False)
        client_id, client_secret = body.get("client_id") or None, body.get("client_secret") or None
        if client_secret is not None and client_id is None:
            raise _Refused(400, "a client secret goes with a client id")
        current = await stored(config.name)
        # "Start again": the pending sign-in is replaced and its client reused. Held now: a flow that ends meanwhile
        # drops its client's secret.
        pending = manager.flows.pending_for(config.name, config.url)
        pending_client, pending_issuer = (pending.client, pending.discovery.issuer) if pending else (None, None)
        reuse = None
        if current is not None:
            if current[0].url != config.url:
                raise _Refused(409, "a server of that name is stored at another address: sign in at its stored address")
            secret = await secret_of(config.name)
            if secret is not None and secret.kind == "oauth":
                reuse = manager.stored_oauth(config.name, config.url, secret)
        try:
            scope = owner_scope(body.get("scope"))
            http, allow = manager.oauth_http, manager.allow_private_hosts
            discovery = await discover(config.url, http, allow, scope)
            # The owner's client id again with no secret ("Start again" clears the typed secret): the pending or
            # stored client of that id keeps its secret, so a confidential client does not turn public.
            known = [(pending_client, pending_issuer)]
            if reuse is not None:
                known.append(
                    (Client(reuse.client_id, reuse.client_secret, reuse.token_auth_method, "provided"), reuse.issuer)
                )
            same = next(
                (c for c, issuer in known if c is not None and c.client_id == client_id and issuer == discovery.issuer),
                None,
            )
            if client_id is not None and client_secret is None and same is not None:
                client = Client(same.client_id, same.client_secret, same.token_auth_method, "provided")
            elif client_id is not None:
                client = owner_client(discovery, client_id, client_secret)
            elif pending_client is not None and pending_issuer == discovery.issuer:
                client = Client(
                    pending_client.client_id,
                    pending_client.client_secret,
                    pending_client.token_auth_method,
                    "reused",
                )
            elif reuse is not None and reuse.issuer == discovery.issuer:
                client = Client(reuse.client_id, reuse.client_secret, reuse.token_auth_method, "reused")
            else:
                client = await register(discovery, redirect, http, allow)
            flow = manager.flows.start(config.name, config.url, discovery, client, redirect)
        except OAuthProblem as exc:
            raise oauth_problem(exc) from None
        logger.info("F112: an OAuth sign-in started for server %r (client %s)", config.name, client.registration)
        return JSONResponse(
            {
                "flow_id": flow.flow_id,
                "authorization_url": flow.authorization_url(),
                "authorization_host": host_of(discovery.authorization_endpoint),
                "token_host": host_of(discovery.token_endpoint),
                "registration": client.registration,
                "scope": discovery.scope,
                "expires_in": FLOW_SECONDS,
            }
        )

    async def store_sign_in(flow: Flow, record: Any) -> bool:
        """A finished sign-in's tokens for a server stored at the flow's URL: stored at once (a new credential row,
        replacing any stored one), audited and applied. False when no server has the flow's name (a new server: the
        create stores them). 409 when the server moved meanwhile. ``_Superseded`` when the sign-in is no longer the
        current pending one (a newer sign-in replaced it, or it expired, while its code was exchanged): checked under
        the lock every store holds, so a slower old exchange never overwrites a newer sign-in's credential."""
        async with changes:
            if manager.flows.status(flow) != "pending":
                raise _Superseded()
            current = await stored(flow.server)
            if current is None:
                return False
            config, tools = current
            if config.url != flow.url:
                raise _Refused(409, "the server's address changed during the sign-in: sign in again")
            sealed = manager.seal_oauth_record(flow.server, config.url, record)
            if sealed is None:
                raise _Refused(409, NO_KEY_TEXT)
            old_secret = await secret_of(flow.server)
            await registry.update_server(flow.server, secret=sealed)
            credentials = "replaced" if old_secret is not None else "set"
            permissions = {tool.tool: tool.permission for tool in tools}
            await audit(
                _change(
                    "sign_in",
                    flow.server,
                    config,
                    config,
                    permissions,
                    permissions,
                    credentials=credentials,
                    header_count=0,
                )
            )
            await apply()
        return True

    async def finish(flow: Flow, params: dict[str, Any]) -> bool:
        """Exchange the provider's answer for tokens and store or hold them; the flow is then complete, or failed with
        a fixed reason."""
        try:
            record = await exchange_code(flow, params, manager.oauth_http, manager.allow_private_hosts)
            stored_now = await store_sign_in(flow, record)
        except _Superseded:  # it stays as it is (failed: replaced, or expired), and nothing is stored
            logger.warning(
                "F112: the OAuth sign-in for server %r was replaced while it finished; nothing stored", flow.server
            )
            return False
        except (OAuthProblem, _Refused) as exc:
            manager.flows.fail(flow, str(exc))
            logger.warning("F112: the OAuth sign-in for server %r did not complete (%s)", flow.server, exc)
            return False
        except Exception as exc:  # the class only, never a traceback: its text can repeat what the provider was sent
            manager.flows.fail(flow, "the sign-in could not be completed")
            logger.warning("F112: the OAuth sign-in for server %r failed (%s)", flow.server, type(exc).__name__)
            return False
        manager.flows.complete(flow, None if stored_now else record)
        logger.info("F112: the OAuth sign-in for server %r completed", flow.server)
        return True

    def flow_view(flow: Flow) -> dict[str, Any]:
        """How a sign-in stands, never a token: ``status``, the reason it failed, and ``stored`` (its tokens were
        stored with the server at once; else a create stores them)."""
        status = manager.flows.status(flow)
        return {
            "status": status,
            "reason": flow.reason if status == "failed" else None,
            "stored": status == "complete" and flow.record is None,
        }

    async def oauth_callback(request: Request) -> Response:
        """GET /integrations/mcp/oauth/callback ``?code=&state=&iss=`` (or ``?error=``): the provider's answer, in the
        owner's browser. One of two fixed pages whatever the request holds; the state is single use."""
        params = {key: request.query_params.getlist(key)[0] for key in request.query_params.keys()}
        flow = manager.flows.take_by_state(params.get("state"))
        ok = flow is not None and await finish(flow, params)
        return Response(
            SIGNED_IN_PAGE if ok else NOT_SIGNED_IN_PAGE,
            status_code=200 if ok else 400,
            media_type="text/html; charset=utf-8",
            headers=_PAGE_HEADERS,
        )

    async def oauth_complete(request: Request) -> JSONResponse:
        """POST /integrations/mcp/oauth/complete ``{flow_id, callback_url}`` or ``{flow_id, code, state, iss?}``: the
        paste fallback, handled as the callback is (``iss`` is checked as there). ``{status, reason, stored}``; 400 when
        the sign-in failed."""
        body = await _body(request)
        _only(body, {"flow_id", "callback_url", "code", "state", "iss"})
        redirect = redirect_address()
        if redirect is None:
            raise _Refused(409, OAUTH_UNAVAILABLE_TEXT)
        if "callback_url" in body:
            if "code" in body or "state" in body or "iss" in body:
                raise _Refused(400, "give callback_url, or code and state (and iss)")
            try:
                params = callback_params(body["callback_url"], redirect)
            except OAuthProblem as exc:
                raise oauth_problem(exc) from None
        else:
            params = {key: body[key] for key in ("code", "state", "iss") if isinstance(body.get(key), str)}
        flow = manager.flows.take(body.get("flow_id"), params.get("state"))
        if flow is None:
            raise _Refused(
                409,
                "that sign-in is not waiting for an answer, or the answer belongs to another sign-in: sign in again",
            )
        if not await finish(flow, params):
            raise _Refused(400, flow.reason or "the sign-in did not complete")
        return JSONResponse(flow_view(flow))

    async def oauth_flow(request: Request) -> JSONResponse:
        """GET /integrations/mcp/oauth/flows/{flow_id}: ``{status: pending|complete|failed|expired, reason, stored}``
        (the screen asks every 2 s while the owner signs in). 404 for a sign-in that is unknown or long expired."""
        flow = manager.flows.get(request.path_params["flow_id"])
        if flow is None:
            raise _Refused(404, "no such sign-in: it may have expired")
        return JSONResponse(flow_view(flow))

    async def sign_out(request: Request) -> JSONResponse:
        """POST /integrations/mcp/servers/{name}/sign-out: delete the server's stored OAuth tokens. The provider's
        grant is not revoked (many providers have no endpoint for it)."""
        name = request.path_params["name"]
        async with changes:
            current = await stored(name)
            if current is None:
                raise _Refused(404, "no such server")
            secret = await secret_of(name)
            if secret is None or secret.kind != "oauth":
                raise _Refused(409, "this server is not signed in with OAuth")
            config, tools = current
            await registry.update_server(name, remove_secret=True)
            permissions = {tool.tool: tool.permission for tool in tools}
            await audit(
                _change(
                    "sign_out",
                    name,
                    config,
                    config,
                    permissions,
                    permissions,
                    credentials="removed",
                    header_count=0,
                )
            )
            await apply()
            server = await view(name)
        return JSONResponse({"credentials": "removed", "server": server})

    return [
        Route("/integrations/mcp/servers", endpoint(list_servers, mutating=False), methods=["GET"]),
        Route("/integrations/mcp/servers", endpoint(create_server, mutating=True), methods=["POST"]),
        Route("/integrations/mcp/probe", endpoint(probe, mutating=True), methods=["POST"]),
        Route("/integrations/mcp/servers/{name}/refresh", endpoint(refresh_server, mutating=True), methods=["POST"]),
        Route("/integrations/mcp/servers/{name}", endpoint(update_server, mutating=True), methods=["PATCH"]),
        Route("/integrations/mcp/servers/{name}", endpoint(delete_server, mutating=True), methods=["DELETE"]),
        Route("/integrations/mcp/servers/{name}/sign-out", endpoint(sign_out, mutating=True), methods=["POST"]),
        Route("/integrations/mcp/oauth/start", endpoint(oauth_start, mutating=True), methods=["POST"]),
        Route(CALLBACK_PATH, endpoint(oauth_callback, mutating=False), methods=["GET"]),
        Route("/integrations/mcp/oauth/complete", endpoint(oauth_complete, mutating=True), methods=["POST"]),
        Route("/integrations/mcp/oauth/flows/{flow_id}", endpoint(oauth_flow, mutating=False), methods=["GET"]),
    ]
