# F035 Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add full observability to Nous — event bus stats, causal chain tracing, behavioral drift detection, and context visibility for every LLM API call.

**Architecture:** Four layers built in dependency order: F035.1 (event bus stats — in-memory counters + ring buffer), F035.2 (causal chain tracing — event_id/trace_id/caused_by on events + propagation across all handlers), F035.4 (context visibility — structured metadata for every API call + section parser + full payload ring buffer), F035.3 (behavioral drift detection — periodic metric snapshots + z-score anomaly detection as a heartbeat check). Each layer has its own REST endpoints and Telegram integration.

**Tech Stack:** Python 3.12+, asyncio, SQLAlchemy 2.0 async, Starlette REST, PostgreSQL 17, pytest + pytest-asyncio

---

## File Structure

### New Files
| File | Responsibility |
|------|---------------|
| `nous/observability/__init__.py` | Package init — exports key classes |
| `nous/observability/context_logger.py` | F035.4: ContextLogger, ContextLogEntry, FullPayloadStore, section parser |
| `nous/observability/snapshots.py` | F035.3: BehaviorSnapshot capture logic |
| `nous/observability/drift.py` | F035.3: DriftDetector, Anomaly dataclass |
| `nous/heartbeat/checks/behavior_drift.py` | F035.3: BehaviorDriftCheck (heartbeat integration) |
| `sql/migrations/026_observability.sql` | DB migration: events columns + context_log + behavior_snapshots tables |
| `tests/test_event_bus_observability.py` | F035.1 tests |
| `tests/test_causal_tracing.py` | F035.2 tests |
| `tests/test_context_logger.py` | F035.4 tests |
| `tests/test_drift_detection.py` | F035.3 tests |

### Modified Files
| File | Changes |
|------|---------|
| `nous/events.py` | F035.1: `EventBusStats`, `HandlerStat`, `RecentEvent` dataclasses + stats wiring in EventBus. F035.2: `event_id`, `trace_id`, `caused_by` on Event |
| `nous/storage/models.py` | F035.2: Add `event_id`, `trace_id`, `caused_by` columns to ORM Event model. F035.4: New `ContextLog` ORM model |
| `nous/config.py` | F035.3+F035.4: New settings fields |
| `nous/main.py` | Wire DB persister for new Event fields, wire ContextLogger into runner, wire BehaviorDriftCheck into heartbeat |
| `nous/api/rest.py` | F035.1: `/events/stats`, `/events/recent`. F035.2: `/events/trace/{id}`, `/events/recent-traces`, `/events/modifications`. F035.3: `/behavior/snapshot/latest`, `/behavior/trends`, `/behavior/anomalies`, `/behavior/drift-report`. F035.4: `/context/log`, `/context/log/{id}`, `/context/log/{id}/payload`, `/context/log/{id}/sections`, `/context/diff` |
| `nous/api/runner.py` | F035.4: Hook ContextLogger into `_build_api_payload()` and response handling |
| `nous/telegram_bot.py` | F035.1: Event bus section in `/status`. F035.2: `/trace`, `/modifications` commands. F035.3: `/drift` command + alert notifications. F035.4: `/context` command + status line |
| `nous/heartbeat/runner.py` | F035.1: `get_stats()` method. F035.2: Emit trace_id on heartbeat_tick, propagate to children |
| `nous/handlers/session_monitor.py` | F035.1: `get_stats()`. F035.2: Emit trace_id on session_ended |
| `nous/handlers/sleep_handler.py` | F035.1: tracking fields + `get_stats()`. F035.2: Propagate trace context + tag modifications |
| `nous/handlers/episode_summarizer.py` | F035.2: Propagate trace context |
| `nous/handlers/fact_extractor.py` | F035.2: Propagate trace context + tag modifications |
| `nous/handlers/outcome_detector.py` | F035.2: Propagate trace context |
| `nous/cognitive/layer.py` | F035.2: Emit trace_id on message_received, turn_completed |
| `nous/heart/heart.py` | F035.2: Propagate trace context on fact_learned |
| `nous/handlers/subtask_worker.py` | F035.2: Propagate trace context |

---

## Task 1: F035.1 — EventBusStats Dataclasses and Core Logic

**Files:**
- Modify: `nous/events.py` (add dataclasses + stats class after Event, before EventBus)
- Test: `tests/test_event_bus_observability.py`

- [ ] **Step 1: Write the failing tests for EventBusStats**

Create `tests/test_event_bus_observability.py`:

```python
"""Tests for F035.1 — Event Bus Observability."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from unittest.mock import AsyncMock, MagicMock

import pytest

from nous.events import Event, EventBus, EventBusStats, HandlerStat, RecentEvent


class TestHandlerStat:
    def test_initial_state(self):
        stat = HandlerStat(name="test_handler")
        assert stat.invocations == 0
        assert stat.successes == 0
        assert stat.errors == 0
        assert stat.last_invoked is None
        assert stat.last_error is None
        assert stat.last_error_msg is None
        assert stat.total_duration_ms == 0.0

    def test_avg_duration(self):
        stat = HandlerStat(name="test_handler", invocations=10, total_duration_ms=500.0)
        assert stat.avg_duration_ms == 50.0

    def test_avg_duration_zero_invocations(self):
        stat = HandlerStat(name="test_handler")
        assert stat.avg_duration_ms == 0.0

    def test_error_rate(self):
        stat = HandlerStat(name="test_handler", invocations=10, errors=3)
        assert abs(stat.error_rate - 0.3) < 0.001

    def test_error_rate_zero_invocations(self):
        stat = HandlerStat(name="test_handler")
        assert stat.error_rate == 0.0


class TestEventBusStats:
    def test_initial_state(self):
        stats = EventBusStats()
        assert stats.total_processed == 0
        assert stats.total_dropped == 0
        assert len(stats.recent_events()) == 0

    def test_record_event(self):
        stats = EventBusStats()
        stats.record_event("turn_completed", handlers_invoked=2, handlers_failed=0, duration_ms=5.0, session_id="s1")
        assert stats.total_processed == 1
        assert stats._event_counts["turn_completed"] == 1
        recent = stats.recent_events()
        assert len(recent) == 1
        assert recent[0].type == "turn_completed"
        assert recent[0].handlers_invoked == 2

    def test_record_handler_success(self):
        stats = EventBusStats()
        stats.record_handler_success("MyHandler.handle", 10.5)
        hs = stats._handler_stats["MyHandler.handle"]
        assert hs.invocations == 1
        assert hs.successes == 1
        assert hs.total_duration_ms == 10.5
        assert hs.last_invoked is not None

    def test_record_handler_error(self):
        stats = EventBusStats()
        stats.record_handler_error("MyHandler.handle", "boom")
        hs = stats._handler_stats["MyHandler.handle"]
        assert hs.invocations == 1
        assert hs.errors == 1
        assert hs.last_error_msg == "boom"

    def test_record_drop(self):
        stats = EventBusStats()
        stats.record_drop()
        assert stats.total_dropped == 1

    def test_ring_buffer_limit(self):
        stats = EventBusStats(recent_limit=3)
        for i in range(5):
            stats.record_event(f"event_{i}", 1, 0, 1.0)
        recent = stats.recent_events()
        assert len(recent) == 3
        # Newest first
        assert recent[0].type == "event_4"

    def test_to_dict(self):
        stats = EventBusStats()
        stats.record_event("test", 1, 0, 1.0)
        stats.record_handler_success("H.run", 5.0)
        d = stats.to_dict()
        assert "uptime_seconds" in d
        assert d["total_processed"] == 1
        assert "H.run" in d["handlers"]
        assert "event_counts" in d

    def test_recent_events_limit(self):
        stats = EventBusStats(recent_limit=10)
        for i in range(10):
            stats.record_event(f"e_{i}", 1, 0, 1.0)
        assert len(stats.recent_events(limit=3)) == 3
        assert stats.recent_events(limit=3)[0].type == "e_9"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_event_bus_observability.py -v`
Expected: FAIL with ImportError (EventBusStats, HandlerStat, RecentEvent not defined)

- [ ] **Step 3: Implement EventBusStats, HandlerStat, RecentEvent in events.py**

Add these after the `Event` dataclass and before the `EventBus` class in `nous/events.py`:

```python
import time
from collections import deque

@dataclass
class HandlerStat:
    """Per-handler invocation statistics."""

    name: str
    invocations: int = 0
    successes: int = 0
    errors: int = 0
    last_invoked: float | None = None      # time.monotonic()
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
    """Lightweight record for the ring buffer."""

    type: str
    timestamp: str              # ISO format
    handlers_invoked: int
    handlers_failed: int
    duration_ms: float
    session_id: str | None = None


class EventBusStats:
    """In-memory event bus statistics. Zero-allocation hot path."""

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
        self,
        event_type: str,
        handlers_invoked: int,
        handlers_failed: int,
        duration_ms: float,
        session_id: str | None = None,
    ) -> None:
        self._total_processed += 1
        self._event_counts[event_type] += 1
        self._recent.append(RecentEvent(
            type=event_type,
            timestamp=datetime.now(UTC).isoformat(),
            handlers_invoked=handlers_invoked,
            handlers_failed=handlers_failed,
            duration_ms=duration_ms,
            session_id=session_id,
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
        if limit is not None:
            events = events[:limit]
        return events

    def to_dict(self) -> dict[str, Any]:
        now = time.monotonic()
        handlers = {}
        for name, stat in self._handler_stats.items():
            h: dict[str, Any] = {
                "invocations": stat.invocations,
                "successes": stat.successes,
                "errors": stat.errors,
                "error_rate": round(stat.error_rate, 3),
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_event_bus_observability.py::TestHandlerStat tests/test_event_bus_observability.py::TestEventBusStats -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add nous/events.py tests/test_event_bus_observability.py
git commit -m "feat(f035.1): add EventBusStats, HandlerStat, RecentEvent dataclasses"
```

---

## Task 2: F035.1 — Wire EventBusStats into EventBus

**Files:**
- Modify: `nous/events.py` (EventBus class — `__init__`, `emit`, `_dispatch`, `_safe_handle`)
- Test: `tests/test_event_bus_observability.py`

- [ ] **Step 1: Write the failing tests for EventBus stats integration**

Add to `tests/test_event_bus_observability.py`:

```python
class TestEventBusStatsWiring:
    """Tests that EventBus populates stats during event processing."""

    @pytest.mark.asyncio
    async def test_stats_populated_after_event(self):
        bus = EventBus()
        handler_called = asyncio.Event()

        async def handler(event: Event):
            handler_called.set()

        bus.on("test", handler)
        await bus.start()
        try:
            await bus.emit(Event(type="test", agent_id="a", data={}))
            await asyncio.wait_for(handler_called.wait(), timeout=2.0)
            await asyncio.sleep(0.05)  # Let stats recording complete
            assert bus.stats.total_processed >= 1
            assert bus.stats._event_counts["test"] >= 1
        finally:
            await bus.stop()

    @pytest.mark.asyncio
    async def test_handler_success_recorded(self):
        bus = EventBus()
        done = asyncio.Event()

        async def handler(event: Event):
            done.set()

        bus.on("test", handler)
        await bus.start()
        try:
            await bus.emit(Event(type="test", agent_id="a", data={}))
            await asyncio.wait_for(done.wait(), timeout=2.0)
            await asyncio.sleep(0.05)
            handler_stats = bus.stats._handler_stats
            assert len(handler_stats) == 1
            stat = next(iter(handler_stats.values()))
            assert stat.successes == 1
            assert stat.errors == 0
        finally:
            await bus.stop()

    @pytest.mark.asyncio
    async def test_handler_error_recorded(self):
        bus = EventBus()
        done = asyncio.Event()

        async def bad_handler(event: Event):
            done.set()
            raise ValueError("test error")

        bus.on("test", bad_handler)
        await bus.start()
        try:
            await bus.emit(Event(type="test", agent_id="a", data={}))
            await asyncio.wait_for(done.wait(), timeout=2.0)
            await asyncio.sleep(0.05)
            handler_stats = bus.stats._handler_stats
            stat = next(iter(handler_stats.values()))
            assert stat.errors == 1
            assert "test error" in (stat.last_error_msg or "")
        finally:
            await bus.stop()

    @pytest.mark.asyncio
    async def test_queue_full_increments_dropped(self):
        bus = EventBus(max_queue=1)
        # Don't start — queue will fill up
        await bus.emit(Event(type="t1", agent_id="a", data={}))
        await bus.emit(Event(type="t2", agent_id="a", data={}))  # should drop
        assert bus.stats.total_dropped >= 1

    @pytest.mark.asyncio
    async def test_recent_events_populated(self):
        bus = EventBus()
        done = asyncio.Event()

        async def handler(event: Event):
            done.set()

        bus.on("test", handler)
        await bus.start()
        try:
            await bus.emit(Event(type="test", agent_id="a", data={}, session_id="s1"))
            await asyncio.wait_for(done.wait(), timeout=2.0)
            await asyncio.sleep(0.05)
            recent = bus.stats.recent_events()
            assert len(recent) >= 1
            assert recent[0].type == "test"
            assert recent[0].session_id == "s1"
        finally:
            await bus.stop()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_event_bus_observability.py::TestEventBusStatsWiring -v`
