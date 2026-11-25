"""F120 tool loading, watch only, over REST: the card "Tool loading" on the Limits and tools screen.

``GET /dashboard/tool-loading?days=7``: the owner's setting (mode, kinds of call, core set, pinned tools and why, last
change), whether model calls and tool calls are recorded, per kind of call over the last ``days`` days (1, 7 or 30)
the successful calls recorded in Watch only (``nous_system.tool_loading_watch`` joined to each call's own
``model_call_log`` row) and what they would have saved weighed by each call's cache state, how many turns then used a
tool that would have been left out, and every registered tool with its calls and sessions in the last 30 days (every
kind of call), its estimated tokens and whether it is kept in full or would be left out. A name in the stored core set
that is not registered now is listed as such and does nothing. A failed read of the numbers or of the tools' use
answers 200 with the setting, that part null and ``numbers_failed`` or ``usage_failed`` true.

``PUT /dashboard/tool-loading``: ``{mode?, kinds?, core?}``, each field optional and merged into the stored setting:
``mode`` ``off`` or ``watch``, ``kinds`` an object of kind to true or false, ``core`` a list of tool names or null for
the default. A name already in the core set is kept whether registered or not; a name being added must be registered
now. A pinned tool is always kept in full, whatever the list says. The setting is stored in ``nous_system.config``
under ``tool_loading:<agent_id>`` with when and by whom (the proof the owner guard accepted), then set on the running
process, so the next model call reads it. A change that changes nothing is not written, unless the stored value could
not be read: then it is replaced. Nothing else is written.

Both routes require the owner's or the proxy's secret (``nous/api/owner_guard.py``), checked before anything else. A
body is at most ``BODY_MAX_BYTES``, a JSON object with strictly typed fields and no other field; a refusal names the
field and the rule. Every failure is logged by its class name alone. With ``NOUS_TOOL_LOADING_ENABLED`` off the read
answers 200 ``{"enabled": false}`` and the change 409, with no database session and no body read.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr, ValidationError
from sqlalchemy import text
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from nous.api.owner_guard import OwnerGuard
from nous.api.tool_classes import MCP_TOOL_PREFIX
from nous.api.tool_loading import (
    CORE_MAX,
    KINDS,
    MODES,
    NAME_PATTERN,
    PINNED,
    READ_RATIO,
    WRITE_RATIO,
    ToolLoadingConfig,
    config_key,
    index_tokens,
    split,
    tokens_est,
)
from nous.autonomy.tool_guard import HARNESS_TOOLS
from nous.runtime_config import RuntimeConfig

logger = logging.getLogger(__name__)

PATH = "/dashboard/tool-loading"
BODY_MAX_BYTES = 16384
ECHO_CHARS = 40
ECHO_NAMES = 5
WINDOWS = (1, 7, 30)
TOOL_DAYS = 30
OFF = {"error": "Tool loading is off on this server.", "enabled": False}
FAILED = "The request could not be processed."

# The watched calls of the window, each joined to its own model-call row (the same agent): only a call that succeeded
# is counted, and a watch row whose call row is missing is left out.
_WATCHED = """
    FROM nous_system.tool_loading_watch w
    JOIN nous_system.model_call_log m ON m.id = w.call_id AND m.agent_id = w.agent_id
   WHERE w.agent_id = :agent AND w.started_at >= :since AND m.status = 'ok'
