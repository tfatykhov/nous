"""F035.4: Context visibility — logs what the LLM actually sees on each API call."""

from __future__ import annotations

import json
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

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
    "## Known Procedures": "known_procedures",
    "## Past Episodes": "past_episodes",
    "## Recent Conversations": "recent_conversations",
    "## Tool Instructions": "frame_instructions",
    "[Execution Ledger]": "execution_ledger",
    "[Previous Turn Corrections]": "corrections",
    "## Output Formatting": "telegram_format",
}

_MARKER_PATTERNS = sorted(SECTION_MARKERS.keys(), key=len, reverse=True)


def parse_system_sections(system_prompt: str) -> dict[str, str]:
    """Split system prompt into named sections."""
    if not system_prompt:
        return {}
    sections: dict[str, str] = {}
    current_name = "preamble"
    current_lines: list[str] = []
    for line in system_prompt.split("\n"):
        matched = False
        for marker in _MARKER_PATTERNS:
            if line.strip().startswith(marker):
                if current_lines:
                    text = "\n".join(current_lines).strip()
                    if text:
                        sections[current_name] = text
                current_name = SECTION_MARKERS[marker]
                current_lines = []
                matched = True
                break
        if not matched:
            current_lines.append(line)
    if current_lines:
        text = "\n".join(current_lines).strip()
        if text:
            sections[current_name] = text
    if "preamble" in sections:
        sections["other"] = sections.pop("preamble")
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
    return total


