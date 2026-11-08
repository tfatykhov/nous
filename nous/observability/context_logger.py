"""F035.4: Context visibility — logs what the LLM actually sees on each API call."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

# The estimate moved to model_calls.py (F108); these names stay importable from here.
from nous.observability.model_calls import (  # noqa: F401
    SECTION_MARKERS,
    ContextStats,
    _estimate_messages_tokens,
    _estimate_tokens,
    analyze_context,
    analyze_sections,
    parse_system_sections,
    system_sections,
)

logger = logging.getLogger(__name__)

# Each DB write is cut off after this long and at most this many run at once, so the
# fire-and-forget writes can never hold the shared connection pool (the April 2026
# heartbeat triage hang was blamed on contention with these writes).
DB_WRITE_TIMEOUT_SECONDS = 30
DB_WRITE_CONCURRENCY = 4

# The call_type of a model call made on a forked runner (the heartbeat's checks, triage and
# callbacks). Those calls share this ring with chat, so the chat views leave them out.
HEARTBEAT_CALL_TYPE = "heartbeat"


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
        preamble: str = "",
        sections: dict[str, str] | None = None,
        stats: ContextStats | None = None,
    ) -> ContextLogEntry:
        """The entry for one call. ``sections`` and ``stats`` are the estimate a caller already made from these
        inputs (``system_sections`` then ``analyze_sections``, as the runner does once per call); without them the
        entry makes it."""
        entry_id = uuid4().hex[:16]
        # analyze_context's estimate, from sections parsed once: the entry keeps their text too.
        if sections is None or stats is None:
            sections = system_sections(system_prompt, preamble)
            stats = analyze_sections(sections, messages, tools, context_window)

        return cls(
            id=entry_id,
            session_id=session_id,
            turn_number=turn_number,
            timestamp=datetime.now(UTC).isoformat(),
            call_type=call_type,
            model=model,
            frame_id=frame_id,
            trace_id=trace_id,
            token_breakdown=stats.section_tokens_est,
            total_tokens_est=stats.total_tokens_est,
            context_window_size=stats.context_window_size,
            utilization_pct=stats.utilization_pct,
            sections_present=stats.sections_present,
            tools_count=stats.tools_count,
            tool_names=stats.tool_names,
            messages_count=stats.messages_count,
            message_roles=stats.message_roles,
            loaded_facts=stats.loaded_facts,
            loaded_decisions=stats.loaded_decisions,
            loaded_procedures=stats.loaded_procedures,
            loaded_episodes=stats.loaded_episodes,
            recent_conversations=stats.recent_conversations,
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
        db_updater=None,
    ):
        self._db_writer = db_writer
        # Audit OB-3 (2026-06-09): optional persister for the response-side
        # columns (tokens/cache/duration/stop_reason). The INSERT at log() time
        # runs before the response is known, so without this the migration-026
        # response columns stayed NULL forever — update_response was in-memory
        # only.
        self._db_updater = db_updater
        self._entries: deque[ContextLogEntry] = deque(maxlen=200)
        self._entries_by_id: dict[str, ContextLogEntry] = {}
        self._payload_store: FullPayloadStore | None = (
            FullPayloadStore(max_per_session=ring_size, max_total=max_total)
            if full_payload_enabled
            else None
        )
        # Audit OB-3/OB-5 (review P1): hold strong refs to fire-and-forget DB
        # tasks. asyncio's loop keeps only a WEAK reference, so a task created
        # and immediately discarded can be GC'd mid-flight — silently dropping
        # the INSERT/UPDATE. Track + auto-discard on completion.
        self._pending_tasks: set = set()
        # The INSERT task of each entry still being written, so its response UPDATE
        # runs after it (an UPDATE that ran first matched no row: a NULL response).
        self._insert_tasks: dict[str, asyncio.Task] = {}
        self._db_slots = asyncio.Semaphore(DB_WRITE_CONCURRENCY)

    def _schedule_bg(self, coro) -> asyncio.Task | None:
        """Run a DB coroutine fire-and-forget while retaining a strong ref. Returns the task, or None."""
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()  # no loop (sync context) — avoid "never awaited" warning
            return None
        task = loop.create_task(coro)
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)
        return task

    async def _bounded(self, make_write: Callable[[], Awaitable[Any]]) -> None:
        """One DB write, within the concurrency cap and the timeout.

        Takes a factory, not a coroutine: the write's coroutine is created only once a slot
        is held, so none is left un-awaited when there is no loop or the task is cancelled
        while queued.
        """
        try:
            async with self._db_slots:
                await asyncio.wait_for(make_write(), timeout=DB_WRITE_TIMEOUT_SECONDS)
        except TimeoutError:
            logger.warning("F035.4: context log DB write timed out after %ss", DB_WRITE_TIMEOUT_SECONDS)

    async def _update_after_insert(self, insert: asyncio.Task | None, entry: ContextLogEntry) -> None:
        if insert is not None:
            await asyncio.wait([insert])  # never raises: a failed INSERT still lets the UPDATE try
        await self._bounded(lambda: self._db_updater(entry))

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
        preamble: str = "",
        sections: dict[str, str] | None = None,
        stats: ContextStats | None = None,
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
            preamble=preamble,
            sections=sections,
            stats=stats,
        )
        self._entries.append(entry)
        self._entries_by_id[entry.id] = entry
        # Periodic cleanup of stale index entries
        if len(self._entries_by_id) > len(self._entries) + 10:
            self._sync_entries_index()

        if self._payload_store and payload:
            self._payload_store.capture(session_id, entry.id, payload)
        if self._db_writer:
            task = self._schedule_bg(self._bounded(lambda: self._db_writer(entry)))
            if task is not None:
                self._insert_tasks[entry.id] = task
                entry_id = entry.id
                task.add_done_callback(lambda _t: self._insert_tasks.pop(entry_id, None))
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
            # OB-3: persist the response columns (fire-and-forget, after the entry's INSERT).
            if self._db_updater is not None:
                self._schedule_bg(self._update_after_insert(self._insert_tasks.get(entry_id), entry))

    def get_recent(
        self, session_id: str | None = None, limit: int = 20, exclude_call_type: str | None = None
    ) -> list[ContextLogEntry]:
        entries = list(reversed(self._entries))
        if session_id:
            entries = [e for e in entries if e.session_id == session_id]
        if exclude_call_type:
            entries = [e for e in entries if e.call_type != exclude_call_type]
        return entries[:limit]

    def get_entry(self, entry_id: str) -> ContextLogEntry | None:
        return self._entries_by_id.get(entry_id)

    def get_payload(self, entry_id: str) -> dict | None:
        return self._payload_store.get(entry_id) if self._payload_store else None
