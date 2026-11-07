"""F108: the tool-call recorder — one ``nous_system.tool_call_log`` row per tool call the model asked for.

``record`` is synchronous and never raises: it redacts and clips the free-text fields with the ledger's
``detail_text`` (head and tail kept, secrets masked before the cut), then hands one parameterised INSERT to the shared
``BoundedWriter``. Nothing here reads the rows; the dashboards and ``db_query`` do.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from nous.cognitive.ledger_store import detail_text
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
# A tool error's text has no setting of its own: the model-call log clips its error text at the same length.
ERROR_TEXT_MAX_CHARS = 1000


def work_kind_for(context_kind: str) -> str:
    """The kind of work a turn of this ``ExecutionContext.kind`` is. An unknown kind counts as a background check,
    like ``background`` (a turn whose caller named no kind): ``work_kind`` is NOT NULL and recording never fails."""
    return _WORK_KIND_BY_CONTEXT.get(context_kind, "background_check")


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    """One tool call, as the runner saw it: the ``tool_call_log`` columns except ``id`` and ``agent_id`` (the
    recorder's). Text fields are given raw; the recorder redacts and clips them. ``started_at`` None means the time
    the row is written."""

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


_COLUMNS: tuple[str, ...] = ("agent_id", *(f.name for f in fields(ToolCallRecord)))
_INSERT = text(
    f"INSERT INTO nous_system.tool_call_log ({', '.join(_COLUMNS)}) VALUES ("
    + ", ".join("COALESCE(:started_at, now())" if c == "started_at" else f":{c}" for c in _COLUMNS)
    + ")"
)


class ToolCallRecorder:
    """Writes ``ToolCallRecord`` rows through the shared ``BoundedWriter`` (built in main.py, never here)."""

    def __init__(self, writer: BoundedWriter, database: Any, agent_id: str, settings: Any) -> None:
        self._writer = writer
        self._database = database
        self._agent_id = agent_id
        self._input_cap = settings.tool_input_max_chars
        self._result_cap = settings.tool_result_preview_chars
        self._reason_cap = settings.tool_reason_max_chars

    @property
    def stats(self) -> RecorderStats:
        """The shared writer's counters."""
        return self._writer.stats

    def record(self, rec: ToolCallRecord) -> None:
        """Redact and clip the record's text, then schedule its INSERT. Synchronous; never raises."""
        try:
            params = {f.name: getattr(rec, f.name) for f in fields(rec)}
            params["agent_id"] = self._agent_id
            params["input_text"] = detail_text(rec.input_text, self._input_cap)
            params["result_preview"] = detail_text(rec.result_preview, self._result_cap)
            if rec.reason is not None:
                params["reason"] = detail_text(rec.reason, self._reason_cap)
            if rec.error_text is not None:
                params["error_text"] = detail_text(rec.error_text, ERROR_TEXT_MAX_CHARS)
        except Exception as exc:
            self._writer.stats.dropped_error += 1
            logger.warning("F108: tool-call record for %s not written: %r", getattr(rec, "tool_name", "?"), exc)
            return
        self._writer.submit(lambda: self._insert(params))

    async def _insert(self, params: dict[str, Any]) -> None:
        async with self._database.session() as session:
            await session.execute(_INSERT, params)
            await session.commit()
