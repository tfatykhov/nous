"""F108: the model-call recorder — one ``nous_system.model_call_log`` row per model call, its retries in
``nous_system.model_call_attempt``, and the context estimate the runner's calls carry.

A client wraps each call in ``ModelCallRecorder.scope(payload, ctx, call_kind)``: a synchronous context manager, so it
works in coroutines and in (async) generators alike. The scope names the call's purpose once, when it is built: the
caller's explicit ``CallContext`` first, else the fallback a caller set with ``model_call_context``, else
``unlabelled`` (counted). While the client's HTTP request runs inside ``scope.bind_for_request()`` the scope is the
active one (``_ACTIVE_SCOPE``), so a client's HTTP response hook can add each attempt to it; a stream binds only
around opening the stream, never across a ``yield``. At exit the scope builds the row and hands it, with its attempts,
to the shared ``BoundedWriter`` as one transaction. Nothing here raises into a call or reads the rows back; the
dashboards and ``db_query`` do. Embedding and rerank calls are counted per minute instead (``record_bucket``). With a
text store (``model_call_text``), the call's text is handed to it once the row is committed.

The context estimate (``analyze_context``, ``parse_system_sections``) moved here from ``context_logger.py``, which
re-exports it unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext, suppress
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text

from nous.api.preamble import CLAUDE_CODE_PREAMBLE
from nous.observability.model_call_text import CONTENT_EVENTS, TextStore
from nous.observability.recording import BoundedWriter, RecorderStats

logger = logging.getLogger(__name__)

# What a model call was for. Anything else is recorded as ``unlabelled`` and counted (``RecorderStats.unlabelled``).
PURPOSES: frozenset[str] = frozenset(
    {
        "chat",
        "subtask",
        "heartbeat",
        "background_check",
        "continuation",
        "dag_summary",
        "mcp",
        "action_gate",
        "reflection",
        "compaction",  # a turn's history summary (ConversationCompactor); not a main-loop call
        "critic",
        "epistemic_gate",
        "query_expansion",
        "date_window",
        "admission_utility",
        "contradiction",
        "fact_dedup",  # the write-time dedup tiebreaker (FactManager.is_distinct_fact)
        "actionability",
        "profile_card",
        "episode_summarizer",
        "fact_extractor",
        "knowledge_extractor",
        "enumerative_extractor",
        "correction_extractor",
        "outcome_detector",
        "procedure_learner",
        "procedure_coverage",  # does a stored procedure already teach this new lesson (ProcedureLearner)
        "strategy_card",
        "sleep_reflection",
        "sleep_contradiction",
        "sleep_merge",
        "job_fix",
        "a2ui_compose",
        "dag_fix",
        "pdf_transcription",
        "email_classify",
        "browser_task",
        "embedding",
        "rerank",
        "unlabelled",
    }
)
# The runner's own calls: what /context/log*, /dashboard/cache and /dashboard/observability list.
MAIN_LOOP_PURPOSES: frozenset[str] = frozenset(
    {
        "chat",
        "subtask",
        "heartbeat",
        "background_check",
        "continuation",
        "dag_summary",
        "mcp",
        "action_gate",
        "reflection",
    }
)
# The main-loop calls the chat views leave out (they replace the old HEARTBEAT_CALL_TYPE exclusion).
QUIET_PURPOSES: frozenset[str] = frozenset({"heartbeat", "background_check"})

# A call's and an attempt's error text, redacted and clipped (the tool-call log uses the same cap).
ERROR_TEXT_MAX_CHARS = 1000

# ---------------------------------------------------------------------------------------------------------------------
# The context estimate (moved from context_logger.py; ContextLogEntry.from_payload uses it)
# ---------------------------------------------------------------------------------------------------------------------

# Section markers in system prompt -> internal names
SECTION_MARKERS: dict[str, str] = {
    "## Current Date/Time": "datetime",
    "## Identity": "identity",
    "## User Profile": "user_profile",
    "## Context Safety": "context_safety",
    "## Active Censors": "censors",
    "## Current Frame": "frame",
    "## Working Memory": "working_memory",
    "## Related Decisions": "related_decisions",
    "## Relevant Facts": "relevant_facts",
    "## Session Profile": "session_profile",
    "## Known Procedures": "known_procedures",
    "## Procedure Catalog": "procedure_catalog",  # F079 catalog-first breadth
    "## Recommended Procedures": "recommended_procedures",  # F079 option C Critic pointer
    "## Past Episodes": "past_episodes",
    "## Recent Conversations": "recent_conversations",
    "## Tool Instructions": "frame_instructions",
    "[Execution Ledger]": "execution_ledger",
    "[Previous Turn Corrections]": "corrections",
    "## Output Formatting": "telegram_format",
    "## Recall Before Clarifying": "recall_before_clarifying",
    "## Tools Outrank Memory": "tools_outrank_memory",
    "## Procedure Awareness": "procedure_awareness",
    "## Epistemic Routing": "epistemic_routing",
    "## Cached Results": "cached_results",
    "## Memory Retrieval Notice": "memory_retrieval_notice",
    "## Retrieval Failures": "retrieval_failures",
    "## Active Guidance": "active_guidance",  # layer.py: censor guidance for this turn
    "[DIAGNOSTIC OBSERVATIONS]": "diagnostic_nudges",  # critic.py format_nudges
    # layer.py's blocks without a ## heading: subtask results (result_inbox off), the result inbox (on), hub shifts
    "=== Completed Subtask ===": "subtask_results",
    "=== Blocked Subtask ===": "subtask_results",
    "=== Failed Subtask ===": "subtask_results",
    "=== Background Results ===": "background_results",
    "[graph] Hub shift:": "hub_shift",  # a line prefix (LINE_MARKERS)
}

# Markers that begin a content line instead of being a heading: the line is part of the section it starts.
LINE_MARKERS: tuple[str, ...] = ("[graph] Hub shift:",)

# The sections whose body renders stored or authored text, not text the builder writes: an unknown ``## `` line inside
# one is part of that text (an identity's ``## Character``, a skill's ``## Steps``, a fact), not a new section.
FREE_TEXT_SECTIONS: frozenset[str] = frozenset(
    {
        "identity",  # the identity sections in the DB (identity/manager.py assemble_prompt)
        "user_profile",  # profile facts, or the profile card's lines
        "censors",  # censor patterns, reasons and instructions
        "frame",  # the frame row's description and questions
        "working_memory",  # current task (the user's input), open threads, items
        "related_decisions",  # decision descriptions
        "relevant_facts",  # fact contents
        "session_profile",  # fact contents
        "known_procedures",  # procedure names and descriptions
        "procedure_catalog",  # procedure names and descriptions
        "recommended_procedures",  # skill bodies, newlines kept
        "past_episodes",  # episode summaries
        "recent_conversations",  # episode titles, summaries and open threads
        "cached_results",  # cached tool inputs
        "execution_ledger",  # tool key arguments and error result summaries
        "corrections",  # quoted claims from the agent's own reply
        "active_guidance",  # censor guidance text
        "subtask_results",  # subtask tasks, results, summaries, findings and errors
        "background_results",  # finished subtask and DAG titles and bodies
        "hub_shift",  # graph entity labels
    }
)

_MARKER_PATTERNS = sorted(SECTION_MARKERS.keys(), key=len, reverse=True)
# The first characters a marker or a ``## `` heading can have: a line that starts with any other cannot start a section,
# so the marker scan is skipped for it (most prompt lines are bullets or plain text).
_SECTION_FIRST_CHARS = frozenset(marker[0] for marker in _MARKER_PATTERNS) | {"#"}


def _section_start(line: str, current_name: str) -> str | None:
    """The section ``line`` starts, or None: a marker in the table, else any other ``## Title`` as ``title``. The text
    before the first heading (``current_name`` ``""``) is free text too: on layer.py's degraded path it is the identity
    prompt alone, with its own ``## `` headings."""
    stripped = line.strip()
    if not stripped or stripped[0] not in _SECTION_FIRST_CHARS:
        return None
    for marker in _MARKER_PATTERNS:
        if stripped.startswith(marker):
            return SECTION_MARKERS[marker]
    if line.startswith("## ") and current_name and current_name not in FREE_TEXT_SECTIONS:
        return re.sub(r"[^a-z0-9]+", "_", line[3:].lower()).strip("_") or None
    return None


