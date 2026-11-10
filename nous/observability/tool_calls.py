"""F108: the tool-call recorder — one ``nous_system.tool_call_log`` row per tool call the model asked for, and one
``nous_system.agent_turn_log`` row per turn.

``record`` and ``record_turn`` are synchronous and never raise: they redact and clip the free-text fields with the
ledger's ``detail_text`` (head and tail kept, secrets masked before the cut), then hand one parameterised statement to
the shared ``BoundedWriter``. Nothing here reads the rows; the dashboards and ``db_query`` do.
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from nous.cognitive.ledger_store import detail_text, redact_structure
from nous.observability.recording import BoundedWriter, RecorderStats

logger = logging.getLogger(__name__)

WORK_KINDS: tuple[str, ...] = (
    "conversation",
    "subtask",
    "workflow_step",
    "intention",
    "heartbeat",
    "background_check",
)
# Every ExecutionContext kind (nous/api/execution_context.py CONTEXT_KINDS). An approved_action runs an owner-approved
# proposal of an intention's continuation turn, so it is intention work.
_WORK_KIND_BY_CONTEXT: dict[str, str] = {
    "interactive": "conversation",
    "mcp": "conversation",
    "agent_action": "conversation",
    "subtask": "subtask",
    "scheduled": "subtask",
    "dag_node": "workflow_step",
    "continuation": "intention",
    "approved_action": "intention",
    "heartbeat_triage": "heartbeat",
    "heartbeat_check": "background_check",
    "heartbeat_callback": "background_check",
    "background": "background_check",
    "dag_summary": "background_check",
}
# The full result kept beside a cut preview (tool_call_log.result_text, migration 104): redacted, head and tail kept.
RESULT_TEXT_MAX_CHARS = 20_000
# A tool error's text has no setting of its own: the model-call log clips its error text at the same length.
ERROR_TEXT_MAX_CHARS = 1000
# The turn row's bounds: what started the turn, the reply, and the recalled ids kept per kind of memory.
TRIGGER_TEXT_MAX_CHARS = 500
REPLY_TEXT_MAX_CHARS = 1000
MEMORY_IDS_PER_KIND = 50


def work_kind_for(context_kind: str) -> str:
    """The kind of work a turn of this ``ExecutionContext.kind`` is. An unknown kind counts as a background check,
    like ``background`` (a turn whose caller named no kind): ``work_kind`` is NOT NULL and recording never fails."""
    return _WORK_KIND_BY_CONTEXT.get(context_kind, "background_check")


# The reason field (NOUS_TOOL_REASON_ENABLED): every tool offered to the model requires a short reason for the call.
# It is added to the offered copies only (AgentRunner._offered_tools), never to the dispatcher's schemas.
# split_reason takes it out of a call's input for the recorder, and ToolDispatcher.dispatch drops any that still
# reaches a handler whose registered schema does not declare it.
REASON_DESCRIPTION = "One sentence: why this tool, for this step."
_REASON_PROPERTY: dict[str, str] = {"type": "string", "description": REASON_DESCRIPTION}


def _declared_reason(tool: dict[str, Any]) -> Any:
    return ((tool.get("input_schema") or {}).get("properties") or {}).get("reason")


def add_reason_field(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``tools`` with a required string ``reason`` in each input schema. Each changed schema is a deep copy: the
    dispatcher's schemas (returned by reference with its schema cache off) and the caller's extra-tool schemas are
    never touched. A tool whose schema already declares ``reason`` is returned unchanged."""
    out: list[dict[str, Any]] = []
    for tool in tools:
        if _declared_reason(tool) is not None:
            out.append(tool)
            continue
        schema = copy.deepcopy(tool.get("input_schema") or {"type": "object"})
        schema["properties"] = {**(schema.get("properties") or {}), "reason": dict(_REASON_PROPERTY)}
        schema["required"] = [*(schema.get("required") or []), "reason"]
        out.append({**tool, "input_schema": schema})
    return out


def own_reason_tools(offered: list[dict[str, Any]]) -> frozenset[str]:
    """Names of the offered tools whose own schema declares ``reason`` (``create_censor``, an F061 extra tool), so
    the argument is theirs and stays in the call. The ``reason`` ``add_reason_field`` adds is not counted, so the
    answer is the same for the offered list before or after the field is added."""
    return frozenset(tool["name"] for tool in offered if _declared_reason(tool) not in (None, _REASON_PROPERTY))


