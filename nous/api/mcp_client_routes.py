"""F112 PR 2a: the owner's routes for the MCP client's servers, under ``/integrations/mcp``.

Mounted by ``rest.py`` only when ``NOUS_MCP_CLIENT_ENABLED`` is on. Approval is a request to these routes: a server
added with ``POST /integrations/mcp/servers`` or a permission set with ``PATCH`` takes effect at once, through the
client's ``reload``. There is no other approval step.

Authentication: the owner's proxy adds ``X-Nous-Proxy-Secret`` to the requests it has authenticated, and this process
holds only the secret's SHA-256 (``NOUS_MCP_CLIENT_UI_SECRET_SHA256``), compared in constant time. Without a valid
digest every route answers 503; without a matching header, 403. Every POST, PATCH and DELETE must be sent as
``application/json`` (415 otherwise), the CSRF control of the other mutating routes. The header is never logged.

What this protects against: a request from the LAN that did not come through the proxy. Not code running in the Nous
process: the agent's ``bash`` and ``run_python`` run here, so they can call these routes on loopback (every proxied
request, header included, reaches this process) or write the rows themselves. The host's two optional ceilings
(``NOUS_MCP_CLIENT_PINNED_SERVERS``, ``NOUS_MCP_CLIENT_BACKGROUND_SERVERS``) are the hard limit for an owner who
wants one.

A tool's description and schema are stored as the server gave them, from a probe this process makes itself (never
from the request), and shown as ``prepare_tool`` makes them, which is what the model is offered. Only ``allow`` and
``deny`` are accepted: ``ask`` is not offered yet.

Every mutating route emits one ``mcp_client_server_changed`` event: the action, the server's name, the names of the
fields that changed, the old and new ``enabled`` and ``background``, and the tools whose permission changed with the
old and new permission. Never a URL or a description: the agent's reader can read the events table.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from nous.api.commitment_routes import owner_secret_matches
from nous.events import Event
from nous.mcp_client.bridge import serialize_schema
from nous.mcp_client.config import ServerConfig, ToolConfig
from nous.mcp_client.manager import ProbedTool, ProbeFailed, ProbeRefused, ServerVerdict, ToolVerdict
from nous.mcp_client.registry import McpRegistry

logger = logging.getLogger(__name__)

PROXY_SECRET_HEADER = "X-Nous-Proxy-Secret"
CHANGE_EVENT = "mcp_client_server_changed"
_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}")
_EVENT_TOOL_CHARS = 80  # a tool's name is the server's text: cut in an event, as in a log line
_SERVER_FIELDS = ("url", "enabled", "background", "call_timeout_seconds")

Handler = Callable[[Request], Awaitable[JSONResponse]]


class _Refused(Exception):
    """A request the route answers with ``status`` and ``message`` (fixed text, or what the caller sent)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


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


def _server_config(name: Any, url: Any, background: bool) -> ServerConfig:
    """The server's name and URL, validated as a row holds them (``url_problem``). The error names the field and the
    rule, never the value."""
    if not isinstance(name, str) or not isinstance(url, str):
        raise _Refused(400, "name and url must be strings")
    try:
        return ServerConfig(name=name, url=url, background=background)
    except ValidationError as exc:
        raise _Refused(400, "; ".join(f"{error['loc'][0]}: {error['msg']}" for error in exc.errors())) from None


def _event_tool(tool: str) -> str:
    return tool[:_EVENT_TOOL_CHARS]


