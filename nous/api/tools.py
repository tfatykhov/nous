"""Tool dispatcher and Nous memory tools for direct Anthropic API integration.

Provides:
- ToolDispatcher: registers tools, dispatches calls, filters by frame
- 4 tool closures that give Claude direct access to Nous memory organs:
  - record_decision: Write decisions to Brain
  - learn_fact: Store facts in Heart
  - recall_deep: Search all memory types (Heart + Brain)
  - create_censor: Add guardrails to Heart

Each tool returns MCP-compliant response format and handles errors gracefully.
"""

from __future__ import annotations

import copy
import inspect
import json
import logging
import math
import re
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from functools import partial
from typing import Any
from uuid import UUID

from nous.brain.brain import Brain
from nous.brain.schemas import ReasonInput, RecordInput
from nous.config import PROGRAMMATIC_TOOLS_TIMEOUT_GRACE_SECONDS, Settings
from nous.heart.exemplars import parse_label
from nous.heart.heart import Heart
from nous.heart.schemas import (
    CensorInput,
    FactInput,
    FactRejected,
    FactSummary,
    ProcedureInput,
)
from nous.observability.retrieval_logger import get_active as get_active_retrieval_logger
from nous.observability.retrieval_trace import RETURNED_TO_SCRIPT, SLICED_OFF
from nous.skills.parser import SkillParser

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ToolDispatcher
# ---------------------------------------------------------------------------


# Protocol-only tools registered for the one-time `initiation` frame. They must
# never leak into the conversational-frame tool superset (the "*" / task set).
_INITIATION_ONLY_TOOLS: frozenset[str] = frozenset(
    {"store_identity", "complete_initiation"}
)


def _tool_error(text: str) -> dict[str, Any]:
    """An MCP error response — error prose that is actually FLAGGED as one.

    Handlers used to return `{"content": [...]}` with error text and no
    `is_error`, so `dispatch` read `bool(result.get("is_error"))` as False and
    reported the failure to the model as a success. The model was told
    `record_decision` worked when nothing had been written.

    Use this for every failure path. Do NOT use it for an empty result: a
    memory search that matched nothing SUCCEEDED and found nothing, and
    flagging that would teach the model an empty corpus is a broken tool.
    """
    return {"is_error": True, "content": [{"type": "text", "text": text}]}

# Trailing run of leaked XML tool syntax inside a JSON string arg. The model
# can slip from JSON tool-input into Claude's internal XML tool-call format
# mid-string (observed in prod 2026-07-13: record_decision's description
# string ended with '</description>\n<parameter name="confidence">0.55', so
# the parsed input had no top-level confidence key). Anchored to end-of-string
# so legitimate XML/HTML quoted mid-string is never touched.
#
# The run must END in an UNTERMINATED <parameter> tag. That is the actual
# evidence of a syntax transition: the model stopped emitting JSON and never
# closed what it started. A well-formed '<parameter name="x">v</parameter>' at
# the end of a string is far more likely to be prose QUOTING the format --
# a decision describing this very bug would otherwise have its text truncated
# and a value invented from the example. When the evidence is ambiguous we do
# not guess: salvage declines, and the missing-arg error tells the model to
# re-emit. Being told beats being silently repaired from a quotation.
# Located by a backward walk rather than one combined pattern. A single regex
# has to lead with `\s*`, which forces the engine to retry that greedy run at
# every start position and rescan the suffix -- quadratic. Measured on the
# combined form: 2k spaces 0.10s, 5k 0.62s, 10k 2.50s, 20k 10.78s, all inside
# an async dispatcher, so one whitespace-heavy arg on a call that is missing a
# required key stalls the shared event loop. Each pattern below is anchored at
# `\Z` and led by a literal, so every non-matching start position is rejected
# on its first character and the whole locator is linear.
_XML_LEAK_FINAL = re.compile(r'<parameter\s+name="[^"]+">[^<]*\Z')
_XML_LEAK_COMPLETE = re.compile(r'<parameter\s+name="[^"]+">[^<]*</parameter>\s*\Z')
_XML_LEAK_CLOSER = re.compile(r"</\w+>\s*\Z")
_XML_PARAM_LEAK_PAIR = re.compile(r'<parameter\s+name="([^"]+)">\s*([^<]*)')


def _leak_tail_start(value: str) -> int | None:
    """Index where the trailing XML-leak run begins, or None if there is none.

    Walks right to left: the final UNTERMINATED tag (the syntax-transition
    evidence -- see the note above), then any complete tags immediately before
    it, then an optional closing tag, then preceding whitespace. Equivalent to
    the old single-regex match, without its backtracking.
    """
    final = _XML_LEAK_FINAL.search(value)
    if final is None:
        return None
    start = final.start()
    # Step to each preceding tag via rfind rather than re-searching the whole
    # prefix. `search(value, 0, start)` rescans from offset zero on every
    # iteration, which is quadratic in the tag count -- measured on that form:
    # 500 tags 0.015s, 1000 0.067s, 2500 0.40s, 5000 (202 KB) 1.67s. rfind
    # walks backward over each gap exactly once, so the whole loop is linear.
    # ("</parameter>" cannot false-match "<parameter" -- the slash is inside.)
    while (cand := value.rfind("<parameter", 0, start)) != -1:
        complete = _XML_LEAK_COMPLETE.match(value, cand, start)
        if complete is None:
            break
        start = cand
    if (closer := _XML_LEAK_CLOSER.search(value, 0, start)) is not None:
        start = closer.start()
    while start > 0 and value[start - 1].isspace():
        start -= 1
    return start


def _satisfies_schema_constraints(value: Any, prop_schema: dict[str, Any]) -> bool:
    """Check a salvaged value against enum / minimum / maximum.

    Primitive-type coercion alone is not enough to call a key recovered: a
    leaked ``confidence=2.0`` parses as a float and passes the type gate, then
    fails RecordInput's ``le=1.0`` deep inside the handler. Nothing is stored,
    yet the call is reported as a repaired success. A value that cannot satisfy
    its declared constraints is not salvage — it stays missing so the model is
    told to re-emit it.
    """
    enum = prop_schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        return False
    if isinstance(value, int | float) and not isinstance(value, bool):
        # NaN/inf first: every comparison against NaN is False, so a bare
        # range check would wave it through as "within bounds" and the
        # handler's own validator would then reject it downstream.
        if not math.isfinite(value):
            return False
        minimum, maximum = prop_schema.get("minimum"), prop_schema.get("maximum")
        if isinstance(minimum, int | float) and value < minimum:
            return False
        if isinstance(maximum, int | float) and value > maximum:
            return False
    return True


def _coerce_to_schema_type(raw: str, prop_schema: dict[str, Any]) -> Any:
    """Coerce a leaked string value to its schema-declared type.

    Returns None when coercion fails — the caller treats the key as still
    missing and falls through to the actionable error.

    Containers are deliberately NOT salvaged. Accepting one would require
    validating `items`, nested `required` and nested enums to know it is
    usable, i.e. a real JSON-schema validator; without that, a leaked
    ``nodes=[{"type":"bogus"}]`` clears the outer array check and is only
    rejected deep inside the handler. The salvage path exists for one observed
    prod failure — a SCALAR leaking into a string — and there is no evidence a
    model emits nested JSON through XML tag text. Declining costs nothing: the
    key stays missing and the model is told to re-emit it.
    """
    raw = raw.strip()
    prop_type = prop_schema.get("type")
    if prop_type in ("array", "object"):
        return None
    try:
        if prop_type == "number":
            value: Any = float(raw)
        elif prop_type == "integer":
            value = int(raw)
        elif prop_type == "boolean":
            value = {"true": True, "false": False}.get(raw.lower())
        else:
            value = raw  # string / untyped
        if value is None:
            return None
        return value if _satisfies_schema_constraints(value, prop_schema) else None
    except ValueError:
        return None