@dataclass
class ContextLogEntry:
    """Structured metadata for one API call."""

    id: str
    session_id: str
    turn_number: int
    timestamp: str
    call_type: str
    model: str
    frame_id: str
    trace_id: str | None = None

    token_breakdown: dict[str, int] = field(default_factory=dict)
    total_tokens_est: int = 0
    context_window_size: int = 0
    utilization_pct: float = 0.0

    sections_present: list[str] = field(default_factory=list)
    tools_count: int = 0
    tool_names: list[str] = field(default_factory=list)
    messages_count: int = 0
    message_roles: dict[str, int] = field(default_factory=dict)

    loaded_facts: int = 0
    loaded_decisions: int = 0
    loaded_procedures: int = 0
    loaded_episodes: int = 0
    recent_conversations: int = 0

    # F035.4: Actual section text for deep inspection (in-memory only, not persisted to DB)
    sections_text: dict[str, str] = field(default_factory=dict)

    input_tokens_actual: int | None = None
    output_tokens: int | None = None
    cache_creation_tokens: int | None = None
    cache_read_tokens: int | None = None

    # F036: Cache break detection (in-memory only, not persisted to DB)
    cache_break: bool = False
    cache_break_components: list[str] = field(default_factory=list)
    cache_break_tokens_lost: int = 0

    duration_ms: float | None = None
    stop_reason: str | None = None

    @classmethod
    def from_payload(
        cls,
        session_id: str,
        turn_number: int,
        call_type: str,
        model: str,
        system_prompt: str,
        messages: list[dict],
        tools: list[dict] | None,
        frame_id: str,
        context_window: int,
        trace_id: str | None = None,
        loaded_counts: dict[str, int] | None = None,
    ) -> ContextLogEntry:
        entry_id = uuid4().hex[:16]
        sections = parse_system_sections(system_prompt)
        token_breakdown = {name: _estimate_tokens(text) for name, text in sections.items()}
        tools_list = tools or []
        token_breakdown["tools_definition"] = _estimate_tokens(json.dumps(tools_list))
        token_breakdown["messages"] = _estimate_messages_tokens(messages)
        total = sum(token_breakdown.values())
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
        # PREFER exact per-section counts derived from the turn's selected objects
        # (`recalled_*_ids`, passed as `loaded_counts`) — text-parsing the rendered
        # prompt cannot distinguish a real item from a marker-shaped line embedded in
        # a verbatim field (codex PR #485). Fall back to the line heuristic only when
        # counts aren't supplied (e.g. utility calls with no TurnContext).
        lc = loaded_counts or {}

        def _resolve(key: str, lc_key: str, marker: str | None) -> int:
            parsed = _count_items(key, marker)
            # Section absent/empty in THIS prompt -> 0 (also guards against a stale
            # per-turn loaded_counts leaking into a utility call whose prompt has no
            # such section). Section present -> prefer the exact count derived from
            # the selected objects; fall back to the line heuristic if not supplied.
            if parsed == 0:
                return 0
            return lc.get(lc_key, parsed)

        facts_count = _resolve("relevant_facts", "facts", None)
        decisions_count = _resolve("related_decisions", "decisions", None)
        procedures_count = _resolve("known_procedures", "procedures", "- **")
        episodes_count = _resolve("past_episodes", "episodes", "- [")
        # Not a recalled type (temporal titles tier); single-line `- [time] title`.
        recent_conversations_count = _count_items("recent_conversations", "- [")

        tool_names = [t.get("name", "") for t in tools_list]

        return cls(
            id=entry_id,
            session_id=session_id,
            turn_number=turn_number,
            timestamp=datetime.now(UTC).isoformat(),
            call_type=call_type,
            model=model,
            frame_id=frame_id,
            trace_id=trace_id,
            token_breakdown=token_breakdown,
            total_tokens_est=total,
            context_window_size=context_window,
            utilization_pct=round(utilization, 2),
            sections_present=list(sections.keys()),
            tools_count=len(tools_list),
            tool_names=tool_names,
            messages_count=len(messages),
            message_roles=role_counts,
            loaded_facts=facts_count,
            loaded_decisions=decisions_count,
            loaded_procedures=procedures_count,
            loaded_episodes=episodes_count,
            recent_conversations=recent_conversations_count,
            sections_text=sections,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "turn_number": self.turn_number,
            "timestamp": self.timestamp,
            "call_type": self.call_type,
            "model": self.model,
            "frame_id": self.frame_id,
            "trace_id": self.trace_id,
            "token_breakdown": self.token_breakdown,
            "total_tokens_est": self.total_tokens_est,
            "context_window_size": self.context_window_size,
            "utilization_pct": self.utilization_pct,
            "sections_present": self.sections_present,
            "tools_count": self.tools_count,
            "tool_names": self.tool_names,
            "messages_count": self.messages_count,
            "message_roles": self.message_roles,
            "loaded_facts": self.loaded_facts,
            "loaded_decisions": self.loaded_decisions,
            "loaded_procedures": self.loaded_procedures,
            "loaded_episodes": self.loaded_episodes,
            "recent_conversations": self.recent_conversations,
            "input_tokens_actual": self.input_tokens_actual,
            "output_tokens": self.output_tokens,
            "cache_creation": self.cache_creation_tokens,
            "cache_read": self.cache_read_tokens,
            "duration_ms": self.duration_ms,
            "stop_reason": self.stop_reason,
        }


@dataclass
class ContextPayload:
    entry_id: str
    session_id: str
    payload: dict
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


class FullPayloadStore:
    """Ring buffer for full API payloads."""

    def __init__(self, max_per_session: int = 10, max_total: int = 50):
        self._store: dict[str, deque[ContextPayload]] = {}
        self._index: dict[str, ContextPayload] = {}
        self._max_per_session = max_per_session
        self._max_total = max_total
        self._total_count = 0

    def capture(self, session_id: str, entry_id: str, payload: dict) -> None:
        if session_id not in self._store:
            self._store[session_id] = deque(maxlen=self._max_per_session)
        q = self._store[session_id]
        if len(q) >= self._max_per_session:
            evicted = q[0]
            self._index.pop(evicted.entry_id, None)
            self._total_count -= 1
        q.append(ContextPayload(entry_id=entry_id, session_id=session_id, payload=payload))
        self._index[entry_id] = q[-1]
        self._total_count += 1
        while self._total_count > self._max_total:
            self._evict_oldest_global()

    def _evict_oldest_global(self) -> None:
        oldest_sid = None
        oldest_ts = None
        for sid, q in self._store.items():
            if q and (oldest_ts is None or q[0].timestamp < oldest_ts):
                oldest_sid = sid
                oldest_ts = q[0].timestamp
        if oldest_sid and self._store[oldest_sid]:
            evicted = self._store[oldest_sid].popleft()
            self._index.pop(evicted.entry_id, None)
            self._total_count -= 1
            if not self._store[oldest_sid]:
                del self._store[oldest_sid]

    def get(self, entry_id: str) -> dict | None:
        cp = self._index.get(entry_id)
        return cp.payload if cp else None

    def get_session(self, session_id: str) -> list[ContextPayload]:
        q = self._store.get(session_id)
        return list(reversed(q)) if q else []