def _change(
    action: str,
    server: str,
    old: ServerConfig | None,
    new: ServerConfig | None,
    old_permissions: dict[str, str],
    new_permissions: dict[str, str],
) -> dict[str, Any]:
    """The ``mcp_client_server_changed`` event's data: names, switches and permissions only."""
    changed = [field for field in _SERVER_FIELDS if getattr(old, field, None) != getattr(new, field, None)]
    tools = [
        {"tool": _event_tool(tool), "old": old_permissions.get(tool), "new": new_permissions.get(tool)}
        for tool in sorted(set(old_permissions) | set(new_permissions))
        if old_permissions.get(tool) != new_permissions.get(tool)
    ]
    if tools:
        changed.append("tools")
    return {
        "action": action,
        "server": server,
        "changed": changed,
        "enabled": {"old": old.enabled if old else None, "new": new.enabled if new else None},
        "background": {"old": old.background if old else None, "new": new.background if new else None},
        "tools": tools,
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
    digest = _digest(settings.mcp_client_ui_secret_sha256)
    if not digest:
        logger.warning(
            "F112: NOUS_MCP_CLIENT_UI_SECRET_SHA256 is not set to a SHA-256 digest, so every /integrations/mcp route "
            "answers 503"
        )
    changes = asyncio.Lock()  # one change at a time: read the old row, write, emit, reload

    def endpoint(handler: Handler, *, mutating: bool) -> Handler:
        async def run(request: Request) -> JSONResponse:
            if not digest:
                return JSONResponse({"error": "the MCP client routes are not configured"}, status_code=503)
            if not owner_secret_matches(request.headers.get(PROXY_SECRET_HEADER), digest):
                logger.warning("F112: refused an /integrations/mcp request: no valid %s", PROXY_SECRET_HEADER)
                return JSONResponse({"error": "forbidden"}, status_code=403)
            if mutating and "application/json" not in (request.headers.get("content-type", "") or ""):
                return JSONResponse({"error": "Content-Type must be application/json"}, status_code=415)
            if not manager:
                return JSONResponse({"error": "the MCP client is not running"}, status_code=503)
            try:
                return await handler(request)
            except _Refused as exc:
                return JSONResponse({"error": str(exc)}, status_code=exc.status)
            except Exception as exc:  # a database error's text can carry the URL: the class only
                logger.warning("F112: %s failed (%s)", handler.__name__, type(exc).__name__)
                logger.debug("F112: %s error", handler.__name__, exc_info=True)
                return JSONResponse({"error": "the request could not be processed"}, status_code=500)

        return run

    async def stored(name: str) -> tuple[ServerConfig, list[ToolConfig]] | None:
        for config, tools in await registry.list_all():
            if config.name == name:
                return config, tools
        return None

    async def probe_tools(name: str, url: str) -> list[ProbedTool]:
        try:
            return await manager.probe(name, url)
        except ProbeRefused as exc:
            raise _Refused(400, f"the address guard refused the URL: {exc}") from None
        except ProbeFailed as exc:
            raise _Refused(502, f"could not list the server's tools: {exc}") from None

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

    def tool_view(verdict: ToolVerdict, server: str) -> dict[str, Any]:
        prepared = verdict.prepared
        return {
            "tool": verdict.tool,
            "name": prepared.name,
            "permission": verdict.permission,
            "registered": prepared.name is not None and manager.registered_server(prepared.name) == server,
            "skip_reason": verdict.reason,
            "blocked_by_ceiling": verdict.ceiling,
            "description": prepared.schema["description"] if prepared.schema else None,
            "schema": serialize_schema(prepared.schema) if prepared.schema else None,
        }

    def server_view(verdict: ServerVerdict, connected: dict[str, bool]) -> dict[str, Any]:
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
        }

    async def views() -> list[dict[str, Any]]:
        connected = {server["name"]: server["connected"] for server in manager.status()["servers"]}
        return [server_view(verdict, connected) for verdict in manager.plan(await registry.list_all())]

    async def view(name: str) -> dict[str, Any] | None:
        return next((server for server in await views() if server["name"] == name), None)

    async def list_servers(request: Request) -> JSONResponse:
        """GET /integrations/mcp/servers: every stored server with its tools, what registers and why not."""
        return JSONResponse({"ceilings": manager.ceilings(), "servers": await views()})

    async def probe(request: Request) -> JSONResponse:
        """POST /integrations/mcp/probe ``{name, url}``: the server's tools as they would be registered. No write."""
        body = await _body(request)
        _only(body, {"name", "url"})
        config = _server_config(body.get("name"), body.get("url"), False)
        return JSONResponse({"tools": [_probe_view(tool) for tool in await probe_tools(config.name, config.url)]})

    async def create_server(request: Request) -> JSONResponse:
        """POST /integrations/mcp/servers ``{name, url, background?, tools?}``: add or replace a server, its tools
        stored from a probe made here, with the request's permissions (a tool not named is ``deny``). Replacing
        enables the server and sets its call timeout to 30 s, as the seed does."""
        body = await _body(request)
        _only(body, {"name", "url", "background", "tools"})
        config = _server_config(body.get("name"), body.get("url"), bool(_flag(body, "background")))
        wanted = _permissions(body.get("tools", {}))
        probed = await probe_tools(config.name, config.url)
        unknown = sorted(set(wanted) - {tool.tool for tool in probed})
        if unknown:
            raise _Refused(400, f"the server does not list these tools: {', '.join(unknown)}")
        rows = _rows_from_probe(config.name, probed, wanted)
        async with changes:
            old = await stored(config.name)
            created = await registry.replace_server(config, rows)
            old_permissions = {tool.tool: tool.permission for tool in old[1]} if old else {}
            # A stored tool the server no longer lists is set to deny (its row is kept).
            new_permissions = {**{tool: "deny" for tool in old_permissions}, **{r.tool: r.permission for r in rows}}
            action = "create" if created else "replace"
            await audit(_change(action, config.name, old[0] if old else None, config, old_permissions, new_permissions))
            await apply()
            server = await view(config.name)
        return JSONResponse({"created": created, "server": server}, status_code=201 if created else 200)

    async def update_server(request: Request) -> JSONResponse:
        """PATCH /integrations/mcp/servers/{name} ``{enabled?, background?, call_timeout_seconds?, tools?}``: change
        the server's switches, its call timeout and the permissions of tools it already has stored."""
        name = request.path_params["name"]
        body = await _body(request)
        _only(body, {"enabled", "background", "call_timeout_seconds", "tools"})
        if not body:
            raise _Refused(400, "nothing to change")
        enabled, background = _flag(body, "enabled"), _flag(body, "background")
        timeout = body.get("call_timeout_seconds")
        if "call_timeout_seconds" in body and (type(timeout) is not int or not 1 <= timeout <= 600):
            raise _Refused(400, "call_timeout_seconds must be a whole number from 1 to 600")
        wanted = _permissions(body["tools"]) if "tools" in body else {}
        async with changes:
            current = await stored(name)
            if current is None:
                raise _Refused(404, "no such server")
            old, tools = current
            old_permissions = {tool.tool: tool.permission for tool in tools}
            unknown = sorted(set(wanted) - set(old_permissions))
            if unknown:
                raise _Refused(400, f"the server has no stored tools named: {', '.join(unknown)}")
            await registry.update_server(
                name,
                enabled=enabled,
                background=background,
                call_timeout_seconds=timeout,
                permissions=wanted,  # type: ignore[arg-type]
            )
            fields = {"enabled": enabled, "background": background, "call_timeout_seconds": timeout}
            new = old.model_copy(update={key: value for key, value in fields.items() if value is not None})
            await audit(_change("update", name, old, new, old_permissions, {**old_permissions, **wanted}))
            await apply()
            server = await view(name)
        return JSONResponse({"server": server})

    async def refresh_server(request: Request) -> JSONResponse:
        """POST /integrations/mcp/servers/{name}/refresh: list the server's tools again. A new tool is stored as
        ``deny``; a tool it no longer lists is set to ``deny``; a tool whose description or schema changed keeps its
        permission and is reported in ``changed``."""
        name = request.path_params["name"]
        before = await stored(name)
        if before is None:
            raise _Refused(404, "no such server")
        probed = await probe_tools(name, before[0].url)
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
            data.update(
                added=[_event_tool(t) for t in added],
                removed=[_event_tool(t) for t in removed],
                redefined=[_event_tool(t) for t in redefined],
            )
            await audit(data)
            await apply()
            server = await view(name)
        return JSONResponse({"added": added, "removed": removed, "changed": redefined, "server": server})

    async def delete_server(request: Request) -> JSONResponse:
        """DELETE /integrations/mcp/servers/{name}: remove the server and its tools."""
        name = request.path_params["name"]
        async with changes:
            current = await stored(name)  # None for a row that no longer validates: it is still deleted
            if not await registry.delete_server(name):
                raise _Refused(404, "no such server")
            old, tools = current if current else (None, [])
            await audit(_change("delete", name, old, None, {tool.tool: tool.permission for tool in tools}, {}))
            await apply()
        return JSONResponse({"deleted": name})

    return [
        Route("/integrations/mcp/servers", endpoint(list_servers, mutating=False), methods=["GET"]),
        Route("/integrations/mcp/servers", endpoint(create_server, mutating=True), methods=["POST"]),
        Route("/integrations/mcp/probe", endpoint(probe, mutating=True), methods=["POST"]),
        Route("/integrations/mcp/servers/{name}/refresh", endpoint(refresh_server, mutating=True), methods=["POST"]),
        Route("/integrations/mcp/servers/{name}", endpoint(update_server, mutating=True), methods=["PATCH"]),
        Route("/integrations/mcp/servers/{name}", endpoint(delete_server, mutating=True), methods=["DELETE"]),
    ]