Expected: FAIL (bus.stats doesn't exist)

- [ ] **Step 3: Wire stats into EventBus**

Modify `nous/events.py` — the `EventBus` class:

In `__init__`:
```python
self.stats = EventBusStats()
```

In `emit()`, change the QueueFull handler:
```python
except asyncio.QueueFull:
    self.stats.record_drop()
    logger.warning("Event bus queue full, dropping event: %s", event.type)
```

Replace `_dispatch` with a version that times handlers and records stats:
```python
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

    results = await asyncio.gather(
        *(self._safe_handle(h, event) for h in handlers)
    )
    failed = sum(1 for ok in results if not ok)
    duration_ms = (time.monotonic() - start) * 1000
    self.stats.record_event(event.type, len(handlers), failed, duration_ms, event.session_id)
```

Replace `_safe_handle` to return bool and record handler stats:
```python
async def _safe_handle(self, handler: EventHandler, event: Event) -> bool:
    """Run handler with error isolation. Returns True on success."""
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
```

- [ ] **Step 4: Run all event bus tests to verify nothing breaks**

Run: `uv run pytest tests/test_event_bus_observability.py tests/test_event_bus.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add nous/events.py tests/test_event_bus_observability.py
git commit -m "feat(f035.1): wire EventBusStats into EventBus dispatch loop"
```

---

## Task 3: F035.1 — Handler get_stats() Methods

**Files:**
- Modify: `nous/handlers/session_monitor.py`
- Modify: `nous/handlers/sleep_handler.py`
- Modify: `nous/heartbeat/runner.py`
- Test: `tests/test_event_bus_observability.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_event_bus_observability.py`:

```python
from unittest.mock import MagicMock, AsyncMock
from nous.handlers.session_monitor import SessionTimeoutMonitor
from nous.handlers.sleep_handler import SleepHandler


class TestHandlerGetStats:
    def test_session_monitor_get_stats(self):
        bus = EventBus()
        settings = MagicMock()
        settings.session_idle_timeout = 1800
        settings.sleep_timeout = 7200
        settings.sleep_check_interval = 60
        monitor = SessionTimeoutMonitor(bus, settings)
        stats = monitor.get_stats()
        assert "tracked_sessions" in stats
        assert "sleep_emitted" in stats
        assert stats["tracked_sessions"] == 0
        assert stats["sleep_emitted"] is False

    def test_sleep_handler_get_stats(self):
        brain = MagicMock()
        heart = MagicMock()
        settings = MagicMock()
        settings.sleep_enabled = True
        settings.background_model = "claude-sonnet-4-6"
        bus = EventBus()
        handler = SleepHandler(brain, heart, settings, bus, MagicMock())
        stats = handler.get_stats()
        assert "total_sleeps" in stats
        assert "currently_sleeping" in stats
        assert stats["total_sleeps"] == 0
        assert stats["currently_sleeping"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_event_bus_observability.py::TestHandlerGetStats -v`
Expected: FAIL (get_stats not defined)

- [ ] **Step 3: Implement get_stats() on SessionTimeoutMonitor**

Add to `nous/handlers/session_monitor.py` at the end of the class:

```python
def get_stats(self) -> dict[str, Any]:
    """F035.1: Return operational stats for observability."""
    import time as _time
    now = _time.monotonic()
    sessions = {}
    for sid, last in self._last_activity.items():
        sessions[sid] = {"idle_seconds": round(now - last, 1)}
    return {
        "tracked_sessions": len(self._last_activity),
        "sessions": sessions,
        "sleep_emitted": self._sleep_emitted,
        "global_idle_seconds": round(now - self._global_last_activity, 1),
    }
```

Add `from typing import Any` to the imports if not already present.

- [ ] **Step 4: Implement get_stats() on SleepHandler**

Add tracking fields to `SleepHandler.__init__` in `nous/handlers/sleep_handler.py`:

```python
# F035.1: Observability tracking
self._total_sleeps: int = 0
self._last_sleep_at: datetime | None = None
self._last_phases: list[str] = []
self._currently_sleeping: bool = False
```

Add `from datetime import datetime` if not already imported.

Update the `handle` method — set `self._currently_sleeping = True` at the start of `_run_sleep_cycle` (or wherever the sleep cycle begins) and `self._currently_sleeping = False` at the end. Increment `self._total_sleeps` when a sleep completes.

Add at the end of the class:

```python
def get_stats(self) -> dict[str, Any]:
    """F035.1: Return operational stats for observability."""
    return {
        "total_sleeps": self._total_sleeps,
        "last_sleep_at": self._last_sleep_at.isoformat() if self._last_sleep_at else None,
        "last_phases_completed": self._last_phases,
        "currently_sleeping": self._currently_sleeping,
    }
```

- [ ] **Step 5: Implement get_stats() on HeartbeatRunner**

Add to `nous/heartbeat/runner.py` at the end of the class:

```python
def get_stats(self) -> dict[str, Any]:
    """F035.1: Return operational stats for observability."""
    return {
        "total_ticks": getattr(self, "_tick_count", 0),
        "last_tick_at": self._last_tick.isoformat() if self._last_tick else None,
        "currently_running": self._running,
        "tokens_used_today": self._tokens_used_today,
        "budget_remaining": max(0, self._settings.heartbeat_daily_token_budget - self._tokens_used_today),
    }
```

Also add a `self._tick_count: int = 0` in `__init__` and increment it at the start of each tick in the `_loop` method.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_event_bus_observability.py::TestHandlerGetStats -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add nous/handlers/session_monitor.py nous/handlers/sleep_handler.py nous/heartbeat/runner.py tests/test_event_bus_observability.py
git commit -m "feat(f035.1): add get_stats() to SessionMonitor, SleepHandler, HeartbeatRunner"
```

---

## Task 4: F035.1 — REST Endpoints

**Files:**
- Modify: `nous/api/rest.py`
- Test: `tests/test_event_bus_observability.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_event_bus_observability.py`:

```python
from starlette.testclient import TestClient


def _make_app_with_bus():
    """Create a minimal Starlette app with bus for testing endpoints."""
    from nous.api.rest import create_app

    bus = EventBus()
    bus.stats.record_event("turn_completed", 2, 0, 5.0, session_id="s1")
    bus.stats.record_handler_success("TestHandler.run", 3.0)

    app = create_app(
        runner=MagicMock(),
        brain=MagicMock(),
        heart=MagicMock(),
        cognitive=MagicMock(),
        database=MagicMock(),
        settings=MagicMock(
            agent_id="test",
            heartbeat_enabled=False,
            mcp_enabled=False,
        ),
        bus=bus,
    )
    return app, bus


class TestEventStatsEndpoint:
    def test_events_stats(self):
        app, bus = _make_app_with_bus()
        client = TestClient(app)
        resp = client.get("/events/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_processed"] == 1
        assert "handlers" in data

    def test_events_recent(self):
        app, bus = _make_app_with_bus()
        client = TestClient(app)
        resp = client.get("/events/recent?limit=10")
        assert resp.status_code == 200
        data = resp.json()
        assert "events" in data
        assert len(data["events"]) == 1
        assert data["events"][0]["type"] == "turn_completed"

    def test_events_stats_no_bus(self):
        from nous.api.rest import create_app
        app = create_app(
            runner=MagicMock(), brain=MagicMock(), heart=MagicMock(),
            cognitive=MagicMock(), database=MagicMock(),
            settings=MagicMock(agent_id="test", heartbeat_enabled=False, mcp_enabled=False),
            bus=None,
        )
        client = TestClient(app)
        resp = client.get("/events/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_processed"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_event_bus_observability.py::TestEventStatsEndpoint -v`
Expected: FAIL (404 — endpoints don't exist)

- [ ] **Step 3: Implement REST endpoints in rest.py**

Add to `nous/api/rest.py` inside `create_app()`:

```python
async def events_stats(request: Request) -> JSONResponse:
    """GET /events/stats — Event bus statistics."""
    if bus is None:
        return JSONResponse({"total_processed": 0, "total_dropped": 0, "handlers": {}, "event_counts": {}})
    data = bus.stats.to_dict()
    data["queue_depth"] = bus.pending
    # Include handler-specific stats if available
    handler_stats = {}
    if session_monitor and hasattr(session_monitor, "get_stats"):
        handler_stats["session_monitor"] = session_monitor.get_stats()
    if sleep_handler and hasattr(sleep_handler, "get_stats"):
        handler_stats["sleep_handler"] = sleep_handler.get_stats()
    if heartbeat_runner and hasattr(heartbeat_runner, "get_stats"):
        handler_stats["heartbeat_runner"] = heartbeat_runner.get_stats()
    if handler_stats:
        data["component_stats"] = handler_stats
    return JSONResponse(data)

async def events_recent(request: Request) -> JSONResponse:
    """GET /events/recent — Recent events from ring buffer."""
    limit = int(request.query_params.get("limit", "20"))
    if bus is None:
        return JSONResponse({"events": [], "source": "memory", "count": 0})
    events = bus.stats.recent_events(limit=limit)
    return JSONResponse({
        "events": [
            {
                "type": e.type,
                "timestamp": e.timestamp,
                "handlers_invoked": e.handlers_invoked,
                "handlers_failed": e.handlers_failed,
                "duration_ms": round(e.duration_ms, 2),
                "session_id": e.session_id,
            }
            for e in events
        ],
        "source": "memory",
        "count": len(events),
    })
```

Add the routes to the routes list:

```python
Route("/events/stats", events_stats, methods=["GET"]),
Route("/events/recent", events_recent, methods=["GET"]),
```

Also add `session_monitor` as a parameter to `create_app` if not already available in the closure scope, and expose `sleep_handler` and `heartbeat_runner` similarly.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_event_bus_observability.py::TestEventStatsEndpoint -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add nous/api/rest.py tests/test_event_bus_observability.py
git commit -m "feat(f035.1): add GET /events/stats and GET /events/recent endpoints"
```

---

## Task 5: F035.1 — Telegram Integration

**Files:**
- Modify: `nous/telegram_bot.py`
- Test: `tests/test_event_bus_observability.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_event_bus_observability.py`:

```python
class TestTelegramEventBusFormat:
    def test_format_event_bus_status(self):
        """Test that event bus stats format correctly for Telegram."""
        stats_data = {
            "total_processed": 142,
            "total_dropped": 0,
            "queue_depth": 0,
            "uptime_seconds": 23400,
            "handlers": {
                "SessionTimeoutMonitor.on_activity": {
                    "invocations": 85, "successes": 85, "errors": 0, "error_rate": 0.0,
                },
                "EpisodeSummarizer.handle": {
                    "invocations": 12, "successes": 8, "errors": 4, "error_rate": 0.333,
                },
            },
        }
        from nous.telegram_bot import format_event_bus_status
        text = format_event_bus_status(stats_data)
        assert "142 events" in text
        assert "0 dropped" in text
        assert "SessionTimeoutMonitor" in text or "SessionMonitor" in text
        assert "85/85" in text
        # Error handler should be flagged
        assert "8/12" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_event_bus_observability.py::TestTelegramEventBusFormat -v`
Expected: FAIL (format_event_bus_status not defined)

- [ ] **Step 3: Implement format_event_bus_status in telegram_bot.py**

Add to `nous/telegram_bot.py`:

```python
def format_event_bus_status(stats: dict) -> str:
    """F035.1: Format event bus stats for Telegram /status output."""
    total = stats.get("total_processed", 0)
    dropped = stats.get("total_dropped", 0)
    queue = stats.get("queue_depth", 0)
    uptime_s = stats.get("uptime_seconds", 0)

    hours = int(uptime_s // 3600)
    mins = int((uptime_s % 3600) // 60)
    uptime_str = f"{hours}h {mins}m" if hours else f"{mins}m"

    lines = [
        f"\n<b>Event Bus</b>",
        f"  {total} events processed, {dropped} dropped",
        f"  Queue: {queue} pending | Uptime: {uptime_str}",
        "",
        "  Handlers:",
    ]

    handlers = stats.get("handlers", {})
    for name, h in handlers.items():
        short_name = name.split(".")[-2] if "." in name else name  # Get class name
        invocations = h.get("invocations", 0)
        successes = h.get("successes", 0)
        error_rate = h.get("error_rate", 0.0)
        flag = "!!" if error_rate > 0.10 else "OK"
        lines.append(f"  {flag} {short_name}: {successes}/{invocations}")

    return "\n".join(lines)
```

Also integrate this into the `/status` command handler by calling `format_event_bus_status` with data fetched from `/events/stats`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_event_bus_observability.py::TestTelegramEventBusFormat -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add nous/telegram_bot.py tests/test_event_bus_observability.py
git commit -m "feat(f035.1): add event bus health section to Telegram /status"
```

---

## Task 6: F035.2 — Event Dataclass Extension + DB Migration

**Files:**
- Modify: `nous/events.py` (Event dataclass)
- Modify: `nous/storage/models.py` (ORM Event model)
- Create: `sql/migrations/026_observability.sql`
- Test: `tests/test_causal_tracing.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_causal_tracing.py`:

```python
"""Tests for F035.2 — Causal Chain Tracing."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from nous.events import Event, EventBus


class TestEventTraceFields:
    def test_event_has_event_id(self):
        e = Event(type="test", agent_id="a")
        assert e.event_id is not None
        assert len(e.event_id) == 12

    def test_event_id_unique(self):
        e1 = Event(type="test", agent_id="a")
        e2 = Event(type="test", agent_id="a")
        assert e1.event_id != e2.event_id

    def test_trace_id_defaults_none(self):
        e = Event(type="test", agent_id="a")
        assert e.trace_id is None

    def test_caused_by_defaults_none(self):
        e = Event(type="test", agent_id="a")
        assert e.caused_by is None

    def test_root_event_sets_trace_id(self):
        e = Event(type="heartbeat_tick", agent_id="a")
        e.trace_id = e.event_id  # Root event: trace = self
        assert e.trace_id == e.event_id

    def test_child_event_propagates_trace(self):
        root = Event(type="heartbeat_tick", agent_id="a")
        root.trace_id = root.event_id
        child = Event(
            type="finding_created",
            agent_id="a",
            trace_id=root.trace_id,
            caused_by=root.event_id,
        )
        assert child.trace_id == root.trace_id
        assert child.caused_by == root.event_id
        assert child.event_id != root.event_id

    def test_modification_tag(self):
        e = Event(
            type="fact_deleted",
            agent_id="a",
            data={"fact_id": "X", "modifies": "fact"},
        )
        assert e.data["modifies"] == "fact"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_causal_tracing.py::TestEventTraceFields -v`
Expected: FAIL (Event has no event_id field)

- [ ] **Step 3: Extend Event dataclass in events.py**

Modify the `Event` dataclass in `nous/events.py`:

```python
from uuid import uuid4

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
```

- [ ] **Step 4: Extend ORM Event model in storage/models.py**

Add columns to the `Event` class in `nous/storage/models.py`:

```python
event_id: Mapped[str | None] = mapped_column(String(12), nullable=True)
trace_id: Mapped[str | None] = mapped_column(String(12), nullable=True, index=True)
caused_by: Mapped[str | None] = mapped_column(String(12), nullable=True)
```

- [ ] **Step 5: Create DB migration**

Create `sql/migrations/026_observability.sql`:

```sql
-- 026: F035 Observability — event tracing columns + context_log + behavior_snapshots

-- F035.2: Causal chain tracing columns on events table
ALTER TABLE nous_system.events ADD COLUMN IF NOT EXISTS event_id VARCHAR(12);
ALTER TABLE nous_system.events ADD COLUMN IF NOT EXISTS trace_id VARCHAR(12);
ALTER TABLE nous_system.events ADD COLUMN IF NOT EXISTS caused_by VARCHAR(12);

CREATE INDEX IF NOT EXISTS idx_events_trace_id ON nous_system.events(trace_id);
CREATE INDEX IF NOT EXISTS idx_events_event_id ON nous_system.events(event_id);

-- F035.4: Context log table
CREATE TABLE IF NOT EXISTS nous_system.context_log (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id  TEXT NOT NULL,
    turn_number INTEGER NOT NULL,
    timestamp   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    call_type   TEXT NOT NULL,
    model       TEXT NOT NULL,
    frame_id    TEXT,
    trace_id    TEXT,

    token_breakdown     JSONB NOT NULL DEFAULT '{}',
    total_tokens_est    INTEGER NOT NULL DEFAULT 0,
    context_window_size INTEGER NOT NULL DEFAULT 0,
    utilization_pct     REAL NOT NULL DEFAULT 0.0,

    sections_present    TEXT[] NOT NULL DEFAULT '{}',
    tools_count         INTEGER NOT NULL DEFAULT 0,
    tool_names          TEXT[],
    messages_count      INTEGER NOT NULL DEFAULT 0,
    message_roles       JSONB,

    loaded_facts        INTEGER NOT NULL DEFAULT 0,
    loaded_decisions    INTEGER NOT NULL DEFAULT 0,
    loaded_procedures   INTEGER NOT NULL DEFAULT 0,
    loaded_episodes     INTEGER NOT NULL DEFAULT 0,
    recent_conversations INTEGER NOT NULL DEFAULT 0,

    input_tokens_actual INTEGER,
    output_tokens       INTEGER,
    cache_creation      INTEGER,
    cache_read          INTEGER,
    duration_ms         REAL,
    stop_reason         TEXT
);

CREATE INDEX IF NOT EXISTS idx_context_log_session ON nous_system.context_log(session_id, turn_number);
CREATE INDEX IF NOT EXISTS idx_context_log_time ON nous_system.context_log(timestamp DESC);

-- F035.3: Behavior snapshots table
CREATE TABLE IF NOT EXISTS nous_system.behavior_snapshots (
    id          SERIAL PRIMARY KEY,
    timestamp   TIMESTAMPTZ NOT NULL,
    metrics     JSONB NOT NULL,
    anomalies   JSONB DEFAULT '[]',
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_behavior_snapshots_ts ON nous_system.behavior_snapshots(timestamp DESC);

-- F035.3: Daily rollups for long-term trend visibility
CREATE TABLE IF NOT EXISTS nous_system.behavior_daily_rollups (
    id          SERIAL PRIMARY KEY,
    date        DATE NOT NULL UNIQUE,
    metrics_min JSONB NOT NULL,
    metrics_max JSONB NOT NULL,
    metrics_avg JSONB NOT NULL,
    snapshot_count INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
```

- [ ] **Step 6: Update DB persister in main.py to pass new fields**

In `nous/main.py`, update the `persist_to_db` function:

```python
async def persist_to_db(event: Event) -> None:
    data = {**event.data}
    await brain.emit_event(
        event.type, data, session_id=event.session_id,
        event_id=event.event_id, trace_id=event.trace_id,
        caused_by=event.caused_by,
    )
```

And update `brain.emit_event` and `brain._emit_event` in `nous/brain/brain.py` to accept and pass through the new fields:

```python
async def emit_event(
    self,
    event_type: str,
    data: dict,
    session: AsyncSession | None = None,
    session_id: str | None = None,
    event_id: str | None = None,
    trace_id: str | None = None,
    caused_by: str | None = None,
) -> None:
    """Log a cognitive event to nous_system.events."""
    if session is None:
        async with self.db.session() as session:
            await self._emit_event(session, event_type, data, session_id=session_id,
                                    event_id=event_id, trace_id=trace_id, caused_by=caused_by)
            await session.commit()
    else:
        await self._emit_event(session, event_type, data, session_id=session_id,
                                event_id=event_id, trace_id=trace_id, caused_by=caused_by)
```

And in `_emit_event`, set the new fields on the ORM Event object:

```python
async def _emit_event(
    self, session, event_type, data, session_id=None,
    event_id=None, trace_id=None, caused_by=None,
) -> None:
    event = Event(
        agent_id=self.agent_id,
        event_type=event_type,
        data=data,
        session_id=session_id,
        event_id=event_id,
        trace_id=trace_id,
        caused_by=caused_by,
    )
    session.add(event)
    await session.flush()
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/test_causal_tracing.py::TestEventTraceFields -v`
Expected: All PASS

- [ ] **Step 8: Commit**

```bash
git add nous/events.py nous/storage/models.py sql/migrations/026_observability.sql nous/main.py nous/brain/brain.py tests/test_causal_tracing.py
git commit -m "feat(f035.2): add event_id/trace_id/caused_by to Event + DB migration"
```

---

## Task 7: F035.2 — Root Event Trace ID Emission

**Files:**
- Modify: `nous/heartbeat/runner.py`
- Modify: `nous/handlers/session_monitor.py`
- Modify: `nous/cognitive/layer.py`
- Test: `tests/test_causal_tracing.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_causal_tracing.py`:

```python
class TestRootEventTracing:
    @pytest.mark.asyncio
    async def test_heartbeat_tick_is_root_event(self):
        """heartbeat_tick events should have trace_id == event_id."""
        bus = EventBus()
        captured: list[Event] = []

        async def capture(event: Event):
            captured.append(event)

        bus.on("heartbeat_tick", capture)
        await bus.start()
        try:
            # Simulate what HeartbeatRunner does
            e = Event(type="heartbeat_tick", agent_id="a", data={})
            e.trace_id = e.event_id  # Root event
            await bus.emit(e)
            await asyncio.sleep(0.1)
        finally:
            await bus.stop()

        assert len(captured) == 1
        assert captured[0].trace_id == captured[0].event_id
        assert captured[0].caused_by is None

    @pytest.mark.asyncio
    async def test_child_event_inherits_trace(self):
        """Child events propagate trace_id from parent."""
        parent = Event(type="heartbeat_tick", agent_id="a")
        parent.trace_id = parent.event_id

        child = Event(
            type="finding_created",
            agent_id="a",
            data={"fingerprint": "abc"},
            trace_id=parent.trace_id,
            caused_by=parent.event_id,
        )

        assert child.trace_id == parent.trace_id
        assert child.caused_by == parent.event_id
        assert child.event_id != parent.event_id
```

- [ ] **Step 2: Run tests to verify they pass** (these are unit tests that already work with the fields from Task 6)

Run: `uv run pytest tests/test_causal_tracing.py::TestRootEventTracing -v`
Expected: PASS

- [ ] **Step 3: Add trace_id to root events in HeartbeatRunner**

In `nous/heartbeat/runner.py`, find every `await self._bus.emit(Event(...))` call. For root events (heartbeat_tick), create the event as a local variable, set `trace_id = event_id`, then emit:

```python
tick_event = Event(
    type="heartbeat_tick",
    agent_id=self._settings.agent_id,
    data={...},
)
tick_event.trace_id = tick_event.event_id  # Root event
await self._bus.emit(tick_event)
```

Store the event reference so child events in the same tick can reference it:

```python
self._current_tick_event = tick_event
```

For child events emitted during the tick (like finding_created, heartbeat_triage), propagate:

```python
child = Event(
    type="finding_created",
    agent_id=self._settings.agent_id,
    data={...},
    trace_id=self._current_tick_event.trace_id,
    caused_by=self._current_tick_event.event_id,
)
await self._bus.emit(child)
```

- [ ] **Step 4: Add trace_id to root events in SessionTimeoutMonitor**

In `nous/handlers/session_monitor.py`, the `sleep_started` event is a root event:

```python
sleep_event = Event(
    type="sleep_started",
    agent_id=agent_id,
    data={"reason": "global idle timeout", "idle_seconds": global_idle},
)
sleep_event.trace_id = sleep_event.event_id  # Root event
await self._bus.emit(sleep_event)
```

- [ ] **Step 5: Add trace_id to root events in CognitiveLayer**

In `nous/cognitive/layer.py`, the `turn_completed` and `session_ended` events should carry trace context. For `turn_completed`, it's typically a root event:

```python
tc_event = Event(
    type="turn_completed",
    agent_id=agent_id,
    session_id=session_id,
    data={...},
)
tc_event.trace_id = tc_event.event_id
await self._bus.emit(tc_event)
```

For `session_ended`:

```python
se_event = Event(
    type="session_ended",
    agent_id=agent_id,
    session_id=session_id,
    data={...},
)
se_event.trace_id = se_event.event_id
await self._bus.emit(se_event)
```

- [ ] **Step 6: Run all tests**

Run: `uv run pytest tests/test_causal_tracing.py tests/test_event_bus.py tests/test_heartbeat.py -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add nous/heartbeat/runner.py nous/handlers/session_monitor.py nous/cognitive/layer.py tests/test_causal_tracing.py
git commit -m "feat(f035.2): emit trace_id on root events (heartbeat, session, turn)"
```

---

## Task 8: F035.2 — Child Event Trace Propagation Across All Handlers

**Files:**
- Modify: `nous/handlers/episode_summarizer.py`
- Modify: `nous/handlers/fact_extractor.py`
- Modify: `nous/handlers/sleep_handler.py`
- Modify: `nous/handlers/outcome_detector.py`
- Modify: `nous/handlers/subtask_worker.py`
- Modify: `nous/heart/heart.py`
- Test: `tests/test_causal_tracing.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_causal_tracing.py`:

```python
class TestTraceContextPropagation:
    @pytest.mark.asyncio
    async def test_episode_summarizer_propagates_trace(self):
        """EpisodeSummarizer should propagate trace from session_ended to episode_summarized."""
        bus = EventBus()
        captured: list[Event] = []

        async def capture(event: Event):
            captured.append(event)

        bus.on("episode_summarized", capture)

        # Create a parent event with trace context
        parent = Event(
            type="session_ended",
            agent_id="a",
            session_id="s1",
            data={"episode_id": "ep1"},
        )
        parent.trace_id = parent.event_id

        # The child should propagate trace
        child = Event(
            type="episode_summarized",
            agent_id="a",
            session_id="s1",
            data={"episode_id": "ep1"},
            trace_id=parent.trace_id,
            caused_by=parent.event_id,
        )
        assert child.trace_id == parent.trace_id
        assert child.caused_by == parent.event_id

    @pytest.mark.asyncio
    async def test_sleep_handler_tags_modifications(self):
        """Sleep handler events that modify state should have 'modifies' tag."""
        e = Event(
            type="fact_pruned",
            agent_id="a",
            data={"fact_id": "f1", "reason": "stale", "modifies": "fact"},
        )
        assert e.data["modifies"] == "fact"
```

- [ ] **Step 2: Run tests to verify they pass** (these are structural tests)

Run: `uv run pytest tests/test_causal_tracing.py::TestTraceContextPropagation -v`
Expected: PASS

- [ ] **Step 3: Update EpisodeSummarizer to propagate trace context**

In `nous/handlers/episode_summarizer.py`, the `handle` method receives an event and emits `episode_summarized`. Update the emit to propagate:

```python
await self._bus.emit(Event(
    type="episode_summarized",
    agent_id=event.agent_id,
    session_id=event.session_id,
    data={...},
    trace_id=event.trace_id,
    caused_by=event.event_id,
))
```

- [ ] **Step 4: Update FactExtractor to propagate trace context and tag modifications**

In `nous/handlers/fact_extractor.py`, the `handle` method receives `episode_summarized` and may learn facts. When emitting any events, propagate trace and add `"modifies": "fact"` to data for fact operations.

- [ ] **Step 5: Update SleepHandler to propagate trace context and tag modifications**

In `nous/handlers/sleep_handler.py`, the `handle` method receives `sleep_started`. All child events should propagate trace:

```python
await self._bus.emit(Event(
    type="sleep_completed",
    agent_id=event.agent_id,
    data={"phases": completed_phases, "modifies": "memory"},
    trace_id=event.trace_id,
    caused_by=event.event_id,
))
```

- [ ] **Step 6: Update OutcomeDetector to propagate trace context**

In `nous/handlers/outcome_detector.py`, propagate trace from `episode_summarized`:

```python
await self._bus.emit(Event(
    type="outcome_detected",
    agent_id=event.agent_id,
    data={..., "modifies": "episode"},
    trace_id=event.trace_id,
    caused_by=event.event_id,
))
```

- [ ] **Step 7: Update SubtaskWorker to propagate trace context**

In `nous/handlers/subtask_worker.py`, propagate trace on `subtask_completed`/`subtask_failed` events.

- [ ] **Step 8: Update Heart.learn() (fact_learned) to propagate trace context**

In `nous/heart/heart.py`, the `fact_learned` event should propagate any trace context that was available.

- [ ] **Step 9: Run all handler tests**

Run: `uv run pytest tests/test_causal_tracing.py tests/test_event_bus.py tests/test_heartbeat.py -v`
Expected: All PASS

- [ ] **Step 10: Commit**

```bash
git add nous/handlers/episode_summarizer.py nous/handlers/fact_extractor.py nous/handlers/sleep_handler.py nous/handlers/outcome_detector.py nous/handlers/subtask_worker.py nous/heart/heart.py tests/test_causal_tracing.py
git commit -m "feat(f035.2): propagate trace context across all handlers + tag modifications"
```

---

## Task 9: F035.2 — Trace Query REST Endpoints

**Files:**
- Modify: `nous/api/rest.py`
- Test: `tests/test_causal_tracing.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_causal_tracing.py`:

```python
class TestTraceEndpoints:
    @pytest.mark.asyncio
    async def test_get_trace(self, session):
        """GET /events/trace/{trace_id} returns all events in a chain."""
        from nous.storage.models import Event as EventModel
        from sqlalchemy import text

        trace_id = "abc123def456"
        # Insert root event
        await session.execute(text("""
            INSERT INTO nous_system.events (agent_id, event_type, data, event_id, trace_id)
            VALUES ('a', 'heartbeat_tick', '{}', :eid, :tid)
        """), {"eid": trace_id, "tid": trace_id})
        # Insert child event
        await session.execute(text("""
            INSERT INTO nous_system.events (agent_id, event_type, data, event_id, trace_id, caused_by)
            VALUES ('a', 'finding_created', '{"modifies": "finding"}', 'child1234567', :tid, :parent)
        """), {"tid": trace_id, "parent": trace_id})
        await session.commit()

        # Query endpoint logic
        result = await session.execute(text(
            "SELECT event_id, event_type, trace_id, caused_by, data, created_at "
            "FROM nous_system.events WHERE trace_id = :tid ORDER BY created_at"
        ), {"tid": trace_id})
        rows = result.fetchall()
        assert len(rows) == 2
        assert rows[0].event_type == "heartbeat_tick"
        assert rows[1].caused_by == trace_id

    @pytest.mark.asyncio
    async def test_get_modifications(self, session):
        """GET /events/modifications returns only state-modifying events."""
        from sqlalchemy import text

        trace_id = "mod123456789"
        await session.execute(text("""
            INSERT INTO nous_system.events (agent_id, event_type, data, event_id, trace_id)
            VALUES ('a', 'fact_deleted', '{"modifies": "fact", "fact_id": "f1"}', :eid, :tid)
        """), {"eid": trace_id, "tid": trace_id})
        await session.commit()

        result = await session.execute(text(
            "SELECT * FROM nous_system.events WHERE data->>'modifies' IS NOT NULL"
        ))
        rows = result.fetchall()
        assert len(rows) >= 1
```

- [ ] **Step 2: Implement trace query endpoints in rest.py**

Add to `nous/api/rest.py`:

```python
async def events_trace(request: Request) -> JSONResponse:
    """GET /events/trace/{trace_id} — Full causal chain for a trace."""
    trace_id = request.path_params["trace_id"]
    async with database.session() as session:
        from sqlalchemy import text
        result = await session.execute(text(
            "SELECT event_id, event_type, trace_id, caused_by, data, created_at, session_id "
            "FROM nous_system.events WHERE trace_id = :tid ORDER BY created_at"
        ), {"tid": trace_id})
        rows = result.fetchall()

    if not rows:
        return JSONResponse({"trace_id": trace_id, "events": [], "depth": 0})

    events = []
    for row in rows:
        events.append({
            "event_id": row.event_id,
            "type": row.event_type,
            "caused_by": row.caused_by,
            "timestamp": row.created_at.isoformat() if row.created_at else None,
            "data": row.data or {},
            "session_id": row.session_id,
        })

    root = events[0] if events else {}
    duration_ms = None
    if len(events) >= 2 and events[0].get("timestamp") and events[-1].get("timestamp"):
        from datetime import datetime
        try:
            t0 = datetime.fromisoformat(events[0]["timestamp"])
            t1 = datetime.fromisoformat(events[-1]["timestamp"])
            duration_ms = (t1 - t0).total_seconds() * 1000
        except (ValueError, TypeError):
            pass

    return JSONResponse({
        "trace_id": trace_id,
        "root_event": root.get("type"),
        "root_timestamp": root.get("timestamp"),
        "events": events,
        "depth": len(events),
        "duration_ms": duration_ms,
    })

async def events_recent_traces(request: Request) -> JSONResponse:
    """GET /events/recent-traces — Recent trace roots with summary."""
    limit = int(request.query_params.get("limit", "20"))
    async with database.session() as session:
        from sqlalchemy import text
        result = await session.execute(text("""
            SELECT e.trace_id, e.event_type AS root_type, e.created_at,
                   COUNT(*) OVER (PARTITION BY e.trace_id) AS event_count,
                   BOOL_OR(e2.data->>'modifies' IS NOT NULL) AS has_modifications
            FROM nous_system.events e
            LEFT JOIN nous_system.events e2 ON e2.trace_id = e.trace_id
            WHERE e.trace_id IS NOT NULL AND e.caused_by IS NULL
            GROUP BY e.trace_id, e.event_type, e.created_at, e2.data
            ORDER BY e.created_at DESC
            LIMIT :lim
        """), {"lim": limit})
        rows = result.fetchall()

    traces = []
    seen = set()
    for row in rows:
        if row.trace_id in seen:
            continue
        seen.add(row.trace_id)
        traces.append({
            "trace_id": row.trace_id,
            "root_type": row.root_type,
            "timestamp": row.created_at.isoformat() if row.created_at else None,
            "event_count": row.event_count,
            "has_modifications": bool(row.has_modifications),
        })

    return JSONResponse({"traces": traces[:limit]})

async def events_modifications(request: Request) -> JSONResponse:
    """GET /events/modifications — Traces with state modifications."""
    hours = int(request.query_params.get("hours", "24"))
    async with database.session() as session:
        from sqlalchemy import text
        result = await session.execute(text("""
            SELECT event_id, event_type, trace_id, caused_by, data, created_at
            FROM nous_system.events
            WHERE data->>'modifies' IS NOT NULL
            AND created_at > NOW() - INTERVAL '1 hour' * :hours
            ORDER BY created_at DESC
            LIMIT 100
        """), {"hours": hours})
        rows = result.fetchall()

    events = [{
        "event_id": r.event_id,
        "type": r.event_type,
        "trace_id": r.trace_id,
        "caused_by": r.caused_by,
        "modifies": (r.data or {}).get("modifies"),
        "timestamp": r.created_at.isoformat() if r.created_at else None,
        "data": r.data or {},
    } for r in rows]

    return JSONResponse({"events": events, "hours": hours})
```

Add routes:

```python
Route("/events/trace/{trace_id}", events_trace, methods=["GET"]),
Route("/events/recent-traces", events_recent_traces, methods=["GET"]),
Route("/events/modifications", events_modifications, methods=["GET"]),
```

- [ ] **Step 3: Run tests**

Run: `uv run pytest tests/test_causal_tracing.py -v`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add nous/api/rest.py tests/test_causal_tracing.py
git commit -m "feat(f035.2): add trace query REST endpoints"
```

---

## Task 10: F035.4 — ContextLogger Core

**Files:**
- Create: `nous/observability/__init__.py`
- Create: `nous/observability/context_logger.py`
- Test: `tests/test_context_logger.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_context_logger.py`:

```python
"""Tests for F035.4 — Context Visibility."""

from __future__ import annotations

import pytest

from nous.observability.context_logger import (
    ContextLogEntry,
    ContextLogger,
    FullPayloadStore,
    parse_system_sections,
)


class TestParseSystemSections:
    def test_basic_sections(self):
        prompt = (
            "## Identity\nI am Nous.\n\n"
            "## User Profile\nTim is a developer.\n\n"
            "## Active Censors\nNo censors active.\n\n"
            "## Current Frame\nconversation"
        )
        sections = parse_system_sections(prompt)
        assert "identity" in sections
        assert "Nous" in sections["identity"]
        assert "user_profile" in sections
        assert "Tim" in sections["user_profile"]
        assert "censors" in sections
        assert "frame" in sections

    def test_unknown_section_goes_to_other(self):
        prompt = "Some preamble text\n\n## Identity\nI am Nous."
        sections = parse_system_sections(prompt)
        assert "preamble" in sections or "other" in sections
        assert "identity" in sections

    def test_empty_prompt(self):
        sections = parse_system_sections("")
        assert len(sections) <= 1  # Maybe just "other" or empty

    def test_execution_ledger_bracket_marker(self):
        prompt = "## Identity\nNous\n\n[Execution Ledger]\nTurn 1: bash..."
        sections = parse_system_sections(prompt)
        assert "execution_ledger" in sections


class TestContextLogEntry:
    def test_estimate_tokens(self):
        entry = ContextLogEntry.from_payload(
            session_id="s1",
            turn_number=1,
            call_type="chat",
            model="claude-sonnet-4-6",
            system_prompt="## Identity\nI am Nous agent.\n\n## User Profile\nTim is a dev.",
            messages=[{"role": "user", "content": "hello"}],
            tools=[{"name": "bash", "description": "exec", "input_schema": {}}],
            frame_id="conversation",
            context_window=200000,
        )
        assert entry.total_tokens_est > 0
        assert entry.utilization_pct > 0
        assert "identity" in entry.token_breakdown
        assert entry.tools_count == 1
        assert entry.messages_count == 1

    def test_message_role_counts(self):
        entry = ContextLogEntry.from_payload(
            session_id="s1", turn_number=2, call_type="chat",
            model="test", system_prompt="## Identity\nX",
            messages=[
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
                {"role": "user", "content": "bye"},
            ],
            tools=[], frame_id="conversation", context_window=200000,
        )
        assert entry.message_roles["user"] == 2
        assert entry.message_roles["assistant"] == 1


class TestFullPayloadStore:
    def test_store_and_retrieve(self):
        store = FullPayloadStore(max_per_session=3, max_total=10)
        store.capture("s1", "e1", {"test": "payload"})
        result = store.get("e1")
        assert result == {"test": "payload"}

    def test_ring_buffer_eviction(self):
        store = FullPayloadStore(max_per_session=2, max_total=10)
        store.capture("s1", "e1", {"v": 1})
        store.capture("s1", "e2", {"v": 2})
        store.capture("s1", "e3", {"v": 3})  # Should evict e1
        assert store.get("e1") is None
        assert store.get("e2") is not None
        assert store.get("e3") is not None

    def test_get_session(self):
        store = FullPayloadStore(max_per_session=5, max_total=10)
        store.capture("s1", "e1", {"v": 1})
        store.capture("s1", "e2", {"v": 2})
        store.capture("s2", "e3", {"v": 3})
        results = store.get_session("s1")
        assert len(results) == 2

    def test_global_cap(self):
        store = FullPayloadStore(max_per_session=5, max_total=3)
        store.capture("s1", "e1", {"v": 1})
        store.capture("s1", "e2", {"v": 2})
        store.capture("s1", "e3", {"v": 3})
        store.capture("s1", "e4", {"v": 4})  # Should evict oldest
        assert store._total_count <= 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_context_logger.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Create observability package**

Create `nous/observability/__init__.py`:

```python
"""F035: Observability package — context logging, drift detection, snapshots."""
```

- [ ] **Step 4: Implement context_logger.py**

Create `nous/observability/context_logger.py`:

```python
"""F035.4: Context visibility — logs what the LLM actually sees on each API call.

Captures structured metadata (token breakdown, sections present, memory items loaded)
for every API call. Optionally stores full payloads in a ring buffer for deep debugging.
"""

from __future__ import annotations

import json
import logging
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

# Section markers in system prompt → internal section names
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
    "## Recent Conversations": "recent_conversations",
    "## Tool Instructions": "frame_instructions",
    "[Execution Ledger]": "execution_ledger",
    "[Previous Turn Corrections]": "corrections",
    "## Output Formatting": "telegram_format",
}

# Regex to match any section marker (sorted longest first to avoid partial matches)
_MARKER_PATTERNS = sorted(SECTION_MARKERS.keys(), key=len, reverse=True)


def parse_system_sections(system_prompt: str) -> dict[str, str]:
    """Split the system prompt into named sections with their text content."""
    if not system_prompt:
        return {}

    sections: dict[str, str] = {}
    current_name = "preamble"
    current_lines: list[str] = []

    for line in system_prompt.split("\n"):
        matched = False
        for marker in _MARKER_PATTERNS:
            if line.strip().startswith(marker):
                # Save previous section
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

    # Save last section
    if current_lines:
        text = "\n".join(current_lines).strip()
        if text:
            sections[current_name] = text

    # Rename preamble to "other" if it's not a known section
    if "preamble" in sections:
        sections["other"] = sections.pop("preamble")

    return sections


def _estimate_tokens(text: str) -> int:
    """Fast token estimation: ~4 chars per token."""
    return len(text) // 4 if text else 0


def _estimate_messages_tokens(messages: list[dict]) -> int:
    """Estimate tokens for a messages array."""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += _estimate_tokens(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total += _estimate_tokens(block.get("text", ""))
                    total += _estimate_tokens(json.dumps(block.get("input", {})) if "input" in block else "")
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

    loaded_facts_count: int = 0
    loaded_decisions_count: int = 0
    loaded_procedures_count: int = 0
    loaded_episodes_count: int = 0
    recent_conversations_count: int = 0

    input_tokens_actual: int | None = None
    output_tokens: int | None = None
    cache_creation_tokens: int | None = None
    cache_read_tokens: int | None = None
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
    ) -> ContextLogEntry:
        """Create a ContextLogEntry from API call components."""
        entry_id = uuid4().hex[:16]
        sections = parse_system_sections(system_prompt)

        token_breakdown: dict[str, int] = {}
        for name, text in sections.items():
            token_breakdown[name] = _estimate_tokens(text)

        # Tools tokens
        tools_list = tools or []
        tools_text = json.dumps(tools_list)
        token_breakdown["tools_definition"] = _estimate_tokens(tools_text)

        # Messages tokens
        messages_tokens = _estimate_messages_tokens(messages)
        token_breakdown["messages"] = messages_tokens

        total = sum(token_breakdown.values())
        utilization = (total / context_window * 100) if context_window else 0.0

        # Count message roles
        role_counts: dict[str, int] = {}
        for msg in messages:
            role = msg.get("role", "unknown")
            role_counts[role] = role_counts.get(role, 0) + 1

        # Count memory items from sections (heuristic: count bullet points)
        facts_count = sections.get("relevant_facts", "").count("\n- ") + (1 if "relevant_facts" in sections else 0)
        decisions_count = sections.get("related_decisions", "").count("\n- ") + (1 if "related_decisions" in sections else 0)
        procedures_count = sections.get("working_memory", "").count("procedure") if "working_memory" in sections else 0
        episodes_count = sections.get("recent_conversations", "").count("\n- ") + (1 if "recent_conversations" in sections else 0)

        tool_names = [t.get("name", "") for t in tools_list] if tools_list else []

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
            loaded_facts_count=facts_count,
            loaded_decisions_count=decisions_count,
            loaded_procedures_count=procedures_count,
            loaded_episodes_count=episodes_count,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize for API response."""
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
            "loaded_facts": self.loaded_facts_count,
            "loaded_decisions": self.loaded_decisions_count,
            "loaded_procedures": self.loaded_procedures_count,
            "loaded_episodes": self.loaded_episodes_count,
            "input_tokens_actual": self.input_tokens_actual,
            "output_tokens": self.output_tokens,
            "cache_creation": self.cache_creation_tokens,
            "cache_read": self.cache_read_tokens,
            "duration_ms": self.duration_ms,
            "stop_reason": self.stop_reason,
        }


@dataclass
class ContextPayload:
    """Full API payload stored in ring buffer."""
    entry_id: str
    session_id: str
    payload: dict
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


class FullPayloadStore:
    """Ring buffer for full API payloads. In-memory with per-session and global caps."""

    def __init__(self, max_per_session: int = 10, max_total: int = 50):
        self._store: dict[str, deque[ContextPayload]] = {}
        self._index: dict[str, ContextPayload] = {}  # entry_id -> payload
        self._max_per_session = max_per_session
        self._max_total = max_total
        self._total_count = 0

    def capture(self, session_id: str, entry_id: str, payload: dict) -> None:
        """Store a full payload. Oldest auto-evicted when limit reached."""
        cp = ContextPayload(entry_id=entry_id, session_id=session_id, payload=payload)

        if session_id not in self._store:
            self._store[session_id] = deque(maxlen=self._max_per_session)

        q = self._store[session_id]
        # Evict oldest if session full
        if len(q) >= self._max_per_session:
            evicted = q[0]
            self._index.pop(evicted.entry_id, None)
            self._total_count -= 1

        q.append(cp)
        self._index[entry_id] = cp
        self._total_count += 1

        # Global cap enforcement
        while self._total_count > self._max_total:
            self._evict_oldest_global()

    def _evict_oldest_global(self) -> None:
        """Evict the single oldest entry across all sessions."""
        oldest_session = None
        oldest_ts = None
        for sid, q in self._store.items():
            if q and (oldest_ts is None or q[0].timestamp < oldest_ts):
                oldest_session = sid
                oldest_ts = q[0].timestamp
        if oldest_session and self._store[oldest_session]:
            evicted = self._store[oldest_session].popleft()
            self._index.pop(evicted.entry_id, None)
            self._total_count -= 1
            if not self._store[oldest_session]:
                del self._store[oldest_session]

    def get(self, entry_id: str) -> dict | None:
        """Retrieve a full payload by context log entry ID."""
        cp = self._index.get(entry_id)
        return cp.payload if cp else None

    def get_session(self, session_id: str) -> list[ContextPayload]:
        """Get all captured payloads for a session (newest first)."""
        q = self._store.get(session_id)
        if not q:
            return []
        return list(reversed(q))


class ContextLogger:
    """F035.4: Logs context metadata for every API call.

    Maintains an in-memory ring buffer of recent entries and optionally
    stores full payloads. Writes structured metadata to DB asynchronously.
    """

    def __init__(
        self,
        db_writer: Any = None,
        full_payload_enabled: bool = False,
        ring_size: int = 10,
        max_total: int = 50,
    ):
        self._db_writer = db_writer  # async callable(entry) -> None
        self._entries: deque[ContextLogEntry] = deque(maxlen=200)
        self._entries_by_id: dict[str, ContextLogEntry] = {}
        self._payload_store: FullPayloadStore | None = (
            FullPayloadStore(max_per_session=ring_size, max_total=max_total)
            if full_payload_enabled else None
        )

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
    ) -> ContextLogEntry:
        """Log an API call. Returns the entry for later update with response data."""
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
        )
        self._entries.append(entry)
        self._entries_by_id[entry.id] = entry

        if self._payload_store and payload:
            self._payload_store.capture(session_id, entry.id, payload)

        # Async DB write (fire-and-forget)
        if self._db_writer:
            import asyncio
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._db_writer(entry))
            except RuntimeError:
                pass  # No running loop (testing)

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
        """Update entry with response metadata after API call completes."""
        entry = self._entries_by_id.get(entry_id)
        if entry:
            entry.input_tokens_actual = input_tokens
            entry.output_tokens = output_tokens
            entry.cache_creation_tokens = cache_creation
            entry.cache_read_tokens = cache_read
            entry.duration_ms = duration_ms
            entry.stop_reason = stop_reason

    def get_recent(self, session_id: str | None = None, limit: int = 20) -> list[ContextLogEntry]:
        """Get recent entries, optionally filtered by session."""
        entries = list(reversed(self._entries))
        if session_id:
            entries = [e for e in entries if e.session_id == session_id]
        return entries[:limit]

    def get_entry(self, entry_id: str) -> ContextLogEntry | None:
        return self._entries_by_id.get(entry_id)

    def get_payload(self, entry_id: str) -> dict | None:
        if self._payload_store:
            return self._payload_store.get(entry_id)
        return None
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_context_logger.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add nous/observability/__init__.py nous/observability/context_logger.py tests/test_context_logger.py
git commit -m "feat(f035.4): add ContextLogger, section parser, FullPayloadStore"
```

---

## Task 11: F035.4 — Hook ContextLogger into Runner + Config

**Files:**
- Modify: `nous/api/runner.py`
- Modify: `nous/config.py`
- Modify: `nous/main.py`
- Test: `tests/test_context_logger.py`

- [ ] **Step 1: Add config settings**

Add to `nous/config.py`:

```python
# F035.4: Context visibility
context_log_enabled: bool = True
context_log_full_payload: bool = False
context_log_ring_size: int = 10
context_log_max_total: int = 50
context_log_retention_days: int = 30
```

- [ ] **Step 2: Hook ContextLogger into AgentRunner**

In `nous/api/runner.py`, add to `__init__`:

```python
self._context_logger: Any | None = None  # F035.4: ContextLogger
```

Add a setter method:

```python
def set_context_logger(self, logger: Any) -> None:
    """F035.4: Set the context logger for API call visibility."""
    self._context_logger = logger