def _salvage_leaked_args(
    tool_name: str,
    args: dict[str, Any],
    missing: list[str],
    schema: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Recover schema-required args leaked as XML <parameter> tags.

    Scans top-level string arg values for a trailing leak run, extracts any
    of the missing keys found there, type-coerces them per the schema, and
    strips the leaked tail from the host string. Returns (args, still_missing)
    — unchanged inputs when nothing salvageable is found.
    """
    properties = schema.get("properties", {})
    salvaged: dict[str, Any] = {}
    cleaned: dict[str, str] = {}
    for host_key, host_value in args.items():
        if not isinstance(host_value, str):
            continue
        tail_start = _leak_tail_start(host_value)
        if tail_start is None:
            continue
        found_in_host = False
        for leaked_key, leaked_raw in _XML_PARAM_LEAK_PAIR.findall(
            host_value[tail_start:]
        ):
            if leaked_key not in missing or leaked_key in salvaged:
                continue
            value = _coerce_to_schema_type(leaked_raw, properties.get(leaked_key, {}))
            if value is None:
                continue
            salvaged[leaked_key] = value
            found_in_host = True
            logger.warning(
                "Salvaged leaked tool arg %s.%s=%r from inside %r (model "
                "emitted XML <parameter> syntax in a JSON string value)",
                tool_name,
                leaked_key,
                value,
                host_key,
            )
        if found_in_host:
            cleaned[host_key] = host_value[:tail_start]
    if not salvaged:
        return args, missing
    return {**args, **cleaned, **salvaged}, [k for k in missing if k not in salvaged]


# Structural type mismatches no downstream coercion layer repairs. Deliberately
# NOT a full JSON-schema validator: pydantic's lax mode happily turns "0.9" into
# 0.9 and "true" into True, so rejecting scalar-for-scalar here would fail calls
# that succeed today. Only container-vs-scalar confusion is flagged, which is
# the shape that actually reaches the model as an opaque pydantic ValidationError
# (observed 2026-08-22: record_decision tags='fannie-mae, cpm, condo, ...').
_CONTAINER_TYPES = {"array": list, "object": dict}
_SCALAR_TYPES = {"string": str, "number": (int, float), "integer": int, "boolean": bool}

_JSON_TYPE_NAMES: dict[type, str] = {
    str: "string", bool: "boolean", int: "integer",
    float: "number", list: "array", dict: "object",
}


def _describe_json_type(value: Any) -> str:
    """Name a Python value's JSON type for an error message."""
    return _JSON_TYPE_NAMES.get(type(value), type(value).__name__)


def _schema_type_errors(args: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """Describe args whose JSON type cannot be what the schema declares.

    Fail-open by construction: anything ambiguous (union types, absent type,
    null values, unknown keys) is skipped. An over-eager check here would
    reject tool calls that work today, which is strictly worse than the
    opaque error it replaces.
    """
    properties = schema.get("properties") or {}
    errors: list[str] = []
    for key, value in args.items():
        if key.startswith("_") or value is None:
            continue
        declared = (properties.get(key) or {}).get("type")
        if not isinstance(declared, str):
            continue  # union / unspecified -- not our business
        expected_container = _CONTAINER_TYPES.get(declared)
        if expected_container is not None:
            if not isinstance(value, expected_container):
                hint = ""
                if declared == "array" and isinstance(value, str):
                    items = (properties[key].get("items") or {}).get("type", "string")
                    hint = (
                        f' Send a JSON array of {items}s -- ["a", "b"] -- '
                        f"not one delimited string."
                    )
                errors.append(
                    f"{key} must be {declared}, got {_describe_json_type(value)}.{hint}"
                )
            continue
        expected_scalar = _SCALAR_TYPES.get(declared)
        # Only the reverse structural error: a container where a scalar belongs.
        # Scalar-for-scalar is left to the handler, which coerces it.
        if expected_scalar is not None and isinstance(value, list | dict):
            errors.append(
                f"{key} must be {declared}, got {_describe_json_type(value)}."
            )
    return errors


def _required_handler_params(handler: Callable[..., Any]) -> set[str] | None:
    """Parameter names the handler cannot be invoked without.

    Returns ``None`` for a ``**kwargs``-only handler, meaning "the signature
    cannot tell us" — the caller must then trust the schema's own ``required``
    list. Without this distinction a variadic handler reports zero required
    params, every schema-required key is treated as optional, and validation
    silently no-ops. That is not hypothetical: ``heartbeat_check_create`` and
    ``heartbeat_check_manage`` are ``(**kwargs)`` and index ``kwargs["name"]``
    / ``kwargs["action"]`` directly, so a missing key raises KeyError inside
    the handler and comes back as prose the dispatcher reports as success.
    """
    try:
        sig = inspect.signature(handler)
    except (TypeError, ValueError):
        return None
    named = {
        p.name
        for p in sig.parameters.values()
        if p.default is inspect.Parameter.empty
        and p.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    accepts_var_kw = any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )
    # A handler with BOTH named required params and **kwargs still can't be
    # trusted to have defaults for the schema-required keys it swallows.
    return None if accepts_var_kw else named


class ToolDispatcher:
    """Registers tool handlers and dispatches tool calls from the API.

    Each handler is an async callable that accepts **kwargs and returns
    an MCP-format response: {"content": [{"type": "text", "text": "..."}]}.

    The dispatcher extracts plain text for the Anthropic API tool_result format.
    """

    def __init__(
        self,
        *,
        tool_schema_cache_enabled: bool = True,
        stable_tool_set_enabled: bool = True,
        arg_salvage_enabled: bool = True,
    ) -> None:
        self._handlers: dict[str, Callable[..., Any]] = {}
        self._schemas: dict[str, dict[str, Any]] = {}  # P0-7 fix
        self._tool_schema_cache: dict[str, list[dict[str, Any]]] = {}  # F036
        self._tool_schema_cache_enabled = tool_schema_cache_enabled  # F036
        # Cache stabilization: collapse conversational frames to one tool
        # superset so the Anthropic prompt-cache prefix isn't busted on frame
        # change (tools sit at the front of the cacheable prefix).
        self._stable_tool_set_enabled = stable_tool_set_enabled
        # Repair model-emitted input where a required arg leaked as an XML
        # <parameter> tag inside another string arg (see _salvage_leaked_args).
        self._arg_salvage_enabled = arg_salvage_enabled

    def register(self, name: str, handler: Callable[..., Any], schema: dict[str, Any]) -> None:
        """Register a tool handler with its JSON schema."""
        self._handlers[name] = handler
        self._schemas[name] = schema
        self._tool_schema_cache.clear()  # F036: invalidate on registration

    async def dispatch(
        self, name: str, args: dict[str, Any], session_id: str | None = None,
        is_background: bool = False, turn_number: int | None = None,
    ) -> tuple[str, bool]:
        """Dispatch a tool call and return (result_text, is_error).

        P0-6 fix: Uses **kwargs unpacking for closures.
        P1-1 fix: Extracts text from MCP-format response.

        is_background: True for heartbeat/subtask turns. Decision-resolution
        tools read it via the injected _is_background kwarg to hard-block
        autopilot self-resolution.
        """
        handler = self._handlers.get(name)
        if not handler:
            return f"Unknown tool: {name}", True
        try:
            # Guard the **args unpacking below: validate schema-required keys
            # up front and, when one is missing, try to salvage a value the
            # model leaked as an XML <parameter> tag inside another string
            # arg (observed prod failure mode, 2026-07-13). Only error when
            # the handler signature would actually raise — schema-required
            # keys with handler defaults keep today's lenient behavior.
            schema = self._schemas.get(name) or {}
            missing = [k for k in schema.get("required") or [] if k not in args]
            salvaged_keys: list[str] = []
            if missing and self._arg_salvage_enabled:
                before = set(missing)
                args, missing = _salvage_leaked_args(name, args, missing, schema)
                salvaged_keys = sorted(before - set(missing))
            if missing:
                handler_required = _required_handler_params(handler)
                # None == variadic handler, signature tells us nothing; fall
                # back to the schema's own required list rather than skipping
                # validation entirely (which is what zero named params meant).
                hard_missing = (
                    list(missing)
                    if handler_required is None
                    else [k for k in missing if k in handler_required]
                )
                if hard_missing:
                    provided = sorted(k for k in args if not k.startswith("_"))
                    return (
                        f"Tool error: {name} is missing required argument(s): "
                        f"{', '.join(hard_missing)}. Provided: "
                        f"{', '.join(provided) if provided else 'none'}. Emit "
                        "every required field as a top-level JSON key in the "
                        "tool input — do not embed values as XML tags inside "
                        "another field's text.",
                        True,
                    )

            # Structural type mismatches: say what is wrong and how to fix it,
            # rather than letting the handler surface a pydantic ValidationError
            # (or worse, coercing silently -- the model never learns it emitted
            # the wrong shape and repeats it next turn).
            type_errors = _schema_type_errors(args, schema)
            if type_errors:
                logger.warning(
                    "Tool arg type mismatch for %s: %s", name, "; ".join(type_errors)
                )
                return (
                    f"Tool error: {name} received argument(s) of the wrong type. "
                    + " ".join(type_errors)
                    + " Re-emit the call with corrected types.",
                    True,
                )

            if name in ("resolve_decision", "resolve_decisions"):
                args = {**args, "_is_background": is_background}
            if session_id is not None and name == "spawn_task":
                args = {**args, "_session_id": session_id}
            if session_id is not None and name == "cache_retrieve":
                args = {**args, "session_id": session_id}
            if session_id is not None and name == "run_python":
                args = {**args, "_session_id": session_id}
            if session_id is not None and name == "ingest_document":
                # F069: inject session_id so the tool can resolve the
                # active episode when the caller omits episode_id.
                args = {**args, "_session_id": session_id}
            if session_id is not None and name == "record_decision":
                # 2026-07-28: RecordInput.session_id has existed since the
                # Brain module shipped, but this tool never passed it — so
                # every tool-recorded decision stored session_id NULL (287 of
                # 1005 prod rows had it, all from the deliberation path).
                # That NULL is what left episode<->decision association with
                # no substrate. Consumers: Brain.get_session_decisions and
                # the optional filter in Brain._query.
                args = {**args, "_session_id": session_id}
            if turn_number is not None and name in ("recall_deep", "run_python"):
                # `run_python` included because its in-script `recall_deep()`
                # opens its own F091 trace; without this those rows store
                # turn_number NULL and cannot be joined to the turn that caused
                # them on the (session_id, turn_number) index — which is most of
                # the value of giving them their own `script` path. Several
                # script recalls legitimately SHARE one turn_number.
                # F091: threaded EXPLICITLY, not via ContextVar. stream_chat is
                # an async generator whose every resume runs in a fresh copied
                # context (see the KNOWN LIMITATION at runner.py:1231), so a
                # contextvar set inside it is invisible to any tool dispatched
                # after the first yielded event — and a tool call always yields
                # tool_start first. An earlier contextvar attempt at this was
                # therefore inert on the streaming path.
                args = {**args, "_turn_number": turn_number}
            if session_id is not None and name == "recall_deep":
                # F051.4 / F055: inject session_id into recall_deep so
                # F055's Cross-Turn Residual Activation can read it via
                # the _session_id kwarg. Until F055 ships, recall_deep
                # silently accepts the kwarg (added by F051.4) and ignores
                # it — fail-open contract.
                args = {**args, "_session_id": session_id}
            result = await handler(**args)  # P0-6: **kwargs unpacking
            # P1-1: Extract text from MCP-format response. is_error honors
            # the MCP field when a handler sets it (#179: run_python error
            # returns) — absent means success, as before.
            result_text = result["content"][0]["text"]
            is_error = bool(result.get("is_error"))
            if salvaged_keys and not is_error:
                # Salvage recovered the call, but a silent success teaches the
                # model nothing -- it emitted XML inside a JSON string and got
                # a clean result, so it will do it again. Say so in the result.
                result_text = (
                    f"[input repaired] {', '.join(salvaged_keys)} had to be recovered "
                    "from XML <parameter> syntax leaked inside another field's text. "
                    "Emit every argument as a top-level JSON key.\n\n" + result_text
                )
            return result_text, is_error
        except Exception as e:
            logger.exception("Tool dispatch error for %s", name)
            return f"Tool error: {e}", True

    def tool_definitions(self) -> list[dict[str, Any]]:
        """Return all tool definitions in Anthropic API format."""
        return [
            {
                "name": name,
                "description": schema.get("description", ""),
                "input_schema": schema,
            }
            for name, schema in self._schemas.items()
        ]

    def available_tools(self, frame_id: str) -> list[dict[str, Any]]:
        """Return tool definitions filtered by frame (D5).

        Uses FRAME_TOOLS map from runner module to determine which
        tools are available for a given frame. Wildcard "*" means all tools.

        F036: Results cached per frame_id. Returns deep copy to prevent
        mutation from corrupting the cache.

        Cache stabilization: when stable_tool_set_enabled, all non-initiation
        frames resolve to the same "task" ("*") superset so the Anthropic
        prompt-cache prefix (tools sit at its front) is not busted when the
        frame changes turn-to-turn. Per-frame *behavioral* steering still
        happens via _get_frame_instructions; FRAME_TOOLS no longer gates the
        wire-level tool array for these frames. 'initiation' keeps its distinct
        minimal set (isolated one-time protocol, never interleaved with chat).
        """
        # Collapse conversational frames to one cache-stable superset. The
        # effective frame is used as BOTH the cache key and the FRAME_TOOLS
        # lookup, so every non-initiation frame returns a byte-identical list.
        effective_frame = (
            "task"
            if self._stable_tool_set_enabled and frame_id != "initiation"
            else frame_id
        )

        # F036: Check cache first (skip if caching disabled)
        if self._tool_schema_cache_enabled and effective_frame in self._tool_schema_cache:
            return copy.deepcopy(self._tool_schema_cache[effective_frame])

        from nous.api.runner import FRAME_TOOLS

        allowed = FRAME_TOOLS.get(effective_frame, [])

        # Wildcard means all tools — minus initiation-only protocol tools,
        # which must never leak into a conversational-frame tool array.
        if "*" in allowed:
            result = [
                d for d in self.tool_definitions()
                if d["name"] not in _INITIATION_ONLY_TOOLS
            ]
        else:
            result = [
                {
                    "name": name,
                    "description": schema.get("description", ""),
                    "input_schema": schema,
                }
                for name, schema in self._schemas.items()
                if name in allowed
            ]

        # F036: Cache and return deep copy
        if self._tool_schema_cache_enabled:
            self._tool_schema_cache[effective_frame] = result
            return copy.deepcopy(result)
        return result


# ---------------------------------------------------------------------------
# recall_deep text formatter (F051 refactor)
# ---------------------------------------------------------------------------


async def _fetch_parent_episodes_for_facts(
    heart: "Heart",
    results: "list[Any]",  # list[PipelineResult]
    max_parents: int,
    truncate: int,
) -> list[tuple[str, str]]:
    """F067 Phase 2: fetch parent episode summaries for fact-typed results.

    Walks ``results`` in rank order, collects each fact's ``source_episode_id``
    (deduped, preserving rank order), caps at ``max_parents``, fetches the
    episode summary for each, and returns ``[(episode_id, summary)]``.

    Returns an empty list when no facts have parent episodes or all lookups
    fail. The caller is responsible for the feature-flag gate.
    """
    from sqlalchemy import text as sa_text
    fact_ids = [str(r.id) for r in results if getattr(r, "type", None) == "fact"]
    if not fact_ids:
        return []
    # Codex fix: cap=0 should disable injection entirely. Early-return when
    # the operator set max_parents=0 — saves a DB round-trip too.
    if max_parents <= 0:
        return []
    async with heart.db.session() as s:
        rows = (await s.execute(
            sa_text(
                "SELECT id::text, source_episode_id::text FROM heart.facts "
                "WHERE agent_id = :a AND id::text = ANY(:ids)"
            ),
            {"a": heart.agent_id, "ids": fact_ids},
        )).all()
        fact_to_ep = {r[0]: r[1] for r in rows if r[1] is not None}
        ordered_eps: list[str] = []
        seen: set[str] = set()
        for fid in fact_ids:
            # Codex fix: enforce cap BEFORE appending so max_parents=N stays
            # exactly N. Previous order ("append then check len >= max")
            # produced N+1 entries when the iteration walked one extra step
            # past a fresh episode_id.
            if len(ordered_eps) >= max_parents:
                break
            ep = fact_to_ep.get(fid)
            if ep and ep not in seen:
                seen.add(ep)
                ordered_eps.append(ep)
        if not ordered_eps:
            return []
        # Codex fix: structured_summary is JSONB with title/topics/key_points/
        # candidate_facts/etc. Casting the whole blob to text bloats context
        # and risks mid-JSON truncation at [:truncate]. Extract the inner
        # 'summary' string via ->> operator; fall back to the legacy summary
        # column when structured_summary is NULL.
        ep_rows = (await s.execute(
            sa_text(
                "SELECT id::text, "
                "COALESCE(structured_summary->>'summary', summary) "
                "FROM heart.episodes WHERE agent_id = :a AND id::text = ANY(:ids)"
            ),
            {"a": heart.agent_id, "ids": ordered_eps},
        )).all()
        ep_summary = {r[0]: r[1] for r in ep_rows if r[1]}
    out: list[tuple[str, str]] = []
    for eid in ordered_eps:
        summary = ep_summary.get(eid)
        if summary:
            out.append((eid, str(summary)[:truncate]))
    return out


def _format_pipeline_text(
    results: "list[Any]",  # list[PipelineResult] — string-quoted to avoid import cycle at module top
    stats: "Any",  # PipelineStats
    search_types: list[str],
    parent_episodes: list[tuple[str, str]] | None = None,
    session_group_heart: bool = False,
    emitted_out: list[tuple[Any, str]] | None = None,
) -> str:
    """Format ``run_recall_pipeline`` output into legacy ``recall_deep`` text.

    ``emitted_out``: optional side-channel collector. When a list is passed,
    every result this function actually WRITES INTO THE TEXT is appended as
    ``(id, type)`` at its point of emission. ``None`` (default) collects
    nothing, so the byte-identical ``recall_deep`` snapshot contract is
    untouched — this is the same side-channel idiom as ``dropped_out`` on
    ``heart.recall`` / chunk search (``retrieval_pipeline.py:910, 991``).

    THE POINT IS THAT IT REPORTS RATHER THAN PREDICTS. Which results reach the
    text depends on `search_types` section eligibility, and every attempt to
    re-derive that rule elsewhere has been wrong in one direction or the other:
    ``metrics.py:49-55`` records two harness attempts that OVERSTATED served
    recall, and the F091 scope-filter block this replaces was incomplete under
    ``search_types=["decision"]`` (a ``heart_graph_memory`` row was dropped by
    the formatter yet read ``rendered``) after an earlier revision had inverted
    the error in the opposite direction. Appending at the emission site is the
    only version that cannot drift from what was written.

    A ``(id, type)`` pair may legitimately appear TWICE — the same decision
    renders in both "Graph-Connected Decisions" and "Brain Decisions" when it
    was found by Stage 2 and by Stage 3, which do not cross-dedup. Consumers
    must use multiset/set semantics, never a uniqueness assert.

    Parent episodes are deliberately NOT collected: they are served content that
    is not in ``results`` (they arrive via the ``parent_episodes`` argument), so
    collecting them would break ``emitted_out ⊆ results`` for every consumer.
    See ``test_parent_episodes_are_not_collected``.

    Byte-identical to the pre-F051 ``recall_deep`` text output for the same
    query + heart/brain state — except when ``parent_episodes`` is provided
    (F067 Phase 2), in which case a `=== Parent Episode Context ===` section
    is appended at the end. When ``parent_episodes`` is empty/None, output
    remains byte-identical for backwards compatibility. F086: ``results``
    entries tagged ``metadata["retrieval_leg"] == "exemplar"`` are excluded
    from the Heart Memory section and rendered instead in a trailing
    `=== Nearest stored examples ===` section; absent when no exemplar rows
    are present (default, flag off).
    """
    search_all = "all" in search_types
    results_text: list[str] = []

    def _emit(r: Any) -> None:
        """Record that ``r`` was written into the text. Call at the emission
        site, immediately alongside the ``results_text.append`` that renders it,
        so the two cannot drift."""
        if emitted_out is not None:
            emitted_out.append((r.id, r.type))

    def _recency_tag(r: "Any") -> str:
        # §1: inline [current|superseded YYYY-MM] tag. Empty string when no
        # recency_status (flag OFF, or non-conflicting fact) => byte-identical.
        status = r.metadata.get("recency_status")
        if not status:
            return ""
        month = r.metadata.get("recency_date", "")
        return f"[{status} {month}]".rstrip()  # no leading space

    # ------------------------------------------------------------------
    # Heart Memory section
    # ------------------------------------------------------------------
    # Heart section is emitted iff Heart sub-search ran (heart_types non-empty).
    # The pre-refactor closure used a local `heart_types` list; we replicate
    # that gate by checking whether any heart-eligible type was in search_types
    # (or 'all' was passed) AND the pipeline produced or attempted Heart results.
    def _via_tag(r) -> str:
        # R3.3 (F085): mark a keyed-leg hit (exact entity-key match, not a
        # score-ranked retrieval) so its distinct provenance stays visible.
        if r.metadata.get("retrieval_leg") == "keyed":
            return "[via keyed] "
        # R3v2: round-2 hop hit — a two-hop associative match is more
        # surprising in context than a direct keyed hit, so it gets its own
        # distinct tag rather than falling through untagged.
        if r.metadata.get("retrieval_leg") == "keyed_r2":
            return "[via keyed-hop] "
        # Mark a Path-A graph-memory neighbour (now interleaved into the ranked Heart
        # Memory list by score) so its associative provenance stays visible to the agent.
        if r.metadata.get("stage_origin") == "heart_graph_memory" and getattr(r, "edge_relation", None):
            return f"[via {r.edge_relation}] "
        return ""

    # Prominence fix: graph-memory neighbours (Path A — fact/episode/chunk surfaced via
    # an edge) are INTERLEAVED into the ranked Heart Memory list (``results`` is
    # score-sorted when rerank_by_score=True) rather than buried in a trailing section,
    # so an associatively-linked memory (e.g. a co-occurrence neighbour) sits at its true
    # rank where the agent actually reads. Empty when heart_graph_all_types is off
    # (default) => the default recall_deep output stays byte-identical.
    heart_results = [
        r for r in results
        if (
            r.source == "heart"
            or r.metadata.get("stage_origin") == "heart_graph_memory"
        )
        and r.metadata.get("retrieval_leg") != "exemplar"
    ]
    # F086: ICL exemplar-leg hits get their own dedicated section (rendered
    # near the end of the function) instead of the Heart Memory list.
    exemplar_rows = [
        r for r in results if r.metadata.get("retrieval_leg") == "exemplar"
    ]
    heart_section_eligible = search_all or any(
        t in search_types for t in ["episode", "fact", "procedure", "censor"]
    )
    # The original closure ALSO requires that heart_types resolves to a non-empty
    # list — which is always true once heart_section_eligible is True (because the
    # filter expression yields the same membership). So the gate matches.
    if heart_section_eligible:
        if heart_results:
            results_text.append("=== Heart Memory ===")
            if session_group_heart:
                # P1.1: group facts/chunks/episodes by source session_id so
                # the LLM sees session boundaries — helps multi-session
                # reasoning. Sessions ordered by first-appearance in the
                # ranked list. Procedures/censors don't belong to a session
                # and are appended flat at the end of the bucket.
                session_buckets: "dict[str, list]" = {}
                no_session: list = []
                for result in heart_results:
                    if result.type == "episode":
                        sess = str(result.id)
                    elif result.type in ("fact", "chunk"):
                        sess = result.metadata.get("source_episode_id")
                        sess = str(sess) if sess else None
                    else:
                        sess = None
                    if sess:
                        session_buckets.setdefault(sess, []).append(result)
                    else:
                        no_session.append(result)
                i = 1
                for sess_id, items in session_buckets.items():
                    results_text.append(f"-- Session {sess_id[:8]} --")
                    for result in items:
                        results_text.append(
                            f"{i}. [{result.type}] {_via_tag(result)}{result.description}{_recency_tag(result)} "
                            f"(id: {result.id}, score: {result.score:.3f})"
                        )
                        _emit(result)
                        i += 1
                if no_session:
                    if session_buckets:
                        results_text.append("-- Other --")
                    for result in no_session:
                        results_text.append(
                            f"{i}. [{result.type}] {_via_tag(result)}{result.description}{_recency_tag(result)} "
                            f"(id: {result.id}, score: {result.score:.3f})"
                        )
                        _emit(result)
                        i += 1
            else:
                # Flat output (byte-identical to pre-P1.1 when no graph-memory neighbours
                # are present, i.e. heart_graph_all_types off — _via_tag returns "").
                for i, result in enumerate(heart_results, 1):
                    results_text.append(
                        f"{i}. [{result.type}] {_via_tag(result)}{result.description}{_recency_tag(result)} "
                        f"(id: {result.id}, score: {result.score:.3f})"
                    )
                    _emit(result)
        elif not exemplar_rows:
            # Codex r14: suppress the empty-section placeholder when the examples
            # block will render below — "No results found." would contradict it.
            # Byte-identical (placeholder retained) when there are no exemplars.
            results_text.append("=== Heart Memory ===\nNo results found.")

    # ------------------------------------------------------------------
    # Graph-Connected Decisions section (F022 Phase 2 cross-type)
    # ------------------------------------------------------------------
    # heart-side graph-expanded decisions (stage 2 of run_recall_pipeline)
    # are tagged ``metadata["stage_origin"] == "heart_graph"`` by
    # ``_heart_graph_to_pipeline``. This tag lets the formatter bucket
    # them correctly regardless of result-list order — important because
    # ``rerank_by_score`` (set when F067 chunks are enabled) globally
    # re-sorts the list and would otherwise break a position-based gate.
    heart_graph: list = [
        r for r in results
        if r.source == "graph_expanded"
        and r.type == "decision"
        and r.metadata.get("stage_origin") == "heart_graph"
    ]

    if heart_graph:
        results_text.append("\n=== Graph-Connected Decisions ===")
        for i, n in enumerate(heart_graph, 1):
            results_text.append(
                f"{i}. [via {n.edge_relation}] {n.description} "
                f"(id: {n.id}, score: {n.score:.3f})"
            )
            _emit(n)

    # NOTE: Path-A graph-memory neighbours (stage_origin == "heart_graph_memory") are no
    # longer a trailing section — they are interleaved into the ranked Heart Memory list
    # above (prominence fix) with a "[via <relation>]" marker, so an associatively-linked
    # memory sits at its true score-rank where the agent reads it.

    # ------------------------------------------------------------------
    # Brain Decisions section
    # ------------------------------------------------------------------
    if search_all or "decision" in search_types:
        decision_results = [r for r in results if r.source == "brain"]
        # Brain-side graph-expanded entries (stage 4): tagged
        # ``metadata["stage_origin"] == "brain_graph"`` by
        # ``_graph_expanded_to_pipeline``. Spreading-activation results
        # share the brain-graph bucket — see the OR below. Companion to
        # the heart-side filter above; the metadata tag replaces the
        # previous position-based gate so the formatter is stable under
        # ``rerank_by_score``.
        brain_graph: list = [
            r for r in results
            if r.source in ("graph_expanded", "spreading_activation")
            and r.metadata.get("stage_origin") == "brain_graph"
        ]

        if decision_results or brain_graph:
            results_text.append("\n=== Brain Decisions ===")
            for i, dec in enumerate(decision_results, 1):
                # Preserve original truthy-elision behavior: ``raw_score``
                # is None or 0.0 -> empty string. Don't use ``dec.score``
                # because it's normalized to 0.0 for None (see _decisions_to_pipeline).
                raw = dec.metadata.get("raw_score")
                score_str = f", score: {raw:.3f}" if raw else ""
                category = dec.metadata.get("category", "")
                stakes = dec.metadata.get("stakes", "")
                confidence = dec.metadata.get("confidence", 0.0)
                # F079 P1: include the abstract pattern when present (B-pull-thin).
                pattern = dec.metadata.get("pattern")
                pattern_str = f" | pattern: {pattern}" if pattern else ""
                # 2026-07-27: render the outcome so recall_deep matches the
                # pre-turn section (context.py _format_decisions already emits
                # "[outcome] desc"). Without this a superseded/failed decision
                # reached the LLM through this tool with NO status at all —
                # the metadata key alone had no consumer (branch-review P1-1).
                outcome = dec.metadata.get("outcome")
                # "pending" is the default (not-yet-reviewed) state and carries
                # no information, so it is suppressed here — every outcome the
                # reader acts on (superseded / noise / failure / success /
                # partial) still renders. This deliberately differs from the
                # pre-turn `_format_decisions`, which prefixes unconditionally;
                # suppressing the no-op label also keeps the F051 byte-identity
                # snapshot (whose fixtures are all pending) intact.
                outcome_str = f"[{outcome}] " if outcome and outcome != "pending" else ""
                results_text.append(
                    f"{i}. {outcome_str}{dec.description} | {category} | {stakes} | "
                    f"confidence: {confidence:.2f}{pattern_str} "
                    f"(id: {dec.id}{score_str})"
                )
                _emit(dec)
            for j, n in enumerate(brain_graph, len(decision_results) + 1):
                results_text.append(
                    f"{j}. [via graph: {n.edge_relation}] {n.description} "
                    f"(id: {n.id}, score: {n.score:.3f})"
                )
                _emit(n)
        elif not exemplar_rows:
            # Codex r14: same as Heart Memory above — no empty placeholder when
            # the examples block will render. Byte-identical without exemplars.
            results_text.append("\n=== Brain Decisions ===\nNo results found.")

    # ------------------------------------------------------------------
    # Contradiction warnings (F022 Phase 3)
    # ------------------------------------------------------------------
    for src_id, src_type, tgt_id, tgt_type in stats.contradiction_edges:
        results_text.append(
            f"\nWarning: Contradiction detected between "
            f"{src_type}({str(src_id)[:8]}) and "
            f"{tgt_type}({str(tgt_id)[:8]})"
        )

    # Codex r14: a classification-shaped recall can return ONLY exemplar hits
    # (the target path) — heart_results/decisions are then empty but the
    # dedicated examples block below WILL render, so "No results found." would
    # contradict it. Emit the no-results line only when there is genuinely
    # nothing to show, exemplar block included. When exemplar_rows is empty
    # (flag off / no exemplars) this is byte-identical to the old check.
    if not results_text and not exemplar_rows:
        results_text.append("No results found.")

    # F067 Phase 2: append parent episode summaries when provided. Skipped
    # when the caller opted out (parent_episodes is None or empty) — keeps
    # legacy output byte-identical.
    if parent_episodes:
        results_text.append("\n=== Parent Episode Context ===")
        for ep_id, summary in parent_episodes:
            results_text.append(f"- ({ep_id[:8]}) {summary}")

    # F086: ICL exemplar leg — nearest stored labeled examples. Kept out of
    # the Heart Memory section and framed as evidence the agent may
    # override (inform-not-force), per the F083 injection-precision lesson.
    if exemplar_rows:
        results_text.append("\n=== Nearest stored examples ===")
        results_text.append(
            "The stored examples most similar to the query, with their stored labels. "
            "Treat them as evidence for classification-style answers; you may override "
            "them if your own judgment clearly disagrees."
        )
        for i, r in enumerate(exemplar_rows, 1):
            sim = r.metadata.get("similarity")
            sim_s = f" [sim {sim:.2f}]" if isinstance(sim, (int, float)) else ""
            # Codex r16: truncate the UTTERANCE portion only, then ALWAYS append
            # the label line — a >500-char utterance must never lose its label to
            # the slice (the label is the whole point of an exemplar).
            desc = r.description or ""
            label = r.metadata.get("label")
            if label is None:
                label = parse_label(desc)
            if label is not None:
                utterance = desc.rsplit("\nlabel:", 1)[0]
                results_text.append(f"{i}.{sim_s} {utterance[:500]}\nlabel: {label}")
            else:
                results_text.append(f"{i}.{sim_s} {desc[:500]}")
            # NB: this section prints no "(id: …)", so a test that verifies
            # served-ness by regexing rendered ids is silently blind here.
            # Collection works because the id comes from the row, not the text.
            _emit(r)

    return "\n".join(results_text)


# ---------------------------------------------------------------------------
# Document ingest core (shared by the ingest_document tool + attachment pipeline)
# ---------------------------------------------------------------------------


async def ingest_document_text(
    heart: Heart,
    settings: Settings,
    *,
    content: str,
    source_ref: str,
    session_id: str | None = None,
    episode_id: str | None = None,
) -> dict[str, Any]:
    """Chunk + embed + persist document text to heart.episode_chunks.

    Extracted from the ingest_document tool. Returns a PLAIN dict:
      {"inserted": N, "source_ref": ..., "episode_id": ...}  on success
      {"error": "..."}                                        on failure
    Honors settings.document_ingest_enabled and the chunk-size settings.
    """
    from uuid import UUID as _UUID
    from sqlalchemy import text as _sql_text

    from nous.heart.document_chunker import chunk_document

    if not settings.document_ingest_enabled:
        return {"code": "disabled", "error": "ingest_document is disabled (NOUS_DOCUMENT_INGEST_ENABLED=false)."}
    if not content or not content.strip():
        return {"code": "empty_content", "error": "content is empty."}
    if not source_ref or not source_ref.strip():
        return {"code": "no_source_ref", "error": "source_ref is required (URL or file path)."}

    try:
        # 1. Resolve target episode.
        target_episode_id: _UUID
        if episode_id:
            try:
                target_episode_id = _UUID(episode_id)
            except ValueError:
                return {"code": "bad_uuid", "error": f"episode_id must be a UUID, got {episode_id!r}."}
        else:
            if not session_id:
                return {"code": "no_session", "error": "no episode_id provided and no active session — pass episode_id explicitly."}
            async with heart.db.session() as session:
                row = await session.execute(
                    _sql_text(
                        "SELECT id FROM heart.episodes "
                        "WHERE agent_id = :a AND session_id = :s AND active = true "
                        "ORDER BY started_at DESC LIMIT 1"
                    ),
                    {"a": heart.agent_id, "s": session_id},
                )
                found = row.scalar()
            if not found:
                return {"code": "no_episode", "error": f"no active episode for session {session_id}; pass episode_id explicitly."}
            target_episode_id = found

        # 2. Chunk the document.
        chunks = chunk_document(
            content,
            target_size=settings.document_chunk_size,
            overlap=settings.document_chunk_overlap,
            min_chars=settings.document_chunk_min_chars,
        )
        if not chunks:
            return {"code": "too_short", "error": (
                f"Ingest skipped: content shorter than min_chars ({settings.document_chunk_min_chars})."
            )}

        # 3. Embed in one batch (cheaper than per-chunk).
        try:
            embeddings = await heart._embeddings.embed_batch(chunks)
        except Exception as exc:
            logger.exception("ingest_document_text: embed_batch failed")
            return {"code": "embed_failed", "error": f"Error embedding chunks: {exc}"}

        # Codex P2 round 3 (2026-05-26): guard against embedder returning
        # fewer vectors than chunks. zip() would silently truncate and
        # drop tail chunks. Mirrors the F067 writer at
        # handlers/episode_summarizer.py:205-211.
        if len(embeddings) != len(chunks):
            logger.warning(
                "ingest_document_text: embedder returned %d vectors for %d chunks "
                "(episode=%s, source_ref=%s); aborting to avoid partial ingest",
                len(embeddings), len(chunks), target_episode_id, source_ref,
            )
            return {"code": "vector_mismatch", "error": (
                f"embedder returned {len(embeddings)} vectors "
                f"for {len(chunks)} chunks; refusing to write a "
                f"partial ingest. Retry."
            )}

        # 4. Atomic idempotency + insert.
        #
        # Lock is keyed on episode_id ONLY (Codex P1 round 4, 2026-05-26),
        # NOT (episode_id, source_ref). Reason: chunk_index is allocated
        # per-episode via MAX(chunk_index)+1. If two concurrent ingests
        # for the same episode but DIFFERENT source_refs took separate
        # locks, they could both pick the same start_idx, both INSERT,
        # and ON CONFLICT (episode_id, chunk_index) DO NOTHING would
        # silently drop one caller's rows — silent data loss.
        #
        # By locking at episode scope, both concurrent ingests serialize
        # so their start_idx allocations are disjoint. The idempotency
        # COUNT (filtered by source_ref) still works correctly inside
        # the same transaction.
        #
        # Single transaction guarded by pg_advisory_xact_lock; lock
        # auto-releases on COMMIT/ROLLBACK. Vector literal format
        # mirrors handlers/episode_summarizer.py:219.
        lock_key = f"ingest_document:{target_episode_id}"
        async with heart.db.session() as session:
            # Advisory lock — released at COMMIT / ROLLBACK.
            await session.execute(
                _sql_text(
                    "SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"
                ),
                {"k": lock_key},
            )

            existing_row = await session.execute(
                _sql_text(
                    "SELECT COUNT(*) AS cnt FROM heart.episode_chunks "
                    "WHERE agent_id = :a AND episode_id = :e "
                    "  AND source_ref = :r"
                ),
                {
                    "a": heart.agent_id,
                    "e": target_episode_id,
                    "r": source_ref,
                },
            )
            existing_cnt = int(existing_row.scalar() or 0)

            if existing_cnt > 0:
                # Release the lock by committing (no writes happened).
                await session.commit()
                return {
                    "already_ingested": True,
                    "inserted": 0,
                    "existing": existing_cnt,
                    "source_ref": source_ref,
                    "episode_id": str(target_episode_id),
                }

            # Determine starting chunk_index: append after any existing
            # chunks for this episode so dialogue chunks (F067) +
            # document chunks (different source_ref) under the same
            # episode do not collide.
            next_idx_row = await session.execute(
                _sql_text(
                    "SELECT COALESCE(MAX(chunk_index), -1) + 1 AS next_idx "
                    "FROM heart.episode_chunks "
                    "WHERE agent_id = :a AND episode_id = :e"
                ),
                {"a": heart.agent_id, "e": target_episode_id},
            )
            start_idx = int(next_idx_row.scalar() or 0)

            inserted = 0
            for offset, (chunk_text, emb) in enumerate(zip(chunks, embeddings)):
                if not emb:
                    # Defensive — embed_batch should never return None,
                    # but a single bad vector should not crash the batch.
                    continue
                vec_lit = "[" + ",".join(f"{v:.6f}" for v in emb) + "]"
                await session.execute(
                    _sql_text(
                        "INSERT INTO heart.episode_chunks "
                        "(agent_id, episode_id, chunk_index, content, embedding, "
                        " source_kind, source_ref) "
                        "VALUES (:a, :e, :i, :c, CAST(:emb AS vector), "
                        " 'document', :ref) "
                        "ON CONFLICT (episode_id, chunk_index) DO NOTHING"
                    ),
                    {
                        "a": heart.agent_id,
                        "e": str(target_episode_id),
                        "i": start_idx + offset,
                        "c": chunk_text,
                        "emb": vec_lit,
                        "ref": source_ref,
                    },
                )
                inserted += 1
            await session.commit()

        return {
            "inserted": inserted,
            "start_index": start_idx,
            "source_ref": source_ref,
            "episode_id": str(target_episode_id),
        }

    except Exception as e:
        logger.exception("ingest_document_text failed")
        return {"code": "exception", "error": f"Error ingesting document: {e}"}


# ---------------------------------------------------------------------------
# Nous memory tool closures
# ---------------------------------------------------------------------------


def create_nous_tools(brain: Brain, heart: Heart, settings: Settings | None = None) -> dict[str, Any]:
    """Create tool closures with Brain and Heart captured in closure context.

    Returns a dict of async callables suitable for ToolDispatcher registration.
    Each closure takes tool parameters, calls Brain/Heart methods, and returns
    MCP-compliant response: {"content": [{"type": "text", "text": "..."}]}.

    All tools are wrapped in try/except to return error messages as tool results.
    """
    if settings is None:
        settings = Settings()

    async def record_decision(
        description: str,
        confidence: float,
        category: str,
        stakes: str,
        context: str | None = None,
        pattern: str | None = None,
        tags: list[str] | None = None,
        reasons: list[dict[str, str]] | None = None,
        _session_id: str | None = None,
    ) -> dict[str, Any]:
        """Record a decision to the Brain.

        Args:
            description: What was decided
            confidence: 0.0-1.0 confidence level
            category: architecture, process, tooling, security, or integration
            stakes: low, medium, high, or critical
            context: Situation and constraints
            pattern: Abstract pattern this decision represents
            tags: Keywords for filtering
            reasons: List of {type, text} dicts (type: analysis, pattern, empirical, etc.)

        Returns:
            MCP-compliant response with decision ID or error message
        """
        try:
            # Validate and construct input
            reason_inputs = []
            if reasons:
                for r in reasons:
                    if not isinstance(r, dict) or "type" not in r or "text" not in r:
                        return _tool_error(
                            "Error: Invalid reason format. Expected dict with "
                            f"'type' and 'text', got: {r}"
                        )
                    reason_inputs.append(ReasonInput(type=r["type"], text=r["text"]))

            input_data = RecordInput(
                description=description,
                confidence=confidence,
                category=category,  # type: ignore
                stakes=stakes,  # type: ignore
                context=context,
                pattern=pattern,
                tags=tags or [],
                reasons=reason_inputs,
                # Flag-gated: see Settings.decision_session_id_enabled. Off by
                # default, so this stays NULL exactly as it is in prod today.
                session_id=(
                    _session_id
                    if settings.decision_session_id_enabled
                    else None
                ),
            )

            # Record to Brain
            result = await brain.record(input_data)

            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Decision recorded successfully.\n"
                            f"ID: {result.id}\n"
                            f"Quality score: {result.quality_score:.2f}\n"
                            f"Category: {result.category}\n"
                            f"Stakes: {result.stakes}"
                        ),
                    }
                ]
            }

        except Exception as e:
            logger.exception("record_decision tool failed")
            return _tool_error(f"Error recording decision: {e}")

    async def learn_fact(
        content: str,
        category: str | None = None,
        subject: str | None = None,
        confidence: float = 1.0,
        source: str | None = None,
        source_episode_id: str | None = None,
        source_decision_id: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Store a fact in the Heart.

        Args:
            content: The fact content
            category: preference, technical, person, tool, concept, or rule.
                person/preference/rule are RESERVED for durable facts about the
                user (identity, stable preferences, standing directives) — they
                load into every future prompt. Session events, dated one-offs,
                and task detail use technical/concept instead.
            subject: What/who the fact is about
            confidence: 0.0-1.0 confidence level
            source: Where this fact came from
            source_episode_id: Episode UUID if learned during episode
            source_decision_id: Decision UUID if learned during decision
            tags: Keywords for filtering

        Returns:
            MCP-compliant response with fact ID or error message
        """
        try:
            # Parse UUIDs if provided
            episode_uuid = UUID(source_episode_id) if source_episode_id else None
            decision_uuid = UUID(source_decision_id) if source_decision_id else None

            input_data = FactInput(
                content=content,
                category=category,
                subject=subject,
                confidence=confidence,
                source="user_direct",  # F023/F038: +0.15 admission bonus for user tool calls
                source_episode_id=episode_uuid,
                source_decision_id=decision_uuid,
                tags=tags or [],
            )

            # Store to Heart
            result = await heart.learn(input_data)

            # F023/F038: Handle rejected facts — user_direct gets +0.15 bonus (F038-2.4)
            # but very low-quality or short content can still be rejected
            if isinstance(result, FactRejected):
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"Fact not stored (admission score {result.composite_score:.2f} "
                                f"< {result.threshold} threshold).\n"
                                f"Scores: {', '.join(f'{k}={v:.2f}' for k, v in result.scores.items())}\n"
                                f"Override with explicit instruction if this should be stored."
                            ),
                        }
                    ]
                }

            warning_msg = ""
            if result.contradiction_warning:
                warning_msg = (
                    f"\nPotential contradiction detected:\n"
                    f"Existing fact: {result.contradiction_warning.existing_content}\n"
                    f"Similarity: {result.contradiction_warning.similarity:.2f}"
                )

            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Fact learned successfully.\n"
                            f"ID: {result.id}\n"
                            f"Category: {result.category or 'none'}\n"
                            f"Subject: {result.subject or 'none'}"
                            f"{warning_msg}"
                        ),
                    }
                ]
            }

        except ValueError as e:
            # UUID parsing error or validation error
            return _tool_error(f"Validation error: {e}")
        except Exception as e:
            logger.exception("learn_fact tool failed")
            return _tool_error(f"Error learning fact: {e}")

    async def recall_deep(  # noqa: C901
        query: str,
        limit: int = 10,
        memory_types: list[str] | None = None,
        _session_id: str | None = None,
        _turn_number: int | None = None,
    ) -> dict[str, Any]:
        """Search memory in Heart and Brain.

        Thin wrapper around ``run_recall_pipeline`` (F051 refactor): the
        pipeline runs the full retrieval stack and returns structured
        results; this closure formats them into the legacy text shape.

        Args:
            query: Search query string
            limit: Result cap for the CORE legs — Heart recall and Brain
                decision query. The combined list can exceed it (audit B7),
                and legs that own their own allotment setting ignore it
                entirely: chunks use episode_chunk_recall_limit, keyed uses
                keyed_fact_leg_k, exemplars use exemplar_top_k. Lowering this
                does NOT proportionally shrink those legs.
            memory_types: Types to search (episode, fact, procedure, censor, decision)
                If None or contains "all": with coherent ranking enabled
                (F080, default ON) the default pool is knowledge-only —
                episodes, facts, chunks, decisions. Procedures and censors
                are EXCLUDED from the default pool (procedures are served
                by the catalog/§14 selection + get_procedure; censors by
                the always-on Active Censors section). Pass an explicit
                memory_types list containing "procedure" or "censor" to
                search those types here (audit R2).
            _session_id: F051.4/F055 — session identifier injected by
                ``ToolDispatcher.dispatch``. When F055 (Cross-Turn Residual
                Activation) ships, the residual_activations path reads this
                to bias recall toward recently-surfaced items in the same
                session. Pre-F055 the kwarg is silently accepted and ignored
                (fail-open). Underscore prefix marks it as
                infrastructure-injected, not a tool-schema-declared parameter.

        Returns:
            MCP-compliant response with ranked results or error message
        """
        from nous.api.retrieval_pipeline import run_recall_pipeline

        # F071: lazy import to avoid runner.py <-> tools.py circular dependency.
        # Mirrors the existing `from nous.api.runner import FRAME_TOOLS`
        # pattern used elsewhere in this module. Returns None when the feature
        # flag is off or no turn is active (e.g. F051 eval harness), and the
        # pipeline's `if exclude_ids:` short-circuit keeps output byte-identical.
        # Deferred import (same as F071 below): runner imports tools, so a
        # module-level import here would be circular.
        from nous.api.runner import CURRENT_TURN_EXCLUDE_IDS
        _f071_exclude_ids = CURRENT_TURN_EXCLUDE_IDS.get()

        try:
            search_types = memory_types or ["all"]

            # F055: compute residual activations BEFORE recall (consumed
            # inside Heart._recall via the threaded `residual_activations`
            # kwarg). Skipped when feature flag off, no session_id, or
            # no activator wired (test fixtures, smoke). Fail-open: any
            # exception falls through to cold recall.
            residual_activations: dict[UUID, float] = {}
            current_turn = 0
            if (
                getattr(settings, "residual_activation_enabled", False)
                and _session_id
                and getattr(heart, "_residual_activator", None) is not None
            ):
                try:
                    activator = heart._residual_activator
                    current_turn = await activator.current_turn(brain.agent_id, _session_id)
                    residual_activations = await activator.compute_activations(
                        brain.agent_id, _session_id, current_turn,
                    )
                except Exception:
                    logger.warning(
                        "F055: compute_activations failed for %s/%s, continuing cold",
                        brain.agent_id, _session_id, exc_info=True,
                    )
                    residual_activations = {}

            # F067 fix: when episode chunks are enabled AND will actually be
            # fetched, rerank by score so chunks (appended after the fact
            # stage in pipeline order) can reach the top-K consumer. Without
            # this, chunks always sit at positions 11+ even when their
            # cosine score beats the surfaced facts — making the chunk-
            # recall leg silently dead in production.
            #
            # Codex P2 gate (PR #443): chunks only fetch when
            # ``episode_chunks_enabled AND (search_all OR "fact" in
            # search_types)`` — see retrieval_pipeline.py:301. Mirror that
            # exact condition so non-chunk recall paths (e.g.
            # memory_types=["decision"]) keep legacy stage-order output
            # even with the feature flag on.
            search_all_for_rerank = "all" in search_types
            chunks_rerank = (
                getattr(settings, "episode_chunks_enabled", False)
                and (search_all_for_rerank or "fact" in search_types)
            )
            # F091: open a telemetry trace for this retrieval. NULL_TRACE when
            # the feature is off, so the pipeline call below is unchanged.
            _tr_committed = False  # F091: guards against a second insert
            _rl = get_active_retrieval_logger()
            _tr = (
                _rl.start(
                    query=query, path="pipeline", session_id=_session_id,
                    # None outside a tool loop (eval harness, scripts), which is
                    # honest — there is no turn to attribute those to.
                    turn_number=_turn_number,
                )
                if _rl is not None else None
            )
            results, stats = await run_recall_pipeline(
                query=query,
                heart=heart,
                brain=brain,
                settings=settings,
                limit=limit,
                memory_types=memory_types,
                residual_activations=residual_activations or None,  # F055
                rerank_by_score=chunks_rerank,
                exclude_ids=_f071_exclude_ids,  # F071
                trace=_tr,  # F091
            )
            # NOTE: the trace is committed further down, AFTER the
            # parent-episode fetch — those summaries reach the model, so
            # committing here would leave them unrepresented in the counts.
            # F067 observability: one INFO line per recall_deep call so
            # operators can grep for chunk surfacing in prod without
            # turning on F055 residual_activation. Logs the gate state
            # (chunks_searched), how many chunks reach the top-of-list,
            # and where the first chunk lands in the global result order
            # so we can spot "chunks retrieved but buried" cases.
            n_chunks_total = sum(
                1 for r in results if getattr(r, "type", None) == "chunk"
            )
            n_chunks_top10 = sum(
                1 for r in results[:10] if getattr(r, "type", None) == "chunk"
            )
            first_chunk_rank = next(
                (i + 1 for i, r in enumerate(results)
                 if getattr(r, "type", None) == "chunk"),
                None,
            )
            logger.info(
                "recall_deep agent=%s query_chars=%d limit=%d "
                "chunks_enabled=%s chunks_searched=%s "
                "n_chunks_total=%d n_chunks_top10=%d first_chunk_rank=%s "
                "excluded_in_context=%d "  # F071
                "n_total=%d "
                "n_keyed_r2=%d keyed_r2_truncated=%s "  # R3v2
                "exemplar_leg_used=%s n_exemplar=%d",  # F086
                brain.agent_id,
                len(query or ""),
                limit,
                getattr(settings, "episode_chunks_enabled", False),
                stats.chunks_searched,
                n_chunks_total,
                n_chunks_top10,
                first_chunk_rank if first_chunk_rank is not None else "n/a",
                stats.excluded_in_context,  # F071
                len(results),
                stats.n_keyed_r2,  # R3v2
                stats.keyed_r2_truncated,  # R3v2
                stats.exemplar_leg_used,  # F086
                stats.n_exemplar,  # F086
            )
            # F067 Phase 2: optionally fetch parent episode summaries for
            # facts in the result set. Failures are non-fatal — the formatter
            # falls back to legacy output when parent_episodes is empty.
            parent_episodes: list[tuple[str, str]] = []
            if getattr(settings, "recall_include_parent_episodes", False):
                try:
                    parent_episodes = await _fetch_parent_episodes_for_facts(
                        heart=heart,
                        results=results,
                        max_parents=settings.recall_max_parent_episodes,
                        truncate=settings.recall_parent_episode_truncate,
                    )
                except Exception:
                    n_facts = sum(
                        1 for r in results if getattr(r, "type", None) == "fact"
                    )
                    logger.warning(
                        "F067: parent-episode fetch failed for agent=%s "
                        "(n_facts=%d, falling back to legacy recall_deep "
                        "output)",
                        heart.agent_id, n_facts, exc_info=True,
                    )
                    parent_episodes = []
            # F091: parent episodes are memory DELIVERED to the model, appended
            # by the formatter below. Register them as their own leg and mark
            # them rendered, then commit — committing before this fetch left
            # them absent from n_candidates/n_rendered on the one retrieval
            # path where the flag makes them appear.
            if _tr is not None and parent_episodes:
                for _rank, (_ep_id, _summary) in enumerate(parent_episodes):
                    _tr.add(_ep_id, "episode", "parent_episode",
                            rank=_rank + 1, content=_summary)
                    _tr.mark_rendered(_ep_id, "episode", "parent_episode_section")
                _tr.leg("parent_episode", attempted=True,
                        n_returned=len(parent_episodes))
            # Format FIRST, commit after. `rendered` means the text reached the
            # model, and until the formatter returns, no text exists — a raise
            # here with the trace already persisted would claim every survivor
            # was delivered while the tool returned only an error. Committing
            # first also let the exception handler attempt a SECOND insert on
            # the same trace id, which merely collided on the primary key.
            _emitted: list[tuple[Any, str]] | None = [] if _tr is not None else None
            text = _format_pipeline_text(
                results, stats, search_types, parent_episodes=parent_episodes,
                session_group_heart=getattr(settings, "session_group_heart_section", False),
                emitted_out=_emitted,
            )
            # F091: attribute the formatter's scope filter from what it REPORTED
            # emitting, not from a re-derivation of its section rules.
            #
            # The block this replaces predicted eligibility: it fired only when
            # `decision` was absent from `search_types` and covered only
            # `source=="brain"` / `stage_origin=="brain_graph"`. That was
            # incomplete — under `search_types=["decision"]` a
            # `heart_graph_memory` row is dropped by the formatter and still
            # read `rendered` — and an earlier revision of the same block had
            # the error inverted, downgrading rows that DID reach the model.
            # Two failures in opposite directions from one predicate is the
            # signature of predicting a rule instead of asking its owner.
            #
            # Set semantics, not a count: the same decision legitimately renders
            # in BOTH the Graph-Connected and Brain sections (Stage 2 and Stage 3
            # do not cross-dedup), so `_emitted` may hold a duplicate pair.
            if _tr is not None and _emitted is not None:
                _emitted_keys = set(_emitted)
                # The parent-episode section (:1540-1550) renders UNCONDITIONALLY
                # and is marked rendered above, but its ids are not collected —
                # they arrive via `parent_episodes`, not `results`. An episode
                # that is in BOTH (e.g. reached through heart_graph_memory on a
                # decision-only recall) would otherwise be downgraded here after
                # its summary genuinely reached the model, re-creating in a new
                # place exactly the false-negative this block was written to end.
                _parent_ids = {str(_e) for _e, _ in (parent_episodes or ())}
                for _r in results:
                    if (_r.id, _r.type) in _emitted_keys:
                        continue
                    if _r.type == "episode" and str(_r.id) in _parent_ids:
                        continue
                    _tr.mark_not_delivered(
                        _r.id, _r.type, SLICED_OFF, "formatter_scope_filter",
                    )
            if _rl is not None and _tr is not None:
                try:
                    _rl.commit(_tr)
                    _tr_committed = True
                except Exception:
                    logger.debug("F091: retrieval trace commit failed", exc_info=True)

            # F055: fire-and-forget record_surfaced so the request returns
            # immediately. record_surfaced opens its OWN DB session inside
            # the WorkingMemoryManager helper (spec §3 fix #2 — must NOT
            # reuse the caller's AsyncSession across asyncio.create_task).
            if (
                results
                and getattr(settings, "residual_activation_enabled", False)
                and _session_id
                and getattr(heart, "_residual_activator", None) is not None
            ):
                try:
                    import asyncio
                    surfaced = [
                        (
                            r.id,
                            r.type,
                            float(r.score) if r.score is not None else 0.0,
                            (r.description or "")[:160],  # audit E2: real summary
                        )
                        for r in results
                    ]
                    asyncio.create_task(
                        heart._residual_activator.record_surfaced(
                            agent_id=brain.agent_id,
                            session_id=_session_id,
                            current_turn=current_turn + 1,  # this turn becomes turn N+1
                            surfaced=surfaced,
                        )
                    )
                except Exception:
                    logger.warning("F055: failed to schedule record_surfaced", exc_info=True)

            return {"content": [{"type": "text", "text": text}]}
        except Exception as e:
            logger.exception("recall_deep tool failed")
            # F091: commit the PARTIAL trace. The only commit sits on the happy
            # path, so an exception escaping run_recall_pipeline left no row at
            # all — hiding exactly the failures the leg `error` field exists to
            # diagnose, and making a crashed retrieval look like one that never
            # happened. Whatever legs/candidates were recorded before the raise
            # are worth more than nothing.
            try:
                _rl_err = get_active_retrieval_logger()
                _tr_err = locals().get("_tr")
                # Only if the happy path did not already commit — a second
                # commit on the same trace id is a duplicate insert that merely
                # collides on the primary key.
                if (
                    _rl_err is not None and _tr_err is not None
                    and not locals().get("_tr_committed", False)
                ):
                    # Nothing was returned to the model, so anything finalize
                    # had already marked `rendered` (e.g. a formatter raise
                    # after the pipeline finished) must be un-claimed before
                    # this row is persisted.
                    _tr_err.undeliver_all(SLICED_OFF, "recall_deep_failed")
                    _tr_err.leg("recall_deep", attempted=True, error=str(e)[:200])
                    _tr_err.finalize([])
                    _rl_err.commit(_tr_err)
            except Exception:
                logger.debug("F091: failed to commit partial trace", exc_info=True)
            return _tool_error(f"Error searching memory: {e}")

    async def create_censor(
        trigger_pattern: str,
        reason: str,
        action: str = "steer",
        domain: str | None = None,
        learned_from_decision: str | None = None,
        learned_from_episode: str | None = None,
    ) -> dict[str, Any]:
        """Create a guardrail censor in the Heart.

        Args:
            trigger_pattern: Pattern to match (substring or regex)
            reason: Why this censor exists
            action: steer (advisory, default), refuse, or abort. Agent-created
                censors are capped at refuse (provenance="agent").
            domain: Domain this censor applies to (architecture, debugging, etc.)
            learned_from_decision: Decision UUID that triggered this censor
            learned_from_episode: Episode UUID that triggered this censor

        Returns:
            MCP-compliant response with censor ID or error message
        """
        try:
            # F078: validate action vocabulary before constructing the input.
            if action not in ("steer", "refuse", "abort"):
                return _tool_error(
                    f"Invalid action {action!r}; must be one of "
                    "steer, refuse, abort."
                )

            # Parse UUIDs if provided
            decision_uuid = UUID(learned_from_decision) if learned_from_decision else None
            episode_uuid = UUID(learned_from_episode) if learned_from_episode else None

            input_data = CensorInput(
                trigger_pattern=trigger_pattern,
                reason=reason,
                action=action,  # type: ignore
                domain=domain,
                learned_from_decision=decision_uuid,
                learned_from_episode=episode_uuid,
                # F078: agent provenance -> _add caps tier at refuse.
                provenance="agent",
            )

            # Create censor
            result = await heart.add_censor(input_data)

            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Censor created successfully.\n"
                            f"ID: {result.id}\n"
                            f"Action: {result.action}\n"
                            f"Domain: {result.domain or 'all'}\n"
                            f"Pattern: {result.trigger_pattern}"
                        ),
                    }
                ]
            }

        except ValueError as e:
            # UUID parsing error or validation error
            return _tool_error(f"Validation error: {e}")
        except Exception as e:
            logger.exception("create_censor tool failed")
            return _tool_error(f"Error creating censor: {e}")

    async def recall_recent(
        hours: int = 48,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Recall recent episodes by time, not topic similarity.

        Use this when the user asks "what did we talk about", "what happened
        recently", or you need a comprehensive overview of recent activity.

        Args:
            hours: Look back this many hours (default 48)
            limit: Maximum episodes to return (default 10)

        Returns:
            MCP-compliant response with time-ordered episode list
        """
        try:
            episodes = await heart.list_episodes(limit=limit, hours=hours)

            if not episodes:
                return {"content": [{"type": "text", "text": f"No episodes found in the last {hours} hours."}]}

            lines = [f"Recent episodes (last {hours}h):"]
            for e in episodes:
                # Prefer the summarizer's output over the legacy columns —
                # mirrors recall_deep's COALESCE(structured_summary->>'summary',
                # summary). For un-summarized episodes the legacy `summary` is
                # the raw creation-time echo (often the first user message);
                # the title-line marker keeps it from masquerading as a
                # produced summary (short echoes BECOME the title, so a
                # body-line suffix would not always be visible).
                structured = e.structured_summary or {}
                body = structured.get("summary") or e.summary
                title = structured.get("title") or e.title or (body[:60] if body else "Untitled")
                marker = "" if structured.get("summary") else " (unsummarized)"
                time_str = e.started_at.strftime("%b %d %H:%M")
                lines.append(f"- [{time_str}] {title}{marker}")
                if body and body != title:
                    lines.append(f"  {body[:150]}")

            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        except Exception as e:
            logger.exception("recall_recent tool failed")
            return _tool_error(f"Error fetching recent episodes: {e}")

    # F011: learn_skill tool — register skills from URL, local path, or inline markdown
    _skill_parser = SkillParser()

    async def learn_skill(
        source: str,
        content: str | None = None,
    ) -> dict[str, Any]:
        """Register a skill from a URL, local path, or raw markdown.

        Args:
            source: URL, local file path, or 'inline' for raw content
            content: Raw SKILL.md markdown when source is 'inline'

        Returns:
            MCP-compliant response with skill registration result
        """
        try:
            # 1. Fetch content
            if source == "inline":
                if not content:
                    return _tool_error("Error: 'content' is required when source is 'inline'")
                markdown = content
            elif source.startswith(("http://", "https://")):
                # Fetch from URL
                import httpx as _httpx
                async with _httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(source)
                    resp.raise_for_status()
                    markdown = resp.text
            else:
                # Local file path
                import os
                workspace = settings.workspace_dir if settings else "."
                path = os.path.join(workspace, source) if not os.path.isabs(source) else source
                if not os.path.exists(path):
                    return _tool_error(f"Error: file not found: {path}")
                with open(path, encoding="utf-8") as f:
                    markdown = f.read()

            # 2. Parse
            manifest = _skill_parser.parse(markdown, source_hint=source)

            # 2b. Check requires (env var validation)
            import os as _os
            missing_requires = [var for var in manifest.requires if not _os.environ.get(var)]
            skill_active = len(missing_requires) == 0

            # 3. Check for existing procedure with same name (dedup, case-insensitive)
            existing = await heart.get_procedure_by_name(manifest.name)
            updated = existing is not None

            # 4. Convert to ProcedureInput and store / update in place
            proc_input = _skill_parser.to_procedure_input(manifest)
            proc_input.active = skill_active
            if existing:
                # Update in place to preserve activation/success/failure counts and
                # task affinity (retire+store reset them — audit bug 9).
                result = await heart.update_procedure_body(existing.id, proc_input)
            else:
                result = await heart.store_procedure(proc_input)

            action = "updated" if updated else "registered"
            if not skill_active:
                active_str = f"inactive (missing: {', '.join(missing_requires)})"
            else:
                active_str = "active"
            warnings_text = ""
            if manifest.warnings:
                warnings_text = "\nWarnings:\n" + "\n".join(f"  - {w}" for w in manifest.warnings)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Skill {action} successfully.\n"
                            f"Name: {manifest.name}\n"
                            f"ID: {result.id}\n"
                            f"Domain: {manifest.domain}\n"
                            f"Status: {active_str}\n"
                            f"Triggers: {', '.join(manifest.triggers) if manifest.triggers else 'none'}"
                            f"{warnings_text}"
                        ),
                    }
                ]
            }

        except ValueError as e:
            return _tool_error(f"Parse error: {e}")
        except Exception as e:
            logger.exception("learn_skill tool failed")
            return _tool_error(f"Error learning skill: {e}")

    async def get_procedure(
        procedure_id: str,
    ) -> dict[str, Any]:
        """Fetch a procedure/skill's full body by name or UUID.

        Use after seeing a procedure in the Procedure Catalog (or a recall_deep
        result) to load its full steps before acting. Accepts either the
        procedure's name (as shown in the catalog, preferred) or its UUID.

        Args:
            procedure_id: the procedure's name (preferred) or UUID

        Returns:
            MCP-compliant response with full procedure details
        """
        try:
            from uuid import UUID as _UUID
            try:
                pid: _UUID | None = _UUID(procedure_id)
            except ValueError:
                pid = None  # not a UUID -> treat as a name (catalog-first depth path)
            detail = None
            if pid is not None:
                try:
                    detail = await heart.get_procedure(pid)
                except ValueError:
                    detail = None  # well-formed UUID but no such row
                if detail is None:
                    # The id missed — but a procedure's NAME can itself be a UUID string
                    # (catalog lists names), so retry the exact input as a name.
                    detail = await heart.get_procedure_by_name(procedure_id)
            else:
                detail = await heart.get_procedure_by_name(procedure_id)
            if detail is None:
                return {"content": [{"type": "text",
                                     "text": f"No procedure found for '{procedure_id}'."}]}

            lines = [
                f"**{detail.name}** ({detail.domain or 'general'})",
            ]
            if detail.description:
                lines.append(f"Description: {detail.description}")
            if detail.goals:
                lines.append(f"Triggers: {', '.join(detail.goals)}")
            if detail.core_tools:
                lines.append(f"Tools: {', '.join(detail.core_tools)}")
            if detail.core_patterns:
                lines.append("")
                lines.append("Core patterns:")
                for pat in detail.core_patterns:
                    lines.append(f"- {pat}")
            if detail.implementation_notes:
                lines.append("")
                lines.append("Implementation notes:")
                for note in detail.implementation_notes:
                    lines.append(note)
            lines.append(f"\nActivated: {detail.activation_count}x | Status: {'active' if detail.active else 'inactive'}")
            if detail.effectiveness is not None:
                lines.append(f"Effectiveness: {detail.effectiveness:.0%}")

            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        except Exception as e:
            logger.exception("get_procedure tool failed")
            return _tool_error(f"Error fetching procedure: {e}")

    async def recall_hubs(
        limit: int = 10,
        node_type: str | None = None,
    ) -> dict[str, Any]:
        """F065: return the most-connected (highest-degree) nodes in the graph.

        Args:
            limit: Top-N to return (1..50, default 10).
            node_type: Optional filter ('decision' | 'fact' | 'episode' | 'procedure').

        Returns:
            MCP-compliant response with a labeled list of hubs, their degree,
            and a per-tier provenance breakdown.
        """
        try:
            hubs = await brain.top_hubs(limit=max(1, min(50, limit)), node_type=node_type)
            if not hubs:
                return {"content": [{"type": "text", "text": "No graph hubs found yet — the graph may be empty."}]}

            lines = [f"Top-{len(hubs)} hubs by degree:"]
            for i, h in enumerate(hubs, start=1):
                bk = h["extraction_method_breakdown"]
                lines.append(
                    f"{i}. [{h['node_type']}] {h['label'][:80]} — "
                    f"degree {h['degree']} "
                    f"(det={bk['deterministic']}, heu={bk['heuristic']}, inf={bk['inferred']})"
                )
            return {"content": [{"type": "text", "text": "\n".join(lines)}]}
        except Exception as e:
            logger.exception("recall_hubs tool failed")
            return _tool_error(f"Error fetching hubs: {e}")

    async def ingest_document(
        content: str,
        source_ref: str,
        episode_id: str | None = None,
        _session_id: str | None = None,
    ) -> dict[str, Any]:
        """F069: chunk and persist a full document body to heart.episode_chunks.

        Unlike F067 transcript chunking (600-char dialogue window), this uses
        the document_chunker (1500-char structure-aware split, 200-char
        overlap). The handler writes chunks directly to the DB at full
        fidelity, so conversation-history soft-trim does not lose content.

        Args:
            content: Full document text. The agent extracts this itself
                (e.g. via run_python + pypdf for PDFs, python-docx for
                .docx, plain str for arxiv HTML). Must be >= configured
                document_chunk_min_chars.
            source_ref: URL or workspace path the content came from.
                Stored on every chunk for traceability.
            episode_id: Optional explicit episode UUID. If omitted, the
                handler attaches chunks to the active episode of the
                current session.
            _session_id: Auto-injected by ToolDispatcher; used to resolve
                the active episode when episode_id is not provided.
        """
        # Thin wrapper over the module-level ingest_document_text helper.
        # The helper returns a PLAIN dict; here we adapt it back into the
        # exact MCP content-block shape (and exact text) this tool has
        # always returned, so the LLM-facing contract is byte-identical.
        result = await ingest_document_text(
            heart,
            settings,
            content=content,
            source_ref=source_ref,
            session_id=_session_id,
            episode_id=episode_id,
        )

        def _block(text: str) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": text}]}

        if "error" in result:
            code = result.get("code")
            # Ingestion failed, so this must be FLAGGED, not just worded like
            # an error -- _block returns an unflagged envelope, which dispatch
            # would report to the model as a successful ingest of nothing.
            # These messages historically carried an "Error: " prefix; the
            # helper drops it so callers get clean text. Re-add it here.
            if code in ("empty_content", "no_source_ref", "bad_uuid",
                        "no_session", "no_episode", "vector_mismatch"):
                return _tool_error(f"Error: {result['error']}")
            # disabled / too_short / embed_failed / exception are emitted
            # verbatim (they never had the prefix).
            return _tool_error(result["error"])

        if result.get("already_ingested"):
            return _block(
                f"Already ingested: {result['existing']} chunks exist for "
                f"source_ref={result['source_ref']!r} under episode "
                f"{result['episode_id']}. Use a different source_ref "
                f"or delete existing rows to re-ingest."
            )

        return _block(
            f"Ingested {result['inserted']} chunks from {result['source_ref']!r} "
            f"into episode {result['episode_id']} "
            f"(start_index={result['start_index']}, source_kind=document, "
            f"target_size={settings.document_chunk_size})."
        )

    # ------------------------------------------------------------------
    # Decision resolution (closes the calibration loop). resolve_decision /
    # resolve_decisions persist an outcome on an existing decision via the
    # pre-existing Brain.review(); list_decisions surfaces the pending set so
    # a sweep can enumerate what to resolve. Background turns (heartbeat /
    # subtask, dispatched with _is_background=True) are HARD-BLOCKED so an
    # autopilot tick can't self-resolve its own noise without interactive
    # reasoning. Cite: live Nous decision 06d62894 / FORGE a22f4ccc.
    # ------------------------------------------------------------------
    _BG_BLOCK_MSG = (
        "Error: decision resolution is blocked in background/heartbeat turns. "
        "Resolving a decision requires an interactive reasoning session."
    )

    async def resolve_decision(
        decision_id: str,
        outcome: str,
        resolution_note: str | None = None,
        superseded_by: str | None = None,
        _is_background: bool = False,
    ) -> dict[str, Any]:
        """Persist an outcome on an existing decision.

        Args:
            decision_id: UUID of the decision to resolve.
            outcome: success, partial, failure, noise, or superseded.
                noise/superseded are excluded from calibration.
            resolution_note: Evidence/why — the resolution trail.
            superseded_by: UUID of the replacing decision. Required when
                outcome=superseded.
        """
        if _is_background:
            return _tool_error(_BG_BLOCK_MSG)
        # A supersession without a successor is a lineage dead end: retrieval
        # can label the row `[superseded]` but cannot point at what replaced
        # it. Validated here, not as a JSON-schema conditional — the dispatcher
        # only enforces `required` keys flatly.
        if outcome == "superseded" and not superseded_by:
            return {
                "is_error": True,
                "content": [{"type": "text", "text": (
                    "Error: outcome='superseded' requires superseded_by — the UUID of the "
                    "decision that replaces this one. Record the replacement decision first, "
                    "then resolve this one with its ID. If nothing replaced it, use a "
                    "different outcome (e.g. 'noise' or 'failure')."
                )}],
            }
        try:
            detail = await brain.review(
                UUID(decision_id),
                outcome=outcome,
                result=resolution_note,
                reviewer="agent",
                superseded_by=UUID(superseded_by) if superseded_by else None,
            )
            text = f"Decision {detail.id} resolved: outcome={detail.outcome}"
            if detail.superseded_by:
                text += f", superseded_by={detail.superseded_by}"
            return {"content": [{"type": "text", "text": text}]}
        except Exception as e:
            logger.exception("resolve_decision tool failed")
            return _tool_error(f"Error resolving decision: {e}")

    async def resolve_decisions(
        resolutions: list[dict[str, Any]],
        _is_background: bool = False,
    ) -> dict[str, Any]:
        """Resolve a batch of decisions in one transaction (sweep path).

        Each item: {decision_id, outcome, resolution_note?, superseded_by?}.
        A per-item failure is reported and does not abort the batch.
        """
        if _is_background:
            return _tool_error(_BG_BLOCK_MSG)
        try:
            # codex #577 r3: NO batch-wide lineage precheck here. ReviewInput's
            # validator (shared by every entry point) rejects a missing-lineage
            # item inside review_many, which reports it as a per-item failure
            # and keeps the rest of the sweep alive — the documented contract.
            # A precheck that aborted the whole batch would discard every valid
            # resolution alongside the one malformed item.
            items = [
                {
                    "decision_id": r.get("decision_id"),
                    "outcome": r.get("outcome"),
                    "result": r.get("resolution_note"),
                    "superseded_by": r.get("superseded_by"),
                }
                for r in resolutions
            ]
            results = await brain.review_many(items, reviewer="agent")
            ok = sum(1 for r in results if r["ok"])
            failed = [r for r in results if not r["ok"]]
            text = f"Resolved {ok}/{len(results)} decisions."
            if failed:
                text += "\nFailures:\n" + "\n".join(
                    f"- {r['decision_id']}: {r['error']}" for r in failed
                )
            return {"content": [{"type": "text", "text": text}], "is_error": bool(failed and ok == 0)}
        except Exception as e:
            logger.exception("resolve_decisions tool failed")
            return _tool_error(f"Error resolving decisions: {e}")

    async def list_decisions(
        outcome: str | None = None,
        reviewed: bool | None = None,
        older_than_days: int | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """List decisions for sweeps (e.g. outcome='pending', reviewed=false).

        older_than_days filters to decisions created before that many days ago.
        """
        try:
            date_to = None
            if older_than_days is not None:
                date_to = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat()
            decisions, total = await brain.list_decisions(
                limit=limit, outcome=outcome, reviewed=reviewed, date_to=date_to,
            )
            if not decisions:
                return {"content": [{"type": "text", "text": "No matching decisions."}]}
            lines = [
                f"- {d.id} [{d.outcome}] ({d.category}/{d.stakes}, conf={d.confidence:.2f}) {d.description[:120]}"
                for d in decisions
            ]
            header = f"{len(decisions)} of {total} matching decisions:"
            return {"content": [{"type": "text", "text": header + "\n" + "\n".join(lines)}]}
        except Exception as e:
            logger.exception("list_decisions tool failed")
            return _tool_error(f"Error listing decisions: {e}")

    return {
        "record_decision": record_decision,
        "learn_fact": learn_fact,
        "recall_deep": recall_deep,
        "create_censor": create_censor,
        "recall_recent": recall_recent,
        "learn_skill": learn_skill,
        "get_procedure": get_procedure,
        "recall_hubs": recall_hubs,
        "ingest_document": ingest_document,
        "resolve_decision": resolve_decision,
        "resolve_decisions": resolve_decisions,
        "list_decisions": list_decisions,
    }


# ---------------------------------------------------------------------------
# Tool schema definitions (Anthropic API format)
# ---------------------------------------------------------------------------

# JSON Schema definitions for each tool's input parameters.
# Field names match the closure parameter names exactly.

_RECORD_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Record a decision to the Brain (decision intelligence organ)",
    "properties": {
        "description": {"type": "string", "description": "What was decided"},
        "confidence": {
            "type": "number",
            "description": "0.0-1.0 confidence level",
            "minimum": 0.0,
            "maximum": 1.0,
        },
        "category": {
            "type": "string",
            "description": "Decision category",
            "enum": ["architecture", "process", "tooling", "security", "integration"],
        },
        "stakes": {
            "type": "string",
            "description": "Stakes level",
            "enum": ["low", "medium", "high", "critical"],
        },
        "context": {"type": "string", "description": "Situation and constraints"},
        "pattern": {"type": "string", "description": "Abstract pattern this decision represents"},
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Keywords for filtering",
        },
        "reasons": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": [
                            "analysis",
                            "pattern",
                            "empirical",
                            "authority",
                            "analogy",
                            "intuition",
                            "elimination",
                            "constraint",
                        ],
                    },
                    "text": {"type": "string"},
                },
                "required": ["type", "text"],
            },
            "description": "Supporting reasons",
        },
    },
    "required": ["description", "confidence", "category", "stakes"],
}

