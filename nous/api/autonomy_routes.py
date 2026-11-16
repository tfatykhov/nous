"""F115 autonomy settings over REST, for the dashboard's "Limits and tools" screen.

Limits: ``GET /autonomy/limits`` (each limit the owner may change: its words, range, value now, the host's value, where
the value comes from, the host's ceiling, when a change applies and its last change), ``PUT /autonomy/limits/{key}``
(body ``{"value": n}``) and ``DELETE /autonomy/limits/{key}`` (back to the host's value). ``GET /autonomy/log``: the
newest 20 changes. Tools: ``GET /autonomy/tools`` (each tool: what it does, its classes, its setting for background
work and where it comes from, whether the host locked it, the kinds of background work the code does not offer it to
and why, and its background calls in the last 30 days when the tool-call log is on), ``PUT /autonomy/tools/{tool}``
(body ``{"setting": "allowed" | "denied"}``) and ``DELETE /autonomy/tools/{tool}`` (back to the default).

Every route requires the owner's or the proxy's secret (``nous/api/owner_guard.py``), checked before anything else.
The actor of a change is the proof the guard accepted (``owner`` or ``proxy``); a body never names one (an unknown
field is a 400). A refusal of the store is answered with its own sentence: 400 for a value or word that is not
allowed, 404 for an unknown limit, 409 for a host ceiling, a host lock or the cross rule.

With ``NOUS_AUTONOMY_SETTINGS_ENABLED`` off there is no store: the reads answer 200 ``{"enabled": false}`` and the
changes 503. The tool routes answer the same way while ``NOUS_TOOL_GUARD_ENABLED`` is off. Neither reads a table.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import func, select
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from nous.api.execution_context import FOREGROUND_KINDS
from nous.api.owner_guard import OwnerGuard
from nous.api.tool_classes import MCP_TOOL_PREFIX, tool_class
from nous.autonomy.offered import PLACES, why_not_offered
from nous.autonomy.registry import LIMITS, by_key
from nous.autonomy.store import (
    BACKGROUND,
    LOG_KINDS,
    LOG_MAX,
    LOG_SHOWN,
    AutonomyRefused,
    AutonomyStore,
    Change,
    tool_key,
)
from nous.autonomy.tool_guard import HARNESS_TOOLS, LOCK, OWNER, also_held, places
from nous.storage.models import ToolCallLog

logger = logging.getLogger(__name__)

OFF = {"error": "Autonomy settings are off on this server.", "enabled": False}
GUARD_OFF = {"error": "The tool guard is off on this server.", "enabled": False}
FAILED = "The request could not be processed."
QUERY_NUMBER_DIGITS = 4
ECHO_CHARS = 40
SUMMARY_CHARS = 160
CALL_DAYS = 30
TOOL_LOG_PREFIX = tool_key("", BACKGROUND)


class _Body(BaseModel):
    """A request body: an unknown field is refused."""

    model_config = ConfigDict(extra="forbid")


class LimitChange(_Body):
    """``PUT /autonomy/limits/{key}``: the new value. Its type and range are the store's to judge (the sentence
    names the range)."""

    value: Any


class ToolScope(_Body):
    """``DELETE /autonomy/tools/{tool}``: optionally the scope, which must be ``background``. Goal sessions and a
    destructive guard are not available yet: the store answers so, with the same sentence as for ``PUT``."""

    scope: Any = None
    destructive_guard: Any = None


class ToolChange(ToolScope):
    """``PUT /autonomy/tools/{tool}``: ``allowed`` or ``denied``."""

    setting: Any


def _scope(body: ToolScope) -> str:
    """The scope a body names: ``background`` when it names none, ``""`` for a value that is not text."""
    if body.scope is None:
        return BACKGROUND
    return body.scope if isinstance(body.scope, str) else ""


class NoBody(_Body):
    """A body that carries nothing: empty, or ``{}``."""


class _BadRequest(Exception):
    """A body the route refuses (400); the text names the field and the rule, never the value."""


async def _parse(request: Request, model: type[_Body]) -> Any:
    raw = await request.body()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except (ValueError, RecursionError):
        raise _BadRequest("The body must be JSON.") from None
    if not isinstance(data, dict):
        raise _BadRequest("The body must be a JSON object.")
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        fields = sorted({str(error["loc"][0])[:ECHO_CHARS] for error in exc.errors() if error["loc"]})
        raise _BadRequest(f"Not accepted: {', '.join(fields) or 'body'}.") from None


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def change_row(change: Change) -> dict[str, Any]:
    """A change as the screen lists it: a limit's key, or the tool (``tool``) for a tool's setting."""
    tool = change.key[len(TOOL_LOG_PREFIX) :] if change.key.startswith(TOOL_LOG_PREFIX) else None
    limit = by_key(change.key)
    return {
        "key": change.key,
        "label": limit.label if limit is not None else None,
        "tool": tool,
        "old": change.old,
        "new": change.new,
        "actor": change.actor,
        "at": _iso(change.at),
    }


def limit_row(store: AutonomyStore, key: str, last: Change | None) -> dict[str, Any]:
    """One limit as ``GET /autonomy/limits`` lists it."""
    limit = by_key(key)
    assert limit is not None
    return {
        "key": key,
        "label": limit.label,
        "unit": limit.unit,
        "low": limit.low,
        "high": limit.high,
        "value": store.effective()[key],
        "host_value": store.host[key],
        "baseline": store.baseline[key],
        "source": store.source(key),
        "ceiling": store.ceilings.get(key),
        "description": limit.description,
        "applies": limit.applies,
        "last_change": change_row(last) if last is not None else None,
    }


_SIDE_EFFECT_WORDS = {
    "none": "Reads only",
    "write": "Changes things in Nous",
    "external": "Reaches outside Nous",
    "irreversible": "Cannot be undone",
}


def tool_group(name: str) -> str:
    """The screen's group of a tool: ``other_servers`` (an MCP server's), ``starting_work``, ``reaching_outside``,
    ``writing`` or ``reading``."""
    if name.startswith(MCP_TOOL_PREFIX):
        return "other_servers"
    cls = tool_class(name)
    if cls is None:
        return "writing"
    if cls.spawns:
        return "starting_work"
    if cls.side_effect in ("external", "irreversible"):
        return "reaching_outside"
    return "writing" if cls.side_effect == "write" else "reading"


def tool_classes_words(name: str) -> list[str]:
    """A tool's classes in words."""
    cls = tool_class(name)
    if cls is None:
        return ["Not classified"]
    words = [_SIDE_EFFECT_WORDS[cls.side_effect]]
    if cls.spawns:
        words.append("Starts other work")
    if cls.ingress == "outside":
        words.append("Brings in outside text")
    return words