def _add_section(sections: dict[str, str], name: str, body: str) -> None:
    """Add ``body`` to section ``name``: a name met again keeps its earlier text too, so both are counted."""
    sections[name] = f"{sections[name]}\n\n{body}" if name in sections else body


def parse_system_sections(system_prompt: str) -> dict[str, str]:
    """Split system prompt into named sections.

    A heading in ``SECTION_MARKERS`` starts the section it names; any other ``## Title Case`` heading starts the
    section ``title_case`` (outside ``FREE_TEXT_SECTIONS``), so it is never folded into the one before it. A name met
    twice holds both texts. Text before the first heading is ``other``. A ``LINE_MARKERS`` line is content."""
    if not system_prompt:
        return {}
    sections: dict[str, str] = {}
    current_name = ""  # the text before the first heading; no heading is named ""
    current_lines: list[str] = []
    for line in system_prompt.split("\n"):
        name = _section_start(line, current_name)
        is_content = name is not None and line.strip().startswith(LINE_MARKERS)
        if name is None or (is_content and name == current_name):
            current_lines.append(line)
            continue
        if current_lines:
            text = "\n".join(current_lines).strip()
            if text:
                _add_section(sections, current_name, text)
        current_name = name
        current_lines = [line] if is_content else []
    if current_lines:
        text = "\n".join(current_lines).strip()
        if text:
            _add_section(sections, current_name, text)
    if "" in sections:
        _add_section(sections, "other", sections.pop(""))
    return sections


def _estimate_tokens(text: str) -> int:
    return len(text) // 4 if text else 0