_RESOLVE_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Persist an outcome on an existing decision, closing the calibration "
        "loop. Use 'noise' for sweep/tick artifacts and 'superseded' when a "
        "later decision replaced this one (both excluded from calibration). "
        "'superseded' REQUIRES superseded_by — record the replacing decision "
        "first, then pass its UUID; without it the call is rejected. "
        "Always include a resolution_note as the evidence trail."
    ),
    "properties": {
        "decision_id": {"type": "string", "description": "UUID of the decision to resolve"},
        "outcome": {
            "type": "string",
            "description": "Resolution outcome",
            "enum": ["success", "partial", "failure", "noise", "superseded"],
        },
        "resolution_note": {"type": "string", "description": "Evidence / why — the resolution trail"},
        "superseded_by": {
            "type": "string",
            "description": (
                "UUID of the decision that replaces this one. REQUIRED when "
                "outcome=superseded (the call is rejected without it); ignored otherwise."
            ),
        },
    },
    "required": ["decision_id", "outcome"],
}

_RESOLVE_DECISIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Resolve a batch of decisions in one transaction (for sweeps). "
        "A per-item failure is reported and does not abort the batch."
    ),
    "properties": {
        "resolutions": {
            "type": "array",
            "description": "Decisions to resolve",
            "items": {
                "type": "object",
                "properties": {
                    "decision_id": {"type": "string"},
                    "outcome": {
                        "type": "string",
                        "enum": ["success", "partial", "failure", "noise", "superseded"],
                    },
                    "resolution_note": {"type": "string"},
                    "superseded_by": {"type": "string"},
                },
                "required": ["decision_id", "outcome"],
            },
        },
    },
    "required": ["resolutions"],
}