def split_reason(
    tool_name: str, tool_input: Any, own_reason_tools: frozenset[str], max_chars: int
) -> tuple[Any, str | None]:
    """``(input without reason, cleaned reason)`` for one call. A tool in ``own_reason_tools`` keeps ``reason`` in
    its input, and the reason is still returned. The reason is whitespace-collapsed, redacted and clipped to
    ``max_chars``; None when it is missing, not a string or blank. An input that is not a dict is returned as it is,
    with None. ``tool_input`` is never changed. For an alias the caller passes the TARGET name."""
    if not isinstance(tool_input, dict):
        return tool_input, None
    raw = tool_input.get("reason")
    rest = tool_input if tool_name in own_reason_tools else {k: v for k, v in tool_input.items() if k != "reason"}
    if not isinstance(raw, str):
        return rest, None
    collapsed = " ".join(raw.split())
    if not collapsed:
        return rest, None
    # detail_text keeps head and tail with a marker between, so over the cap its result is longer than the cap.
    # Both windows are redacted before that, so the slice cannot uncover a secret.
    return rest, detail_text(collapsed, max_chars)[:max_chars]


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    """One tool call, as the runner saw it: the ``tool_call_log`` columns except ``id`` and ``agent_id`` (the
    recorder's). Text fields are given raw; the recorder redacts and clips them. ``input_text`` is the input as JSON,
    serialised by the runner after its strings were redacted (``redact_structure``): a string holding JSON is escaped
    in the JSON text, where the patterns cannot see its secrets. ``started_at`` None means the time the row is
    written."""

    call_index: int
    work_kind: str
    tool_name: str
    tool_name_called: str
    outcome: str
    session_id: str | None = None
    parent_session_id: str | None = None
    turn_id: UUID | None = None
    turn_number: int | None = None
    context_kind: str | None = None
    subtask_id: UUID | None = None
    dag_id: UUID | None = None
    dag_node_id: UUID | None = None
    reason: str | None = None
    input_text: str = ""
    result_preview: str = ""
    is_error: bool = False
    error_text: str | None = None
    refused_by: str | None = None
    duration_ms: int | None = None
    started_at: datetime | None = None
    ledger_entry_id: UUID | None = None
    # The recorder's: it sets this from ``result_preview`` when that was cut. A caller leaves it None.
    result_text: str | None = None


_COLUMNS: tuple[str, ...] = ("agent_id", *(f.name for f in fields(ToolCallRecord)))
_INSERT = text(
    f"INSERT INTO nous_system.tool_call_log ({', '.join(_COLUMNS)}) VALUES ("
    + ", ".join("COALESCE(:started_at, now())" if c == "started_at" else f":{c}" for c in _COLUMNS)
    + ")"
)


@dataclass(frozen=True, slots=True)
class TurnRecord:
    """One turn, as the runner saw it at the turn's end: the ``agent_turn_log`` columns except ``agent_id`` (the
    recorder's). ``id`` is the turn's ``turn_id``, the one its tool-call rows carry. Text and JSON fields are given
    raw; the recorder redacts and clips them. ``memory_context`` maps a kind of memory (decision, fact, procedure,
    episode) to the ids recalled into the turn's context. ``started_at`` None means the time the row is written.
    F110: ``frame_reason`` is the frame's ``match_method``, ``critic_frame`` the frame the critic recommended as a label
    (a frame id, or ``unknown`` for anything else; None when the critic did not run or its model gave no frame) and
    ``had_error`` whether the turn ended with an error."""

    id: UUID
    work_kind: str
    session_id: str | None = None
    parent_session_id: str | None = None
    turn_number: int | None = None
    context_kind: str | None = None
    frame_id: str | None = None
    frame_reason: str | None = None
    critic_frame: str | None = None
    had_error: bool | None = None
    trigger_text: str | None = None
    memory_context: dict[str, list[str]] | None = None
    critic: dict[str, Any] | None = None
    claim_check: dict[str, Any] | None = None
    reply_text: str | None = None
    error: str | None = None
    tool_call_count: int | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None


_TURN_COLUMNS: tuple[str, ...] = ("agent_id", *(f.name for f in fields(TurnRecord)))
_TURN_JSON_COLUMNS = frozenset({"memory_context", "critic", "claim_check"})


def _turn_value(column: str) -> str:
    if column == "started_at":
        return "COALESCE(:started_at, now())"
    if column in _TURN_JSON_COLUMNS:
        return f"CAST(:{column} AS jsonb)"
    return f":{column}"