class ContextLogger:
    """Logs context metadata for every API call. In-memory + optional DB persistence."""

    def __init__(
        self,
        db_writer=None,
        full_payload_enabled: bool = False,
        ring_size: int = 10,
        max_total: int = 50,
    ):
        self._db_writer = db_writer
        self._entries: deque[ContextLogEntry] = deque(maxlen=200)
        self._entries_by_id: dict[str, ContextLogEntry] = {}
        self._payload_store: FullPayloadStore | None = (
            FullPayloadStore(max_per_session=ring_size, max_total=max_total)
            if full_payload_enabled
            else None
        )

    def _sync_entries_index(self) -> None:
        """REVIEW FIX P1-8: Sync _entries_by_id with deque to prevent memory leak."""
        valid_ids = {e.id for e in self._entries}
        stale = [k for k in self._entries_by_id if k not in valid_ids]
        for k in stale:
            del self._entries_by_id[k]

    def log(
        self,
        session_id: str,
        turn_number: int,
        call_type: str,
        model: str,
        system_prompt: str,
        messages: list[dict],
        tools: list[dict] | None,
        frame_id: str,
        context_window: int,
        payload: dict | None = None,
        trace_id: str | None = None,
        loaded_counts: dict[str, int] | None = None,
    ) -> ContextLogEntry:
        entry = ContextLogEntry.from_payload(
            session_id=session_id,
            turn_number=turn_number,
            call_type=call_type,
            model=model,
            system_prompt=system_prompt,
            messages=messages,
            tools=tools,
            frame_id=frame_id,
            context_window=context_window,
            trace_id=trace_id,
            loaded_counts=loaded_counts,
        )
        self._entries.append(entry)
        self._entries_by_id[entry.id] = entry
        # Periodic cleanup of stale index entries
        if len(self._entries_by_id) > len(self._entries) + 10:
            self._sync_entries_index()

        if self._payload_store and payload:
            self._payload_store.capture(session_id, entry.id, payload)
        if self._db_writer:
            import asyncio

            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._db_writer(entry))
            except RuntimeError:
                pass
        return entry

    def update_response(
        self,
        entry_id: str,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cache_creation: int | None = None,
        cache_read: int | None = None,
        duration_ms: float | None = None,
        stop_reason: str | None = None,
    ) -> None:
        entry = self._entries_by_id.get(entry_id)
        if entry:
            entry.input_tokens_actual = input_tokens
            entry.output_tokens = output_tokens
            entry.cache_creation_tokens = cache_creation
            entry.cache_read_tokens = cache_read
            entry.duration_ms = duration_ms
            entry.stop_reason = stop_reason

    def get_recent(self, session_id: str | None = None, limit: int = 20) -> list[ContextLogEntry]:
        entries = list(reversed(self._entries))
        if session_id:
            entries = [e for e in entries if e.session_id == session_id]
        return entries[:limit]

    def get_entry(self, entry_id: str) -> ContextLogEntry | None:
        return self._entries_by_id.get(entry_id)

    def get_payload(self, entry_id: str) -> dict | None:
        return self._payload_store.get(entry_id) if self._payload_store else None