_LIST_DECISIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "List this agent's decisions for a sweep. Filter by outcome (e.g. "
        "'pending'), reviewed status, and age to enumerate what to resolve."
    ),
    "properties": {
        "outcome": {
            "type": "string",
            "description": "Filter by outcome",
            "enum": ["pending", "success", "partial", "failure", "noise", "superseded"],
        },
        "reviewed": {"type": "boolean", "description": "Filter to reviewed (true) or unreviewed (false) decisions"},
        "older_than_days": {"type": "integer", "description": "Only decisions created more than this many days ago"},
        "limit": {"type": "integer", "description": "Max results (default 20)"},
    },
    "required": [],
}

_LEARN_FACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Store a fact in the Heart (memory system)",
    "properties": {
        "content": {"type": "string", "description": "The fact content"},
        "category": {
            "type": "string",
            "description": (
                "Fact category. person/preference/rule are RESERVED for durable "
                "facts about the user (identity, stable preferences, standing "
                "directives) — they are injected into every future prompt. "
                "Session events, dated one-offs, and task detail use "
                "technical/concept instead."
            ),
            "enum": ["preference", "technical", "person", "tool", "concept", "rule"],
        },
        "subject": {"type": "string", "description": "What/who the fact is about"},
        "confidence": {
            "type": "number",
            "description": "0.0-1.0 confidence level",
            "minimum": 0.0,
            "maximum": 1.0,
            "default": 1.0,
        },
        "source": {"type": "string", "description": "Where this fact came from"},
        "source_episode_id": {"type": "string", "description": "Episode UUID"},
        "source_decision_id": {"type": "string", "description": "Decision UUID"},
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Keywords for filtering",
        },
    },
    "required": ["content"],
}

_RECALL_DEEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Search across all memory types in Heart and Brain",
    "properties": {
        "query": {"type": "string", "description": "Search query string"},
        "limit": {
            "type": "integer",
            "description": "Maximum results to return",
            "default": 10,
            "minimum": 1,
            "maximum": 50,
        },
        "memory_types": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": ["all", "episode", "fact", "procedure", "censor", "decision"],
            },
            "description": (
                "Types to search. If omitted or contains 'all', searches the "
                "knowledge pool: episodes, facts, decisions (+ transcript "
                "chunks). Procedures and censors are NOT in the default pool "
                "— name them explicitly (e.g. [\"procedure\"]) to search them; "
                "a null default-pool result does not mean none exist."
            ),
        },
    },
    "required": ["query"],
}

_CREATE_CENSOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Create a guardrail censor in the Heart",
    "properties": {
        "trigger_pattern": {
            "type": "string",
            "description": "Pattern to match (substring or regex)",
        },
        "reason": {"type": "string", "description": "Why this censor exists"},
        "action": {
            "type": "string",
            "description": (
                "Censor tier: steer (advisory directive, non-blocking), "
                "refuse (LLM declines + write tools stripped), or abort (hard cut). "
                "Agent-created censors are capped at refuse."
            ),
            "enum": ["steer", "refuse", "abort"],
            "default": "steer",
        },
        "domain": {"type": "string", "description": "Domain this censor applies to"},
        "learned_from_decision": {"type": "string", "description": "Decision UUID"},
        "learned_from_episode": {"type": "string", "description": "Episode UUID"},
    },
    "required": ["trigger_pattern", "reason"],
}

_RECALL_RECENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Recall recent episodes by time (not topic similarity). Use when the user asks what you discussed recently or you need a temporal overview.",
    "properties": {
        "hours": {
            "type": "integer",
            "description": "Look back this many hours (default 48)",
            "default": 48,
        },
        "limit": {
            "type": "integer",
            "description": "Maximum episodes to return (default 10)",
            "default": 10,
        },
    },
    "required": [],
}


_LEARN_SKILL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Register a skill from a URL, local file path, or raw markdown. "
        "Skills are stored as procedures and auto-activate during recall when relevant to the task."
    ),
    "properties": {
        "source": {
            "type": "string",
            "description": (
                "URL (https://...), local file path relative to workspace, "
                "or 'inline' for raw content"
            ),
        },
        "content": {
            "type": "string",
            "description": "Raw SKILL.md markdown when source is 'inline'",
        },
    },
    "required": ["source"],
}

_GET_PROCEDURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Load a procedure/skill's full body (steps, core patterns, triggers, tools). "
        "Call this with the procedure's NAME as shown in your Procedure Catalog before "
        "acting on a matching task — no search/UUID lookup needed. A UUID also works."
    ),
    "properties": {
        "procedure_id": {
            "type": "string",
            "description": (
                "The procedure's NAME exactly as listed in the Procedure Catalog "
                "(preferred), or its UUID."
            ),
        },
    },
    "required": ["procedure_id"],
}


_RECALL_HUBS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "F065: return the most-connected (highest-degree) nodes in Nous's "
        "knowledge graph. Use to discover which concepts, decisions, facts, "
        "or episodes act as hubs that many other memories reference. "
        "Optionally filter by node_type."
    ),
    "properties": {
        "limit": {
            "type": "integer",
            "description": "Top-N to return (1..50, default 10)",
            "default": 10,
            "minimum": 1,
            "maximum": 50,
        },
        "node_type": {
            "type": "string",
            "description": "Optional filter by node type",
            "enum": ["decision", "fact", "episode", "procedure"],
        },
    },
    "required": [],
}


_INGEST_DOCUMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "F069: chunk and persist a full document body (arxiv paper, doc "
        "page, parsed PDF/.docx text) to long-term memory at full fidelity. "
        "Use after you have extracted text yourself (e.g. via run_python "
        "with pypdf for PDFs, python-docx for .docx, or after fetching "
        "with web_fetch at max_chars=200000 for a large HTML page). "
        "Unlike web_fetch + dialogue chunking which retains ~1% of a paper, "
        "this writes the full content to heart.episode_chunks with "
        "source_kind='document' using a 1500-char structure-aware chunker."
    ),
    "properties": {
        "content": {
            "type": "string",
            "description": (
                "Full document body to ingest. Should already be plain text "
                "(decode binary formats client-side via run_python). Must be "
                "at least document_chunk_min_chars (default 100) chars."
            ),
        },
        "source_ref": {
            "type": "string",
            "description": (
                "URL or workspace path the document came from. Persisted on "
                "every chunk for traceability and so the agent can recall "
                "where a fact was sourced."
            ),
        },
        "episode_id": {
            "type": "string",
            "description": (
                "Optional explicit episode UUID. If omitted, chunks are "
                "attached to the active episode of the current session."
            ),
        },
    },
    "required": ["content", "source_ref"],
}


def register_nous_tools(dispatcher: ToolDispatcher, brain: Brain, heart: Heart, settings: Settings | None = None) -> None:
    """Create Nous memory tools and register them with the dispatcher.

    This is the main wiring function called at startup to register
    all memory tools with their schemas.
    """
    closures = create_nous_tools(brain, heart, settings=settings)

    dispatcher.register("record_decision", closures["record_decision"], _RECORD_DECISION_SCHEMA)
    dispatcher.register("learn_fact", closures["learn_fact"], _LEARN_FACT_SCHEMA)
    dispatcher.register("recall_deep", closures["recall_deep"], _RECALL_DEEP_SCHEMA)
    dispatcher.register("create_censor", closures["create_censor"], _CREATE_CENSOR_SCHEMA)
    dispatcher.register("recall_recent", closures["recall_recent"], _RECALL_RECENT_SCHEMA)
    dispatcher.register("learn_skill", closures["learn_skill"], _LEARN_SKILL_SCHEMA)
    dispatcher.register("get_procedure", closures["get_procedure"], _GET_PROCEDURE_SCHEMA)
    dispatcher.register("recall_hubs", closures["recall_hubs"], _RECALL_HUBS_SCHEMA)
    dispatcher.register("ingest_document", closures["ingest_document"], _INGEST_DOCUMENT_SCHEMA)
    if settings is None or settings.decision_resolution_enabled:
        dispatcher.register("resolve_decision", closures["resolve_decision"], _RESOLVE_DECISION_SCHEMA)
        dispatcher.register("resolve_decisions", closures["resolve_decisions"], _RESOLVE_DECISIONS_SCHEMA)
        dispatcher.register("list_decisions", closures["list_decisions"], _LIST_DECISIONS_SCHEMA)


# ---------------------------------------------------------------------------
# F020: cache_retrieve tool
# ---------------------------------------------------------------------------