# An UPSERT on the turn id, so a retried write leaves one row.
_UPSERT_TURN = text(
    f"INSERT INTO nous_system.agent_turn_log ({', '.join(_TURN_COLUMNS)}) VALUES ("
    + ", ".join(_turn_value(c) for c in _TURN_COLUMNS)
    + ") ON CONFLICT (id) DO UPDATE SET "
    + ", ".join(f"{c} = EXCLUDED.{c}" for c in _TURN_COLUMNS if c != "id")
)


def _redacted_json(value: Any) -> Any:
    """``value`` with every string in it redacted and clipped like an error text. The critic's rationale and the
    claim check's quoted claims are model text that can carry a secret the reply's own redaction would mask."""
    return redact_structure(value, ERROR_TEXT_MAX_CHARS)


def _json_param(value: Any) -> str | None:
    """A JSONB parameter: SQL NULL for None, else the JSON text."""
    return None if value is None else json.dumps(value, ensure_ascii=False, default=str)


class ToolCallRecorder:
    """Writes ``ToolCallRecord`` and ``TurnRecord`` rows through the shared ``BoundedWriter`` (built in main.py,
    never here)."""

    def __init__(self, writer: BoundedWriter, database: Any, agent_id: str, settings: Any) -> None:
        self._writer = writer
        self._database = database
        self._agent_id = agent_id
        self._input_cap = settings.tool_input_max_chars
        self._result_cap = settings.tool_result_preview_chars
        self._reason_cap = settings.tool_reason_max_chars
        self._stats = RecorderStats()

    @property
    def stats(self) -> RecorderStats:
        """This recorder's own counters (the shared writer's ``stats`` are the totals of every recorder)."""
        return self._stats

    def record(self, rec: ToolCallRecord) -> None:
        """Redact and clip the record's text, then schedule its INSERT. Synchronous; never raises."""
        try:
            params = {f.name: getattr(rec, f.name) for f in fields(rec)}
            params["agent_id"] = self._agent_id
            params["input_text"] = detail_text(rec.input_text, self._input_cap)
            params["result_preview"] = detail_text(rec.result_preview, self._result_cap)
            # The full copy only where the preview lost text, and never for an error (error_text holds its message).
            params["result_text"] = (
                detail_text(rec.result_preview, RESULT_TEXT_MAX_CHARS)
                if len(rec.result_preview) > self._result_cap and not rec.is_error
                else None
            )
            if rec.reason is not None:
                params["reason"] = detail_text(rec.reason, self._reason_cap)
            if rec.error_text is not None:
                params["error_text"] = detail_text(rec.error_text, ERROR_TEXT_MAX_CHARS)
        except Exception as exc:
            # Never reached the writer, so only this recorder counts it.
            self._stats.dropped_error += 1
            logger.warning("F108: tool-call record for %s not written: %r", getattr(rec, "tool_name", "?"), exc)
            return
        self._writer.submit(lambda: self._insert(params), stats=self._stats)

    def record_turn(self, rec: TurnRecord) -> None:
        """Redact and clip the turn's text, keep at most ``MEMORY_IDS_PER_KIND`` ids per kind of memory, then
        schedule its UPSERT on ``id``. Synchronous; never raises."""
        try:
            params = {f.name: getattr(rec, f.name) for f in fields(rec)}
            params["agent_id"] = self._agent_id
            if rec.trigger_text is not None:
                params["trigger_text"] = detail_text(rec.trigger_text, TRIGGER_TEXT_MAX_CHARS)
            if rec.reply_text is not None:
                params["reply_text"] = detail_text(rec.reply_text, REPLY_TEXT_MAX_CHARS)
            if rec.error is not None:
                params["error"] = detail_text(rec.error, ERROR_TEXT_MAX_CHARS)
            memory = rec.memory_context
            if memory is not None:
                memory = {kind: list(ids)[:MEMORY_IDS_PER_KIND] for kind, ids in memory.items()}
            params["memory_context"] = _json_param(memory)
            params["critic"] = _json_param(_redacted_json(rec.critic))
            params["claim_check"] = _json_param(_redacted_json(rec.claim_check))
        except Exception as exc:
            # Never reached the writer, so only this recorder counts it. The type only: the message can echo text.
            self._stats.dropped_error += 1
            logger.warning("F108: turn record not written (%s)", type(exc).__name__)
            return
        self._writer.submit(lambda: self._upsert_turn(params), stats=self._stats)

    async def _insert(self, params: dict[str, Any]) -> None:
        async with self._database.session() as session:
            await session.execute(_INSERT, params)
            await session.commit()

    async def _upsert_turn(self, params: dict[str, Any]) -> None:
        async with self._database.session() as session:
            await session.execute(_UPSERT_TURN, params)
            await session.commit()