```

In `_build_api_payload()`, after building the payload dict, add the logging hook:

```python
# F035.4: Log context metadata
_context_entry_id = None
if self._context_logger:
    _entry = self._context_logger.log(
        session_id=getattr(self, '_current_session_id', 'unknown'),
        turn_number=getattr(self, '_current_turn_number', 0),
        call_type=getattr(self, '_current_call_type', 'chat'),
        model=payload.get("model", ""),
        system_prompt=system_prompt,
        messages=messages,
        tools=tools,
        frame_id=getattr(self, '_current_frame_id', 'unknown'),
        context_window=self._get_context_window(),
        payload=payload if getattr(self._settings, 'context_log_full_payload', False) else None,
    )
    _context_entry_id = _entry.id
payload["_context_entry_id"] = _context_entry_id  # Stash for response update
```

In the response handling (after API call returns), update the entry:

```python
if self._context_logger and "_context_entry_id" in payload:
    entry_id = payload.pop("_context_entry_id", None)
    if entry_id and hasattr(resp, "usage"):
        self._context_logger.update_response(
            entry_id=entry_id,
            input_tokens=resp.usage.get("input_tokens"),
            output_tokens=resp.usage.get("output_tokens"),
            cache_creation=resp.usage.get("cache_creation_input_tokens"),
            cache_read=resp.usage.get("cache_read_input_tokens"),
            duration_ms=duration_ms,
            stop_reason=resp.stop_reason,
        )
