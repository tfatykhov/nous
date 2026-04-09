"""In-process async event bus for Nous.

Events are dispatched to registered handlers asynchronously.
Handlers run concurrently but errors are isolated — one broken
handler never crashes the bus or blocks other handlers.

Also persists events to DB (existing behavior) for audit trail.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

# Handler type: async function taking an Event
EventHandler = Callable[["Event"], Awaitable[None]]


@dataclass
class Event:
    """A typed event flowing through the bus."""

    type: str
    agent_id: str
    data: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    # F035.2: Causal chain fields
    event_id: str = field(default_factory=lambda: uuid4().hex[:12])
    trace_id: str | None = None       # root cause identifier (shared across chain)
    caused_by: str | None = None      # event_id of the direct parent event


@dataclass
class HandlerStat:
    """Per-handler invocation statistics (F035.1)."""
    name: str
    invocations: int = 0
    successes: int = 0
    errors: int = 0
    last_invoked: float | None = None
    last_error: float | None = None
    last_error_msg: str | None = None
    total_duration_ms: float = 0.0

    @property
    def avg_duration_ms(self) -> float:
        return self.total_duration_ms / self.invocations if self.invocations else 0.0

    @property
    def error_rate(self) -> float:
        return self.errors / self.invocations if self.invocations else 0.0


@dataclass
class RecentEvent:
    """Lightweight record for the ring buffer (F035.1)."""
    type: str
    timestamp: str
    handlers_invoked: int
    handlers_failed: int
    duration_ms: float
    session_id: str | None = None


class EventBusStats:
    """In-memory event bus statistics. Zero-allocation hot path (F035.1)."""

    def __init__(self, recent_limit: int = 100):
        self._event_counts: dict[str, int] = defaultdict(int)
        self._handler_stats: dict[str, HandlerStat] = {}
        self._recent: deque[RecentEvent] = deque(maxlen=recent_limit)
        self._total_processed: int = 0
        self._total_dropped: int = 0
        self._started_at: float = time.monotonic()

    @property
    def total_processed(self) -> int:
        return self._total_processed

    @property
    def total_dropped(self) -> int:
        return self._total_dropped

    def record_event(
        self, event_type: str, handlers_invoked: int, handlers_failed: int,
        duration_ms: float, session_id: str | None = None,
    ) -> None:
        self._total_processed += 1
        self._event_counts[event_type] += 1
        self._recent.append(RecentEvent(
            type=event_type, timestamp=datetime.now(UTC).isoformat(),
            handlers_invoked=handlers_invoked, handlers_failed=handlers_failed,
            duration_ms=duration_ms, session_id=session_id,
        ))

    def record_handler_success(self, handler_name: str, duration_ms: float) -> None:
        stat = self._handler_stats.setdefault(handler_name, HandlerStat(name=handler_name))
        stat.invocations += 1
        stat.successes += 1
        stat.total_duration_ms += duration_ms
        stat.last_invoked = time.monotonic()

    def record_handler_error(self, handler_name: str, error_msg: str) -> None:
        stat = self._handler_stats.setdefault(handler_name, HandlerStat(name=handler_name))
        stat.invocations += 1
        stat.errors += 1
        stat.last_error = time.monotonic()
        stat.last_error_msg = error_msg

    def record_drop(self) -> None:
        self._total_dropped += 1

    def recent_events(self, limit: int | None = None) -> list[RecentEvent]:
        events = list(reversed(self._recent))
        return events[:limit] if limit is not None else events

    def to_dict(self) -> dict[str, Any]:
        now = time.monotonic()
        handlers = {}
        for name, stat in self._handler_stats.items():
            h: dict[str, Any] = {
                "invocations": stat.invocations, "successes": stat.successes,
                "errors": stat.errors, "error_rate": round(stat.error_rate, 3),
                "avg_duration_ms": round(stat.avg_duration_ms, 2),
            }
            if stat.last_invoked is not None:
                h["last_invoked_ago_s"] = round(now - stat.last_invoked, 1)
            if stat.last_error is not None:
                h["last_error_ago_s"] = round(now - stat.last_error, 1)
                h["last_error_msg"] = stat.last_error_msg
            handlers[name] = h
        return {
            "uptime_seconds": round(now - self._started_at, 1),
            "total_processed": self._total_processed,
            "total_dropped": self._total_dropped,
            "event_counts": dict(self._event_counts),
            "handlers": handlers,
        }


class EventBus:
    """In-process async event bus with error isolation.

    Events are queued and processed by a background asyncio task.
    Handlers registered via on() are called concurrently for each event.
    Handler errors are logged but never propagate.

    The bus also delegates to a DB persister (the existing emit_event
    pattern) so all events remain in the audit table.
    """

    def __init__(self, max_queue: int = 1000):
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=max_queue)
        self._task: asyncio.Task | None = None
        self._running = False
        self._db_persister: EventHandler | None = None
        self.stats = EventBusStats()

    def on(self, event_type: str, handler: EventHandler) -> None:
        """Register a handler for an event type. Can register multiple."""
        self._handlers[event_type].append(handler)
        logger.debug("Registered handler for '%s': %s", event_type, handler.__qualname__)

    def set_db_persister(self, persister: EventHandler) -> None:
        """Set the DB persistence handler (existing Brain.emit_event pattern)."""
        self._db_persister = persister

    async def emit(self, event: Event) -> None:
        """Emit an event. Non-blocking — queued for async processing.

        If queue is full, logs warning and drops event (never blocks caller).
        """
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self.stats.record_drop()
            logger.warning("Event bus queue full, dropping event: %s", event.type)

    async def start(self) -> None:
        """Start the background processing loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._process_loop(), name="event-bus")
        logger.info("Event bus started")

    async def stop(self) -> None:
        """Stop the bus. Drains remaining events before stopping.

        P1-2 fix: Cancel task first, await it, THEN drain remaining events.
        """
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            # THEN drain remaining events
            while not self._queue.empty():
                try:
                    event = self._queue.get_nowait()
                    await self._dispatch(event)
                except asyncio.QueueEmpty:
                    break
        logger.info("Event bus stopped")

    async def _process_loop(self) -> None:
        """Main processing loop — runs as background task."""
        while self._running:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                await self._dispatch(event)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Unexpected error in event bus loop")

    async def _dispatch(self, event: Event) -> None:
        """Dispatch event to all registered handlers + DB persister."""
        start = time.monotonic()
        # DB persistence (fire-and-forget, errors logged)
        if self._db_persister:
            try:
                await self._db_persister(event)
            except Exception:
                logger.warning("DB persist failed for event %s", event.type)

        # Handlers — run concurrently, errors isolated
        handlers = self._handlers.get(event.type, [])
        if not handlers:
            duration_ms = (time.monotonic() - start) * 1000
            self.stats.record_event(event.type, 0, 0, duration_ms, event.session_id)
            return

        results = await asyncio.gather(*(self._safe_handle(h, event) for h in handlers))
        failed = sum(1 for ok in results if not ok)
        duration_ms = (time.monotonic() - start) * 1000
        self.stats.record_event(event.type, len(handlers), failed, duration_ms, event.session_id)

    async def _safe_handle(self, handler: EventHandler, event: Event) -> bool:
        """Run handler with error isolation. Never propagates (except CancelledError).

        P0-13 fix: catch BaseException, re-raise CancelledError.
        F035.1: Returns True on success, False on error. Records handler stats.
        """
        handler_name = handler.__qualname__
        start = time.monotonic()
        try:
            await handler(event)
            duration_ms = (time.monotonic() - start) * 1000
            self.stats.record_handler_success(handler_name, duration_ms)
            return True
        except asyncio.CancelledError:
            raise  # Propagate cancellation
        except BaseException as exc:
            duration_ms = (time.monotonic() - start) * 1000
            self.stats.record_handler_error(handler_name, str(exc))
            logger.exception(
                "Handler %s failed for event %s",
                handler_name,
                event.type,
            )
            return False

    @property
    def pending(self) -> int:
        """Number of events waiting in queue."""
        return self._queue.qsize()