_CACHE_RETRIEVE_SCHEMA = {
    "description": "Retrieve original content from a previously compressed search or fetch result. Use when you see a [SmartCompressed] marker and need more detail.",
    "type": "object",
    "properties": {
        "hash_key": {
            "type": "string",
            "description": "The hash key from the [SmartCompressed] marker (e.g., 'abc123de01234567')",
        },
        "query": {
            "type": "string",
            "description": "Optional: return only items matching this query instead of everything.",
        },
    },
    "required": ["hash_key"],
}

CACHE_RETRIEVE_TOOL_DEF = {
    "name": "cache_retrieve",
    "description": _CACHE_RETRIEVE_SCHEMA["description"],
    "input_schema": _CACHE_RETRIEVE_SCHEMA,
}


def register_cache_retrieve_tool(
    dispatcher: ToolDispatcher,
    db_session_factory,
) -> None:
    """Register the cache_retrieve tool (F020)."""
    from nous.api.tool_cache import retrieve_cached_result

    async def _cache_retrieve(
        hash_key: str, query: str | None = None, session_id: str | None = None, **kwargs,
    ) -> dict:
        if not session_id:
            return _tool_error("Error: no active session for cache lookup.")
        try:
            async with db_session_factory() as db_sess:
                result = await retrieve_cached_result(db_sess, session_id, hash_key, query)
                if result is None:
                    text = f"No cached result found for hash key '{hash_key}'. It may have expired or the key may be incorrect."
                else:
                    text = result
                return {"content": [{"type": "text", "text": text}]}
        except Exception as e:
            logger.exception("cache_retrieve error")
            return _tool_error(f"Error retrieving cached result: {e}")

    dispatcher.register("cache_retrieve", _cache_retrieve, _CACHE_RETRIEVE_SCHEMA)


# ---------------------------------------------------------------------------
# Subtask prefix builder (012.2)
# ---------------------------------------------------------------------------


_F061_DEFAULT_SUCCESS_CRITERIA = (
    "The summary directly addresses the task and is internally consistent."
)
_F061_DEFAULT_BOUNDARIES = (
    "Do not spawn further subtasks. Do not modify files unless the task "
    "explicitly requires it. Cap tool calls per the runner limit."
)
_F061_FRAME_OUTPUT_FORMATS = {
    "task":         "Concise summary of what was done + verification of success.",
    "research":     "Synthesis of findings, with key facts in `findings[]` and sources in `evidence_refs[]`.",
    "decision":     "Decision recommendation + reasoning + confidence; record the decision via record_decision and reference its ID in `evidence_refs[]`.",
    "debug":        "Root cause + fix suggestion + verification steps.",
    "conversation": "Direct natural-language answer to the question.",
}


def _f061_default_output_format(frame_type: str | None) -> str:
    return _F061_FRAME_OUTPUT_FORMATS.get(
        frame_type or "", "Free-form summary appropriate to the task."
    )


def build_subtask_prefix(
    task: str,
    frame_type: str | None = None,
    *,
    output_format: str | None = None,
    success_criteria: str | None = None,
    boundaries: str | None = None,
    hardening_enabled: bool = False,
    payload_schema: dict | None = None,
) -> str:
    """Build a system prompt prefix for subtask execution.

    Used by both inline (await_result) and background worker subtask execution
    to ensure consistent frame-aware context assembly.

    When ``hardening_enabled=False`` (default), emits the legacy text —
    byte-identical to pre-F061. PR-6 will remove this branch once the flag
    is locked on. When ``hardening_enabled=True``, emits the F061 four-part
    brief + termination contract (objective + output_format + success_criteria
    + boundaries) and instructs the agent to terminate via the
    ``submit_final_report`` tool.

    F062: when ``payload_schema`` is non-None (and hardening_enabled is true),
    appends a "Result schema (REQUIRED)" block instructing the model to
    populate ``submit_final_report``'s ``payload`` field with data that
    validates against the supplied JSON Schema. Ignored when hardening is off
    — the legacy text path does not register ``submit_final_report`` at all.
    """
    from nous.api.runner import FRAME_TOOLS

    if not hardening_enabled:
        # LEGACY — DO NOT MODIFY. PR-6 deletes this branch.
        base = (
            "You are executing a background subtask.\n"
            "Deliver a clear, complete result. Do not ask questions."
        )
        frame_instruction = ""
        if frame_type and frame_type in FRAME_TOOLS:
            frame_instruction = (
                f"\n\nFrame: {frame_type} — apply {frame_type}-appropriate "
                "reasoning and tool usage."
            )
        return f"{base}{frame_instruction}\n\nTask: {task}"

    of = output_format or _f061_default_output_format(frame_type)
    sc = success_criteria or _F061_DEFAULT_SUCCESS_CRITERIA
    bd = boundaries or _F061_DEFAULT_BOUNDARIES
    # Render the Frame block for any non-empty frame name (including
    # 'research' which is not in FRAME_TOOLS). The Frame block is
    # informational; FRAME_TOOLS gates only tool availability.
    frame_block = ""
    if frame_type:
        frame_block = (
            f"\n# Frame\n{frame_type} — apply {frame_type}-appropriate "
            "reasoning and tool usage.\n"
        )
    # F062: when a payload schema was supplied, append a result-schema block
    # that the model populates via submit_final_report's optional `payload`
    # field. Compact JSON to keep prompt token spend bounded.
    payload_schema_block = ""
    if payload_schema is not None:
        import json as _json
        compact_schema = _json.dumps(payload_schema, separators=(",", ":"))
        payload_schema_block = (
            "\n# Result schema (REQUIRED)\n"
            "When you call submit_final_report, the `payload` field MUST be "
            "a JSON value that validates against this schema. Use the "
            "schema's property names exactly; do not invent keys.\n\n"
            "<schema>\n"
            f"{compact_schema}\n"
            "</schema>\n"
        )

    return (
        "You are a Nous subtask agent. Your ONLY way to terminate is to call "
        "the submit_final_report tool with a schema-valid payload.\n\n"
        f"# Objective\n{task}\n\n"
        f"# Output format\n{of}\n\n"
        f"# Success criteria\n{sc}\n\n"
        f"# Boundaries\n{bd}\n"
        f"{frame_block}"
        f"{payload_schema_block}\n"
        "# Termination\n"
        "When you are done — and ONLY when done — call submit_final_report. "
        "Do not produce a final text-only response; the parent agent will "
        "read only the report payload. If you genuinely cannot complete the "
        "task, call submit_final_report with incomplete=true and a specific "
        "blocked_reason."
    )


async def _persist_and_emit_inline_outcome(
    *,
    heart: Heart,
    bus: object,
    settings: "Settings",
    subtask: Any,
    final_outcome: str,
    error_msg: str,
    state: Any,
) -> None:
    """F061 round 4: inline path's outer timeout/exception handler must
    persist accurate attempts + token counts AND emit a subtask_outcome
    event. Mirrors ``SubtaskWorkerPool._emit_outcome_from_outer`` for the
    inline ``await_result=true`` path.
    """
    attempts = max(1, getattr(state, "attempts", 0) or 0)
    tokens_in = getattr(state, "tokens_in", 0)
    tokens_out = getattr(state, "tokens_out", 0)
    tool_calls_made = getattr(state, "tool_calls_made", 0)

    await heart.subtasks.fail(
        subtask.id, error_msg,
        final_outcome=final_outcome,
        attempts=attempts,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        tool_calls_made=tool_calls_made,
    )

    if bus is None or not settings.subtask_outcome_persistence_enabled:
        return
    try:
        from nous.events import Event
        from nous.handlers.subtask_executor import SUBTASK_OUTCOME_EVENT_TYPE

        data: dict[str, Any] = {
            "subtask_id": str(subtask.id),
            "agent_id": getattr(subtask, "agent_id", None),
            "frame_type": getattr(subtask, "frame_type", None),
            "final_outcome": final_outcome,
            "ok": False,
            "validator_reason": error_msg,
            "attempts": attempts,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "tool_calls_made": tool_calls_made,
            "duration_ms": None,
            "dag_node_id": (
                str(subtask.dag_node_id)
                if getattr(subtask, "dag_node_id", None) is not None
                else None
            ),
        }
        await bus.emit(Event(
            type=SUBTASK_OUTCOME_EVENT_TYPE,
            agent_id=getattr(subtask, "agent_id", "") or settings.agent_id,
            session_id=f"subtask-{subtask.id.hex[:8]}",
            data=data,
        ))
    except Exception:
        logger.exception(
            "Failed to emit subtask_outcome from inline outer handler for %s",
            subtask.id.hex[:8],
        )


# ---------------------------------------------------------------------------
# Subtask & Schedule tool closures (011.1)
# ---------------------------------------------------------------------------

def create_subtask_tools(
    heart: Heart,
    settings: "Settings",
    runner: object = None,
    bus: object = None,
) -> dict[str, Any]:
    """Create subtask/schedule tool closures with Heart captured in closure context.

    Returns a dict of async callables suitable for ToolDispatcher registration.
    The optional ``runner`` param enables inline (await_result) subtask execution.
    The optional ``bus`` param (F061 PR-3) is the EventBus the inline hardened
    path uses to emit ``subtask_outcome`` telemetry; ``None`` disables that
    emission for inline subtasks (background subtasks emit via the worker pool).
    """
    from nous.config import Settings as _Settings  # noqa: F811 — deferred to avoid circular

    # F061 PR-3 silent-failure review P2.1: warn loudly when inline telemetry
    # is silently disabled because no bus was wired through. Operator who
    # forgets to pass bus= would otherwise see dashboard rows for inline
    # subtasks without subtask_outcome events for them.
    if (
        bus is None
        and getattr(settings, "subtask_outcome_persistence_enabled", True)
        and getattr(settings, "subtask_hardening_enabled", False)
    ):
        logger.warning(
            "F061 PR-3: inline subtask telemetry disabled (bus=None passed to "
            "create_subtask_tools). subtask_outcome events from "
            "await_result=True path will be lost."
        )

    async def spawn_task(
        task: str,
        priority: str = "normal",
        timeout: int | None = None,
        notify: bool = False,
        frame_type: str | None = None,
        await_result: bool = False,
        model: str | None = None,
        # F061: 2 of 4 brief fields are persisted. ``boundaries`` is
        # intentionally NOT a parameter — it has no DB column and would
        # silently drop. Fold boundary constraints into ``task`` until a
        # follow-up PR adds storage. Worker / executor synthesize frame-
        # derived defaults at execute time when these are None.
        output_format: str | None = None,
        success_criteria: str | None = None,
        # F062: optional JSON Schema for the result payload. Only honored
        # when settings.subtask_payload_schema_enabled is True; otherwise
        # silently dropped (the property is also gated out of
        # _SPAWN_TASK_SCHEMA so the model can't see it). Persisted on the
        # subtask row even when dropped at execute time — operators can
        # inspect via /dashboard/subtasks.
        payload_schema: dict | None = None,
        # F062: internal-only lookup token written to metadata so spawn_sync
        # can find the row it just created without overriding the caller's
        # parent_session_id (Codex round-14 P2). Not exposed in the public
        # tool schema.
        _lookup_token: str | None = None,
        _session_id: str | None = None,
    ) -> dict[str, Any]:
        """Spawn a subtask, optionally waiting for its result inline.

        Args:
            task: Natural-language instruction for the subtask
            priority: urgent, normal, or low
            timeout: Max seconds (clamped to settings.subtask_max_timeout)
            notify: Whether to notify on completion
            frame_type: Cognitive frame for the subtask
            await_result: If true, execute inline and return result
            model: Model override for this subtask

        Returns:
            MCP-compliant response with subtask ID or inline result
        """
        try:
            # 012.2: Apply frame-default model mapping
            effective_model = model
            if not effective_model and frame_type:
                effective_model = settings.frame_default_models.get(frame_type)
            if not effective_model:
                effective_model = settings.background_model

            # 012.2: Differentiate timeout defaults
            if await_result:
                effective_timeout = min(
                    timeout or settings.inline_subtask_timeout,
                    settings.subtask_max_timeout,
                )
            else:
                effective_timeout = min(
                    timeout or settings.subtask_default_timeout,
                    settings.subtask_max_timeout,
                )

            # F078 (R3): Check censors on subtask task text at creation time.
            # Subtasks are non-interactive so censor checks are skipped during
            # execution (pre_turn) — the spawn gate is the ONLY censor enforcement
            # the background path gets, so it must honor the new tiers:
            #   abort / refuse -> REJECT the subtask (the autonomous-exfil path).
            #   steer          -> do NOT reject; inject the directive into the task.
            # Email censors are `steer`, so daily email subtasks pass unaffected.
            try:
                from nous.heart.censor_actions import CensorActionExecutor
                _steer_directives: list[str] = []
                matches = await heart.check_censors(task)
                for match in matches:
                    if match.action in ("abort", "refuse"):
                        # F031: refuse may downgrade to steer via unblock_pattern.
                        unblocked = False
                        if match.trigger_action and match.unblock_pattern:
                            executor = CensorActionExecutor(heart)
                            action_result = await executor.execute(match.trigger_action)
                            if action_result:
                                import re
                                try:
                                    if re.search(match.unblock_pattern, action_result, re.IGNORECASE):
                                        unblocked = True
                                except re.error:
                                    pass
                        if not unblocked:
                            reason = match.reason or match.trigger_pattern
                            msg = f"Subtask rejected by censor: {reason}"
                            if match.action_instruction:
                                msg += f"\n{match.action_instruction}"
                            return _tool_error(msg)
                        # downgraded refuse -> treat as steer directive below
                        directive = match.action_instruction or match.reason
                        if directive:
                            _steer_directives.append(directive)
                    elif match.action == "steer":
                        logger.info("Censor STEER on subtask creation: %s", match.trigger_pattern)
                        directive = match.action_instruction or match.reason
                        if directive:
                            _steer_directives.append(directive)
                # Inject steer directives into the task text so the subtask honors them.
                if _steer_directives:
                    task = task + "\n\n## Active Guidance\n" + "\n".join(
                        f"- {d}" for d in _steer_directives
                    )
            except Exception:
                # The spawn gate is the ONLY censor enforcement a subtask gets (the exfil
                # path) — a swallowed failure here is a silent enforcement gap, so WARN.
                logger.warning("Censor check failed during spawn_task, proceeding", exc_info=True)

            # F062: persist payload_schema only when BOTH flags are on
            # (Codex round-9 P2). Without F061 hardening the legacy executor
            # path runs and never validates; storing a schema there would
            # make the row LOOK schema-constrained while never enforcing —
            # a fail-closed violation that misleads downstream consumers
            # who read payload_schema from the row.
            effective_payload_schema = (
                payload_schema
                if (
                    settings.subtask_payload_schema_enabled
                    and settings.subtask_hardening_enabled
                )
                else None
            )
            # F062: stash the spawn_sync lookup token in metadata when set
            # so spawn_sync can find this row without clobbering the
            # caller's parent_session_id.
            _metadata: dict | None = None
            if _lookup_token:
                _metadata = {"f062_spawn_sync_token": _lookup_token}
            subtask = await heart.subtasks.create(
                task=task,
                priority=priority,
                timeout=effective_timeout,
                notify=notify,
                parent_session_id=_session_id,
                frame_type=frame_type,
                model=effective_model,
                metadata=_metadata,
                # F061: persist the four-part brief for the executor.
                output_format=output_format,
                success_criteria=success_criteria,
                # F062: caller-supplied JSON Schema for the result payload.
                payload_schema=effective_payload_schema,
            )

            if not await_result:
                # Fire-and-forget (existing behavior)
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"Subtask spawned.\n"
                                f"ID: {subtask.id}\n"
                                f"Priority: {priority}\n"
                                f"Timeout: {effective_timeout}s"
                            ),
                        }
                    ]
                }

            # 012.2: Synchronous inline execution
            if runner is None:
                return _tool_error(
                    "Cannot execute inline subtask: runner not available. "
                    "Use await_result=false."
                )

            import asyncio as _asyncio

            subtask_session_id = f"subtask-{subtask.id.hex[:8]}"

            # F061: route inline path through the SAME hardened executor as
            # the worker pool when the flag is on. Closes the silent-failure
            # gap (P1.1 from spec review): otherwise the new prompt would
            # mention submit_final_report on a path where that tool isn't
            # registered, relocating the exact failure F061 fixes.
            if settings.subtask_hardening_enabled:
                # Late import: nous.handlers.subtask_executor imports
                # build_subtask_prefix from THIS module — top-level import
                # would deadlock at startup. (functools.partial is stdlib
                # and circular-safe; imported at module top.)
                from nous.handlers.subtask_executor import (
                    HardenedRunState,
                    emit_outcome_event,
                    execute_hardened,
                )

                # F061 PR-3: pass an emit_event callback so inline subtasks
                # also produce subtask_outcome telemetry. ``bus`` is captured
                # by the outer create_subtask_tools closure when available.
                _outcome_emitter = (
                    partial(emit_outcome_event, bus, settings=settings)
                    if bus is not None else None
                )

                # F061 round 4: HardenedRunState side channel so the timeout
                # / exception handlers below can read accurate attempts +
                # token counts, persist them on the row, AND emit a
                # subtask_outcome event (execute_hardened skips persist+emit
                # on cancel — outer handler is the authoritative classifier).
                state = HardenedRunState()

                # `executed` flag prevents double-persist on programming-
                # error in the response-formatting code below: if any
                # AttributeError leaks from the body builder after
                # execute_hardened has already persisted via _persist_outcome,
                # the outer `except Exception` below would overwrite the row
                # with status='failed' even though it was already 'completed'.
                executed = False
                try:
                    final_text, _result = await _asyncio.wait_for(
                        execute_hardened(
                            subtask, subtask_session_id,
                            runner=runner, heart=heart, settings=settings,
                            emit_event=_outcome_emitter,
                            state=state,
                        ),
                        timeout=effective_timeout,
                    )
                    executed = True
                    if _result.ok:
                        body = (
                            f"[Subtask {subtask.id.hex[:8]} completed]\n\n{final_text}"
                        )
                    elif _result.outcome == "incomplete_blocked":
                        body = (
                            f"[Subtask {subtask.id.hex[:8]} blocked: {_result.reason}]"
                        )
                    else:
                        body = (
                            f"[Subtask {subtask.id.hex[:8]} {_result.outcome}: "
                            f"{_result.reason}]"
                        )
                    # Mixed path: `body` is a completion OR a blocked/failed
                    # outcome, so the flag follows _result.ok rather than being
                    # set blanket either way.
                    if not _result.ok:
                        return _tool_error(body)
                    return {"content": [{"type": "text", "text": body}]}
                except _asyncio.TimeoutError:
                    if not executed:
                        await _persist_and_emit_inline_outcome(
                            heart=heart, bus=bus, settings=settings,
                            subtask=subtask,
                            final_outcome="timed_out",
                            error_msg=f"Timeout after {effective_timeout}s",
                            state=state,
                        )
                    return _tool_error(
                        f"[Subtask {subtask.id.hex[:8]} timed out after "
                        f"{effective_timeout}s]"
                    )
                except Exception as e:
                    if not executed:
                        await _persist_and_emit_inline_outcome(
                            heart=heart, bus=bus, settings=settings,
                            subtask=subtask,
                            final_outcome="errored",
                            error_msg=str(e),
                            state=state,
                        )
                    return _tool_error(f"[Subtask {subtask.id.hex[:8]} failed: {e}]")

            # Legacy inline path — bytewise unchanged from pre-F061.
            system_prefix = build_subtask_prefix(task, frame_type)

            try:
                response_text, _ctx, _usage = await _asyncio.wait_for(
                    runner.run_turn(
                        session_id=subtask_session_id,
                        user_message=task,
                        agent_id=settings.agent_id,
                        system_prompt_prefix=system_prefix,
                        skip_episode=True,
                        is_subtask=True,
                        max_tool_calls=settings.subtask_tool_call_limit,
                        model_override=effective_model,
                        is_background=True,
                    ),
                    timeout=effective_timeout,
                )

                # F061 PR-1: record outcome on legacy path so dashboard rows
                # are never NULL between PR-1 ship and PR-2 hardened-executor ship.
                await heart.subtasks.complete(
                    subtask.id, response_text, final_outcome="completed",
                )
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"[Subtask {subtask.id.hex[:8]} completed]\n\n{response_text}",
                        }
                    ]
                }

            except _asyncio.TimeoutError:
                # F061 PR-3 Codex review: attempts=1 because one execution
                # attempt definitely happened before the timeout.
                await heart.subtasks.fail(
                    subtask.id, f"Timeout after {effective_timeout}s",
                    final_outcome="timed_out", attempts=1,
                )
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"[Subtask {subtask.id.hex[:8]} timed out after {effective_timeout}s]",
                        }
                    ]
                }
            except Exception as e:
                await heart.subtasks.fail(
                    subtask.id, str(e), final_outcome="errored", attempts=1,
                )
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"[Subtask {subtask.id.hex[:8]} failed: {e}]",
                        }
                    ]
                }

        except ValueError as e:
            return _tool_error(f"Cannot spawn subtask: {e}")
        except Exception as e:
            logger.exception("spawn_task tool failed")
            return _tool_error(f"Error spawning subtask: {e}")

    async def schedule_task(
        task: str,
        when: str | None = None,
        every: str | None = None,
        notify: bool = False,
        model: str | None = None,
        frame_type: str | None = None,
    ) -> dict[str, Any]:
        """Schedule a task for later or recurring execution.

        Exactly one of ``when`` or ``every`` must be provided.

        Args:
            task: Natural-language instruction
            when: One-shot time (e.g. "in 2 hours", ISO 8601)
            every: Recurring pattern (e.g. "daily at 8am", "30 minutes")
            notify: Whether to notify on each fire

        Returns:
            MCP-compliant response with schedule ID and next fire time
        """
        try:
            if bool(when) == bool(every):
                return _tool_error(
                    "Exactly one of 'when' or 'every' must be provided."
                )

            from nous.handlers.time_parser import parse_every, parse_when

            if when:
                fire_at = parse_when(when)
                schedule = await heart.schedules.create(
                    task=task,
                    schedule_type="once",
                    fire_at=fire_at,
                    notify=notify,
                    timeout=settings.subtask_default_timeout,
                    model=model,
                    frame_type=frame_type,
                )
            else:
                interval_seconds, cron_expr = parse_every(every)  # type: ignore[arg-type]
                schedule = await heart.schedules.create(
                    task=task,
                    schedule_type="recurring",
                    interval_seconds=interval_seconds,
                    cron_expr=cron_expr,
                    notify=notify,
                    timeout=settings.subtask_default_timeout,
                    model=model,
                    frame_type=frame_type,
                )

            next_fire = (
                schedule.next_fire_at.isoformat() if schedule.next_fire_at else "N/A"
            )
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Schedule created.\n"
                            f"ID: {schedule.id}\n"
                            f"Type: {schedule.schedule_type}\n"
                            f"Next fire: {next_fire}"
                        ),
                    }
                ]
            }
        except ValueError as e:
            return _tool_error(f"Schedule error: {e}")
        except Exception as e:
            logger.exception("schedule_task tool failed")
            return _tool_error(f"Error scheduling task: {e}")

    async def list_tasks(
        status: str | None = None,
    ) -> dict[str, Any]:
        """List subtasks and schedules.

        Args:
            status: Filter subtasks by status (pending, running, completed, failed, cancelled)

        Returns:
            MCP-compliant response with formatted task list
        """
        try:
            subtasks = await heart.subtasks.list(status=status, limit=20)
            schedules = await heart.schedules.list(active_only=True, limit=20)

            lines: list[str] = []

            if subtasks:
                lines.append("=== Subtasks ===")
                for st in subtasks:
                    line = f"- [{st.status}] {st.id} | {st.task[:80]}"
                    if st.status == "completed" and st.result:
                        line += f"\n  Result: {st.result}"
                    elif st.status == "failed" and st.error:
                        line += f"\n  Error: {st.error}"
                    lines.append(line)
            else:
                lines.append("=== Subtasks ===\nNo subtasks found.")

            if schedules:
                lines.append("\n=== Schedules ===")
                for sc in schedules:
                    next_fire = (
                        sc.next_fire_at.strftime("%Y-%m-%d %H:%M UTC")
                        if sc.next_fire_at
                        else "N/A"
                    )
                    lines.append(
                        f"- [{sc.schedule_type}] {sc.id} | {sc.task[:80]} (next: {next_fire})"
                    )
            else:
                lines.append("\n=== Schedules ===\nNo active schedules.")

            return {"content": [{"type": "text", "text": "\n".join(lines)}]}
        except Exception as e:
            logger.exception("list_tasks tool failed")
            return _tool_error(f"Error listing tasks: {e}")

    async def cancel_task(
        task_id: str,
    ) -> dict[str, Any]:
        """Cancel a subtask or deactivate a schedule by ID.

        Args:
            task_id: UUID of the subtask or schedule to cancel

        Returns:
            MCP-compliant response confirming cancellation or error
        """
        try:
            from uuid import UUID as _UUID

            uid = _UUID(task_id)

            # Try subtask cancel first
            cancelled = await heart.subtasks.cancel(uid)
            if cancelled:
                return {
                    "content": [
                        {"type": "text", "text": f"Subtask {task_id} cancelled."}
                    ]
                }

            # Try schedule deactivation
            schedule = await heart.schedules.get(uid)
            if schedule:
                await heart.schedules.deactivate(uid)
                return {
                    "content": [
                        {"type": "text", "text": f"Schedule {task_id} deactivated."}
                    ]
                }

            return {
                "content": [
                    {"type": "text", "text": f"No pending subtask or active schedule found for {task_id}."}
                ]
            }
        except ValueError:
            return _tool_error(f"Invalid task ID: {task_id}")
        except Exception as e:
            logger.exception("cancel_task tool failed")
            return _tool_error(f"Error cancelling task: {e}")

    # F062: spawn_sync — typed counterpart to spawn_task(await_result=True).
    # Returns SubtaskResult.to_dict() as a JSON blob in the content. The
    # spawn_task fire-and-forget contract and legacy string return stay
    # untouched; spawn_sync is the new entry-point for callers that want
    # a structured, schema-validated payload.
    async def spawn_sync(
        task: str,
        payload_schema: dict | None = None,
        frame_type: str | None = None,
        timeout_seconds: int | None = None,
        model: str | None = None,
        success_criteria: str | None = None,
        _session_id: str | None = None,
    ) -> dict[str, Any]:
        import json as _json
        import uuid as _uuid

        from nous.api.models import SubtaskResult

        # Generate a unique lookup token written to the subtask's metadata
        # (Codex round-14 P2). Earlier rounds put this in parent_session_id,
        # but that clobbered the caller's real parent_session and broke the
        # cognitive-layer delivery sweep (heart.subtasks.get_undelivered).
        # The metadata token is a clean side channel.
        sync_lookup_token = f"spawn_sync-{_uuid.uuid4().hex[:16]}"

        # spawn_sync always blocks inline. Reuse spawn_task's plumbing so
        # the censor / hardened-executor / inline-outcome paths all stay
        # in one place. spawn_task already enforces effective_payload_schema
        # gating on settings.subtask_payload_schema_enabled.
        raw = await spawn_task(
            task=task,
            priority="normal",
            timeout=timeout_seconds,
            notify=False,
            frame_type=frame_type,
            await_result=True,
            model=model,
            output_format=None,
            success_criteria=success_criteria,
            payload_schema=payload_schema,
            _lookup_token=sync_lookup_token,
            _session_id=_session_id,  # preserve caller's parent_session_id
        )

        # spawn_task always returns {"content": [{"type":"text","text":...}]}.
        text = ""
        try:
            text = raw["content"][0]["text"]
        except (KeyError, IndexError, TypeError):
            text = ""

        # Race-free lookup via metadata token — preserves caller session
        # linkage and is unbounded-correct under any concurrency.
        match = await heart.subtasks.get_by_spawn_sync_token(sync_lookup_token)
        if match is None:
            # Censor-rejection path or runner-unavailable path — no row
            # was created at all. Return a degraded but typed result.
            result = SubtaskResult(
                task_id="",
                status="errored",
                payload={},
                raw_text=text,
                confidence=None,
                elapsed_seconds=0.0,
                validator_reason="spawn_sync: no subtask row created (censor or runner unavailable)",
            )
            return {
                "content": [
                    {"type": "text", "text": _json.dumps(result.to_dict(), indent=2)}
                ]
            }

        report = match.report_jsonb or {}
        # Codex round-9 P2: inline subtasks (await_result=True) bypass the
        # worker pool's dequeue path, so started_at remains NULL. Fall back
        # to created_at — inline runs start essentially immediately after
        # creation, so the difference is negligible and the metric is no
        # longer systematically 0.0 for the spawn_sync's primary use case.
        elapsed = 0.0
        if match.completed_at:
            start_anchor = match.started_at or match.created_at
            if start_anchor:
                elapsed = (match.completed_at - start_anchor).total_seconds()

        # Codex round-12 P2: raw_text was the spawn_task wrapper string
        # (e.g. "[Subtask abc12345 completed]"), which dropped the actual
        # report content on failure paths where callers need it to debug.
        # Use the persisted report content directly: prefer the validated
        # summary, then fall back to .result (legacy column), and only as
        # a last resort to spawn_task's wrapper text.
        diagnostic_text = ""
        if isinstance(report, dict) and isinstance(report.get("summary"), str) and report["summary"].strip():
            diagnostic_text = report["summary"]
        elif match.result:
            diagnostic_text = match.result
        else:
            diagnostic_text = text

        # status mirrors final_outcome (Codex round-2 P1 invariant). Fall back
        # to legacy `status` only if final_outcome is somehow NULL — that
        # signals a pre-F061 row, which spawn_sync should never hit but we
        # surface as "errored" for safety rather than guessing.
        if match.final_outcome:
            status: Any = match.final_outcome
        else:
            status = "errored"

        # Payload contract:
        #   status == "completed"        → payload = the validated value from
        #                                  the report. Preserves explicit
        #                                  None (e.g., schema {"type":"null"})
        #                                  vs. unset (no payload key in
        #                                  report) — the former is a valid
        #                                  schema-typed result and must NOT
        #                                  be coerced to {} (Codex round-4
        #                                  P2). Only fall back to {} when
        #                                  the `payload` key was never set.
        #   any other terminal outcome   → payload = {} (do NOT surface a
        #                                  schema-invalid payload that a
        #                                  caller might accidentally consume
        #                                  thinking it validated).
        # raw_text + validator_reason carry the diagnostic info if the
        # caller actually wants to inspect the failed payload.
        _UNSET = object()
        if status == "completed":
            payload_or_unset: Any = (
                report.get("payload", _UNSET)
                if isinstance(report, dict)
                else _UNSET
            )
            if payload_or_unset is _UNSET:
                payload = {}
            else:
                payload = payload_or_unset
        else:
            payload = {}

        confidence = None
        if isinstance(report, dict):
            c = report.get("confidence")
            if isinstance(c, (int, float)):
                confidence = float(c)

        validator_reason = match.error if match.error else None

        result = SubtaskResult(
            task_id=str(match.id),
            status=status,
            payload=payload,
            raw_text=diagnostic_text,
            confidence=confidence,
            elapsed_seconds=elapsed,
            validator_reason=validator_reason,
        )
        return {
            "content": [
                {"type": "text", "text": _json.dumps(result.to_dict(), indent=2)}
            ]
        }

    return {
        "spawn_task": spawn_task,
        "schedule_task": schedule_task,
        "list_tasks": list_tasks,
        "cancel_task": cancel_task,
        "spawn_sync": spawn_sync,
    }