"""
_NET = "CAST(w.record->>'deferred_tokens_est' AS bigint) - CAST(w.record->>'index_tokens_est' AS bigint)"
_CALLS = text(
    f"""
    SELECT w.kind,
           count(*) AS calls,
           count(*) FILTER (WHERE w.record->>'cache' = 'read') AS read_calls,
           count(*) FILTER (WHERE w.record->>'cache' = 'written') AS written_calls,
           coalesce(sum(CAST(w.record->>'deferred' AS integer)), 0) AS deferred,
           coalesce(sum(CAST(w.record->>'deferred_tokens_est' AS bigint)), 0) AS deferred_tokens,
           coalesce(sum(CAST(w.record->>'index_tokens_est' AS bigint)), 0) AS index_tokens,
           coalesce(sum({_NET}) FILTER (WHERE w.record->>'cache' = 'read'), 0) AS read_net,
           coalesce(sum({_NET}) FILTER (WHERE w.record->>'cache' = 'written'), 0) AS written_net,
           count(DISTINCT w.turn_id) AS turns
    {_WATCHED}
     GROUP BY 1
    """
)
# The turns in which a tool the call would have left out (sent, and not kept) was then called. The calls of a turn
# mostly send the same tools and keep the same names: DISTINCT before the join keeps one row of each.
_USED = text(
    f"""
    WITH watched AS (
        SELECT DISTINCT w.turn_id, w.kind, m.tool_names, w.record->'kept' AS kept
        {_WATCHED}
           AND w.turn_id IS NOT NULL
    )
    SELECT w.kind, count(DISTINCT w.turn_id) AS turns, count(DISTINCT t.id) AS calls
      FROM watched w
      JOIN nous_system.tool_call_log t
        ON t.agent_id = :agent AND t.started_at >= :since AND t.turn_id = w.turn_id
       AND t.tool_name = ANY (w.tool_names) AND NOT (w.kept @> to_jsonb(t.tool_name))
     GROUP BY w.kind
    """
)
# Each tool's calls and sessions over every kind of call.
_TOOL_USE = text(
    """
    SELECT tool_name, count(*) AS calls, count(DISTINCT session_id) AS sessions
      FROM nous_system.tool_call_log
     WHERE agent_id = :agent AND started_at >= :since
     GROUP BY tool_name
    """
)


class SettingChange(BaseModel):
    """``PUT``: the fields to change; an absent field keeps its value, a null ``core`` is the default core set."""

    model_config = ConfigDict(extra="forbid")

    mode: StrictStr | None = None
    kinds: dict[StrictStr, StrictBool] | None = None
    core: list[StrictStr] | None = None


class _BadRequest(Exception):
    """A body the route refuses (400); the text names the field and the rule."""


async def _parse(request: Request) -> SettingChange:
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > BODY_MAX_BYTES:
            raise _BadRequest(f"The body must be at most {BODY_MAX_BYTES} bytes.")
    try:
        data = json.loads(bytes(raw)) if bytes(raw).strip() else {}
    except (ValueError, RecursionError):
        raise _BadRequest("The body must be JSON.") from None
    if not isinstance(data, dict):
        raise _BadRequest("The body must be a JSON object.")
    try:
        return SettingChange.model_validate(data)
    except ValidationError as exc:
        fields = sorted(
            {
                "an unknown field" if error["type"] == "extra_forbidden" else str(error["loc"][0])[:ECHO_CHARS]
                for error in exc.errors()
                if error["loc"]
            }
        )
        raise _BadRequest(f"Not accepted: {', '.join(fields) or 'body'}.") from None


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _echo(names: list[str]) -> str:
    return ", ".join(name[:ECHO_CHARS] for name in names[:ECHO_NAMES]) + (", …" if len(names) > ECHO_NAMES else "")


def changed_setting(
    current: ToolLoadingConfig, body: SettingChange, registered: set[str], actor: str, now: datetime
) -> ToolLoadingConfig:
    """``current`` with the body's fields, or ``_BadRequest`` with the sentence. A core name already in the core set
    is kept whatever it is; a name being added must be in ``registered``. Pinned names are not stored: they are kept
    in full whatever the list says."""
    mode = current.mode
    if body.mode is not None:
        if body.mode not in MODES:
            raise _BadRequest("The mode must be off or watch.")
        mode = body.mode
    kinds = set(current.kinds)
    if body.kinds is not None:
        if any(kind not in KINDS for kind in body.kinds):
            raise _BadRequest("Not accepted: a kind of call this server does not know.")
        kinds |= {kind for kind, on in body.kinds.items() if on}
        kinds -= {kind for kind, on in body.kinds.items() if not on}
    core = current.core
    if "core" in body.model_fields_set:
        if body.core is None:
            core = None
        else:
            if len(body.core) > CORE_MAX:
                raise _BadRequest(f"The core set may hold at most {CORE_MAX} tools.")
            if not all(NAME_PATTERN.match(name) for name in body.core):
                raise _BadRequest("Not accepted: a core name that is not a tool name.")
            already = set(current.core_names())
            unknown = sorted({name for name in body.core if name not in already and name not in registered})
            if unknown:
                raise _BadRequest(f"Not registered now: {_echo(unknown)}.")
            core = tuple(dict.fromkeys(name for name in body.core if name not in PINNED))
    return ToolLoadingConfig(mode, frozenset(kinds), core, now.isoformat(), actor)


def setting_view(config: ToolLoadingConfig, problem: str | None) -> dict[str, Any]:
    """The setting as the card shows it."""
    return {
        "mode": config.mode,
        "kinds": [{"key": key, "words": words, "on": key in config.kinds} for key, words in KINDS.items()],
        "core_default": config.core is None,
        "core": sorted(name for name in config.core_names() if name not in PINNED),
        "pinned": [{"name": name, "why": why} for name, why in sorted(PINNED.items())],
        "updated_at": config.updated_at,
        "updated_by": config.updated_by,
        "problem": problem,
    }


_NO_CALLS = {
    "calls": 0,
    "read_calls": 0,
    "written_calls": 0,
    "deferred": 0,
    "deferred_tokens": 0,
    "index_tokens": 0,
    "read_net": 0,
    "written_net": 0,
    "turns": 0,
}


def numbers_row(key: str, words: str, on: bool, row: Any, used: tuple[int, int] | None) -> dict[str, Any]:
    """One row of the card's numbers, with the fields the card's ``KindNumbers`` reads (dashboard-app
    ``lib/tool_loading.ts``): the kind's ``key``, its words, whether it is watched, and ``kind_numbers``."""
    return {"key": key, "words": words, "on": on, **kind_numbers(row or _NO_CALLS, used)}


