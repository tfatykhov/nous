"""F108: what the context endpoints read — the F035.4 ring, or the model-call table.

``/context/log*``, ``/context/diff``, ``/dashboard/cache`` and ``/dashboard/observability``'s ``context_log`` read
through a ``ContextView``. ``RingContextView`` is the in-memory ring, as every endpoint read it before PR 4: it is
what they read while ``NOUS_MODEL_CALL_LOG_ENABLED`` is off, and it touches no table. ``TableContextView`` reads
``nous_system.model_call_log`` (the runner's own calls only, ``MAIN_LOOP_PURPOSES``), so the views survive a restart
and show failed calls. It rebuilds each row as a ``ContextLogEntry``, so every key the endpoints served keeps its
name and meaning; ``call_type`` carries the purpose, and a few keys are added (``TableContextLogEntry``). A call's
payload and section text come from the text store when the text is recorded, else from the ring (its full payload,
when ``NOUS_CONTEXT_LOG_FULL_PAYLOAD`` captures it, and its section text), which knows each call by the row's id.
"""

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import text

from nous.observability.context_logger import ContextLogEntry
from nous.observability.model_calls import MAIN_LOOP_PURPOSES, payload_preamble, system_sections


class ContextView(Protocol):
    """What the context endpoints read. ``reads_table`` is True for the model-call table, whose responses add keys."""

    reads_table: bool

    async def recent(
        self, session_id: str | None, limit: int, exclude_purposes: frozenset[str]
    ) -> list[ContextLogEntry]: ...

    async def get(self, entry_id: str) -> ContextLogEntry | None: ...

    async def payload(self, entry_id: str) -> dict | None: ...

    async def sections_text(self, entry_id: str) -> dict[str, str]: ...


class RingContextView:
    """The F035.4 in-memory ring, read exactly as the endpoints read it before PR 4. Its call types are ``chat``,
    ``subtask`` and ``heartbeat`` (every call on the heartbeat's fork), so leaving out ``QUIET_PURPOSES`` leaves out
    the heartbeat's calls, as the old ``HEARTBEAT_CALL_TYPE`` exclusion did."""

    reads_table = False

    def __init__(self, context_logger: Any) -> None:
        self._ring = context_logger

    async def recent(
        self, session_id: str | None, limit: int, exclude_purposes: frozenset[str]
    ) -> list[ContextLogEntry]:
        """The newest ``limit`` entries (of ``session_id``), the excluded call types left out before the limit."""
        entries = self._ring.get_recent(session_id=session_id, limit=sys.maxsize)
        return [e for e in entries if e.call_type not in exclude_purposes][:limit]

    async def get(self, entry_id: str) -> ContextLogEntry | None:
        return self._ring.get_entry(entry_id)

    async def payload(self, entry_id: str) -> dict | None:
        return self._ring.get_payload(entry_id)

    async def sections_text(self, entry_id: str) -> dict[str, str]:
        entry = self._ring.get_entry(entry_id)
        return entry.sections_text if entry else {}


@dataclass
class TableContextLogEntry(ContextLogEntry):
    """A ``model_call_log`` row as a ``ContextLogEntry``. ``to_dict()`` keeps every key and adds five:
    ``input_total_tokens`` (uncached input plus cache reads plus cache writes; null when the call reported none),
    ``status`` (ok, error or cancelled), ``purpose`` (also in ``call_type``), ``attempts`` (HTTP attempts) and
    ``turn_id``. ``input_tokens_actual`` stays the uncached input."""

    input_total_tokens: int | None = None
    status: str | None = None
    purpose: str | None = None
    attempts: int | None = None
    turn_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            **super().to_dict(),
            "input_total_tokens": self.input_total_tokens,
            "status": self.status,
            "purpose": self.purpose,
            "attempts": self.attempts,
            "turn_id": self.turn_id,
        }


_COLUMNS = (
    "id, started_at, purpose, model, session_id, turn_id, turn_number, frame_id, trace_id, status, stop_reason, "
    "input_uncached, cache_read, cache_write, input_total, output_tokens, duration_ms, attempts, section_tokens_est, "
    "total_tokens_est, context_window_size, utilization_pct, sections_present, tools_count, tool_names, "
    "messages_count, message_roles, loaded_facts, loaded_decisions, loaded_procedures, loaded_episodes, "
    "recent_conversations, cache_break, cache_break_components, cache_break_tokens_lost"
)
_RECENT = (
    f"SELECT {_COLUMNS} FROM nous_system.model_call_log "
    "WHERE agent_id = :agent_id AND purpose = ANY(CAST(:purposes AS text[])){session} "
    "ORDER BY started_at DESC, id DESC LIMIT :limit"
)
_GET = text(
    f"SELECT {_COLUMNS} FROM nous_system.model_call_log "
    "WHERE id = :id AND agent_id = :agent_id AND purpose = ANY(CAST(:purposes AS text[]))"
)
_MAIN_LOOP = sorted(MAIN_LOOP_PURPOSES)
# The most entries one read returns: the number /dashboard/cache asks for, and the most the ring's entry list ever
# held (``ContextLogger._entries``, a deque of 200; its full payloads are capped apart, at ``context_log_max_total``,
# 50). A limit the caller passes is held to 0 to this (a negative LIMIT is an error in Postgres).
MAX_RECENT = 200