# ---------------------------------------------------------------------------
# Subtask & Schedule tool schemas (Anthropic API format)
# ---------------------------------------------------------------------------

_SPAWN_TASK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Spawn a subtask. Use await_result=true to wait for the result inline, or leave false for fire-and-forget background execution.",
    "properties": {
        "task": {
            "type": "string",
            "description": "Natural-language instruction for the subtask",
        },
        "priority": {
            "type": "string",
            "description": "Task priority",
            "enum": ["urgent", "normal", "low"],
            "default": "normal",
        },
        "timeout": {
            "type": "integer",
            "description": "Max execution seconds (clamped to server max)",
            "minimum": 10,
        },
        "notify": {
            "type": "boolean",
            "description": "Notify on completion",
            "default": False,
        },
        "frame_type": {
            "type": "string",
            "description": "Cognitive frame for the subtask. If omitted, auto-detected.",
            "enum": ["task", "research", "conversation", "decision", "debug"],
        },
        "await_result": {
            "type": "boolean",
            "description": "If true, wait for subtask completion and return result inline. Default false (fire-and-forget).",
            "default": False,
        },
        "model": {
            "type": "string",
            "description": "Model to use for this subtask. If omitted, uses default background model. Use a smaller model for fast lookup/summarization tasks.",
        },
        # F061: 2 of 4 brief fields exposed via schema (output_format,
        # success_criteria). The 4th field "boundaries" is NOT exposed yet
        # — it has no persistence path so would silently drop. Fold
        # boundary constraints into the ``task`` string until a follow-up
        # PR adds a column.
        "output_format": {
            "type": "string",
            "description": "How the subtask should structure its final report. Optional; sensible default applied per frame_type.",
        },
        "success_criteria": {
            "type": "string",
            "description": "What 'done' looks like. Optional; default 'summary directly addresses the task and is internally consistent'.",
        },
    },
    "required": ["task"],
}

_SCHEDULE_TASK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Schedule a task for later or recurring execution. Provide exactly one of 'when' or 'every'.",
    "properties": {
        "task": {
            "type": "string",
            "description": "Natural-language instruction for the task",
        },
        "when": {
            "type": "string",
            "description": "One-shot time: 'in 2 hours', ISO 8601, or natural language",
        },
        "every": {
            "type": "string",
            "description": "Recurring pattern: 'daily at 8am', '30 minutes', 'every monday at 10am'",
        },
        "notify": {
            "type": "boolean",
            "description": "Notify on each fire",
            "default": False,
        },
        "model": {
            "type": "string",
            "description": "Model to use for this scheduled task. If omitted, uses default background model.",
        },
        "frame_type": {
            "type": "string",
            "description": "Cognitive frame for the task (e.g. 'research', 'task')",
            "enum": ["task", "research", "conversation", "decision", "debug"],
        },
    },
    "required": ["task"],
}

_LIST_TASKS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "List current subtasks and active schedules",
    "properties": {
        "status": {
            "type": "string",
            "description": "Filter subtasks by status",
            "enum": ["pending", "running", "completed", "failed", "cancelled"],
        },
    },
    "required": [],
}

_CANCEL_TASK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Cancel a subtask or deactivate a schedule by ID",
    "properties": {
        "task_id": {
            "type": "string",
            "description": "UUID of the subtask or schedule to cancel",
        },
    },
    "required": ["task_id"],
}

# F062: spawn_sync tool schema. Typed counterpart to spawn_task(await_result=True);
# returns a SubtaskResult JSON blob with status/payload/confidence/etc. Only
# registered when settings.subtask_payload_schema_enabled is True.
_SPAWN_SYNC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Run a subtask synchronously (inline) and return a typed SubtaskResult "
        "containing status, structured payload, raw text, and confidence. "
        "Pass payload_schema to require the subtask's report.payload to "
        "validate against a JSON Schema; on mismatch the SubtaskResult will "
        "have status='validation_failed'. Use spawn_task for fire-and-forget."
    ),
    "properties": {
        "task": {
            "type": "string",
            "description": "Natural-language instruction for the subtask",
        },
        "payload_schema": {
            # Codex round-10 P2: draft-2020-12 allows boolean schemas
            # (true accepts anything, false rejects everything) in addition
            # to object schemas. Accept both at the tool-arg layer so valid
            # callers aren't rejected up-front.
            "type": ["object", "boolean"],
            "description": (
                "Optional JSON Schema (draft 2020-12 compatible). When set, "
                "the subtask is instructed to populate submit_final_report's "
                "`payload` field with data matching this schema; the executor "
                "validates the result post-hoc. May be a JSON object or a "
                "boolean (draft 2020-12 boolean-as-schema)."
            ),
        },
        "frame_type": {
            "type": "string",
            "description": "Cognitive frame for the subtask. Defaults to current.",
            "enum": ["task", "research", "conversation", "decision", "debug"],
        },
        "timeout_seconds": {
            "type": "integer",
            "description": "Max execution seconds (clamped to server max).",
            "minimum": 10,
        },
        "model": {
            "type": "string",
            "description": "Model to use for this subtask. Defaults to background model.",
        },
        "success_criteria": {
            "type": "string",
            "description": (
                "What 'done' looks like. Optional; defaults to a generic check."
            ),
        },
    },
    "required": ["task"],
}


def _build_spawn_task_schema(payload_schema_enabled: bool) -> dict[str, Any]:
    """Return a copy of _SPAWN_TASK_SCHEMA with the F062 payload_schema
    property conditionally injected.

    Plan §3 Commit B step 3 — we don't mutate the module-level constant
    because that would (a) leak the property on subsequent registrations
    after a settings flip in tests and (b) cross-contaminate dispatchers
    that share the import.
    """
    import copy
    schema = copy.deepcopy(_SPAWN_TASK_SCHEMA)
    if payload_schema_enabled:
        schema["properties"]["payload_schema"] = {
            # Codex round-11 P2: draft-2020-12 permits boolean schemas
            # (true/false) — mirror the same fix already in _SPAWN_SYNC_SCHEMA.
            "type": ["object", "boolean"],
            "description": (
                "F062: optional JSON Schema (draft 2020-12 compatible) for "
                "the subtask's result payload. When supplied, the executor "
                "validates submit_final_report.payload against this schema; "
                "on mismatch the subtask retries (per F061) and ultimately "
                "persists final_outcome='validation_failed'. Accepts a JSON "
                "object or a boolean (true accepts any payload, false rejects)."
            ),
        }
    return schema


def register_subtask_tools(
    dispatcher: ToolDispatcher,
    heart: Heart,
    settings: "Settings",
    runner: object = None,
    bus: object = None,
) -> None:
    """Create subtask/schedule tools and register them with the dispatcher.

    Called at startup when subtask_enabled is True.
    The optional ``runner`` enables inline (await_result) subtask execution.
    The optional ``bus`` (F061 PR-3) lets inline hardened subtasks emit
    ``subtask_outcome`` telemetry via the EventBus.

    F062: when ``settings.subtask_payload_schema_enabled`` is True, the
    payload_schema property is added to spawn_task's schema AND spawn_sync
    is registered as a separate tool. When False, neither is exposed —
    F062 is entirely dormant.
    """
    closures = create_subtask_tools(heart, settings, runner, bus=bus)

    # F062: expose payload_schema on spawn_task's tool schema ONLY when BOTH
    # F062 master flag AND F061 hardening are on. Without F061 hardening,
    # spawn_task takes the legacy inline / worker path that never calls
    # execute_hardened — the property would be advertised but never enforced
    # (Codex round-7 P2; mirrors the same gate on spawn_sync registration).
    spawn_task_schema = _build_spawn_task_schema(
        settings.subtask_payload_schema_enabled and settings.subtask_hardening_enabled
    )
    dispatcher.register("spawn_task", closures["spawn_task"], spawn_task_schema)
    dispatcher.register("schedule_task", closures["schedule_task"], _SCHEDULE_TASK_SCHEMA)
    dispatcher.register("list_tasks", closures["list_tasks"], _LIST_TASKS_SCHEMA)
    dispatcher.register("cancel_task", closures["cancel_task"], _CANCEL_TASK_SCHEMA)
    # F062 requires F061's hardened executor — without subtask_hardening_enabled
    # the legacy inline path runs, which never calls execute_hardened, never
    # validates the payload, and would silently break F062's typed contract.
    # Also requires a runner — spawn_sync always uses await_result=True
    # (inline execution), which is a no-op without a wired runner. Gate
    # registration on ALL THREE so the operator can't accidentally end up
    # with a tool that creates pending subtask rows and synthesizes false
    # error results (Codex round-10 P2).
    if (
        settings.subtask_payload_schema_enabled
        and settings.subtask_hardening_enabled
        and runner is not None
    ):
        dispatcher.register("spawn_sync", closures["spawn_sync"], _SPAWN_SYNC_SCHEMA)
    elif settings.subtask_payload_schema_enabled and settings.subtask_hardening_enabled:
        logger.warning(
            "F062: both NOUS_SUBTASK_PAYLOAD_SCHEMA_ENABLED and "
            "NOUS_SUBTASK_HARDENING_ENABLED are true but no runner is wired; "
            "spawn_sync NOT registered. spawn_sync requires an inline runner."
        )
    elif settings.subtask_payload_schema_enabled:
        logger.warning(
            "F062: NOUS_SUBTASK_PAYLOAD_SCHEMA_ENABLED=true but "
            "NOUS_SUBTASK_HARDENING_ENABLED=false; spawn_sync NOT registered. "
            "F062 requires F061's hardened executor — enable both flags to use it."
        )


# ---------------------------------------------------------------------------
# Programmatic tool calling (012.3)
# ---------------------------------------------------------------------------

_MAX_WRITES = 5

# Seconds the awaiting coroutine waits past the script deadline before giving
# up on the worker thread. The in-thread trace hook normally raises at the
# deadline, so this grace only matters when the thread is stuck below Python
# level (a blocking C call), which no in-process mechanism can interrupt.
_TIMEOUT_GRACE = PROGRAMMATIC_TOOLS_TIMEOUT_GRACE_SECONDS

# Keys the pre-2026-08-25 in-script `recall_deep` returned, via
# `FactSummary.model_dump()`, mapped to the value used when the field does not
# apply to a result type. Facts get real values out of `metadata`; episodes,
# chunks, procedures and decisions get the default, so key ACCESS never raises
# on a mixed list.
#
# DERIVED from FactSummary rather than hand-listed. A hand-written list drifted
# twice in one review cycle — first missing active/tags/superseded_by, then
# actionable/actionable_confidence/overrides_prior/recency_status/recency_date —
# because nothing tied it to the model it claims to reproduce. Now a new field on
# FactSummary joins this map automatically.
#
# `id`/`content`/`score` are excluded: the result dict already owns those, and
# they must never be overwritten by a metadata value.
#
# These are FALLBACKS FOR NON-FACT ROWS ONLY. Fact rows carry their real values:
# `Heart._to_recall_result` propagates the persisted FactSummary fields into
# metadata, so a fact reaching this map already has `active`, `tags`,
# `actionable`, `superseded_by` and friends populated and the default is never
# consulted. That propagation is the load-bearing half — without it these
# defaults would stop scripts raising KeyError only to have them silently decide
# from fabricated values instead, which is strictly worse than the crash: a
# script filtering `[f for f in facts if f["active"]]` would drop every fact and
# look like it simply found nothing.
#
# Default policy is deliberately NOT the model's own defaults: `overrides_prior`
# defaults to False on FactSummary, but asserting False about an episode is
# fabricating an answer to a question that does not apply to it. None means "not
# applicable", which is the truth. `tags` is the one exception — an empty list is
# genuinely true for a non-fact and keeps iteration safe.
_LEGACY_FACT_KEYS: dict[str, Any] = {
    _name: ([] if _name == "tags" else None)
    for _name in FactSummary.model_fields
    if _name not in ("id", "content", "score")
}

# Trace-hook check interval — number of traced events between deadline checks.
# Keeps the per-line overhead down without letting an overrun go unnoticed.
_DEADLINE_CHECK_EVERY = 64

_active_runs = 0
_active_runs_lock = threading.Lock()


class ScriptDeadlineExceeded(BaseException):
    """Raised inside the run_python worker thread when its deadline passes.

    Derives from BaseException, not Exception, so ordinary agent code using
    `try/except Exception` cannot swallow its own timeout.
    """


def run_python_active_runs() -> int:
    """Number of run_python executions currently occupying a worker thread."""
    with _active_runs_lock:
        return _active_runs


def _acquire_run_slot(max_concurrent: int) -> bool:
    """Take a concurrency slot if one is free."""
    global _active_runs
    with _active_runs_lock:
        if _active_runs >= max_concurrent:
            return False
        _active_runs += 1
        return True


def _release_run_slot() -> None:
    global _active_runs
    with _active_runs_lock:
        _active_runs -= 1