def _estimate_messages_tokens(messages: list[dict]) -> int:
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += _estimate_tokens(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total += _estimate_tokens(block.get("text", ""))
                    if "input" in block:
                        total += _estimate_tokens(json.dumps(block["input"]))
                    if block.get("type") == "tool_result":
                        result = block.get("content", "")
                        if isinstance(result, str):
                            total += _estimate_tokens(result)
                        elif isinstance(result, list):
                            total += sum(_estimate_tokens(b.get("text", "")) for b in result if isinstance(b, dict))
    return total


@dataclass(frozen=True, slots=True)
class ContextStats:
    """What one model call's context holds, estimated from its payload (the runner's calls only).

    ``section_tokens_est`` is ``context_log.token_breakdown`` renamed (a ``token_`` prefix trips the db_query
    reader's credential check). The ``cache_break`` fields are the runner's to set, with ``dataclasses.replace``."""

    section_tokens_est: dict[str, int]
    total_tokens_est: int
    context_window_size: int
    utilization_pct: float
    sections_present: list[str]
    tools_count: int
    tool_names: list[str]
    messages_count: int
    message_roles: dict[str, int]
    loaded_facts: int = 0
    loaded_decisions: int = 0
    loaded_procedures: int = 0
    loaded_episodes: int = 0
    recent_conversations: int = 0
    cache_break: bool = False
    cache_break_components: list[str] = field(default_factory=list)
    cache_break_tokens_lost: int = 0


def _system_text(system_blocks: str | list[dict[str, Any]] | None) -> str:
    """The system prompt as one string: a list of blocks is joined by a blank line, as the runner flattens it."""
    if isinstance(system_blocks, str):
        return system_blocks
    if not system_blocks:
        return ""
    return "\n\n".join(b["text"] for b in system_blocks if isinstance(b, dict) and b.get("text"))


def payload_preamble(system_blocks: list[dict[str, Any]]) -> str:
    """The payload's system block 0 when it is the Claude Code preamble, else "". The runner's flat prompt holds every
    other block, so any other block 0 is counted there, with the rest, and never twice."""
    head = system_blocks[0] if system_blocks else None
    return CLAUDE_CODE_PREAMBLE if isinstance(head, dict) and head.get("text") == CLAUDE_CODE_PREAMBLE else ""


def system_sections(system_blocks: str | list[dict[str, Any]] | None, preamble: str = "") -> dict[str, str]:
    """The system prompt's sections, the estimate's input. ``preamble`` is the payload's system block 0 (the Claude
    Code preamble), which the runner's flat prompt leaves out: it is the section ``preamble``, counted like any
    other."""
    preamble = preamble.strip()
    sections = {"preamble": preamble} if preamble else {}
    for name, body in parse_system_sections(_system_text(system_blocks)).items():
        _add_section(sections, name, body)
    return sections


def analyze_sections(
    sections: dict[str, str], messages: list[dict], tools: list[dict] | None, context_window: int
) -> ContextStats:
    """The estimate from already-parsed sections (``ContextLogEntry.from_payload`` keeps the sections' text too)."""
    section_tokens = {name: _estimate_tokens(text) for name, text in sections.items()}
    tools_list = tools or []
    section_tokens["tools_definition"] = _estimate_tokens(json.dumps(tools_list))
    section_tokens["messages"] = _estimate_messages_tokens(messages)
    total = sum(section_tokens.values())
    utilization = (total / context_window * 100) if context_window else 0.0

    role_counts: dict[str, int] = {}
    for msg in messages:
        role = msg.get("role", "unknown")
        role_counts[role] = role_counts.get(role, 0) + 1

    # Heuristic memory item counts from section content (one bullet per item).
    def _count_items(key: str, marker: str | None = None) -> int:
        text = sections.get(key, "")
        if not text.strip():
            return 0
        if marker is not None:
            # Count only TOP-LEVEL item lines. The formatter emits exactly one
            # item per line starting with `marker`; an embedded "\n- " inside a
            # verbatim-interpolated field (e.g. a multiline episode summary or
            # procedure description) is NOT a delivered item. (codex PR #485)
            return sum(1 for ln in text.split("\n") if ln.startswith(marker))
        # Legacy heuristic for facts/decisions (mixed "- [subj]"/bare "- " bullets).
        return text.count("\n-") + 1

    # F079 Phase 0: previously-unpopulated counters (the columns + INSERT already
    # existed; only the population was missing, so procedure/episode delivery was
    # invisible on the dashboard).
    # Count items in the RENDERED (post-truncation) section — that is what the
    # model actually received. NOTE: deliberately NOT the pre-truncation
    # `recalled_*_ids` counts, which over-state delivery (they're collected before
    # `_truncate_to_budget` and include episodes that `_format_episodes` skips,
    # e.g. abandoned ones) — codex PR #485 P1. Top-level item marker keeps the
    # common embedded-bullet case from inflating the count; a field that embeds a
    # literal marker-shaped line ("\n- **x**") is a known, rare telemetry
    # approximation (same limitation the facts/decisions counters have always had).
    return ContextStats(
        section_tokens_est=section_tokens,
        total_tokens_est=total,
        context_window_size=context_window,
        utilization_pct=round(utilization, 2),
        sections_present=list(sections.keys()),
        tools_count=len(tools_list),
        tool_names=[t.get("name", "") for t in tools_list],
        messages_count=len(messages),
        message_roles=role_counts,
        loaded_facts=_count_items("relevant_facts"),
        loaded_decisions=_count_items("related_decisions"),
        # F079: procedures arrive either as the passive "Known Procedures" list (- **name**)
        # or the catalog-first "Procedure Catalog" (- name (domain)); count whichever rendered.
        loaded_procedures=(
            _count_items("known_procedures", "- **")  # context.py:1047
            + _count_items("procedure_catalog", "- ")
        ),
        loaded_episodes=_count_items("past_episodes", "- ["),  # context.py:1063
        recent_conversations=_count_items("recent_conversations", "- ["),  # context.py:609
    )


def analyze_context(
    system_blocks: str | list[dict[str, Any]] | None,
    messages: list[dict],
    tools: list[dict] | None,
    context_window: int,
    preamble: str = "",
) -> ContextStats:
    """Estimate what a call's context holds: tokens per system-prompt section, for the tool definitions and for the
    messages (4 characters a token), the tools and message roles, and the memory items each section delivered. Pure.
    ``system_blocks`` is the system prompt as a string or as a list of text blocks (joined by a blank line);
    ``preamble`` is system block 0, counted as the section ``preamble`` (``system_sections``)."""
    return analyze_sections(system_sections(system_blocks, preamble), messages, tools, context_window)


# ---------------------------------------------------------------------------------------------------------------------
# The call context
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CallContext:
    """Who made a model call and why: the ``model_call_log`` columns a caller knows and the client does not."""

    purpose: str = "unlabelled"
    session_id: str | None = None
    parent_session_id: str | None = None
    turn_id: UUID | None = None
    turn_number: int | None = None
    frame_id: str | None = None
    subtask_id: UUID | None = None
    dag_id: UUID | None = None
    trace_id: str | None = None
    stats: ContextStats | None = None  # runner calls only: sections, estimates, loaded_* counts, cache_break
    group_id: UUID | None = None
    is_fallback: bool = False


def context_kwargs(client: Any, ctx: CallContext) -> dict[str, Any]:
    """The keyword arguments that pass ``ctx`` to ``client.call``: ``{"call_context": ctx}`` for a client that takes
    it (``supports_call_context`` is True), else none, so a test double that takes only the payload keeps working. A
    ``MagicMock`` answers every attribute with a truthy mock, so only ``True`` itself counts."""
    return {"call_context": ctx} if getattr(client, "supports_call_context", False) is True else {}


# The fallback a caller sets around calls it cannot pass a context to (model_call_context). A scope reads it once,
# when it is built.
_FALLBACK_CALL_CONTEXT: ContextVar[CallContext | None] = ContextVar("f108_fallback_call_context", default=None)
# The scope whose HTTP request is in flight in this context (``ModelCallScope.bind_for_request``), so a client's HTTP
# response hook can add attempts to it. A different variable from the fallback, with a different job. It is never
# held across a ``yield``: a variable set in an async generator's body stays set in its consumer's context while the
# generator is suspended, and another request's hook would then add its attempt to the stream's scope.
_ACTIVE_SCOPE: ContextVar[ModelCallScope | None] = ContextVar("f108_active_model_call_scope", default=None)


def _reset(var: ContextVar[Any], token: Token[Any]) -> None:
    """Undo a ``set``. A generator closed from another Context cannot reset there; recording must not raise."""
    try:
        var.reset(token)
    except ValueError:
        pass


@contextmanager
def model_call_context(**fields: Any) -> Iterator[CallContext]:
    """Within the block, a model call made without an explicit context is labelled with
    ``CallContext(**fields)`` (``label_source='context'``)."""
    ctx = CallContext(**fields)
    token = _FALLBACK_CALL_CONTEXT.set(ctx)
    try:
        yield ctx
    finally:
        _reset(_FALLBACK_CALL_CONTEXT, token)


def current_fallback() -> CallContext | None:
    """The context ``model_call_context`` set around this code, or None."""
    return _FALLBACK_CALL_CONTEXT.get()


def active_scope() -> ModelCallScope | None:
    """The scope whose HTTP request is in flight in this context (``ModelCallScope.bind_for_request``), or None."""
    return _ACTIVE_SCOPE.get()


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _clip_error(text_: str) -> str:
    # Imported here: nous.cognitive imports nous.observability, which imports this module through context_logger.
    from nous.cognitive.ledger_store import detail_text

    return detail_text(text_, ERROR_TEXT_MAX_CHARS)


def _get(obj: Any, name: str) -> Any:
    """``obj[name]`` for a dict (a raw response or event), else ``obj.name``; None when missing."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _http_status_of(exc: BaseException) -> int | None:
    """The HTTP status an SDK error (``status_code``) or an httpx error (``response.status_code``) carries."""
    for status in (getattr(exc, "status_code", None), getattr(getattr(exc, "response", None), "status_code", None)):
        if isinstance(status, int) and not isinstance(status, bool):
            return status
    return None


# The API's usage keys -> the row's columns.
_USAGE_COLUMNS: dict[str, str] = {
    "input_tokens": "input_uncached",
    "cache_read_input_tokens": "cache_read",
    "cache_creation_input_tokens": "cache_write",
    "output_tokens": "output_tokens",
}
_INPUT_USAGE_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")

_CALL_COLUMNS: tuple[str, ...] = (
    "id",
    "agent_id",
    "group_id",
    "is_fallback",
    "started_at",
    "purpose",
    "label_source",
    "call_kind",
    "model",
    "session_id",
    "parent_session_id",
    "turn_id",
    "turn_number",
    "frame_id",
    "subtask_id",
    "dag_id",
    "trace_id",
    "status",
    "http_status",
    "error_text",
    "stop_reason",
    "input_uncached",
    "cache_read",
    "cache_write",
    "output_tokens",
    "duration_ms",
    "attempts",
    "section_tokens_est",
    "total_tokens_est",
    "context_window_size",
    "utilization_pct",
    "sections_present",
    "tools_count",
    "tool_names",
    "messages_count",
    "message_roles",
    "loaded_facts",
    "loaded_decisions",
    "loaded_procedures",
    "loaded_episodes",
    "recent_conversations",
    "cache_break",
    "cache_break_components",
    "cache_break_tokens_lost",
)
_CTX_COLUMNS = (
    "session_id",
    "parent_session_id",
    "turn_id",
    "turn_number",
    "frame_id",
    "subtask_id",
    "dag_id",
    "trace_id",
    "group_id",
    "is_fallback",
)
_STATS_COLUMNS = (
    "section_tokens_est",
    "total_tokens_est",
    "context_window_size",
    "utilization_pct",
    "sections_present",
    "tools_count",
    "tool_names",
    "messages_count",
    "message_roles",
    "loaded_facts",
    "loaded_decisions",
    "loaded_procedures",
    "loaded_episodes",
    "recent_conversations",
    "cache_break",
    "cache_break_components",
    "cache_break_tokens_lost",
)
_JSON_COLUMNS = frozenset({"section_tokens_est", "message_roles"})
_ARRAY_COLUMNS = frozenset({"sections_present", "tool_names", "cache_break_components"})


def _call_value(column: str) -> str:
    if column in _JSON_COLUMNS:
        return f"CAST(:{column} AS jsonb)"
    if column in _ARRAY_COLUMNS:
        return f"CAST(:{column} AS text[])"
    return f":{column}"


_INSERT_CALL = text(
    f"INSERT INTO nous_system.model_call_log ({', '.join(_CALL_COLUMNS)}) VALUES ("
    + ", ".join(_call_value(c) for c in _CALL_COLUMNS)
    + ")"
)
_INSERT_ATTEMPT = text(
    "INSERT INTO nous_system.model_call_attempt "
    "(call_id, agent_id, attempt_no, started_at, duration_ms, http_status, error_type, error_text) "
    "VALUES (:call_id, :agent_id, :attempt_no, :started_at, :duration_ms, :http_status, :error_type, :error_text)"
)
# One row per (minute, purpose, model, status): the id is derived from that key, so the primary key merges a
# minute's calls and concurrent writes of one bucket wait for each other instead of adding a second row.
_UPSERT_BUCKET = text(
    "INSERT INTO nous_system.model_call_log "
    "(id, agent_id, started_at, purpose, label_source, call_kind, model, status, input_uncached, duration_ms, "
    "call_count) "
    "VALUES (:id, :agent_id, :started_at, :purpose, 'explicit', :call_kind, :model, :status, :tokens, :duration_ms, 1) "
    "ON CONFLICT (id) DO UPDATE SET "
    "call_count = model_call_log.call_count + 1, "
    "input_uncached = CASE WHEN model_call_log.input_uncached IS NULL AND EXCLUDED.input_uncached IS NULL THEN NULL "
    "ELSE coalesce(model_call_log.input_uncached, 0) + coalesce(EXCLUDED.input_uncached, 0) END, "
    "duration_ms = coalesce(model_call_log.duration_ms, 0) + coalesce(EXCLUDED.duration_ms, 0)"
)
_BUCKET_NAMESPACE = uuid.UUID("6b1f7c1e-5d0a-4c55-9a0e-f108b0c4e7a1")


def _shallow_message(message: Any) -> Any:
    """``message`` copied two container levels deep: the message dict, and its content list with each block dict
    copied. The strings inside are shared, not copied."""
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return dict(message) if isinstance(message, dict) else message
    return {**message, "content": [dict(block) if isinstance(block, dict) else block for block in content]}


def _json_param(value: Any) -> str | None:
    """A JSONB parameter: SQL NULL for None, else the JSON text."""
    return None if value is None else json.dumps(value, ensure_ascii=False, default=str)


class ModelCallScope:
    """One model call being recorded: a synchronous context manager, so it works in coroutines and generators.

    Built by ``ModelCallRecorder.scope``. The client reports the call with ``finish(response)`` or ``fail(exc)``,
    streamed events with ``observe_stream_event`` (a ``done`` or ``message_stop`` reports ``ok``, an ``error`` event
    reports ``error``) and each HTTP attempt with ``attempt``, or binds the scope around its request
    (``bind_for_request``) so its HTTP response hook can report the attempts. At exit the row is written.
    A reported status stands (the call completed or failed, whatever its caller did next); when neither was
    reported, exit labels the call ``cancelled`` on ``CancelledError`` or ``GeneratorExit``, ``error`` for any other
    exception, and ``ok`` when there was none, except a ``stream`` that ended with no terminal event, which was cut
    off: ``error``. Nothing here raises into the client, and exit never swallows the exception.
    """

    def __init__(self, recorder: ModelCallRecorder, payload: Any, ctx: CallContext | None, call_kind: str) -> None:
        self._recorder = recorder
        self._call_kind = call_kind
        self._model = payload.get("model") if isinstance(payload, dict) else None
        # The purpose is named once, now: a fallback set or changed later does not move this call.
        self._ctx, self._label_source, self._purpose = recorder._resolve(ctx)
        self._started_at = _utcnow()
        self._t0 = time.perf_counter()
        self._usage: dict[str, int] = {}
        self._stop_reason: str | None = None
        self._status: str | None = None
        self._http_status: int | None = None
        self._error_text: str | None = None
        self._attempts: list[dict[str, Any]] = []
        self._written = False
        # The call's text, when the recorder stores it: references only (the payload as handed over, the response's
        # content, a stream's content events). Exit snapshots them; the text write does all the work.
        keep_text = recorder._text is not None and isinstance(payload, dict)
        self._text_payload: dict[str, Any] | None = payload if keep_text else None
        self._response_content: Any = None
        self._stream_events: list[Any] | None = [] if keep_text else None

    def __enter__(self) -> ModelCallScope:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: Any) -> bool:
        try:
            if self._status is None and exc_type is not None:
                if issubclass(exc_type, asyncio.CancelledError | GeneratorExit):
                    self._status = "cancelled"
                elif exc is not None:
                    self.fail(exc)
            elif self._status is None and self._call_kind == "stream":
                # A stream that ended normally without done, message_stop or error was cut off (a consumer that
                # stops early arrives as GeneratorExit instead).
                self._status = "error"
                self._error_text = "stream ended without a terminal event"
            self._write()
        except Exception as err:
            self._recorder._stats.dropped_error += 1
            logger.warning("F108: model-call record not written (%s)", type(err).__name__)
        return False

    @contextmanager
    def bind_for_request(self) -> Iterator[ModelCallScope]:
        """Make this the active scope (``active_scope()``) for the block: wrap the HTTP request itself, its retries
        included, and never a ``yield`` (see ``_ACTIVE_SCOPE``)."""
        token = _ACTIVE_SCOPE.set(self)
        try:
            yield self
        finally:
            _reset(_ACTIVE_SCOPE, token)

    def _lost(self, what: str, exc: Exception) -> None:
        """Count and log a report that could not be taken (the call goes on; the row may miss a part)."""
        self._recorder._stats.dropped_error += 1
        logger.warning("F108: model-call %s not recorded (%s)", what, type(exc).__name__)

    def attempt(
        self, http_status: int | None, error_type: str | None, error_text: str | None, duration_ms: float | None
    ) -> None:
        """Add one HTTP attempt (the call's retries are its attempts). Its error text is redacted and clipped. An
        attempt reported after the row was written is not recorded; it is counted (``dropped_late``). Never raises:
        a client's response hook calls it."""
        if self._written:
            self._recorder._late_report()
            return
        try:
            now = _utcnow()
            self._attempts.append(
                {
                    "attempt_no": len(self._attempts) + 1,
                    "started_at": now - timedelta(milliseconds=duration_ms) if duration_ms else now,
                    "duration_ms": None if duration_ms is None else round(duration_ms),
                    "http_status": http_status,
                    "error_type": error_type,
                    "error_text": None if error_text is None else _clip_error(str(error_text)),
                }
            )
        except Exception as exc:
            self._lost("attempt", exc)

    def _merge_usage(self, usage: Any, keys: tuple[str, ...]) -> None:
        """Keep the largest count seen for each of ``keys`` (a later event never lowers one)."""
        if not isinstance(usage, dict):
            return
        for key in keys:
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                self._usage[key] = max(value, self._usage.get(key, value))

    def observe_stream_event(self, event: Any) -> None:
        """Take what a stream reports, before the client yields the event: the input counts from ``message_start``;
        the output count and the stop reason from ``done`` (``message_delta``), which also reports ``ok``, as does
        ``message_stop`` (the response is complete, so a consumer that stops now does not make it ``cancelled``); an
        ``error`` event reports ``error`` with the event's text (the first error's text is kept). An event observed
        after the row was written is not taken; it is counted (``dropped_late``), as a late attempt is. Never raises."""
        if self._written:
            self._recorder._late_report()
            return
        try:
            kind = _get(event, "type")
            if kind == "message_start":
                self._merge_usage(_get(event, "usage"), _INPUT_USAGE_KEYS)
            elif kind == "done":
                self._merge_usage(_get(event, "usage"), ("output_tokens",))
                stop = _get(event, "stop_reason")
                if stop:
                    self._stop_reason = stop
                self._status = self._status or "ok"
            elif kind == "message_stop":
                self._status = self._status or "ok"
            elif kind == "error":
                self._status = "error"
                if self._error_text is None:
                    self._error_text = _clip_error(str(_get(event, "text") or "stream error"))
            elif kind in CONTENT_EVENTS and self._stream_events is not None:
                self._stream_events.append(event)  # the response's text, rebuilt by the text write
        except Exception as exc:
            self._lost("stream event", exc)

    def finish(self, response: Any) -> None:
        """The call returned ``response`` (an ``ApiResponse`` or the raw response dict): take its usage and stop
        reason; the status is ``ok``. Never raises."""
        self._status = "ok"
        try:
            self._merge_usage(_get(response, "usage"), tuple(_USAGE_COLUMNS))
            stop = _get(response, "stop_reason")
            if stop:
                self._stop_reason = stop
            if self._text_payload is not None:
                self._response_content = _get(response, "content")
        except Exception as exc:
            self._lost("response", exc)

    def fail(self, exc: BaseException) -> None:
        """The call failed with ``exc``: status ``error``, the HTTP status it carries, its text redacted and
        clipped. Never raises: a client calls it while it handles the exception it will raise."""
        self._status = "error"
        try:
            self._http_status = _http_status_of(exc)
            try:
                message = f"{type(exc).__name__}: {exc}"
            except Exception:
                message = type(exc).__name__
            self._error_text = _clip_error(message)
        except Exception as err:
            self._lost("error", err)

    def _write(self) -> None:
        if self._written:
            return
        self._written = True
        ctx = self._ctx
        http_status = self._http_status
        if http_status is None and self._attempts:
            http_status = self._attempts[-1]["http_status"]
        params: dict[str, Any] = {
            "id": uuid.uuid4(),
            "agent_id": self._recorder._agent_id,
            "started_at": self._started_at,
            "purpose": self._purpose,
            "label_source": self._label_source,
            "call_kind": self._call_kind,
            "model": self._model,
            "status": self._status or "ok",
            "http_status": http_status,
            "error_text": self._error_text,
            "stop_reason": self._stop_reason,
            "duration_ms": round((time.perf_counter() - self._t0) * 1000),
            "attempts": max(1, len(self._attempts)),
        }
        for key, column in _USAGE_COLUMNS.items():
            params[column] = self._usage.get(key)
        for column in _CTX_COLUMNS:
            params[column] = getattr(ctx, column)
        stats = ctx.stats
        for column in _STATS_COLUMNS:
            params[column] = None if stats is None else getattr(stats, column)
        params["cache_break"] = bool(params["cache_break"])
        for column in _JSON_COLUMNS:
            params[column] = _json_param(params[column])
        attempts = [{**a, "call_id": params["id"], "agent_id": params["agent_id"]} for a in self._attempts]
        sent = self._text_snapshot()
        if sent is None:
            self._recorder._submit_call(params, attempts)
        else:
            self._recorder._submit_call(params, attempts, sent)

    def _text_snapshot(self) -> tuple[dict[str, Any], Any, list[Any] | None] | None:
        """What the text write needs, taken now: ``(sent, response_content, stream_events)``. Right after the call
        returns, before the write runs, the runner appends the response and the tool results to the payload's
        ``messages`` list, and compaction (``prune_tool_results``) rewrites an aged tool result's content in place:
        the payload holds the runner's own message dicts. So the lists are copied here, and each message down to its
        content blocks (``_shallow_message``); no string is copied or serialised. A change made deeper than a content
        block (inside a block's own content list) is out of reach of this copy; nothing makes one today. None when
        the text is not stored, or when the snapshot fails (counted and logged; the numbers row is written anyway)."""
        payload = self._text_payload
        if payload is None:
            return None
        try:
            sent: dict[str, Any] = {}
            for key in ("system", "messages", "tools"):
                value = payload.get(key)
                if key == "messages" and isinstance(value, list):
                    sent[key] = [_shallow_message(message) for message in value]
                elif value is not None:
                    sent[key] = list(value) if isinstance(value, list) else value
            content = self._response_content
            return sent, list(content) if isinstance(content, list) else content, self._stream_events
        except Exception as exc:
            self._recorder._text.stats.dropped_error += 1
            logger.warning("F108: model-call text not recorded (%s)", type(exc).__name__)
            return None


class ModelCallRecorder:
    """Writes model-call rows through the shared ``BoundedWriter`` (built in main.py, never here).

    With an enabled ``text_store``, each call's text is handed to it once the call's row is committed (its refs need
    the row): a write of its own, so a text failure never touches the row."""

    def __init__(
        self,
        writer: BoundedWriter,
        database: Any,
        agent_id: str,
        settings: Any,
        text_store: TextStore | None = None,
    ) -> None:
        self._writer = writer
        self._database = database
        self._agent_id = agent_id
        self._settings = settings
        self._text = text_store if text_store is not None and text_store.enabled else None
        self._stats = RecorderStats()
        self._warned_purposes: set[str] = set()
        self._warned_late = False

    @property
    def stats(self) -> RecorderStats:
        """This recorder's own counters (the shared writer's ``stats`` are the totals of every recorder)."""
        return self._stats

    def scope(self, payload: dict[str, Any], ctx: CallContext | None, call_kind: str) -> ModelCallScope:
        """A scope for one call of ``call_kind`` (``call``, ``stream``, ``aggregated``, ``browser_step``) sending
        ``payload`` (its ``model`` is recorded). Its purpose is ``ctx``'s, else the ``model_call_context``
        fallback's, else ``unlabelled``."""
        return ModelCallScope(self, payload, ctx, call_kind)

    def record_bucket(
        self, purpose: str, model: str | None, tokens: int | None, duration_ms: float, status: str
    ) -> None:
        """Count one embedding or rerank call in its minute's row: one row per (minute, purpose, model, status),
        ``call_count`` calls, ``input_uncached`` their summed tokens (NULL while none reported any) and
        ``duration_ms`` their summed time. A failed call (``status='error'``) gets a row of its own. Synchronous;
        never raises."""
        try:
            resolved = self._label(purpose)
            minute = _utcnow().replace(second=0, microsecond=0)
            call_kind = "rerank" if purpose == "rerank" else "embedding"
            key = "|".join((self._agent_id, minute.isoformat(), resolved, model or "", status, call_kind))
            params = {
                "id": uuid.uuid5(_BUCKET_NAMESPACE, key),
                "agent_id": self._agent_id,
                "started_at": minute,
                "purpose": resolved,
                "call_kind": call_kind,
                "model": model,
                "status": status,
                "tokens": tokens,
                "duration_ms": round(duration_ms),
            }
        except Exception as exc:
            self._stats.dropped_error += 1
            logger.warning("F108: %s bucket not written (%s)", purpose, type(exc).__name__)
            return
        self._writer.submit(lambda: self._upsert_bucket(params), stats=self._stats)

    def _label(self, purpose: Any) -> str:
        """``purpose`` when it is one of ``PURPOSES``, else ``unlabelled``; every unlabelled call is counted, and an
        unknown name is logged once."""
        if isinstance(purpose, str) and purpose in PURPOSES and purpose != "unlabelled":
            return purpose
        self._stats.unlabelled += 1
        if purpose != "unlabelled" and repr(purpose) not in self._warned_purposes:
            self._warned_purposes.add(repr(purpose))
            logger.warning("F108: model call with unknown purpose %r recorded as unlabelled", purpose)
        return "unlabelled"

    def _resolve(self, ctx: CallContext | None) -> tuple[CallContext, str, str]:
        """``(context, label_source, purpose)`` for a new scope: the explicit context, else the fallback, else none.

        Never raises: a ``CallContext`` whose purpose is not a string keeps its other fields (session, turn, frame,
        trace) and is labelled ``unlabelled`` (``none``); a context that is not a ``CallContext``, or any fault here,
        gives an empty context labelled the same. Either is counted, and the type of the fault logged once."""
        try:
            if ctx is not None:
                base, source = ctx, "explicit"
            else:
                fallback = _FALLBACK_CALL_CONTEXT.get()
                base, source = (fallback, "context") if fallback is not None else (CallContext(), "none")
            if not isinstance(base, CallContext):
                raise TypeError("a model-call context must be a CallContext")
            if isinstance(base.purpose, str):
                return base, source, self._label(base.purpose)
            # Only the purpose is bad: the row keeps the rest of the context (the purpose column is the third value).
            kept, fault_name = base, TypeError.__name__
        except Exception as exc:
            kept, fault_name = CallContext(), type(exc).__name__
        self._stats.unlabelled += 1
        fault = f"bad context: {fault_name}"
        if fault not in self._warned_purposes:
            self._warned_purposes.add(fault)
            logger.warning("F108: bad model-call context (%s) recorded as unlabelled", fault_name)
        return kept, "none", "unlabelled"

    def _late_report(self) -> None:
        """Count an attempt or a stream event reported after its call's row was written; log the first one."""
        self._stats.dropped_late += 1
        if not self._warned_late:
            self._warned_late = True
            logger.warning(
                "F108: a model-call attempt or stream event was reported after its call was written; it is counted, "
                "not recorded"
            )

    def _submit_call(
        self, params: dict[str, Any], attempts: list[dict[str, Any]], sent: tuple[Any, ...] | None = None
    ) -> None:
        self._writer.submit(lambda: self._insert_call(params, attempts, sent), stats=self._stats)

    async def _insert_call(
        self, params: dict[str, Any], attempts: list[dict[str, Any]], sent: tuple[Any, ...] | None = None
    ) -> None:
        async with self._database.session() as session:
            await session.execute(_INSERT_CALL, params)
            if attempts:
                await session.execute(_INSERT_ATTEMPT, attempts)
            await session.commit()
        if sent is not None and self._text is not None:
            self._text.submit(params["id"], *sent)  # after the commit: the text's refs need this row

    async def _upsert_bucket(self, params: dict[str, Any]) -> None:
        async with self._database.session() as session:
            await session.execute(_UPSERT_BUCKET, params)
            await session.commit()


def open_scope(
    recorder: ModelCallRecorder | None, payload: Any, ctx: CallContext | None, call_kind: str
) -> AbstractContextManager[ModelCallScope | None]:
    """The scope of one call, for a ``with``: it gives a ``ModelCallScope``, or None when nothing records (no
    recorder, or one that could not open a scope, which is logged at WARNING and counted). Never raises."""
    if recorder is None:
        return nullcontext()
    try:
        return recorder.scope(payload, ctx, call_kind)
    except Exception as exc:
        logger.warning("F108: model call not recorded (%s, %s)", call_kind, type(exc).__name__)
        with suppress(Exception):
            recorder.stats.dropped_error += 1
        return nullcontext()