def kind_numbers(row: dict[str, Any], used: tuple[int, int] | None) -> dict[str, Any]:
    """One kind of call's numbers over the window (``row``, the sums of ``_CALLS``; ``used``, the turns and calls of
    ``_USED``, None when tool calls are not recorded): the calls recorded, what each would have left out on average,
    and the would-be saving per call weighed by each call's cache state, in uncached input tokens' worth. A call with
    no cache state (no usage reported) is not weighed."""
    calls = int(row["calls"])
    weighed = int(row["read_calls"]) + int(row["written_calls"])
    saved = float(row["read_net"]) * READ_RATIO + float(row["written_net"]) * WRITE_RATIO
    return {
        "calls": calls,
        "read_calls": int(row["read_calls"]),
        "written_calls": int(row["written_calls"]),
        "deferred_avg": round(int(row["deferred"]) / calls, 1) if calls else 0,
        "deferred_tokens_avg": round(int(row["deferred_tokens"]) / calls) if calls else 0,
        "index_tokens_avg": round(int(row["index_tokens"]) / calls) if calls else 0,
        "saved_per_call": round(saved / weighed) if weighed else None,
        "turns": int(row["turns"]),
        "turns_used_left_out": None if used is None else used[0],
        "calls_used_left_out": None if used is None else used[1],
    }


def _server(name: str) -> str | None:
    if not name.startswith(MCP_TOOL_PREFIX):
        return None
    return name[len(MCP_TOOL_PREFIX) :].partition("__")[0]