def create_programmatic_tools(
    brain: Brain,
    heart: Heart,
    settings: Settings,
    episode_id_resolver: "Callable[[str], str | None] | None" = None,
) -> dict[str, Any]:
    """Create run_python tool closure for client-side programmatic execution.

    Returns a dict with a single "run_python" async callable.
    The closure captures heart (for memory wrappers) and settings (for timeout).

    episode_id_resolver: optional callable(session_id) -> episode_id_str | None.
    When provided, run_python captures the active episode at call time and
    injects it into the _learn_fact closure so script-learned facts get
    fact→episode edges (F022 P2-1 fix).
    """
    import asyncio
    import builtins
    import collections
    import concurrent.futures
    import datetime
    import functools
    import io
    import itertools
    import json
    import math
    import re
    import statistics

    async def run_python(
        code: str,
        _session_id: str | None = None,
        _turn_number: int | None = None,
    ) -> dict[str, Any]:
        """Execute Python code with Nous memory functions in scope."""
        write_count = {"n": 0}
        output_buf = io.StringIO()
        # F022 P2-1: Capture active episode at call time so _learn_fact can
        # link script-learned facts to the current episode without the model
        # needing to pass the UUID.
        _active_episode_id: str | None = None
        if _session_id and episode_id_resolver is not None:
            _active_episode_id = episode_id_resolver(_session_id)

        # F071 + F055 parity, resolved HERE on the main loop.
        #
        # Both are read from state the worker thread cannot see:
        # `CURRENT_TURN_EXCLUDE_IDS` is a ContextVar (not propagated into an
        # executor thread), and the activator calls are coroutines. Without
        # these, in-script recall ranks against cold state and can return
        # memories already in the system prompt — i.e. the "same retrieval as
        # the tool" claim would be false in exactly the way that is hardest to
        # notice, since the results still look plausible.
        # `NOUS_RESIDUAL_ACTIVATION_ENABLED=true` in prod, so this is live, not
        # theoretical.
        from nous.api.runner import CURRENT_TURN_EXCLUDE_IDS
        _script_exclude_ids = CURRENT_TURN_EXCLUDE_IDS.get()

        # F055 state is resolved LAZILY, on the first in-script recall — see
        # `_load_residual` below. Doing it here would charge every run_python
        # call two DB reads even when the script never calls recall_deep, and
        # would do it OUTSIDE both the run slot and the script deadline, so a
        # slow activation read could eat the dispatcher's remaining timeout
        # before the script's own deadline had started.
        _residual_state: dict[str, Any] = {"loaded": False, "turn": 0, "acts": {}}
        _residual_on = (
            getattr(settings, "residual_activation_enabled", False)
            and bool(_session_id)
            and getattr(heart, "_residual_activator", None) is not None
        )

        # Get the running event loop so threads can schedule DB coroutines back on it.
        # Using run_coroutine_threadsafe instead of asyncio.run() prevents deadlocks:
        # asyncio.run() creates a NEW loop in the thread, which can't access the
        # connection pool belonging to the main loop. run_coroutine_threadsafe schedules
        # the coroutine on the MAIN loop while it's awaiting the executor — no deadlock.
        loop = asyncio.get_running_loop()
        timeout = settings.programmatic_tools_timeout
        deadline = time.monotonic() + timeout

        def _schedule(coro):
            """Schedule a coroutine on the main loop and block the thread until done.

            Uses deadline-relative timeout so per-call budget tracks remaining
            wall time — prevents thread from outliving the main asyncio.wait_for.

            CANCELS the future on timeout. `Future.result(timeout=...)` only
            stops waiting; without the cancel the coroutine keeps running on the
            main loop after the worker has reported an error and released its
            concurrency slot — so stalled work accumulates OUTSIDE
            `programmatic_tools_max_concurrent`, and a still-running
            `run_recall_pipeline` can go on doing DB work and mutating a trace
            that has already been committed. Latent before, and much likelier
            now that a single call is a ~5s retrieval rather than a fact lookup.
            """
            remaining = max(0.1, deadline - time.monotonic())
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
            try:
                return fut.result(timeout=remaining)
            except TimeoutError:
                # Since 3.11 `concurrent.futures.TimeoutError` IS the builtin
                # `TimeoutError`, so this one clause covers both.
                fut.cancel()
                raise

        def _load_residual() -> None:
            """Resolve F055 state on first use, inside the script's deadline.

            Runs on the worker thread, so the activator's coroutines go through
            `_schedule` — which means these reads are bounded by the script
            deadline and charged to a held run slot, unlike an eager read before
            the executor. Fails open to cold recall, exactly as the tool does.
            """
            if _residual_state["loaded"] or not _residual_on:
                return
            _residual_state["loaded"] = True
            try:
                act = heart._residual_activator
                turn = _schedule(act.current_turn(brain.agent_id, _session_id))
                _residual_state["turn"] = turn
                _residual_state["acts"] = _schedule(
                    act.compute_activations(brain.agent_id, _session_id, turn)
                )
            except Exception:
                logger.warning(
                    "F055: compute_activations failed for %s/%s in run_python, "
                    "continuing cold", brain.agent_id, _session_id, exc_info=True,
                )
                _residual_state["acts"] = {}

        def _recall_deep(query: str, limit: int = 5) -> list[dict]:
            """The same retrieval the `recall_deep` TOOL runs — not a fact search.

            Until 2026-08-25 this was `heart.search_facts`, i.e. a hybrid search
            over `heart.facts` alone: no episodes, procedures or decisions, no
            F067 chunk leg, no graph expansion, no spreading activation, no
            relevance floor, no recency resolution. It shared a name with the
            tool and almost nothing else, so a script that "batched memory
            lookups" — precisely what this tool's description recommends — got a
            quietly weaker answer than the same query issued as a tool call,
            with nothing in the output to signal the downgrade.

            That is also why script recalls never appeared on the F091 retrieval
            dashboard: telemetry instruments `run_recall_pipeline`, and this
            never reached it. The rows were not dropped; there was no retrieval
            to log.
            """
            from nous.api.retrieval_pipeline import run_recall_pipeline

            _load_residual()

            # Mirrors the tool's `chunks_rerank`. A script passes no
            # memory_types, so the tool's `search_all` branch is always taken and
            # the condition collapses to the chunk flag alone.
            rerank = getattr(settings, "episode_chunks_enabled", False)

            _rl = get_active_retrieval_logger()
            # path="script", not "pipeline": being able to tell script recalls
            # apart from tool recalls is the whole reason this gap was noticed,
            # and blending them would destroy that the moment it was fixed.
            # turn_number is None — a script runs wholly inside one tool call and
            # has no turn of its own to attribute to.
            _tr = (
                _rl.start(
                    query=query, path="script", session_id=_session_id,
                    # Several in-script recalls legitimately share one turn.
                    turn_number=_turn_number,
                )
                if _rl is not None else None
            )
            def _commit(tr) -> None:
                """Commit from the MAIN loop, never from this worker thread.

                `RetrievalLogger.commit` schedules the DB write via
                `_schedule_bg`, which calls `asyncio.get_running_loop()` — in an
                executor thread that raises, and the except branch calls
                `coro.close()`. The write would be silently discarded and the
                trace would survive only in the in-memory ring, which has no
                reader (both dashboard endpoints query Postgres). Every script
                retrieval would therefore be recorded nowhere — the exact
                invisibility this change exists to end. `call_soon_threadsafe`
                marshals it back, mirroring what `_schedule` does for coroutines.
                """
                if _rl is None or tr is None:
                    return
                try:
                    loop.call_soon_threadsafe(_rl.commit, tr)
                except Exception:
                    logger.debug(
                        "F091: script retrieval trace commit failed", exc_info=True,
                    )

            def _fail_trace(exc: BaseException) -> None:
                """Commit the PARTIAL trace: a crashed retrieval must not look
                like one that never happened. Mirrors the tool's error path.

                Runs with the deadline tracer OFF. The tracer fires per line and
                re-arms after each raise, so on a genuine timeout it would raise
                AGAIN partway through `undeliver_all`/`finalize` — which walk
                every captured candidate — and the timeout row would be lost
                despite this handler existing to save it.

                Uses `_original_settrace`, NOT `sys.settrace`: `_run` rebinds
                `sys.settrace` to `_settrace_shim`, which discards its argument
                and reinstalls `_tracer`. Calling `sys.settrace(None)` here
                therefore RE-ARMS the deadline instead of clearing it, which is
                what the first version of this fix did — inert, and looking
                exactly like a fix. Per-thread, and `_run`'s finally restores
                the real hook regardless, so this stays local.
                """
                if _tr is None:
                    return
                # Off for the duration of cleanup, then RESTORED unless the
                # script is already dead. An ordinary Exception here is
                # catchable: a script doing `try: recall_deep(...) except
                # Exception: pass` carries on running, and leaving the tracer
                # off would hand it a thread with no deadline enforcement at all
                # — reopening the `while True: pass` hole the tracer exists to
                # close, holding a worker and a concurrency slot indefinitely.
                # Only ScriptDeadlineExceeded means the script cannot continue.
                _is_deadline = isinstance(exc, ScriptDeadlineExceeded)
                try:
                    _original_settrace(None)
                except Exception:  # pragma: no cover - defensive
                    pass
                try:
                    _tr.undeliver_all(SLICED_OFF, "script_recall_failed")
                    _tr.leg("script_recall", attempted=True, error=str(exc)[:200])
                    _tr.finalize([])
                    _commit(_tr)
                finally:
                    if not _is_deadline:
                        try:
                            _original_settrace(_tracer)
                        except Exception:  # pragma: no cover - defensive
                            pass

            # EVERYTHING that decides what the script receives sits inside this
            # try, and the success trace is committed only after `out` exists.
            # The `_schedule` deadline can expire during the backfill or the
            # dict construction, and a trace committed before those would assert
            # `returned_to_script` for every survivor while the script actually
            # received a timeout error and nothing at all. The tool commits late
            # for the same reason (see the note at its own commit site: the
            # parent-episode fetch still changes what reaches the model).
            try:
                results, _stats = _schedule(run_recall_pipeline(
                    query=query, heart=heart, brain=brain, settings=settings,
                    limit=limit, rerank_by_score=rerank, trace=_tr,
                    residual_activations=_residual_state["acts"] or None,  # F055
                    exclude_ids=_script_exclude_ids,  # F071
                ))
                out = _apply_script_limit(
                    results, _build_script_results(results), limit, _tr,
                )
            except (Exception, ScriptDeadlineExceeded) as e:
                # ScriptDeadlineExceeded derives from BaseException ON PURPOSE,
                # so agent code cannot swallow its own timeout with
                # `except Exception` — which means a bare `except Exception`
                # here misses the single likeliest failure in this block. The
                # deadline firing mid-conversion is exactly the case this
                # error path exists for, and it would otherwise commit no trace
                # at all.
                _fail_trace(e)
                raise

            # `run_recall_pipeline` has already called `finalize`, which marks its
            # survivors RENDERED — a claim that is true on the tool path and FALSE
            # here. These results were delivered to the Python interpreter, and
            # the script decides what (if anything) the model ever sees; filtering
            # and aggregating is the entire purpose of run_python. Leaving them
            # `rendered` would inflate n_rendered and every disposition rollup
            # with a population the model may never have seen. We cannot know what
            # the script emits, so we record what IS true: returned to the script.
            if _tr is not None:
                _tr.undeliver_all(RETURNED_TO_SCRIPT, "run_python")
            _commit(_tr)

            # F055: advance activation for later turns. Without this a scripted
            # recall reads residual state but never contributes to it, so the
            # session's activation depends on which calling path was used.
            # Scheduled onto the main loop (the activator is async and this is a
            # worker thread) and never awaited — matching the tool, where it is
            # deliberately fire-and-forget so the request returns immediately.
            # Same predicate as the read side, by reference rather than
            # restated: a consumer that re-derives its producer's control flow
            # is one edit away from disagreeing with it.
            if _residual_on:
                try:
                    # Only what the script ACTUALLY received. `results` may be
                    # longer than `out` after the script limit, and recording
                    # the excess would boost memories the interpreter never saw
                    # on later turns — telemetry saying "excluded" while ranking
                    # state says "delivered". Index-aligned, since
                    # `_build_script_results` emits one dict per result in order
                    # and the limit only truncates the tail.
                    _surfaced = [
                        (
                            r.id, r.type,
                            float(r.score) if r.score is not None else 0.0,
                            (r.description or "")[:160],
                        )
                        for r in results[:len(out)]
                    ]
                    loop.call_soon_threadsafe(
                        lambda: asyncio.ensure_future(
                            heart._residual_activator.record_surfaced(
                                agent_id=brain.agent_id,
                                session_id=_session_id,
                                current_turn=_residual_state["turn"] + 1,
                                surfaced=_surfaced,
                            )
                        )
                    )
                except Exception:
                    logger.warning(
                        "F055: failed to schedule record_surfaced from run_python",
                        exc_info=True,
                    )

            return out

        def _build_script_results(results: list) -> list[dict]:
            """Convert pipeline results into the dicts the script receives.

            Separated so it runs INSIDE the caller's try: the `_schedule`
            deadline can expire in here, and a success trace committed before
            this returns would claim `returned_to_script` for rows the script
            never got.
            """
            # Backfill legacy fields for fact rows whose leg did not carry them.
            # Only the primary Heart leg populates them in metadata; the keyed,
            # keyed_r2, exemplar and graph-expansion legs build their own
            # metadata from narrower SELECTs — and all four are ENABLED in prod,
            # so without this a script filtering on `active` or `confidence`
            # silently misclassifies a fact based on which leg happened to find
            # it. Keyed off the MISSING FIELD, not the leg, so a leg added later
            # is covered for free. One batched query, and it fetches nothing
            # when every fact already arrived complete.
            _needs = [
                r.id for r in results
                if r.type == "fact" and "active" not in r.metadata
            ]
            _filled: dict = {}
            if _needs:
                try:
                    _filled = _schedule(heart.facts.fetch_legacy_fields(_needs))
                except Exception:
                    # Better to fall through to the None defaults than to fail
                    # the whole recall over a compatibility backfill.
                    logger.warning(
                        "run_python: legacy fact field backfill failed",
                        exc_info=True,
                    )

            out: list[dict] = []
            for r in results:
                d = {
                    # UUID, not str. The old return was
                    # `FactSummary.model_dump()` in python mode, whose `id` is a
                    # UUID, and `recall_recent` still returns python-mode values
                    # — so stringifying here both breaks `f["id"].hex` / UUID
                    # comparisons in stored scripts AND makes the two wrappers
                    # disagree. `str()` would be friendlier to `json.dumps`, but
                    # that was never the contract.
                    "id": r.id,
                    "type": r.type,
                    "description": r.description,
                    # Alias: the old return was a `FactSummary`, whose body lived
                    # under "content".
                    "content": r.description,
                    "score": r.score,
                    "source": r.source,
                    "edge_relation": r.edge_relation,
                    "metadata": r.metadata,
                }
                # Legacy FactSummary keys, present on EVERY item.
                #
                # `Heart._to_recall_result` nests category/subject/confidence
                # under `metadata`, so `f["confidence"]` would raise KeyError
                # against a dict holding the value one level down. Populating
                # them only when present is not enough either: the old function
                # returned FACTS ONLY, so `sorted(recall_deep(q), key=lambda f:
                # f["confidence"])` was valid — and the pipeline returns mixed
                # types, so the first episode or procedure in the list would
                # raise. The keys are therefore always present.
                #
                # `None` where a field does not apply, never a stand-in value: a
                # fabricated `confidence` of 0.0 would sort and compare as real
                # data. A None makes a sort fail loudly instead, which is the
                # honest outcome for a list that genuinely is no longer
                # all-facts. Canonical keys are never overwritten.
                _extra = _filled.get(r.id, {}) if r.type == "fact" else {}
                for _k, _default in _LEGACY_FACT_KEYS.items():
                    if _k in d:
                        continue
                    if _k in r.metadata:
                        d[_k] = r.metadata[_k]
                    elif _k in _extra:
                        d[_k] = _extra[_k]
                    else:
                        # Copy a mutable default — otherwise every result shares
                        # ONE `tags` list and a script appending to one row
                        # silently mutates the whole batch.
                        d[_k] = list(_default) if isinstance(_default, list) else _default
                # `event_date` was a `datetime.date` on FactSummary, so scripts
                # do `f["event_date"].year` or compare it with a date. The
                # PIPELINE standardises on an ISO string (F075 isoformats it
                # into metadata), which is fine for its own consumers but is a
                # silent type change at this boundary — a comparison against a
                # date stops matching instead of failing. Coerce back here only;
                # `metadata` keeps the pipeline's string.
                _ed = d.get("event_date")
                if isinstance(_ed, str):
                    try:
                        d["event_date"] = date.fromisoformat(_ed)
                    except ValueError:
                        pass  # unparseable — leave it rather than invent a date
                out.append(d)
            return out

        def _apply_script_limit(
            results: list, out: list[dict], limit: int, tr,
        ) -> list[dict]:
            """Honour `limit` as a cap on what the SCRIPT receives.

            `heart.search_facts(..., limit=limit)` returned at most that many
            items, and scripts pass `limit` to bound their own processing. The
            pipeline treats it as the core Heart/Brain allotment only: chunks
            use `episode_chunk_recall_limit` (30 in prod), the keyed and
            exemplar legs use their own K, and graph rows are appended
            independently — so `recall_deep(q, limit=4)` could hand back dozens
            of rows. Restoring the cap keeps the documented contract.

            The trace is corrected to match: rows cut here reach the script no
            more than a row cut at a gate does, so leaving them
            `returned_to_script` would make the telemetry claim a delivery that
            did not happen. Marked BEFORE the caller's `undeliver_all`, which
            only touches candidates still `RENDERED`.
            """
            # `limit=0` means ZERO rows, not "no cap". The old path passed it to
            # SQL as `LIMIT 0`, and this tool's own description promises at most
            # `limit` dicts — so lumping it in with None turned the tightest
            # possible bound into an unbounded one, which is what a dynamically
            # computed budget of 0 would hit. Only None means uncapped.
            if limit is None:
                return out
            limit = max(0, limit)
            if len(out) <= limit:
                return out
            if tr is not None:
                # A candidate is keyed on (id, type) and the pipeline can emit
                # the same one twice — a decision reached through BOTH the graph
                # and Brain legs, which deliberately do not cross-dedup. If one
                # copy lands in the delivered prefix and the other in the tail,
                # marking the tail copy downgrades the SHARED candidate, and the
                # caller's `undeliver_all` cannot undo it because that only
                # rewrites candidates still `rendered`. The script did receive
                # that memory, so reporting a drop would be false.
                _delivered = {(r.id, r.type) for r in results[:limit]}
                for r in results[limit:]:
                    if (r.id, r.type) in _delivered:
                        continue
                    tr.mark_not_delivered(r.id, r.type, SLICED_OFF, "script_limit")
            return out[:limit]

        def _recall_recent(hours: int = 24, limit: int = 5) -> list[dict]:
            results = _schedule(heart.list_episodes(limit=limit, hours=hours))
            return [r.model_dump() if hasattr(r, "model_dump") else r for r in results]

        def _list_tasks(status: str | None = None) -> list[dict]:
            results = _schedule(heart.subtasks.list(status=status))
            return [r.model_dump() if hasattr(r, "model_dump") else r for r in results]

        def _learn_fact(
            content: str,
            category: str = "technical",
            subject: str | None = None,
            confidence: float = 1.0,
        ) -> str:
            if write_count["n"] >= _MAX_WRITES:
                raise RuntimeError(f"learn_fact write cap ({_MAX_WRITES}) exceeded")
            write_count["n"] += 1
            from uuid import UUID as _UUID
            ep_uuid = _UUID(_active_episode_id) if _active_episode_id else None
            result = _schedule(heart.learn(FactInput(
                content=content, category=category,
                subject=subject, confidence=confidence,
                source="user_direct",  # F023/F038: +0.15 admission bonus
                source_episode_id=ep_uuid,
            )))
            # F023/F038: Handle FactRejected (user_direct gets +0.15 bonus
            # but very low-quality facts can still be rejected)
            if hasattr(result, "admitted") and not result.admitted:
                return f"rejected: {content[:60]} (score={result.composite_score:.2f})"
            return f"stored: {content[:60]}"

        def _print(*args: object) -> None:
            output_buf.write(" ".join(str(a) for a in args) + "\n")

        namespace: dict[str, Any] = {
            # Full builtins: the allowlist that used to live here bought no
            # security (the same agent holds an unrestricted `bash` tool) and
            # broke harmless code — `import`, `except Exception`, class bodies.
            "__builtins__": builtins,
            # Nous memory functions
            "recall_deep": _recall_deep,
            "recall_recent": _recall_recent,
            "list_tasks": _list_tasks,
            "learn_fact": _learn_fact,
            "print": _print,
            "result": None,
            # Pre-injected for convenience — `import` also works.
            "json": json,
            "re": re,
            "math": math,
            "datetime": datetime,
            "collections": collections,
            "itertools": itertools,
            "functools": functools,
            "statistics": statistics,
        }

        # Deadline enforcement inside the worker thread. asyncio.wait_for only
        # cancels the *await* — the thread keeps running — so a pure-Python
        # spin loop (`while True: pass`) would otherwise hold a thread (and the
        # GIL) inside the API process forever. A trace hook fires on every line
        # of the executing script and raises once the deadline passes.
        countdown = [_DEADLINE_CHECK_EVERY]

        def _tracer(frame, event, arg):  # noqa: ANN001, ANN202 - CPython trace signature
            countdown[0] -= 1
            if countdown[0] <= 0:
                countdown[0] = _DEADLINE_CHECK_EVERY
                if time.monotonic() >= deadline:
                    raise ScriptDeadlineExceeded(f"execution timed out ({timeout}s)")
            return _tracer

        # P1 Fix 1: Block sys.settrace bypass (slot leak). Monkey-patch
        # sys.settrace and sys.setprofile to reinstall our tracer and
        # then silently ignore the script's call.
        _original_settrace = sys.settrace
        _original_setprofile = sys.setprofile

        def _settrace_shim(func):
            # Reinstall our deadline tracer after the script's call
            _original_settrace(_tracer)

        def _setprofile_shim(func):
            # No-op for setprofile, but keep tracer alive
            pass

        def _run() -> None:
            try:
                # Install our tracer
                sys.settrace(_tracer)
                # Replace sys.settrace/setprofile with shims in this thread
                sys.settrace = _settrace_shim
                sys.setprofile = _setprofile_shim
                exec(compile(code, "<nous_script>", "exec"), namespace)
            finally:
                # Restore original functions
                sys.settrace = _original_settrace
                sys.setprofile = _original_setprofile
                sys.settrace(None)
                _release_run_slot()

        max_concurrent = settings.programmatic_tools_max_concurrent
        if not _acquire_run_slot(max_concurrent):
            logger.warning(
                "run_python rejected: %d/%d concurrent executions in flight",
                run_python_active_runs(), max_concurrent,
            )
            return {
                "content": [{
                    "type": "text",
                    "text": (
                        f"Error: too many concurrent run_python executions "
                        f"({max_concurrent} max) — retry shortly"
                    ),
                }],
                "is_error": True,
            }

        logger.info(
            "run_python | %d chars | %d/%d slots in use\n%s",
            len(code), run_python_active_runs(), max_concurrent, code,
        )
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            # await run_in_executor releases the event loop so DB coroutines
            # scheduled via run_coroutine_threadsafe can actually execute.
            # The grace covers the trace hook's own check interval; the hook,
            # not this wait_for, is what actually stops the script.
            await asyncio.wait_for(
                loop.run_in_executor(executor, _run), timeout=timeout + _TIMEOUT_GRACE
            )
        except (asyncio.TimeoutError, ScriptDeadlineExceeded):
            # is_error per MCP (#179): lets downstream consumers (runner
            # tool_result block, compaction bulk-failure detection)
            # distinguish a real execution failure from a successful run
            # whose OUTPUT merely begins with "Error: ".
            return {
                "content": [{"type": "text", "text": f"Error: execution timed out ({timeout}s)"}],
                "is_error": True,
            }
        except Exception as e:
            return {
                "content": [{"type": "text", "text": f"Error: {type(e).__name__}: {e}"}],
                "is_error": True,
            }
        except BaseException as exc:
            # P1 Fix 2: Translate SystemExit/KeyboardInterrupt to is_error.
            # ScriptDeadlineExceeded is a BaseException so `except Exception`
            # can't swallow it. But SystemExit/KeyboardInterrupt are also
            # BaseException subclasses and would escape past the Exception
            # catch, propagate through ToolDispatcher.dispatch, and crash
            # the API process. Catch and translate to is_error.
            return {
                "is_error": True,
                "content": [{
                    "type": "text",
                    "text": f"Error: script raised {type(exc).__name__}: {exc}"
                }],
            }
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        output = output_buf.getvalue()
        result = namespace.get("result")
        text = output or (str(result) if result is not None else "OK")
        return {"content": [{"type": "text", "text": text}]}

    return {"run_python": run_python}


_RUN_PYTHON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Execute Python code with Nous memory functions in scope. "
        "Full Python: import, try/except, class and function definitions, open() all work. "
        "Memory functions: recall_deep(query, limit=5) — the SAME full retrieval "
        "as the recall_deep tool (facts, episodes, decisions, chunks, graph), "
        "returning at most `limit` dicts with id/type/description/score/source, "
        "and costing about 5s per call, so budget a handful per script, not dozens; "
        "recall_recent(hours=24, limit=5), "
        "list_tasks(status=None), learn_fact(content, category, subject, confidence). "
        "Pre-imported for convenience (no import needed): json, re, math, datetime, "
        "collections, itertools, functools, statistics — anything else, just import it. "
        "Use this to batch multiple memory lookups, filter results, and return only what's needed — "
        "reducing token usage compared to separate tool calls. "
        "Set result = <value> to return structured data. Use print() to emit text output. "
        "Runs in-process on a worker thread with a wall-clock deadline (default 90s): "
        "Python-level code is interrupted when it expires and the call returns a timeout error, "
        "so keep work short and shell out to `bash` for anything long-running. "
        "Max 5 learn_fact calls per execution."
    ),
    "properties": {
        "code": {
            "type": "string",
            "description": "Python code to execute",
        }
    },
    "required": ["code"],
}


def register_programmatic_tools(
    dispatcher: ToolDispatcher,
    brain: Brain,
    heart: Heart,
    settings: Settings,
    cognitive: "Any | None" = None,
) -> None:
    """Register run_python tool if programmatic tools are enabled.

    cognitive: optional CognitiveLayer; when provided, run_python gains
    access to the active episode ID so script-learned facts get fact→episode
    edges (F022 P2-1).
    """
    if not settings.programmatic_tools_enabled:
        return
    resolver = cognitive.get_active_episode_id if cognitive is not None else None
    closures = create_programmatic_tools(brain, heart, settings, episode_id_resolver=resolver)
    dispatcher.register("run_python", closures["run_python"], _RUN_PYTHON_SCHEMA)


# ---------------------------------------------------------------------------
# Heartbeat dynamic check tools (F034.5)
# ---------------------------------------------------------------------------