def summary(description: str) -> str:
    """The first line of a tool's description, cut to ``SUMMARY_CHARS``."""
    line = (description or "").strip().splitlines()[0] if (description or "").strip() else ""
    return line if len(line) <= SUMMARY_CHARS else line[: SUMMARY_CHARS - 1].rstrip() + "…"


def registered_tools(dispatcher: Any) -> dict[str, str]:
    """The tools a background turn can be offered (every registered tool but the initiation ones and the harness's own
    per-turn tools, which no setting holds), by name, with each one's description."""
    return {
        tool["name"]: tool.get("description", "")
        for tool in dispatcher.available_tools("task")
        if tool["name"] not in HARNESS_TOOLS
    }


def build_autonomy_routes(
    *, database: Any, settings: Any, store: Any, dispatcher: Any, guard: OwnerGuard
) -> list[Route]:
    """The autonomy routes, every one behind ``guard``. ``store`` is the app's ``AutonomyStore`` (main.py passes
    its lazy proxy, falsy until the lifespan built one and always with the flag off) and ``dispatcher`` the tool
    dispatcher (the same), both read at each request."""
    agent_id = settings.agent_id

    def store_now() -> AutonomyStore | None:
        return store if settings.autonomy_settings_enabled and store else None

    def tools_on() -> bool:
        return bool(settings.tool_guard_enabled and store_now() is not None and dispatcher)

    def actor(request: Request) -> str:
        proof = guard.proof(request)
        assert proof is not None  # the guard let the call in on one
        return proof

    def not_loaded(current: AutonomyStore) -> dict[str, Any]:
        """What a read answers while the saved settings could not be read: the sentence, and no row."""
        return {"enabled": True, "loaded": False, "message": current.not_loaded_message()}

    def failed(what: str, exc: Exception) -> JSONResponse:
        # The class only: an error's text can repeat what it was given.
        logger.warning("F115: %s failed (%s)", what, type(exc).__name__)
        return _error(500, FAILED)

    async def limits(request: Request) -> Response:
        """GET /autonomy/limits"""
        current = store_now()
        if current is None:
            return JSONResponse({"enabled": False})
        if not current.loaded:
            return JSONResponse(not_loaded(current))
        try:
            last = await current.last_changes(limit.key for limit in LIMITS)
        except Exception as exc:
            return failed("reading the limits", exc)
        return JSONResponse(
            {
                "enabled": True,
                "loaded": True,
                "tool_guard": tools_on(),
                "ceilings_set": bool(current.ceilings),
                "limits": [limit_row(current, limit.key, last.get(limit.key)) for limit in LIMITS],
            }
        )

    async def changed_limit(current: AutonomyStore, key: str, extra: dict[str, Any]) -> JSONResponse:
        try:
            last = (await current.last_changes([key])).get(key)
        except Exception as exc:
            logger.warning("F115: reading a change back failed (%s)", type(exc).__name__)
            last = None
        return JSONResponse({"enabled": True, **extra, "limit": limit_row(current, key, last)})

    async def put_limit(request: Request) -> Response:
        """PUT /autonomy/limits/{key}"""
        current = store_now()
        if current is None:
            return JSONResponse(OFF, status_code=503)
        try:
            body: LimitChange = await _parse(request, LimitChange)
        except _BadRequest as exc:
            return _error(400, str(exc))
        key = request.path_params["key"]
        try:
            changed = await current.set_limit(key, body.value, actor(request))
        except AutonomyRefused as exc:
            return _error(exc.status, str(exc))
        except Exception as exc:
            return failed("changing a limit", exc)
        return await changed_limit(current, key, {"changed": changed})

    async def delete_limit(request: Request) -> Response:
        """DELETE /autonomy/limits/{key}"""
        current = store_now()
        if current is None:
            return JSONResponse(OFF, status_code=503)
        try:
            await _parse(request, NoBody)
        except _BadRequest as exc:
            return _error(400, str(exc))
        key = request.path_params["key"]
        try:
            changed = await current.clear_limit(key, actor(request))
        except AutonomyRefused as exc:
            return _error(exc.status, str(exc))
        except Exception as exc:
            return failed("returning a limit to the host's value", exc)
        return await changed_limit(current, key, {"changed": changed})

    async def log(request: Request) -> Response:
        """GET /autonomy/log?kind=all&limit=20"""
        current = store_now()
        if current is None:
            return JSONResponse({"enabled": False})
        kind = request.query_params.get("kind", "all")
        if kind not in LOG_KINDS:
            return _error(400, "The kind must be all, limits or tools.")
        raw = request.query_params.get("limit", str(LOG_SHOWN))
        limit = int(raw) if raw.isascii() and raw.isdigit() and len(raw) <= QUERY_NUMBER_DIGITS else 0
        if not 1 <= limit <= LOG_MAX:
            return _error(400, f"The limit must be a whole number from 1 to {LOG_MAX}.")
        try:
            changes = await current.changes(limit, kind)
        except Exception as exc:
            return failed("reading the changes", exc)
        return JSONResponse({"enabled": True, "changes": [change_row(change) for change in changes]})

    async def background_calls() -> dict[str, tuple[int, datetime]] | None:
        """Each tool's background calls in the last ``CALL_DAYS`` days and the newest, from the tool-call log; None
        when the log is off."""
        if not settings.tool_call_log_enabled:
            return None
        async with database.session() as session:
            now = (await session.execute(select(func.now()))).scalar_one()
            rows = await session.execute(
                select(ToolCallLog.tool_name, func.count(), func.max(ToolCallLog.started_at))
                .where(
                    ToolCallLog.agent_id == agent_id,
                    ToolCallLog.started_at >= now - timedelta(days=CALL_DAYS),
                    ToolCallLog.context_kind.not_in(sorted(FOREGROUND_KINDS)),
                )
                .group_by(ToolCallLog.tool_name)
            )
            return {name: (int(count), newest) for name, count, newest in rows.all()}

    def tool_row(current: AutonomyStore, name: str, description: str | None, calls: Any) -> dict[str, Any]:
        locked = name in current.locked
        setting = current.tools.get(name)
        lineage_shell = bool(settings.f099_lineage_shell)
        goal_grants = bool(settings.goal_tool_grants_enabled)
        not_offered = [
            {"place": place.key, "why": why}
            for place in PLACES
            if (why := why_not_offered(place, name, lineage_shell=lineage_shell, goal_grants=goal_grants)) is not None
        ]
        counted = calls.get(name) if calls is not None else None
        return {
            "name": name,
            "summary": summary(description) if description is not None else "Not registered now.",
            "registered": description is not None,
            "group": tool_group(name),
            "classes": tool_classes_words(name),
            "setting": "denied" if locked else (setting or "allowed"),
            "source": "server" if locked else ("owner" if setting is not None else "default"),
            "locked": locked,
            "not_offered": not_offered,
            "calls_30d": (counted[0] if counted else 0) if calls is not None else None,
            "last_call": _iso(counted[1]) if counted else None,
        }

    def listed_tools() -> dict[str, str]:
        return registered_tools(dispatcher)

    async def tools(request: Request) -> Response:
        """GET /autonomy/tools"""
        current = store_now()
        if current is None or not tools_on():
            return JSONResponse({"enabled": False})
        if not current.loaded:
            return JSONResponse(not_loaded(current))
        try:
            calls = await background_calls()
        except Exception as exc:
            return failed("reading the tool calls", exc)
        listed = listed_tools()
        names = sorted(set(listed) | set(current.tools) | set(current.locked))
        return JSONResponse(
            {
                "enabled": True,
                "loaded": True,
                "recorded": calls is not None,
                # Where the owner's Not allowed applies and where it does not, and where a host lock holds beyond
                # it: each from the guard's one table (tool_guard.KINDS).
                "held_in": places(OWNER),
                "not_held": places(OWNER, held=False),
                "locked_also_in": also_held(LOCK),
                "locks_set": bool(current.locked),
                "places": [{"key": place.key, "words": place.words} for place in PLACES],
                "tools": [tool_row(current, name, listed.get(name), calls) for name in names],
            }
        )

    async def changed_tool(current: AutonomyStore, name: str, changed: bool) -> JSONResponse:
        listed = listed_tools()
        return JSONResponse(
            {"enabled": True, "changed": changed, "tool": tool_row(current, name, listed.get(name), None)}
        )

    async def put_tool(request: Request) -> Response:
        """PUT /autonomy/tools/{tool}"""
        current = store_now()
        if current is None or not tools_on():
            return JSONResponse(GUARD_OFF if current is not None else OFF, status_code=503)
        try:
            body: ToolChange = await _parse(request, ToolChange)
        except _BadRequest as exc:
            return _error(400, str(exc))
        name = request.path_params["tool"]
        if name not in listed_tools():
            return _error(400, f"There is no tool named {name[:ECHO_CHARS]!r}.")
        setting = body.setting if isinstance(body.setting, str) else ""
        try:
            changed = await current.set_tool(
                name, setting, actor(request), scope=_scope(body), destructive_guard=body.destructive_guard
            )
        except AutonomyRefused as exc:
            return _error(exc.status, str(exc))
        except Exception as exc:
            return failed("changing a tool's setting", exc)
        return await changed_tool(current, name, changed)

    async def delete_tool(request: Request) -> Response:
        """DELETE /autonomy/tools/{tool}"""
        current = store_now()
        if current is None or not tools_on():
            return JSONResponse(GUARD_OFF if current is not None else OFF, status_code=503)
        try:
            body: ToolScope = await _parse(request, ToolScope)
        except _BadRequest as exc:
            return _error(400, str(exc))
        name = request.path_params["tool"]
        try:
            changed = await current.clear_tool(
                name, actor(request), scope=_scope(body), destructive_guard=body.destructive_guard
            )
        except AutonomyRefused as exc:
            return _error(exc.status, str(exc))
        except Exception as exc:
            return failed("returning a tool to its default", exc)
        return await changed_tool(current, name, changed)

    return [
        guard.route("/autonomy/limits", limits, methods=["GET"]),
        guard.route("/autonomy/limits/{key}", put_limit, methods=["PUT"]),
        guard.route("/autonomy/limits/{key}", delete_limit, methods=["DELETE"]),
        guard.route("/autonomy/log", log, methods=["GET"]),
        guard.route("/autonomy/tools", tools, methods=["GET"]),
        guard.route("/autonomy/tools/{tool}", put_tool, methods=["PUT"]),
        guard.route("/autonomy/tools/{tool}", delete_tool, methods=["DELETE"]),
    ]