def build_tool_loading_routes(*, database: Any, settings: Any, dispatcher: Any, guard: OwnerGuard) -> list[Route]:
    """The two routes, both behind ``guard``. ``dispatcher`` is the tool dispatcher (main.py passes its lazy proxy,
    falsy until the lifespan built one), read at each request."""
    agent_id = settings.agent_id
    changing = asyncio.Lock()

    def actor(request: Request) -> str:
        proof = guard.proof(request)
        assert proof is not None  # the guard let the call in on one
        return proof

    def tool_defs() -> list[dict[str, Any]]:
        """The registered tools, as the widest offered set lists them (no initiation-only and no per-turn tool)."""
        if not dispatcher:
            return []
        return [tool for tool in dispatcher.available_tools("task") if tool.get("name") not in HARNESS_TOOLS]

    async def numbers(days: int, config: ToolLoadingConfig) -> list[dict[str, Any]] | None:
        if not settings.model_call_log_enabled:
            return None
        since = datetime.now(UTC) - timedelta(days=days)
        async with database.session() as session:
            result = await session.execute(_CALLS, {"agent": agent_id, "since": since})
            rows = {row["kind"]: dict(row) for row in result.mappings()}
            used: dict[str, tuple[int, int]] | None = None
            if settings.tool_call_log_enabled:
                result = await session.execute(_USED, {"agent": agent_id, "since": since})
                used = {row["kind"]: (int(row["turns"]), int(row["calls"])) for row in result.mappings()}
        return [
            numbers_row(key, words, key in config.kinds, rows.get(key), None if used is None else used.get(key, (0, 0)))
            for key, words in KINDS.items()
        ]

    async def tool_use() -> dict[str, tuple[int, int]] | None:
        if not settings.tool_call_log_enabled:
            return None
        since = datetime.now(UTC) - timedelta(days=TOOL_DAYS)
        async with database.session() as session:
            result = await session.execute(_TOOL_USE, {"agent": agent_id, "since": since})
            return {row.tool_name: (int(row.calls), int(row.sessions)) for row in result}

    def tool_rows(
        defs: list[dict[str, Any]], config: ToolLoadingConfig, use: dict[str, tuple[int, int]] | None
    ) -> list[dict[str, Any]]:
        core = set(config.core_names())
        rows = []
        for tool in defs:
            name = str(tool.get("name", ""))
            counted = use.get(name, (0, 0)) if use is not None else None
            rows.append(
                {
                    "name": name,
                    "server": _server(name),
                    "registered": True,
                    "tokens_est": tokens_est(tool),
                    "calls_30d": counted[0] if counted is not None else None,
                    "sessions_30d": counted[1] if counted is not None else None,
                    "state": "pinned" if name in PINNED else ("core" if name in core else "left_out"),
                    "why": PINNED.get(name),
                }
            )
        listed = {row["name"] for row in rows}
        for name in sorted(core - listed - set(PINNED)):
            counted = use.get(name, (0, 0)) if use is not None else None
            rows.append(
                {
                    "name": name,
                    "server": _server(name),
                    "registered": False,
                    "tokens_est": None,
                    "calls_30d": counted[0] if counted is not None else None,
                    "sessions_30d": counted[1] if counted is not None else None,
                    "state": "core",
                    "why": None,
                }
            )
        return sorted(rows, key=lambda row: row["name"])

    async def read(request: Request) -> Response:
        """GET /dashboard/tool-loading"""
        if not settings.tool_loading_enabled:
            return JSONResponse({"enabled": False})
        raw = request.query_params.get("days", "7")
        days = int(raw) if raw.isascii() and raw.isdigit() and len(raw) <= 2 else 0
        if days not in WINDOWS:
            return _error(400, "The window must be 1, 7 or 30 days.")
        runtime = RuntimeConfig.get()
        config = runtime.get_tool_loading(agent_id)
        try:
            defs = tool_defs()
            kept, left_out = split(defs, config)
        except Exception as exc:
            logger.warning("F120: reading the tool-loading card failed (%s)", type(exc).__name__)
            return _error(500, FAILED)
        # A failed read of the numbers or of the tools' use leaves the setting on the card, so it can still be changed.
        numbers_failed = usage_failed = False
        try:
            per_kind = await numbers(days, config)
        except Exception as exc:
            logger.warning("F120: reading the tool-loading numbers failed (%s)", type(exc).__name__)
            per_kind, numbers_failed = None, True
        try:
            use = await tool_use()
        except Exception as exc:
            logger.warning("F120: reading the tools' use failed (%s)", type(exc).__name__)
            use, usage_failed = None, True
        return JSONResponse(
            {
                "enabled": True,
                **setting_view(config, runtime.tool_loading_problem(agent_id)),
                "recorded": {
                    "model_calls": bool(settings.model_call_log_enabled),
                    "tool_calls": bool(settings.tool_call_log_enabled),
                },
                "days": days,
                "numbers": per_kind,
                "numbers_failed": numbers_failed,
                "usage_failed": usage_failed,
                "totals": {
                    "tools": len(defs),
                    "kept": len(kept),
                    "left_out": len(left_out),
                    "tokens_est": tokens_est(defs) if defs else 0,
                    "left_out_tokens_est": sum(tokens_est(tool) for tool in left_out),
                    "index_tokens_est": index_tokens(left_out),
                },
                "tools": tool_rows(defs, config, use),
            }
        )

    async def change(request: Request) -> Response:
        """PUT /dashboard/tool-loading"""
        if not settings.tool_loading_enabled:
            return JSONResponse(OFF, status_code=409)
        try:
            body = await _parse(request)
        except _BadRequest as exc:
            return _error(400, str(exc))
        runtime = RuntimeConfig.get()
        async with changing:
            current = runtime.get_tool_loading(agent_id)
            try:
                registered = {str(tool.get("name")) for tool in tool_defs()}
                new = changed_setting(current, body, registered, actor(request), datetime.now(UTC))
            except _BadRequest as exc:
                return _error(400, str(exc))
            except Exception as exc:
                logger.warning("F120: checking a tool-loading change failed (%s)", type(exc).__name__)
                return _error(500, FAILED)
            # A stored value that could not be read is replaced by any save, even one that changes nothing.
            unchanged = (new.mode, new.kinds, new.core) == (current.mode, current.kinds, current.core)
            if unchanged and runtime.tool_loading_problem(agent_id) is None:
                return JSONResponse({"enabled": True, "changed": False, **setting_view(current, None)})
            try:
                async with database.session() as session:
                    await runtime.persist_to_db(session, config_key(agent_id), new.to_json())
            except Exception as exc:
                # Not stored, so not set: the running process keeps what is stored.
                logger.warning("F120: storing the tool-loading setting failed (%s)", type(exc).__name__)
                return _error(500, FAILED)
            runtime.set_tool_loading(agent_id, new)
        return JSONResponse({"enabled": True, "changed": True, **setting_view(new, None)})

    return [
        guard.route(PATH, read, methods=["GET"]),
        guard.route(PATH, change, methods=["PUT"]),
    ]