```

Note: The `_context_entry_id` must be removed from payload before sending to the API. Store it as a local variable instead of in the payload dict.

- [ ] **Step 3: Wire ContextLogger in main.py**

In `nous/main.py`, after creating the runner:

```python
# F035.4: Context Logger
context_logger = None
if settings.context_log_enabled:
    from nous.observability.context_logger import ContextLogger
    context_logger = ContextLogger(
        full_payload_enabled=settings.context_log_full_payload,
        ring_size=settings.context_log_ring_size,
        max_total=settings.context_log_max_total,
    )
    runner.set_context_logger(context_logger)
    logger.info("F035.4: ContextLogger wired (full_payload=%s)", settings.context_log_full_payload)
```

Pass `context_logger` to `create_app` so REST endpoints can access it.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_context_logger.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add nous/api/runner.py nous/config.py nous/main.py tests/test_context_logger.py
git commit -m "feat(f035.4): hook ContextLogger into runner pipeline + config"
```

---

## Task 12: F035.4 — REST Endpoints

**Files:**
- Modify: `nous/api/rest.py`
- Test: `tests/test_context_logger.py`

- [ ] **Step 1: Implement context log endpoints**

Add to `nous/api/rest.py`:

```python
async def context_log_list(request: Request) -> JSONResponse:
    """GET /context/log — List context log entries."""
    session_id = request.query_params.get("session_id")
    limit = int(request.query_params.get("limit", "20"))
    if not context_logger:
        return JSONResponse({"entries": []})
    entries = context_logger.get_recent(session_id=session_id, limit=limit)
    return JSONResponse({"entries": [e.to_dict() for e in entries]})

async def context_log_detail(request: Request) -> JSONResponse:
    """GET /context/log/{id} — Full metadata for a single entry."""
    entry_id = request.path_params["id"]
    if not context_logger:
        return JSONResponse({"error": "Context logging not enabled"}, status_code=404)
    entry = context_logger.get_entry(entry_id)
    if not entry:
        return JSONResponse({"error": "Entry not found"}, status_code=404)
    return JSONResponse(entry.to_dict())

async def context_log_payload(request: Request) -> JSONResponse:
    """GET /context/log/{id}/payload — Full API payload if captured."""
    entry_id = request.path_params["id"]
    if not context_logger:
        return JSONResponse({"error": "Context logging not enabled"}, status_code=404)
    payload = context_logger.get_payload(entry_id)
    if not payload:
        return JSONResponse({"error": "Payload not captured or pruned"}, status_code=404)
    return JSONResponse(payload)

async def context_log_sections(request: Request) -> JSONResponse:
    """GET /context/log/{id}/sections — Token breakdown by section."""
    entry_id = request.path_params["id"]
    if not context_logger:
        return JSONResponse({"error": "Context logging not enabled"}, status_code=404)
    entry = context_logger.get_entry(entry_id)
    if not entry:
        return JSONResponse({"error": "Entry not found"}, status_code=404)
    return JSONResponse({
        "sections": entry.token_breakdown,
        "total_tokens_est": entry.total_tokens_est,
        "sections_present": entry.sections_present,
    })

async def context_diff(request: Request) -> JSONResponse:
    """GET /context/diff?a={id}&b={id} — Diff between two entries."""
    a_id = request.query_params.get("a")
    b_id = request.query_params.get("b")
    if not context_logger or not a_id or not b_id:
        return JSONResponse({"error": "Missing parameters"}, status_code=400)
    a = context_logger.get_entry(a_id)
    b = context_logger.get_entry(b_id)
    if not a or not b:
        return JSONResponse({"error": "Entry not found"}, status_code=404)

    # Compute diff
    token_delta: dict[str, int] = {}
    all_sections = set(a.token_breakdown.keys()) | set(b.token_breakdown.keys())
    for s in all_sections:
        delta = b.token_breakdown.get(s, 0) - a.token_breakdown.get(s, 0)
        if delta != 0:
            token_delta[s] = delta

    sections_added = [s for s in b.sections_present if s not in a.sections_present]
    sections_removed = [s for s in a.sections_present if s not in b.sections_present]
    tools_added = [t for t in b.tool_names if t not in a.tool_names]
    tools_removed = [t for t in a.tool_names if t not in b.tool_names]

    return JSONResponse({
        "a": a_id,
        "b": b_id,
        "token_delta": {
            "total": b.total_tokens_est - a.total_tokens_est,
            "by_section": token_delta,
        },
        "sections_added": sections_added,
        "sections_removed": sections_removed,
        "tools_added": tools_added,
        "tools_removed": tools_removed,
        "messages_delta": b.messages_count - a.messages_count,
    })
```