def register_heartbeat_tools(dispatcher: ToolDispatcher, loader: "Any") -> None:
    """F034.5: Register dynamic heartbeat check management tools."""

    async def heartbeat_check_create(**kwargs) -> dict:
        try:
            result = await loader.create_check(
                name=kwargs["name"],
                description=kwargs["description"],
                prompt=kwargs["prompt"],
                tools=kwargs.get("tools"),
                interval_seconds=kwargs.get("interval_seconds", 3600),
                cron_expr=kwargs.get("cron_expr"),
                timeout_seconds=kwargs.get("timeout_seconds"),
                urgent=kwargs.get("urgent", False),
                on_complete_prompt=kwargs.get("on_complete_prompt"),
                on_complete_tools=kwargs.get("on_complete_tools"),
            )
            return {"content": [{"type": "text", "text": f"Created dynamic check: {json.dumps(result)}"}]}
        except ValueError as e:
            return _tool_error(f"Error: {e}")
        except Exception as e:
            return _tool_error(f"Failed to create check: {e}")

    async def heartbeat_check_manage(**kwargs) -> dict:
        try:
            result = await loader.manage_check(
                action=kwargs["action"],
                name=kwargs.get("name"),
                updates=kwargs.get("updates"),
            )
            return {"content": [{"type": "text", "text": json.dumps(result)}]}
        except ValueError as e:
            return _tool_error(f"Error: {e}")
        except Exception as e:
            return _tool_error(f"Failed: {e}")

    dispatcher.register("heartbeat_check_create", heartbeat_check_create, {
        "type": "object",
        "description": "Create a new dynamic heartbeat check that runs on a schedule",
        "properties": {
            "name": {"type": "string", "description": "Unique check name (slug, e.g. 'arxiv-agent-papers')"},
            "description": {"type": "string", "description": "Human-readable description of what this check monitors"},
            "prompt": {"type": "string", "description": "Instruction for what to check and report on"},
            "tools": {"type": "array", "items": {"type": "string"}, "description": "Tools the check can use (allowed: web_search, web_fetch, recall_deep, recall_recent, bash, read_file, heartbeat_check_create, heartbeat_check_manage)"},
            "interval_seconds": {"type": "integer", "description": "Seconds between runs (min 300, default 3600)"},
            "cron_expr": {"type": "string", "description": "Cron expression for scheduling (overrides interval_seconds)"},
            "timeout_seconds": {"type": "integer", "description": "Max seconds per run (default from NOUS_HEARTBEAT_DEFAULT_CHECK_TIMEOUT)"},
            "urgent": {"type": "boolean", "description": "If true, runs during quiet hours too"},
            "on_complete_prompt": {"type": "string", "description": "Prompt to execute when check self-disables (callback)"},
            "on_complete_tools": {"type": "array", "items": {"type": "string"}, "description": "Tools for callback (must be subset of check tools)"},
        },
        "required": ["name", "description", "prompt"],
    })

    dispatcher.register("heartbeat_check_manage", heartbeat_check_manage, {
        "type": "object",
        "description": "List, enable, disable, delete, or update dynamic heartbeat checks",
        "properties": {
            "action": {"type": "string", "enum": ["list", "enable", "disable", "delete", "update"], "description": "Action to perform"},
            "name": {"type": "string", "description": "Check name (required for enable/disable/delete/update)"},
            "updates": {"type": "object", "description": "Fields to update when action=update (allowed: description, prompt, tools, interval_seconds, cron_expr, timeout_seconds, urgent, on_complete_prompt, on_complete_tools)"},
        },
        "required": ["action"],
    })


# ---------------------------------------------------------------------------
# DAG orchestration tools (F038)
# ---------------------------------------------------------------------------

# codex P2 round 5 (FINDING 9): `status`'s per-node result preview. [:80]
# (the `error` truncation width) is right for a classified error string and
# useless for an LLM's actual output — a subtask result is routinely
# hundreds to thousands of characters. 500 is a deliberate middle ground,
# not the `error`/`recent` widths reused blindly: enough characters for a
# genuine paragraph of prose (the shape most subtask results take), while
# DAGCreateRequest's validator caps every DAG at MAX_WAVES(4) x
# MAX_PARALLEL_PER_WAVE(4) = 16 nodes (schemas.py), so worst case this adds
# ~8000 chars to `status` — a bigger overview, not a flood. `status` stays
# an OVERVIEW, not the recovery path — a truncated line always says how
# much was cut and names `node_result`, the lossless per-node retrieval
# action, rather than silently slicing.
_STATUS_RESULT_PREVIEW_CHARS = 500


def register_dag_tools(
    dispatcher: ToolDispatcher,
    store: "Any",
    orchestrator: "Any",
) -> None:
    """F038: Register DAG orchestration tools."""
    from nous.dag.schemas import DAGCreateRequest, DAGEdgeSpec, DAGNodeSpec, DAGNodeType

    async def dag_create(**kwargs: Any) -> dict:
        """Create a DAG with dependency-tracked nodes."""
        # F087: refuse rather than create a DAG nothing will ever advance.
        # The orchestrator's only clock is the heartbeat loop, so with the
        # heartbeat disabled a created DAG launches wave-0 and then sits
        # forever — previously with no error surfaced anywhere.
        if not getattr(orchestrator, "clock_wired", True):
            return _tool_error(
                "Error: DAG execution is not wired — no heartbeat runner is "
                "active, so a created DAG would never advance past its first "
                "wave. Set NOUS_HEARTBEAT_ENABLED=true and restart."
            )
        try:
            # Parse nodes
            node_specs: list[DAGNodeSpec] = []
            for n in kwargs.get("nodes", []):
                # F066.1 (2026-05-23): the orchestrator + schemas + migration
                # shipped fix-node support, but this handler historically only
                # threaded a fixed set of fields into DAGNodeSpec — so fix-node
                # fields (parent_node, fix_actions, max_fix_attempts,
                # expected_modes) were silently dropped at the tool surface,
                # making fix nodes unauthorable via dag_create. Same silent-
                # drop pattern affected F064.1's stall_timeout_seconds. The
                # block below threads every field the schema accepts so
                # future field additions to DAGNodeSpec land in dag_create
                # automatically as long as the tool schema description in
                # register() also exposes them.
                node_data: dict[str, Any] = {
                    "name": n["name"],
                    "type": DAGNodeType(n["type"]),
                    "instructions": n.get("instructions", ""),
                    "description": n.get("description", ""),
                    "tools": n.get("tools"),
                    "frame_type": n.get("frame_type"),
                    "model": n.get("model"),
                    "timeout_seconds": n.get("timeout_seconds"),
                    "completion_condition": n.get("completion_condition"),
                    "completion_check": n.get("completion_check"),
                    "completion_check_interval": n.get("completion_check_interval"),
                    "max_check_attempts": n.get("max_check_attempts"),
                    "stall_timeout_seconds": n.get("stall_timeout_seconds"),
                    # F066.1 fix-node fields. Optional except for type='fix';
                    # the DAGCreateRequest validator enforces parent_node +
                    # non-empty fix_actions when type=='fix'.
                    "parent_node": n.get("parent_node"),
                    "fix_actions": n.get("fix_actions"),
                    "expected_modes": n.get("expected_modes", []),
                }
                # max_fix_attempts has no nullable type (ge=1, le=3). Only
                # forward it if the caller supplied a value so pydantic's
                # default (1) applies otherwise.
                if "max_fix_attempts" in n:
                    node_data["max_fix_attempts"] = n["max_fix_attempts"]
                node_specs.append(DAGNodeSpec(**node_data))

            # Parse edges
            edge_specs: list[DAGEdgeSpec] = []
            for e in kwargs.get("edges", []):
                edge_specs.append(DAGEdgeSpec(
                    from_node=e["from_node"],
                    to_node=e["to_node"],
                    edge_type=e.get("edge_type", "dependency"),
                ))

            request = DAGCreateRequest(
                name=kwargs["name"],
                description=kwargs.get("description", ""),
                source=kwargs.get("source", "conversation"),
                token_budget=kwargs.get("token_budget"),
                nodes=node_specs,
                edges=edge_specs,
            )

            dag = await store.create(request)
            await orchestrator.start_dag(dag.id)

            # Re-fetch to get actual status
            started_dag = await store.get_dag(dag.id)
            actual_status = started_dag.status if started_dag else "unknown"

            # Compute wave summary
            waves = request.compute_waves()
            wave_groups: dict[int, list[str]] = {}
            for name, wave in waves.items():
                wave_groups.setdefault(wave, []).append(name)

            lines = [f"Created DAG '{dag.name}' ({str(dag.id)[:8]})"]
            lines.append(f"  {len(dag.nodes)} nodes, {len(dag.edges)} edges")
            for w in sorted(wave_groups):
                lines.append(f"  Wave {w}: {', '.join(wave_groups[w])}")
            lines.append(f"Status: {actual_status}")

            return {"content": [{"type": "text", "text": "\n".join(lines)}]}
        except Exception as e:
            logger.exception("dag_create failed")
            return _tool_error(f"Error creating DAG: {e}")

    async def dag_manage(**kwargs: Any) -> dict:
        """List, inspect, cancel, or retry nodes in DAGs."""
        action = kwargs["action"]
        dag_id_str = kwargs.get("dag_id")

        try:
            if action == "list":
                dags = await store.get_active_dags()
                # F087: an unwired clock is the difference between "nothing is
                # running" and "nothing can ever run" — say which.
                if not getattr(orchestrator, "clock_wired", True):
                    warning = (
                        "WARNING: DAG execution is not wired (no heartbeat "
                        "runner) — DAGs cannot advance."
                    )
                    if not dags:
                        return {"content": [{"type": "text", "text": warning}]}
                    lines = [warning, f"Active DAGs ({len(dags)}):"]
                elif not dags:
                    return {"content": [{"type": "text", "text": "No active DAGs."}]}
                else:
                    lines = [f"Active DAGs ({len(dags)}):"]
                for d in dags:
                    completed = sum(1 for n in d.nodes if n.status == "completed")
                    total = len(d.nodes)
                    lines.append(f"  {str(d.id)[:8]} | {d.name} | {d.status} | {completed}/{total} nodes done")
                return {"content": [{"type": "text", "text": "\n".join(lines)}]}

            if action == "recent":
                # F090.3: `list` serves only pending/running. A finished DAG
                # was reachable by `status` only if you already knew its id
                # prefix — there was no way to DISCOVER one, which made the
                # F087 delivery notification the sole record of an outcome.
                #
                # codex P2: filtering to finished status in PYTHON after
                # get_recent_dags' created_at-ordered LIMIT meant a DAG that
                # finished after `limit` newer DAGs were CREATED never
                # appeared here — hitting exactly the long-running DAGs this
                # action exists to surface. get_recent_finished_dags filters
                # to terminal status and orders by completed_at in SQL
                # instead, so the limit applies to the finished population.
                finished = await store.get_recent_finished_dags(limit=20)
                if not finished:
                    return {"content": [{"type": "text",
                                         "text": "No finished DAGs."}]}
                lines = [f"Recent finished DAGs ({len(finished)}):"]
                for d in finished:
                    done = sum(1 for n in d.nodes if n.status == "completed")
                    when = d.completed_at.strftime("%Y-%m-%d %H:%M") if d.completed_at else "—"
                    lines.append(
                        f"  {str(d.id)[:8]} | {d.name} | {d.status} | "
                        f"{done}/{len(d.nodes)} nodes | {when}"
                    )
                    # codex P2: result_summary is the generic constant
                    # _check_dag_completion writes ("All nodes completed
                    # successfully", "Failed nodes: ...") -- never the real
                    # outcome. delivery_summary is the agent-authored prose
                    # F087 caches for exactly this purpose (delivery.py,
                    # ahead of retries), so prefer it when present.
                    summary = d.delivery_summary or d.result_summary
                    if summary:
                        lines.append(f"      {summary[:120]}")
                return {"content": [{"type": "text", "text": "\n".join(lines)}]}

            if not dag_id_str:
                return _tool_error("Error: dag_id required for this action")

            # Support 8-char prefix lookup
            dag = await _resolve_dag(store, dag_id_str)
            if dag is None:
                return _tool_error(f"Error: DAG '{dag_id_str}' not found")

            if action == "status":
                status_icons = {
                    "completed": "+", "failed": "X", "running": ">",
                    "ready": "~", "pending": ".", "blocked": "!", "cancelled": "-",
                    "awaiting_check": "*",
                }
                lines = [
                    f"DAG: {dag.name} ({str(dag.id)[:8]})",
                    f"Status: {dag.status}",
                    f"Nodes ({len(dag.nodes)}):",
                ]
                for node in sorted(dag.nodes, key=lambda n: (n.wave or 0, n.name)):
                    icon = status_icons.get(node.status, "?")
                    wave_str = f"w{node.wave}" if node.wave is not None else "w?"
                    line = f"  [{icon}] {node.name} ({node.node_type}, {wave_str}) — {node.status}"
                    if node.status == "awaiting_check" and hasattr(node, 'check_attempts'):
                        line += f" | polls: {node.check_attempts}"
                    if node.error:
                        line += f" | error: {node.error[:80]}"
                    # codex P2 round 5 (FINDING 9): result was rendered
                    # nowhere here -- an agent recovering a missed delivery
                    # could see THAT a node finished but not WHAT it
                    # produced. Not `elif` on error: a failed node can carry
                    # both (F061 outcome-aware branch sets error to the
                    # classified message and result to the subtask's raw
                    # output in the same update). Truncation is never
                    # silent -- FINDING 9 exists because the original [:80]
                    # slice gave no indication anything was cut, let alone
                    # how to get the rest.
                    if node.result:
                        result = node.result
                        if len(result) > _STATUS_RESULT_PREVIEW_CHARS:
                            preview = result[:_STATUS_RESULT_PREVIEW_CHARS]
                            line += (
                                f" | result: {preview} "
                                f"[truncated, {len(result)} chars total -- "
                                f'use dag_manage(action="node_result", '
                                f'dag_id="{str(dag.id)[:8]}", '
                                f'node_name="{node.name}") for the full result]'
                            )
                        else:
                            line += f" | result: {result}"
                    lines.append(line)
                return {"content": [{"type": "text", "text": "\n".join(lines)}]}

            elif action == "cancel":
                await orchestrator.cancel_dag(dag.id, reason="cancelled by user")
                return {"content": [{"type": "text", "text": f"Cancelled DAG '{dag.name}' ({str(dag.id)[:8]})"}]}

            elif action == "retry_node":
                node_name = kwargs.get("node_name")
                if not node_name:
                    return _tool_error("Error: node_name required for retry_node")
                await orchestrator.retry_node(dag.id, node_name)
                return {"content": [{"type": "text", "text": f"Reset node '{node_name}' to pending for retry"}]}

            elif action == "node_result":
                # codex P2 round 5 (FINDING 9): the lossless retrieval path
                # `status`'s truncated preview points at. Full node.result,
                # byte-for-byte -- no cap here. `status` is the bounded
                # overview; this is the deliberately unbounded escape hatch,
                # scoped to exactly the one node asked for.
                node_name = kwargs.get("node_name")
                if not node_name:
                    return _tool_error("Error: node_name required for node_result")
                node = next((n for n in dag.nodes if n.name == node_name), None)
                if node is None:
                    available = ", ".join(sorted(n.name for n in dag.nodes)) or "(none)"
                    return _tool_error(
                        f"Error: node '{node_name}' not found in DAG "
                        f"'{dag.name}' ({str(dag.id)[:8]}). "
                        f"Available nodes: {available}"
                    )
                if node.result is None:
                    return {"content": [{"type": "text", "text": (
                        f"Node '{node_name}' has no result yet (status: {node.status})"
                    )}]}
                return {"content": [{"type": "text", "text": node.result}]}

            else:
                return _tool_error(f"Error: unknown action '{action}'")

        except Exception as e:
            logger.exception("dag_manage failed")
            return _tool_error(f"Error: {e}")

    dispatcher.register("dag_create", dag_create, {
        "type": "object",
        "description": (
            "Create a DAG to orchestrate subtasks and checks with dependency tracking. "
            "You do NOT need to poll for the result: when the DAG reaches a terminal "
            "state its outcome is delivered to you automatically (F087), so create it "
            "and move on. Use dag_manage only when the user asks about progress "
            "mid-flight, to cancel or retry, or to look up a DAG whose delivery you "
            "missed or that finished before this session (dag_manage action='recent')."
        ),
        "properties": {
            "name": {"type": "string", "description": "DAG name"},
            "description": {"type": "string"},
            "nodes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        # F066.1 (2026-05-23): added "fix" so fix-stage
                        # recovery nodes are authorable. Phase 1 ships
                        # rule-based dispatch; Phase 1.5 (NOUS_DAG_FIX_LLM_
                        # DISPATCH_ENABLED) routes to Haiku tool-use.
                        "type": {
                            "type": "string",
                            "enum": ["subtask", "check", "gate", "callback", "fix"],
                            "description": (
                                "'callback' runs AFTER its predecessors and receives "
                                "their results as context — use it to interpret or act "
                                "on what earlier nodes produced (point a context_flow "
                                "edge at it). It accepts frame_type / model / "
                                "timeout_seconds like a subtask. Requires "
                                "NOUS_DAG_CALLBACK_EXECUTION_ENABLED=true; with the flag "
                                "off a callback completes instantly without running. "
                                "'gate' currently auto-passes — it is a marker, not an "
                                "enforced quality check. Note: 'tools' below is honored "
                                "ONLY for 'check' nodes — on every other node type "
                                "(subtask, callback, gate, fix) it is silently ignored."
                            ),
                        },
                        "instructions": {"type": "string"},
                        "tools": {"type": "array", "items": {"type": "string"}},
                        "frame_type": {"type": "string"},
                        "model": {"type": "string"},
                        "timeout_seconds": {"type": "integer", "minimum": 1, "description": "Execution timeout in seconds (default: NOUS_DAG_NODE_DEFAULT_TIMEOUT, ceiling: NOUS_DAG_NODE_MAX_TIMEOUT). F087: now a REAL bound — a node still executing past this plus NOUS_DAG_NODE_TIMEOUT_GRACE_SECONDS is cancelled and failed, so size it to the work rather than leaving the default on a long job."},
                        "stall_timeout_seconds": {"type": "integer", "minimum": 0, "description": "F064.1: max seconds without activity before failing this node. 0 = disabled for this node. Unset = inherit NOUS_DAG_NODE_DEFAULT_STALL_TIMEOUT."},
                        "completion_condition": {"type": "string"},
                        "completion_check": {"type": "string", "description": "Shell command polled each tick. Exit 0 = success, 1 = failed, 2 = still running."},
                        "completion_check_interval": {"type": "integer", "description": "Seconds between completion check polls (default: every tick)"},
                        "max_check_attempts": {"type": "integer", "description": "Max poll attempts before node fails"},
                        # F066.1 — fix-stage recovery fields. Only meaningful
                        # when type='fix'; the DAGCreateRequest validator
                        # enforces parent_node + non-empty fix_actions for
                        # fix nodes and rejects these fields on non-fix nodes.
                        "parent_node": {"type": "string", "description": "F066.1 (type='fix' only): name of the node this fix attaches to. Fires when the parent transitions to 'failed'. The matching 'on_failure' edge MUST point from the parent to this fix node — i.e. from_node = (this parent_node value), to_node = (the fix node's own name). Pointing the edge the other direction fails validation."},
                        "fix_actions": {
                            "type": "array",
                            "items": {"type": "string", "enum": ["retry_as_is", "retry_with_amended_prompt", "mark_unrecoverable", "skip_and_continue"]},
                            "description": "F066.1 (type='fix' only): allowed action vocabulary. Phase 1 dispatcher rules: incomplete/validation_failed errors → retry_as_is; timed_out → skip_and_continue (or mark_unrecoverable); other errors → skip_and_continue then mark_unrecoverable as final fallback. retry_with_amended_prompt only acts when NOUS_DAG_FIX_LLM_DISPATCH_ENABLED=true."
                        },
                        "max_fix_attempts": {"type": "integer", "minimum": 1, "maximum": 3, "description": "F066.1 (type='fix' only): max fix attempts per parent failure. Default 1."},
                        "expected_modes": {"type": "array", "items": {"type": "string"}, "description": "F066.1 (type='fix' only): declared failure modes for typed dispatch (Phase 2). Phase 1 ignores this field."},
                    },
                    "required": ["name", "type", "instructions"],
                },
            },
            "edges": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "from_node": {"type": "string"},
                        "to_node": {"type": "string"},
                        # F066.1: added "on_failure" so the fix-node attach
                        # edge is authorable. The validator requires exactly
                        # one on_failure inbound edge per fix node, with
                        # from_node == fix.parent_node.
                        "edge_type": {"type": "string", "enum": ["dependency", "cancel_cascade", "context_flow", "on_failure"]},
                    },
                    "required": ["from_node", "to_node"],
                },
            },
            "source": {"type": "string"},
            "token_budget": {"type": "integer"},
        },
        # `edges` is NOT required: the handler reads kwargs.get("edges", []) and
        # DAGCreateRequest.edges is default_factory=list, so a single-node DAG
        # legitimately omits it. Listing it here told the model a lie, and once
        # required-arg validation began trusting the schema for variadic
        # handlers that lie became a rejection. `nodes` stays required — its
        # .get default hits DAGCreateRequest's min_length=1 and fails anyway.
        "required": ["name", "nodes"],
    })

    dispatcher.register("dag_manage", dag_manage, {
        "type": "object",
        "description": (
            "List, inspect, cancel, or retry nodes in DAGs. Not needed to collect "
            "results — a finished DAG announces itself; use 'recent' only as a "
            "fallback when a delivery was missed or you want an older outcome. "
            "'recent' lists finished DAGs (completed/failed/cancelled) — the only way "
            "to find one you don't already have the id or id-prefix for. 'status' "
            f"shows every node with a preview of its result (truncated past "
            f"{_STATUS_RESULT_PREVIEW_CHARS} chars — it says so when it does, and "
            "names the node). 'node_result' returns one named node's COMPLETE "
            "result, byte-for-byte — use it whenever 'status' shows a truncated "
            "preview. 'retry_node' re-queues a failed node (and any descendants it "
            "alone blocked); it is refused on a cancelled DAG, since cancellation "
            "is deliberate."
        ),
        "properties": {
            "action": {"type": "string", "enum": ["list", "recent", "status", "cancel", "retry_node", "node_result"]},
            "dag_id": {"type": "string"},
            "node_name": {"type": "string", "description": "Required for 'retry_node' and 'node_result'; ignored by every other action."},
        },
        "required": ["action"],
    })

    logger.info("F038: Registered dag_create and dag_manage tools")


async def _resolve_dag(store: "Any", dag_id_str: str) -> "Any | None":
    """Resolve a DAG by full UUID or id prefix, any status, any age.

    codex P2 (FINDING 3): previously two Python-side scans — active DAGs,
    then a `get_recent_dags(limit=20)` created_at-bounded window — with the
    same blind spot FINDING 1 fixed for `dag_manage action=recent`: a
    finished DAG outside that window was unresolvable by prefix no matter
    how recently it finished. Collapsed into one agent-scoped SQL prefix
    match (`DAGStore.find_dags_by_id_prefix`), which also closes a dormant
    bug the two-scan version had: it could never detect an active DAG and a
    finished DAG sharing a prefix as mutually ambiguous, because it
    returned on the first pool's single match without ever consulting the
    second.

    Raises ValueError if prefix matches multiple DAGs.
    Returns None if no match found.
    """
    from uuid import UUID as _UUID

    # Try full UUID first
    try:
        dag_id = _UUID(dag_id_str)
        return await store.get_dag(dag_id)
    except ValueError:
        pass

    matches = await store.find_dags_by_id_prefix(dag_id_str)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        ids = ", ".join(str(d.id)[:8] for d in matches)
        raise ValueError(f"Prefix '{dag_id_str}' is ambiguous, matches: {ids}")

    return None
