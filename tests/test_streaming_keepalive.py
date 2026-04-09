"""Tests for streaming keepalive during tool execution."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from nous.api.runner import AgentRunner, StreamEvent
from nous.config import Settings


def _make_settings(**overrides) -> Settings:
    defaults = {
        "ANTHROPIC_API_KEY": "test-key",
        "NOUS_TOOL_TIMEOUT": "5",
        "NOUS_KEEPALIVE_INTERVAL": "1",
    }
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture
def settings():
    """Minimal settings for runner tests."""
    return _make_settings()


@pytest.fixture
def runner(settings):
    """AgentRunner with mocked dependencies."""
    cognitive = MagicMock()
    brain = MagicMock()
    heart = MagicMock()
    r = AgentRunner(cognitive, brain, heart, settings)
    r._dispatcher = MagicMock()
    return r


class TestDispatchWithKeepalive:
    """Tests for _dispatch_with_keepalive helper."""

    @pytest.mark.asyncio
    async def test_fast_tool_no_keepalive(self, runner):
        """Tools completing within keepalive_interval emit no keepalives."""
        runner._dispatcher.dispatch = AsyncMock(return_value=("result", False))

        events = []
        async for item in runner._dispatch_with_keepalive("test_tool", {}):
            events.append(item)

        # Only the final result tuple, no keepalives
        assert len(events) == 1
        assert events[0] == ("result", False)

    @pytest.mark.asyncio
    async def test_slow_tool_emits_keepalives(self, runner):
        """Tools taking longer than keepalive_interval emit keepalive events."""

        async def slow_dispatch(name, args):
            await asyncio.sleep(2.5)  # 2.5s with 1s interval = 2 keepalives
            return ("done", False)

        runner._dispatcher.dispatch = slow_dispatch

        events = []
        async for item in runner._dispatch_with_keepalive("slow_tool", {}):
            events.append(item)

        keepalives = [e for e in events if isinstance(e, StreamEvent)]
        results = [e for e in events if isinstance(e, tuple)]

        assert len(keepalives) >= 2
        assert all(k.type == "keepalive" for k in keepalives)
        assert len(results) == 1
        assert results[0] == ("done", False)

    @pytest.mark.asyncio
    async def test_tool_timeout_returns_error(self, runner):
        """Tools exceeding tool_timeout are cancelled with an error."""

        async def hanging_dispatch(name, args):
            await asyncio.sleep(100)  # Way beyond 5s timeout
            return ("never", False)

        runner._dispatcher.dispatch = hanging_dispatch

        events = []
        async for item in runner._dispatch_with_keepalive("hang_tool", {}):
            events.append(item)

        results = [e for e in events if isinstance(e, tuple)]
        assert len(results) == 1
        result_text, is_error = results[0]
        assert is_error is True
        assert "timed out" in result_text
        assert "hang_tool" in result_text

    @pytest.mark.asyncio
    async def test_tool_exception_returns_error(self, runner):
        """Tool exceptions are caught and returned as errors."""

        async def failing_dispatch(name, args):
            raise RuntimeError("tool broke")

        runner._dispatcher.dispatch = failing_dispatch

        events = []
        async for item in runner._dispatch_with_keepalive("bad_tool", {}):
            events.append(item)

        results = [e for e in events if isinstance(e, tuple)]
        assert len(results) == 1
        result_text, is_error = results[0]
        assert is_error is True
        assert "tool broke" in result_text

    @pytest.mark.asyncio
    async def test_keepalive_event_format(self, runner):
        """Keepalive events have correct StreamEvent format."""

        async def slow_dispatch(name, args):
            await asyncio.sleep(1.5)
            return ("ok", False)

        runner._dispatcher.dispatch = slow_dispatch

        events = []
        async for item in runner._dispatch_with_keepalive("tool", {}):
            events.append(item)

        keepalives = [e for e in events if isinstance(e, StreamEvent)]
        assert len(keepalives) >= 1
        for k in keepalives:
            assert k.type == "keepalive"
            assert k.text == ""
            assert k.tool_name == ""


    @pytest.mark.asyncio
    async def test_generator_cleanup_cancels_task(self, runner):
        """Closing the generator cancels the underlying task."""
        task_started = asyncio.Event()
        task_cancelled = asyncio.Event()

        async def slow_dispatch(name, args):
            task_started.set()
            try:
                await asyncio.sleep(100)
                return ("never", False)
            except asyncio.CancelledError:
                task_cancelled.set()
                raise

        runner._dispatcher.dispatch = slow_dispatch

        gen = runner._dispatch_with_keepalive("cancellable", {})
        # Get first keepalive to ensure task is running
        item = await gen.__anext__()
        assert isinstance(item, StreamEvent)
        assert item.type == "keepalive"
        # Close the generator — should cancel the task
        await gen.aclose()
        # Give the event loop a tick to process cancellation
        await asyncio.sleep(0.1)
        assert task_cancelled.is_set()


class TestConfigFields:
    """Tests for new config fields."""

    def test_default_tool_timeout(self):
        s = Settings(ANTHROPIC_API_KEY="test")
        assert s.tool_timeout == 120

    def test_default_keepalive_interval(self):
        s = Settings(ANTHROPIC_API_KEY="test")
        assert s.keepalive_interval == 10

    def test_custom_tool_timeout(self):
        s = _make_settings(NOUS_TOOL_TIMEOUT="60")
        assert s.tool_timeout == 60

    def test_custom_keepalive_interval(self):
        s = _make_settings(NOUS_KEEPALIVE_INTERVAL="2", NOUS_TOOL_TIMEOUT="10")
        assert s.keepalive_interval == 2

    def test_keepalive_interval_must_be_less_than_timeout(self):
        with pytest.raises(ValueError, match="keepalive_interval"):
            _make_settings(
                NOUS_KEEPALIVE_INTERVAL="120",
                NOUS_TOOL_TIMEOUT="60",
            )