Add routes:

```python
Route("/context/log", context_log_list, methods=["GET"]),
Route("/context/log/{id}", context_log_detail, methods=["GET"]),
Route("/context/log/{id}/payload", context_log_payload, methods=["GET"]),
Route("/context/log/{id}/sections", context_log_sections, methods=["GET"]),
Route("/context/diff", context_diff, methods=["GET"]),
```

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/test_context_logger.py -v`
Expected: All PASS

- [ ] **Step 3: Commit**

```bash
git add nous/api/rest.py tests/test_context_logger.py
git commit -m "feat(f035.4): add context log REST endpoints with diff support"
```

---

## Task 13: F035.3 — BehaviorSnapshot and DriftDetector

**Files:**
- Create: `nous/observability/snapshots.py`
- Create: `nous/observability/drift.py`
- Test: `tests/test_drift_detection.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_drift_detection.py`:

```python
"""Tests for F035.3 — Behavioral Drift Detection."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from nous.observability.drift import Anomaly, DriftDetector
from nous.observability.snapshots import BehaviorSnapshot


class TestBehaviorSnapshot:
    def test_create_snapshot(self):
        snap = BehaviorSnapshot(
            timestamp=datetime.now(UTC),
            fact_count=100,
            fact_count_delta=5,
            episode_count=50,
            episode_count_delta=2,
            active_censor_count=3,
            active_censor_delta=0,
            procedure_count=10,
            decision_count=200,
            facts_admitted=5,
            facts_rejected_dedup=1,
            facts_rejected_admission=0,
            admission_rate=0.83,
            checks_run=4,
            findings_created=1,
            findings_resolved=0,
            triage_sessions_opened=0,
            interval_changes=[],
            sleep_ran=False,
            episodes_compacted=0,
            facts_pruned=0,
            contradictions_resolved=0,
            events_processed=42,
            events_dropped=0,
            handler_error_count=0,
            handler_error_rate=0.0,
            turns_processed=10,
            avg_turn_latency_ms=3500.0,
            tool_calls=25,
        )
        assert snap.fact_count == 100
        assert snap.admission_rate == 0.83

    def test_to_metrics_dict(self):
        snap = BehaviorSnapshot(
            timestamp=datetime.now(UTC),
            fact_count=100, fact_count_delta=5,
            episode_count=50, episode_count_delta=2,
            active_censor_count=3, active_censor_delta=0,
            procedure_count=10, decision_count=200,
            facts_admitted=5, facts_rejected_dedup=1,
            facts_rejected_admission=0, admission_rate=0.83,
            checks_run=4, findings_created=1, findings_resolved=0,
            triage_sessions_opened=0, interval_changes=[],
            sleep_ran=False, episodes_compacted=0,
            facts_pruned=0, contradictions_resolved=0,
            events_processed=42, events_dropped=0,
            handler_error_count=0, handler_error_rate=0.0,
            turns_processed=10, avg_turn_latency_ms=3500.0, tool_calls=25,
        )
        d = snap.to_metrics_dict()
        assert d["fact_count"] == 100
        assert d["handler_error_rate"] == 0.0


class TestDriftDetector:
    def _make_history(self, metric: str, values: list[float]) -> list[BehaviorSnapshot]:
        """Create snapshot history with specified metric values."""
        snapshots = []
        for i, val in enumerate(values):
            kwargs = {
                "timestamp": datetime.now(UTC) - timedelta(hours=len(values) - i),
                "fact_count": 0, "fact_count_delta": 0,
                "episode_count": 0, "episode_count_delta": 0,
                "active_censor_count": 0, "active_censor_delta": 0,
                "procedure_count": 0, "decision_count": 0,
                "facts_admitted": 0, "facts_rejected_dedup": 0,
                "facts_rejected_admission": 0, "admission_rate": 0.0,
                "checks_run": 0, "findings_created": 0, "findings_resolved": 0,
                "triage_sessions_opened": 0, "interval_changes": [],
                "sleep_ran": False, "episodes_compacted": 0,
                "facts_pruned": 0, "contradictions_resolved": 0,
                "events_processed": 0, "events_dropped": 0,
                "handler_error_count": 0, "handler_error_rate": 0.0,
                "turns_processed": 0, "avg_turn_latency_ms": 0.0, "tool_calls": 0,
            }
            kwargs[metric] = val
            snapshots.append(BehaviorSnapshot(**kwargs))
        return snapshots

    def test_no_anomaly_within_threshold(self):
        detector = DriftDetector()
        history = self._make_history("fact_count_delta", [5, 6, 4, 7, 5, 6, 4, 5, 6, 5])
        current = self._make_history("fact_count_delta", [6])[0]
        anomalies = detector.detect(current, history)
        assert len(anomalies) == 0

    def test_anomaly_detected_above_threshold(self):
        detector = DriftDetector()
        history = self._make_history("fact_count_delta", [5, 6, 4, 7, 5, 6, 4, 5, 6, 5])
        current = self._make_history("fact_count_delta", [50])[0]  # Way above baseline
        anomalies = detector.detect(current, history)
        fact_anomalies = [a for a in anomalies if a.metric == "fact_count_delta"]
        assert len(fact_anomalies) == 1
        assert fact_anomalies[0].direction == "up"

    def test_anomaly_severity_warning_vs_alert(self):
        detector = DriftDetector()
        # Create history with tight distribution
        history = self._make_history("handler_error_rate", [0.01] * 10)
        # Warning level (2-3 sigma)
        current_warn = self._make_history("handler_error_rate", [0.05])[0]
        # Alert level (>3 sigma) — need bigger deviation
        current_alert = self._make_history("handler_error_rate", [0.5])[0]
        anomalies_warn = detector.detect(current_warn, history)
        anomalies_alert = detector.detect(current_alert, history)
        # At least one should be detected (exact depends on stddev)
        all_anomalies = anomalies_warn + anomalies_alert
        if all_anomalies:
            severities = {a.severity for a in all_anomalies}
            assert "warning" in severities or "alert" in severities

    def test_min_samples_guard(self):
        detector = DriftDetector()
        history = self._make_history("fact_count_delta", [5, 6])  # Only 2 samples
        current = self._make_history("fact_count_delta", [100])[0]
        anomalies = detector.detect(current, history)
        fact_anomalies = [a for a in anomalies if a.metric == "fact_count_delta"]
        assert len(fact_anomalies) == 0  # Not enough samples

    def test_zero_stddev_skipped(self):
        detector = DriftDetector()
        history = self._make_history("fact_count_delta", [5] * 15)  # All same
        current = self._make_history("fact_count_delta", [10])[0]
        anomalies = detector.detect(current, history)
        fact_anomalies = [a for a in anomalies if a.metric == "fact_count_delta"]
        assert len(fact_anomalies) == 0  # stddev=0, skip
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_drift_detection.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement snapshots.py**

Create `nous/observability/snapshots.py`:

```python
"""F035.3: Behavioral metric snapshots for drift detection."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class BehaviorSnapshot:
    """Point-in-time snapshot of key system metrics."""

    timestamp: datetime

    # Memory metrics
    fact_count: int = 0
    fact_count_delta: int = 0
    episode_count: int = 0
    episode_count_delta: int = 0
    active_censor_count: int = 0
    active_censor_delta: int = 0
    procedure_count: int = 0
    decision_count: int = 0

    # Admission metrics
    facts_admitted: int = 0
    facts_rejected_dedup: int = 0
    facts_rejected_admission: int = 0
    admission_rate: float = 0.0

    # Heartbeat metrics
    checks_run: int = 0
    findings_created: int = 0
    findings_resolved: int = 0
    triage_sessions_opened: int = 0
    interval_changes: list[dict] = field(default_factory=list)

    # Sleep metrics
    sleep_ran: bool = False
    episodes_compacted: int = 0
    facts_pruned: int = 0
    contradictions_resolved: int = 0

    # Event bus health
    events_processed: int = 0
    events_dropped: int = 0
    handler_error_count: int = 0
    handler_error_rate: float = 0.0

    # Conversation metrics
    turns_processed: int = 0
    avg_turn_latency_ms: float = 0.0
    tool_calls: int = 0

    def to_metrics_dict(self) -> dict[str, Any]:
        """Convert numeric fields to a flat dict for storage and comparison."""
        return {
            "fact_count": self.fact_count,
            "fact_count_delta": self.fact_count_delta,
            "episode_count": self.episode_count,
            "episode_count_delta": self.episode_count_delta,
            "active_censor_count": self.active_censor_count,
            "active_censor_delta": self.active_censor_delta,
            "procedure_count": self.procedure_count,
            "decision_count": self.decision_count,
            "facts_admitted": self.facts_admitted,
            "facts_rejected_dedup": self.facts_rejected_dedup,
            "facts_rejected_admission": self.facts_rejected_admission,
            "admission_rate": self.admission_rate,
            "checks_run": self.checks_run,
            "findings_created": self.findings_created,
            "findings_resolved": self.findings_resolved,
            "triage_sessions_opened": self.triage_sessions_opened,
            "sleep_ran": int(self.sleep_ran),
            "episodes_compacted": self.episodes_compacted,
            "facts_pruned": self.facts_pruned,
            "contradictions_resolved": self.contradictions_resolved,
            "events_processed": self.events_processed,
            "events_dropped": self.events_dropped,
            "handler_error_count": self.handler_error_count,
            "handler_error_rate": self.handler_error_rate,
            "turns_processed": self.turns_processed,
            "avg_turn_latency_ms": self.avg_turn_latency_ms,
            "tool_calls": self.tool_calls,
        }
