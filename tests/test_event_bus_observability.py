"""Tests for F035.1 Event Bus Observability.

Tests cover:
1. HandlerStat: initial state, avg_duration, error_rate
2. EventBusStats: record_event, record_handler_success/error, record_drop, ring buffer, to_dict
3. EventBus wiring: stats populated after event, handler success/error recorded, queue full drops
4. Handler get_stats: SessionTimeoutMonitor, SleepHandler
5. Telegram format function
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from nous.events import Event, EventBus, EventBusStats, HandlerStat

# ---------------------------------------------------------------------------
# 1. HandlerStat unit tests
# ---------------------------------------------------------------------------


class TestHandlerStat:
    def test_initial_state(self):
        stat = HandlerStat(name="test_handler")
        assert stat.invocations == 0
        assert stat.successes == 0
        assert stat.errors == 0
        assert stat.total_duration_ms == 0.0
        assert stat.last_invoked is None
        assert stat.last_error is None
        assert stat.last_error_msg is None

    def test_avg_duration_zero_invocations(self):
        stat = HandlerStat(name="test")
        assert stat.avg_duration_ms == 0.0

    def test_avg_duration_with_invocations(self):
        stat = HandlerStat(name="test", invocations=4, total_duration_ms=100.0)
        assert stat.avg_duration_ms == 25.0

    def test_error_rate_zero_invocations(self):
        stat = HandlerStat(name="test")
        assert stat.error_rate == 0.0

    def test_error_rate_with_errors(self):
        stat = HandlerStat(name="test", invocations=10, errors=3)
        assert abs(stat.error_rate - 0.3) < 1e-9


# ---------------------------------------------------------------------------
# 2. EventBusStats unit tests
# ---------------------------------------------------------------------------


class TestEventBusStats:
    def test_initial_state(self):
        stats = EventBusStats()
        assert stats.total_processed == 0
        assert stats.total_dropped == 0

    def test_record_event(self):
        stats = EventBusStats()
        stats.record_event("test_type", 2, 0, 10.5, session_id="s1")
        assert stats.total_processed == 1
        assert stats._event_counts["test_type"] == 1

    def test_record_handler_success(self):
        stats = EventBusStats()
        stats.record_handler_success("MyHandler.handle", 5.0)
        stat = stats._handler_stats["MyHandler.handle"]
        assert stat.invocations == 1
        assert stat.successes == 1
        assert stat.total_duration_ms == 5.0
        assert stat.last_invoked is not None

    def test_record_handler_error(self):
        stats = EventBusStats()
        stats.record_handler_error("MyHandler.handle", "boom")
        stat = stats._handler_stats["MyHandler.handle"]
        assert stat.invocations == 1
        assert stat.errors == 1
        assert stat.last_error is not None
        assert stat.last_error_msg == "boom"

    def test_record_drop(self):
        stats = EventBusStats()
        stats.record_drop()
        stats.record_drop()
        assert stats.total_dropped == 2

    def test_ring_buffer_limit(self):
        stats = EventBusStats(recent_limit=3)
        for i in range(5):
            stats.record_event(f"type_{i}", 1, 0, 1.0)
        assert len(stats._recent) == 3
        # Most recent should be type_4
        recent = stats.recent_events()
        assert recent[0].type == "type_4"
        assert recent[-1].type == "type_2"

    def test_recent_events_with_limit(self):
        stats = EventBusStats()
        for i in range(10):
            stats.record_event(f"type_{i}", 1, 0, 1.0)
        recent = stats.recent_events(limit=3)
        assert len(recent) == 3
        assert recent[0].type == "type_9"

    def test_to_dict(self):
        stats = EventBusStats()
        stats.record_event("test", 1, 0, 5.0)
        stats.record_handler_success("MyClass.handle", 5.0)
        d = stats.to_dict()
        assert d["total_processed"] == 1
        assert d["total_dropped"] == 0
        assert "test" in d["event_counts"]
        assert "MyClass.handle" in d["handlers"]
        handler_d = d["handlers"]["MyClass.handle"]
        assert handler_d["invocations"] == 1
        assert handler_d["successes"] == 1
        assert "uptime_seconds" in d

    def test_to_dict_with_errors(self):
        stats = EventBusStats()
        stats.record_handler_error("Bad.handler", "something broke")
        d = stats.to_dict()
        h = d["handlers"]["Bad.handler"]
        assert h["errors"] == 1
        assert h["error_rate"] == 1.0
        assert "last_error_ago_s" in h
        assert h["last_error_msg"] == "something broke"


# ---------------------------------------------------------------------------
# 3. EventBus wiring tests
# ---------------------------------------------------------------------------


class TestEventBusWiring:
    @pytest.mark.asyncio
    async def test_stats_populated_after_event(self):
        bus = EventBus()
        handled = asyncio.Event()

        async def handler(event: Event) -> None:
            handled.set()

        bus.on("test", handler)
        await bus.start()
        try:
            await bus.emit(Event(type="test", agent_id="a1", session_id="s1"))
            await asyncio.wait_for(handled.wait(), timeout=2.0)
            # Give dispatch a moment to finish recording
            await asyncio.sleep(0.05)
            assert bus.stats.total_processed >= 1
            assert bus.stats._event_counts["test"] >= 1
        finally:
            await bus.stop()

    @pytest.mark.asyncio
    async def test_handler_success_recorded(self):
        bus = EventBus()
        handled = asyncio.Event()

        async def good_handler(event: Event) -> None:
            handled.set()

        bus.on("test", good_handler)
        await bus.start()
        try:
            await bus.emit(Event(type="test", agent_id="a1"))
            await asyncio.wait_for(handled.wait(), timeout=2.0)
            await asyncio.sleep(0.05)
            # Check handler was recorded
            found = False
            for name, stat in bus.stats._handler_stats.items():
                if "good_handler" in name:
                    assert stat.successes >= 1
                    found = True
            assert found, f"Handler not found in stats: {list(bus.stats._handler_stats.keys())}"
        finally:
            await bus.stop()

    @pytest.mark.asyncio
    async def test_handler_error_recorded(self):
        bus = EventBus()
        error_raised = asyncio.Event()

        async def bad_handler(event: Event) -> None:
            error_raised.set()
            raise ValueError("test error")

        bus.on("test", bad_handler)
        await bus.start()
        try:
            await bus.emit(Event(type="test", agent_id="a1"))
            await asyncio.wait_for(error_raised.wait(), timeout=2.0)
            await asyncio.sleep(0.05)
            found = False
            for name, stat in bus.stats._handler_stats.items():
                if "bad_handler" in name:
                    assert stat.errors >= 1
                    assert stat.last_error_msg == "test error"
                    found = True
            assert found
        finally:
            await bus.stop()

    @pytest.mark.asyncio
    async def test_queue_full_records_drop(self):
        bus = EventBus(max_queue=1)
        # Don't start the bus so nothing is consumed
        # Fill the queue
        await bus.emit(Event(type="fill", agent_id="a1"))
        # This should be dropped
        await bus.emit(Event(type="dropped", agent_id="a1"))
        assert bus.stats.total_dropped >= 1

    @pytest.mark.asyncio
    async def test_no_handlers_still_records_event(self):
        bus = EventBus()
        await bus.start()
        try:
            await bus.emit(Event(type="orphan", agent_id="a1"))
            await asyncio.sleep(0.1)
            assert bus.stats.total_processed >= 1
            recent = bus.stats.recent_events()
            assert any(e.type == "orphan" for e in recent)
        finally:
            await bus.stop()


# ---------------------------------------------------------------------------
# 4. Handler get_stats tests
# ---------------------------------------------------------------------------


class TestSessionMonitorGetStats:
    def test_get_stats_initial(self):
        from nous.handlers.session_monitor import SessionTimeoutMonitor

        bus = EventBus()
        settings = MagicMock()
        settings.session_idle_timeout = 1800
        settings.sleep_timeout = 7200
        settings.sleep_check_interval = 60
        monitor = SessionTimeoutMonitor(bus, settings)
        stats = monitor.get_stats()
        assert stats["tracked_sessions"] == 0
        assert stats["sessions"] == {}
        assert stats["sleep_emitted"] is False
        assert "global_idle_seconds" in stats

    @pytest.mark.asyncio
    async def test_get_stats_with_activity(self):
        from nous.handlers.session_monitor import SessionTimeoutMonitor

        bus = EventBus()
        settings = MagicMock()
        settings.session_idle_timeout = 1800
        settings.sleep_timeout = 7200
        settings.sleep_check_interval = 60
        monitor = SessionTimeoutMonitor(bus, settings)
        # Simulate activity
        await monitor.on_activity(Event(type="turn_completed", agent_id="a1", session_id="sess-1"))
        stats = monitor.get_stats()
        assert stats["tracked_sessions"] == 1
        assert "sess-1" in stats["sessions"]
        assert "idle_seconds" in stats["sessions"]["sess-1"]


class TestSleepHandlerGetStats:
    def test_get_stats_initial(self):
        from nous.handlers.sleep_handler import SleepHandler

        brain = MagicMock()
        heart = MagicMock()
        settings = MagicMock()
        settings.background_model = "test"
        bus = EventBus()
        handler = SleepHandler(brain, heart, settings, bus)
        stats = handler.get_stats()
        assert stats["total_sleeps"] == 0
        assert stats["last_sleep_at"] is None
        assert stats["last_phases_completed"] == []
        assert stats["currently_sleeping"] is False


# ---------------------------------------------------------------------------
# 5. Telegram format function
# ---------------------------------------------------------------------------


class TestTelegramFormat:
    def test_format_event_bus_status_basic(self):
        from nous.telegram_bot import format_event_bus_status

        stats = {
            "total_processed": 42,
            "total_dropped": 1,
            "queue_depth": 3,
            "uptime_seconds": 7260,  # 2h 1m
            "handlers": {
                "MyModule.MyClass.handle": {
                    "invocations": 10,
                    "successes": 9,
                    "error_rate": 0.1,
                },
            },
        }
        result = format_event_bus_status(stats)
        assert "42 events processed" in result
        assert "1 dropped" in result
        assert "3 pending" in result
        assert "2h 1m" in result
        assert "OK" in result  # error_rate == 0.10, not > 0.10

    def test_format_event_bus_status_high_error_rate(self):
        from nous.telegram_bot import format_event_bus_status

        stats = {
            "total_processed": 10,
            "total_dropped": 0,
            "queue_depth": 0,
            "uptime_seconds": 300,
            "handlers": {
                "BadHandler.run": {
                    "invocations": 10,
                    "successes": 5,
                    "error_rate": 0.5,
                },
            },
        }
        result = format_event_bus_status(stats)
        assert "!!" in result

    def test_format_handler_name_without_dots(self):
        from nous.telegram_bot import format_event_bus_status

        stats = {
            "total_processed": 1,
            "total_dropped": 0,
            "queue_depth": 0,
            "uptime_seconds": 60,
            "handlers": {
                "simple_handler": {
                    "invocations": 1,
                    "successes": 1,
                    "error_rate": 0.0,
                },
            },
        }
        result = format_event_bus_status(stats)
        # Should not crash; should use the name as-is
        assert "simple_handler" in result

    def test_format_empty_handlers(self):
        from nous.telegram_bot import format_event_bus_status

        stats = {
            "total_processed": 0,
            "total_dropped": 0,
            "queue_depth": 0,
            "uptime_seconds": 0,
            "handlers": {},
        }
        result = format_event_bus_status(stats)
        assert "Event Bus" in result
        assert "0 events processed" in result

    def test_format_minutes_only(self):
        from nous.telegram_bot import format_event_bus_status

        stats = {
            "total_processed": 5,
            "total_dropped": 0,
            "queue_depth": 0,
            "uptime_seconds": 900,  # 15m, no hours
            "handlers": {},
        }
        result = format_event_bus_status(stats)
        assert "15m" in result
        # Should not show "0h"
        assert "0h" not in result