def _entry(row: Any) -> TableContextLogEntry:
    """One row as an entry. A column the row left NULL takes the default the ring's entries had."""
    parts = (row.input_uncached, row.cache_read, row.cache_write)
    return TableContextLogEntry(
        id=str(row.id),
        session_id=row.session_id,
        turn_number=row.turn_number,
        timestamp=row.started_at.isoformat(),
        call_type=row.purpose,
        model=row.model or "",
        frame_id=row.frame_id,
        trace_id=row.trace_id,
        token_breakdown=row.section_tokens_est or {},
        total_tokens_est=row.total_tokens_est or 0,
        context_window_size=row.context_window_size or 0,
        # REAL holds 0.01 as 0.00999999977: the estimate rounds to 2 places, and so does this.
        utilization_pct=round(row.utilization_pct or 0.0, 2),
        sections_present=list(row.sections_present or []),
        tools_count=row.tools_count or 0,
        tool_names=list(row.tool_names or []),
        messages_count=row.messages_count or 0,
        message_roles=row.message_roles or {},
        loaded_facts=row.loaded_facts or 0,
        loaded_decisions=row.loaded_decisions or 0,
        loaded_procedures=row.loaded_procedures or 0,
        loaded_episodes=row.loaded_episodes or 0,
        recent_conversations=row.recent_conversations or 0,
        input_tokens_actual=row.input_uncached,
        output_tokens=row.output_tokens,
        cache_creation_tokens=row.cache_write,
        cache_read_tokens=row.cache_read,
        cache_break=bool(row.cache_break),
        cache_break_components=list(row.cache_break_components or []),
        cache_break_tokens_lost=row.cache_break_tokens_lost or 0,
        duration_ms=row.duration_ms,
        stop_reason=row.stop_reason,
        input_total_tokens=None if all(p is None for p in parts) else row.input_total,
        status=row.status,
        purpose=row.purpose,
        attempts=row.attempts,
        turn_id=None if row.turn_id is None else str(row.turn_id),
    )


def _parsed(body: str) -> Any:
    """A stored JSON block, parsed; a block cut at the size cap is no longer JSON and stays text."""
    try:
        return json.loads(body)
    except ValueError:
        return body


def _payload_from_text(model: str, parts: dict[str, list[str]]) -> dict[str, Any]:
    """A call's stored text as a payload: ``system`` text blocks, ``messages``, ``tools`` and, unlike a sent
    payload, the ``response`` content. Redacted, as stored."""
    payload: dict[str, Any] = {"model": model}
    if "system" in parts:
        payload["system"] = [{"type": "text", "text": body} for body in parts["system"]]
    if "messages" in parts:
        payload["messages"] = [_parsed(body) for body in parts["messages"]]
    if "tools" in parts:
        payload["tools"] = _parsed(parts["tools"][0])
    if "response" in parts:
        payload["response"] = _parsed(parts["response"][0])
    return payload


def _sections_from_system(bodies: list[str]) -> dict[str, str]:
    """The section text of a call's stored system blocks, parsed as the runner parsed the prompt: block 0 is the
    section ``preamble`` when it is the Claude Code preamble, and the rest is the prompt."""
    blocks = [{"type": "text", "text": body} for body in bodies]
    preamble = payload_preamble(blocks)
    return system_sections(blocks[1:] if preamble else blocks, preamble)


class TableContextView:
    """``nous_system.model_call_log``, the runner's calls only (``MAIN_LOOP_PURPOSES``), newest first.

    ``text_store`` (a ``TextStore``, when ``NOUS_MODEL_CALL_TEXT_ENABLED`` records the text) serves a call's payload
    and section text; else, or when a call has none, ``context_logger``'s ring does, by the same id; else there is
    none. A database fault raises to the endpoint."""

    reads_table = True

    def __init__(self, database: Any, agent_id: str, text_store: Any = None, context_logger: Any = None) -> None:
        self._database = database
        self._agent_id = agent_id
        self._text = text_store
        self._ring = context_logger

    async def recent(
        self, session_id: str | None, limit: int, exclude_purposes: frozenset[str]
    ) -> list[TableContextLogEntry]:
        """The newest ``limit`` runner calls (of ``session_id``), the excluded purposes left out."""
        params: dict[str, Any] = {
            "agent_id": self._agent_id,
            "purposes": [p for p in _MAIN_LOOP if p not in exclude_purposes],
            "limit": min(max(limit, 0), MAX_RECENT),
        }
        session = ""
        if session_id:
            session, params["session_id"] = " AND session_id = :session_id", session_id
        async with self._database.session() as s:
            rows = (await s.execute(text(_RECENT.format(session=session)), params)).all()
        return [_entry(row) for row in rows]

    async def get(self, entry_id: str) -> TableContextLogEntry | None:
        """The runner call ``entry_id`` (its row id), or None (not a row id, or not a runner call of this agent)."""
        try:
            call_id = uuid.UUID(entry_id)
        except (ValueError, TypeError, AttributeError):
            return None
        async with self._database.session() as s:
            row = (await s.execute(_GET, {"id": call_id, "agent_id": self._agent_id, "purposes": _MAIN_LOOP})).first()
        return None if row is None else _entry(row)

    async def _text_of(self, entry_id: str) -> tuple[TableContextLogEntry, dict[str, list[str]]] | None:
        """The runner call and its stored text, when the text is recorded and this call has some."""
        if self._text is None:
            return None
        entry = await self.get(entry_id)
        if entry is None:
            return None
        parts = await self._text.load(uuid.UUID(entry.id))
        return (entry, parts) if parts else None

    async def payload(self, entry_id: str) -> dict | None:
        stored = await self._text_of(entry_id)
        if stored is not None:
            return _payload_from_text(stored[0].model, stored[1])
        return self._ring.get_payload(entry_id) if self._ring else None

    async def sections_text(self, entry_id: str) -> dict[str, str]:
        stored = await self._text_of(entry_id)
        if stored is not None and stored[1].get("system"):
            return _sections_from_system(stored[1]["system"])
        entry = self._ring.get_entry(entry_id) if self._ring else None
        return entry.sections_text if entry else {}