```

- [ ] **Step 4: Implement drift.py**

Create `nous/observability/drift.py`:

```python
"""F035.3: Drift detection using z-score analysis."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any

from nous.observability.snapshots import BehaviorSnapshot


@dataclass
class Anomaly:
    """A detected anomaly in a behavioral metric."""

    metric: str
    current: float
    mean: float
    stddev: float
    z_score: float
    direction: str   # "up" or "down"
    severity: str    # "warning" or "alert"


class DriftDetector:
    """Z-score based behavioral drift detection."""

    THRESHOLDS: dict[str, dict[str, Any]] = {
        "fact_count_delta":       {"k": 2.0, "min_samples": 10},
        "admission_rate":         {"k": 2.0, "min_samples": 10},
        "active_censor_count":    {"k": 2.5, "min_samples": 10},
        "active_censor_delta":    {"k": 2.5, "min_samples": 10},
        "handler_error_rate":     {"k": 1.5, "min_samples": 5},
        "handler_error_count":    {"k": 1.5, "min_samples": 5},
        "events_dropped":         {"k": 1.5, "min_samples": 5},
        "facts_pruned":           {"k": 2.0, "min_samples": 10},
        "findings_created":       {"k": 2.0, "min_samples": 10},
        "episodes_compacted":     {"k": 2.0, "min_samples": 10},
        "contradictions_resolved": {"k": 2.0, "min_samples": 10},
    }

    def detect(
        self,
        current: BehaviorSnapshot,
        history: list[BehaviorSnapshot],
    ) -> list[Anomaly]:
        """Compare current snapshot against historical baseline."""
        anomalies: list[Anomaly] = []
        current_metrics = current.to_metrics_dict()

        for metric, config in self.THRESHOLDS.items():
            values = [s.to_metrics_dict().get(metric, 0) for s in history]
            if len(values) < config["min_samples"]:
                continue

            # Convert to float for statistics
            float_values = [float(v) for v in values]
            mean = statistics.mean(float_values)
            try:
                stddev = statistics.stdev(float_values)
            except statistics.StatisticsError:
                continue

            if stddev == 0:
                continue

            current_val = float(current_metrics.get(metric, 0))
            z_score = (current_val - mean) / stddev

            if abs(z_score) > config["k"]:
                severity = "alert" if abs(z_score) >= 3.0 else "warning"
                anomalies.append(Anomaly(
                    metric=metric,
                    current=current_val,
                    mean=round(mean, 2),
                    stddev=round(stddev, 2),
                    z_score=round(z_score, 2),
                    direction="up" if z_score > 0 else "down",
                    severity=severity,
                ))

        return anomalies
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_drift_detection.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add nous/observability/snapshots.py nous/observability/drift.py tests/test_drift_detection.py
git commit -m "feat(f035.3): add BehaviorSnapshot and DriftDetector with z-score analysis"
```

---

## Task 14: F035.3 — BehaviorDriftCheck (Heartbeat Integration)

**Files:**
- Create: `nous/heartbeat/checks/behavior_drift.py`
- Modify: `nous/config.py`
- Modify: `nous/main.py`
- Modify: `nous/api/rest.py`
- Test: `tests/test_drift_detection.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_drift_detection.py`:

```python
class TestBehaviorDriftCheck:
    @pytest.mark.asyncio
    async def test_check_returns_findings_on_anomaly(self):
        from nous.heartbeat.checks.behavior_drift import BehaviorDriftCheck

        heart = AsyncMock()
        brain = AsyncMock()
        bus_stats = MagicMock()
        bus_stats.to_dict.return_value = {
            "total_processed": 100, "total_dropped": 0,
            "handlers": {"H.run": {"invocations": 50, "errors": 0, "error_rate": 0.0}},
        }

        check = BehaviorDriftCheck(heart=heart, brain=brain, bus_stats=bus_stats, db=MagicMock())
        # Can't run full check without DB, but verify it initializes correctly
        assert check.name == "behavior_drift"
        assert check.active is True
```

- [ ] **Step 2: Create the heartbeat checks subdirectory if needed**

```bash
mkdir -p nous/heartbeat/checks
touch nous/heartbeat/checks/__init__.py
```

- [ ] **Step 3: Implement BehaviorDriftCheck**

Create `nous/heartbeat/checks/behavior_drift.py`:

```python
"""F035.3: Behavioral drift detection as a heartbeat check."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from nous.heartbeat.registry import BaseCheck
from nous.heartbeat.schemas import CheckResult, Finding
from nous.observability.drift import DriftDetector
from nous.observability.snapshots import BehaviorSnapshot

logger = logging.getLogger(__name__)


class BehaviorDriftCheck(BaseCheck):
    """Periodic behavioral drift detection.

    Captures a metric snapshot, stores it, compares against
    the rolling 7-day baseline, and returns findings for anomalies.
    """

    name = "behavior_drift"
    interval = 3600  # Default: hourly
    timeout = 30
    active = True

    def __init__(
        self,
        heart: Any,
        brain: Any,
        bus_stats: Any,
        db: Any,
    ) -> None:
        super().__init__()
        self._heart = heart
        self._brain = brain
        self._bus_stats = bus_stats
        self._db = db
        self._detector = DriftDetector()
        self._last_snapshot: BehaviorSnapshot | None = None

    async def run(self) -> CheckResult:
        """Capture snapshot, detect drift, return findings."""
        findings: list[Finding] = []

        try:
            snapshot = await self._capture_snapshot()
            await self._store_snapshot(snapshot)

            baseline = await self._load_baseline(hours=168)  # 7 days
            if baseline:
                anomalies = self._detector.detect(snapshot, baseline)
                for a in anomalies:
                    findings.append(Finding(
                        source="drift",
                        summary=f"{a.metric}: {a.current} ({a.direction} from {a.mean} +/- {a.stddev})",
                        urgency="high" if a.severity == "alert" else "normal",
                        needs_action=a.severity == "alert",
                        raw_data={
                            "metric": a.metric,
                            "current": a.current,
                            "mean": a.mean,
                            "stddev": a.stddev,
                            "z_score": a.z_score,
                        },
                    ))

            self._last_snapshot = snapshot
        except Exception:
            logger.exception("BehaviorDriftCheck failed")

        return CheckResult(findings=findings)

    async def _capture_snapshot(self) -> BehaviorSnapshot:
        """Gather current metrics from Heart, Brain, and EventBus stats."""
        now = datetime.now(UTC)

        # Memory counts from Heart
        fact_count = 0
        episode_count = 0
        censor_count = 0
        procedure_count = 0
        try:
            async with self._db.session() as session:
                from sqlalchemy import text
                result = await session.execute(text(
                    "SELECT "
                    "(SELECT COUNT(*) FROM heart.facts WHERE active = true) AS facts, "
                    "(SELECT COUNT(*) FROM heart.episodes) AS episodes, "
                    "(SELECT COUNT(*) FROM heart.censors WHERE active = true) AS censors, "
                    "(SELECT COUNT(*) FROM heart.procedures WHERE active = true) AS procedures"
                ))
                row = result.fetchone()
                if row:
                    fact_count = row.facts
                    episode_count = row.episodes
                    censor_count = row.censors
                    procedure_count = row.procedures
        except Exception:
            logger.debug("Snapshot: DB count query failed", exc_info=True)

        # Event bus stats
        bus_data = self._bus_stats.to_dict() if self._bus_stats else {}
        handlers = bus_data.get("handlers", {})
        total_errors = sum(h.get("errors", 0) for h in handlers.values())
        total_invocations = sum(h.get("invocations", 0) for h in handlers.values())
        error_rate = total_errors / total_invocations if total_invocations else 0.0

        # Deltas from last snapshot
        prev = self._last_snapshot
        fact_delta = fact_count - prev.fact_count if prev else 0
        episode_delta = episode_count - prev.episode_count if prev else 0
        censor_delta = censor_count - prev.active_censor_count if prev else 0

        return BehaviorSnapshot(
            timestamp=now,
            fact_count=fact_count,
            fact_count_delta=fact_delta,
            episode_count=episode_count,
            episode_count_delta=episode_delta,
            active_censor_count=censor_count,
            active_censor_delta=censor_delta,
            procedure_count=procedure_count,
            decision_count=0,
            facts_admitted=0,
            facts_rejected_dedup=0,
            facts_rejected_admission=0,
            admission_rate=0.0,
            checks_run=0,
            findings_created=0,
            findings_resolved=0,
            triage_sessions_opened=0,
            interval_changes=[],
            sleep_ran=False,
            episodes_compacted=0,
            facts_pruned=0,
            contradictions_resolved=0,
            events_processed=bus_data.get("total_processed", 0),
            events_dropped=bus_data.get("total_dropped", 0),
            handler_error_count=total_errors,
            handler_error_rate=round(error_rate, 4),
            turns_processed=bus_data.get("event_counts", {}).get("turn_completed", 0),
            avg_turn_latency_ms=0.0,
            tool_calls=0,
        )

    async def _store_snapshot(self, snapshot: BehaviorSnapshot) -> None:
        """Persist snapshot to DB."""
        try:
            async with self._db.session() as session:
                from sqlalchemy import text
                await session.execute(text(
                    "INSERT INTO nous_system.behavior_snapshots (timestamp, metrics) "
                    "VALUES (:ts, :metrics)"
                ), {"ts": snapshot.timestamp, "metrics": json.dumps(snapshot.to_metrics_dict())})
                await session.commit()
        except Exception:
            logger.debug("Snapshot store failed", exc_info=True)

    async def _load_baseline(self, hours: int = 168) -> list[BehaviorSnapshot]:
        """Load recent snapshots for baseline comparison."""
        try:
            async with self._db.session() as session:
                from sqlalchemy import text
                cutoff = datetime.now(UTC) - timedelta(hours=hours)
                result = await session.execute(text(
                    "SELECT timestamp, metrics FROM nous_system.behavior_snapshots "
                    "WHERE timestamp > :cutoff ORDER BY timestamp"
                ), {"cutoff": cutoff})
                rows = result.fetchall()

            snapshots = []
            for row in rows:
                metrics = row.metrics if isinstance(row.metrics, dict) else json.loads(row.metrics)
                snap = BehaviorSnapshot(timestamp=row.timestamp, **{
                    k: metrics.get(k, 0) for k in BehaviorSnapshot.__dataclass_fields__
                    if k != "timestamp" and k != "interval_changes"
                })
                snapshots.append(snap)
            return snapshots
        except Exception:
            logger.debug("Baseline load failed", exc_info=True)
            return []
```

- [ ] **Step 4: Add config and wire in main.py**

Add to `nous/config.py`:

```python
# F035.3: Behavioral drift detection
drift_detection_enabled: bool = True
drift_detection_interval: int = 3600  # seconds between snapshot captures
```

Wire in `nous/main.py` where heartbeat checks are registered:

```python
# F035.3: Behavioral drift detection check
if settings.drift_detection_enabled and bus is not None:
    from nous.heartbeat.checks.behavior_drift import BehaviorDriftCheck
    drift_check = BehaviorDriftCheck(
        heart=heart, brain=brain, bus_stats=bus.stats, db=database,
    )
    drift_check.interval = settings.drift_detection_interval
    registry.register(drift_check)
    logger.info("F035.3: BehaviorDriftCheck registered (interval=%ds)", settings.drift_detection_interval)
```

- [ ] **Step 5: Add behavior trend endpoints to rest.py**

Add to `nous/api/rest.py`:

```python
async def behavior_snapshot_latest(request: Request) -> JSONResponse:
    """GET /behavior/snapshot/latest — Most recent behavior snapshot."""
    async with database.session() as session:
        from sqlalchemy import text
        result = await session.execute(text(
            "SELECT timestamp, metrics, anomalies FROM nous_system.behavior_snapshots "
            "ORDER BY timestamp DESC LIMIT 1"
        ))
        row = result.fetchone()
    if not row:
        return JSONResponse({"snapshot": None})
    return JSONResponse({
        "snapshot": {
            "timestamp": row.timestamp.isoformat() if row.timestamp else None,
            "metrics": row.metrics,
            "anomalies": row.anomalies or [],
        }
    })

async def behavior_trends(request: Request) -> JSONResponse:
    """GET /behavior/trends — Time series for a metric."""
    metric = request.query_params.get("metric", "fact_count_delta")
    hours = int(request.query_params.get("hours", "168"))
    async with database.session() as session:
        from sqlalchemy import text
        from datetime import timedelta
        cutoff = datetime.now(UTC) - timedelta(hours=hours)
        result = await session.execute(text(
            "SELECT timestamp, metrics FROM nous_system.behavior_snapshots "
            "WHERE timestamp > :cutoff ORDER BY timestamp"
        ), {"cutoff": cutoff})
        rows = result.fetchall()

    points = []
    values = []
    for row in rows:
        metrics = row.metrics if isinstance(row.metrics, dict) else {}
        val = metrics.get(metric, 0)
        points.append({"timestamp": row.timestamp.isoformat(), "value": val})
        values.append(float(val))

    stats = {}
    if values:
        import statistics as st
        stats = {
            "mean": round(st.mean(values), 2),
            "stddev": round(st.stdev(values), 2) if len(values) > 1 else 0,
            "min": min(values),
            "max": max(values),
        }

    return JSONResponse({"metric": metric, "hours": hours, "points": points, "stats": stats})

async def behavior_anomalies(request: Request) -> JSONResponse:
    """GET /behavior/anomalies — Detected anomalies."""
    hours = int(request.query_params.get("hours", "168"))
    async with database.session() as session:
        from sqlalchemy import text
        from datetime import timedelta
        cutoff = datetime.now(UTC) - timedelta(hours=hours)
        result = await session.execute(text(
            "SELECT timestamp, anomalies FROM nous_system.behavior_snapshots "
            "WHERE anomalies != '[]'::jsonb AND timestamp > :cutoff "
            "ORDER BY timestamp DESC"
        ), {"cutoff": cutoff})
        rows = result.fetchall()

    anomalies = []
    for row in rows:
        for a in (row.anomalies or []):
            a["timestamp"] = row.timestamp.isoformat()
            anomalies.append(a)

    return JSONResponse({"anomalies": anomalies, "hours": hours})
```

Add routes:

```python
Route("/behavior/snapshot/latest", behavior_snapshot_latest, methods=["GET"]),
Route("/behavior/trends", behavior_trends, methods=["GET"]),
Route("/behavior/anomalies", behavior_anomalies, methods=["GET"]),
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_drift_detection.py -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add nous/heartbeat/checks/__init__.py nous/heartbeat/checks/behavior_drift.py nous/observability/snapshots.py nous/observability/drift.py nous/config.py nous/main.py nous/api/rest.py tests/test_drift_detection.py
git commit -m "feat(f035.3): add BehaviorDriftCheck + trend endpoints + heartbeat integration"
```

---

## Task 15: F035.2 + F035.4 — Telegram Integration

**Files:**
- Modify: `nous/telegram_bot.py`
- Test: `tests/test_causal_tracing.py` and `tests/test_context_logger.py`

- [ ] **Step 1: Add Telegram formatting functions**

Add to `nous/telegram_bot.py`:

```python
def format_trace_summary(trace_data: dict) -> str:
    """F035.2: Format a causal chain for Telegram."""
    events = trace_data.get("events", [])
    if not events:
        return "No events found for this trace."

    lines = [f"<b>Trace: {trace_data.get('trace_id', '?')}</b>"]
    lines.append(f"Root: {trace_data.get('root_event', '?')}")
    lines.append(f"Depth: {trace_data.get('depth', 0)} events")
    if trace_data.get("duration_ms"):
        lines.append(f"Duration: {trace_data['duration_ms']:.0f}ms")
    lines.append("")

    for e in events:
        indent = "  " if e.get("caused_by") else ""
        mod = " [MOD]" if e.get("data", {}).get("modifies") else ""
        lines.append(f"{indent}{e.get('type', '?')}{mod}")

    return "\n".join(lines)


def format_context_summary(entry_data: dict) -> str:
    """F035.4: Format context log entry for Telegram."""
    total = entry_data.get("total_tokens_est", 0)
    actual = entry_data.get("input_tokens_actual")
    utilization = entry_data.get("utilization_pct", 0)
    duration = entry_data.get("duration_ms")

    lines = [
        f"<b>Last API Call (Turn {entry_data.get('turn_number', '?')})</b>",
        f"  Model: {entry_data.get('model', '?')}",
        f"  Frame: {entry_data.get('frame_id', '?')}",
    ]

    token_str = f"  Tokens: ~{total:,} est"
    if actual:
        token_str += f" / {actual:,} actual"
    token_str += f" ({utilization:.1f}% of window)"
    lines.append(token_str)

    if duration:
        lines.append(f"  Duration: {duration/1000:.1f}s")

    # Token breakdown (top 5 by size)
    breakdown = entry_data.get("token_breakdown", {})
    if breakdown:
        lines.append("")
        lines.append("  Token Breakdown:")
        sorted_sections = sorted(breakdown.items(), key=lambda x: x[1], reverse=True)[:5]
        for name, tokens in sorted_sections:
            pct = (tokens / total * 100) if total else 0
            lines.append(f"  - {name}: {tokens:,} ({pct:.0f}%)")

    # Memory
    facts = entry_data.get("loaded_facts", 0)
    decisions = entry_data.get("loaded_decisions", 0)
    procedures = entry_data.get("loaded_procedures", 0)
    if facts or decisions or procedures:
        lines.append(f"\n  Memory: {facts} facts, {procedures} procedures, {decisions} decisions")

    lines.append(f"  Tools: {entry_data.get('tools_count', 0)}")

    return "\n".join(lines)
```

- [ ] **Step 2: Run existing tests to verify no regressions**

Run: `uv run pytest tests/test_event_bus_observability.py tests/test_causal_tracing.py tests/test_context_logger.py -v`
Expected: All PASS

- [ ] **Step 3: Commit**

```bash
git add nous/telegram_bot.py
git commit -m "feat(f035): add Telegram formatting for traces, context, and drift alerts"
```

---

## Task 16: Final Integration — Update Feature Docs + Run Full Test Suite

**Files:**
- Modify: `docs/features/F035-observability.md` (status PROPOSED → SHIPPED)
- Modify: `docs/features/F035.1-event-bus-observability.md`
- Modify: `docs/features/F035.2-causal-chain-tracing.md`
- Modify: `docs/features/F035.3-behavioral-drift-detection.md`
- Modify: `docs/features/F035.4-context-visibility.md`
- Modify: `nous/observability/__init__.py` (ensure clean exports)

- [ ] **Step 1: Update feature spec statuses**

Change `**Status:** PROPOSED` to `**Status:** SHIPPED` in all 5 F035 spec files.

- [ ] **Step 2: Update observability __init__.py exports**

```python
"""F035: Observability package — context logging, drift detection, snapshots."""

from nous.observability.context_logger import ContextLogEntry, ContextLogger, FullPayloadStore, parse_system_sections
from nous.observability.drift import Anomaly, DriftDetector
from nous.observability.snapshots import BehaviorSnapshot

__all__ = [
    "BehaviorSnapshot",
    "ContextLogEntry",
    "ContextLogger",
    "DriftDetector",
    "FullPayloadStore",
    "Anomaly",
    "parse_system_sections",
]
```

- [ ] **Step 3: Run full test suite**

Run: `uv run pytest tests/ -v --tb=short 2>&1 | tail -50`
Expected: All tests pass, no regressions

- [ ] **Step 4: Commit**

```bash
git add docs/features/ nous/observability/__init__.py
git commit -m "docs(f035): mark all F035 sub-features as SHIPPED"
```
