"""Tests for the decoupled DAG orchestrator tick loop (fix/dag-tick-own-loop).

Verifies:
  a. A hung heartbeat _tick does NOT block dag_orchestrator.tick() from being called.
  b. Overlapping orchestrator ticks are not run concurrently (single-flight).
  c. A hung orchestrator tick is timed out and the loop keeps going.
  d. stop() cancels the DAG loop cleanly.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nous.config import Settings
from nous.heartbeat.runner import HeartbeatRunner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(**overrides) -> Settings:
    """Create a minimal Settings with fast tick intervals for testing."""
    defaults = {
        "dag_tick_interval": 1,  # 1s for fast tests
        "dag_tick_timeout": 5,
        "heartbeat_tick_interval": 30,  # slow HB tick so it doesn't interfere
        "heartbeat_enabled": False,
        "heartbeat_quiet_start": 0,
        "heartbeat_quiet_end": 0,
        "heartbeat_daily_token_budget": 10_000,
        "heartbeat_dynamic_sync_ticks": 0,
    }
    defaults.update(overrides)
    return Settings.model_construct(**defaults)


def _make_runner(settings: Settings, dag_orchestrator=None) -> HeartbeatRunner:
    """Build a HeartbeatRunner with minimal dependencies."""
    runner = HeartbeatRunner(
        settings=settings,
        registry=MagicMock(),
        runner=MagicMock(),
        brain=MagicMock(),
        heart=MagicMock(),
        bus=None,
        http_client=None,
        finding_store=None,
        api_client=None,
        dynamic_loader=None,
    )
    if dag_orchestrator is not None:
        runner.dag_orchestrator = dag_orchestrator
    return runner


# ---------------------------------------------------------------------------
# Test a: hung heartbeat _tick does NOT block dag_orchestrator.tick()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dag_tick_independent_of_hung_heartbeat():
    """A heartbeat _tick that hangs must not block dag_orchestrator.tick()."""
    settings = _make_settings(dag_tick_interval=1, heartbeat_tick_interval=9999)

    hang_event = asyncio.Event()
    dag_tick_event = asyncio.Event()

    async def hung_tick(*args, **kwargs):
        await hang_event.wait()  # blocks until released
        return []

    dag_orchestrator = MagicMock()
    async def counting_dag_tick():
        dag_tick_event.set()

    dag_orchestrator.tick = AsyncMock(side_effect=counting_dag_tick)

    runner = _make_runner(settings, dag_orchestrator)

    # Patch _tick to hang
    with patch.object(runner, "_tick", side_effect=hung_tick):
        with patch.object(runner, "_detect_missed_checks", AsyncMock()):
            await runner.start()
            try:
                # DAG tick should fire within ~2s even though heartbeat _tick hangs
                await asyncio.wait_for(dag_tick_event.wait(), timeout=4.0)
                assert dag_tick_event.is_set(), "dag_orchestrator.tick() was never called"
            finally:
                hang_event.set()  # release hung heartbeat
                await runner.stop()


# ---------------------------------------------------------------------------
# Test b: overlapping orchestrator ticks are not run concurrently
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dag_tick_single_flight():
    """A slow dag_orchestrator.tick() must block the next tick from starting."""
    settings = _make_settings(dag_tick_interval=1, dag_tick_timeout=10)

    slow_started = asyncio.Event()
    slow_done = asyncio.Event()
    concurrent_detected = asyncio.Event()
    active_count = 0

    async def slow_dag_tick():
        nonlocal active_count
        active_count += 1
        if active_count > 1:
            concurrent_detected.set()
        slow_started.set()
        await slow_done.wait()
        active_count -= 1

    dag_orchestrator = MagicMock()
    dag_orchestrator.tick = AsyncMock(side_effect=slow_dag_tick)

    runner = _make_runner(settings, dag_orchestrator)

    with patch.object(runner, "_detect_missed_checks", AsyncMock()):
        await runner.start()
        try:
            # Wait for the slow tick to start
            await asyncio.wait_for(slow_started.wait(), timeout=3.0)
            # Let enough time pass for a second tick interval to fire
            await asyncio.sleep(1.5)
            # Release the slow tick
            slow_done.set()
            # Let things settle
            await asyncio.sleep(0.2)
        finally:
            await runner.stop()

    assert not concurrent_detected.is_set(), "Two DAG ticks ran concurrently"


# ---------------------------------------------------------------------------
# Test c: hung orchestrator tick is timed out and loop keeps going
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dag_tick_timeout_continues_loop():
    """A timed-out dag_orchestrator.tick() must not wedge the DAG loop."""
    settings = _make_settings(dag_tick_interval=1, dag_tick_timeout=1)

    call_count = 0
    second_call = asyncio.Event()

    async def slow_then_fast():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            await asyncio.sleep(10)  # will be cancelled by wait_for timeout
        else:
            second_call.set()

    dag_orchestrator = MagicMock()
    dag_orchestrator.tick = AsyncMock(side_effect=slow_then_fast)

    runner = _make_runner(settings, dag_orchestrator)

    with patch.object(runner, "_detect_missed_checks", AsyncMock()):
        await runner.start()
        try:
            # After the first tick times out, the loop should continue
            # and the second tick should fire within a few seconds
            await asyncio.wait_for(second_call.wait(), timeout=6.0)
            assert second_call.is_set(), "Loop did not continue after timeout"
        finally:
            await runner.stop()


# ---------------------------------------------------------------------------
# Test d: stop() cancels the DAG loop cleanly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dag_loop_stops_cleanly():
    """stop() must cancel the DAG loop task without errors."""
    settings = _make_settings(dag_tick_interval=10)  # long interval so no tick fires

    dag_orchestrator = MagicMock()
    dag_orchestrator.tick = AsyncMock()

    runner = _make_runner(settings, dag_orchestrator)

    with patch.object(runner, "_detect_missed_checks", AsyncMock()):
        await runner.start()
        assert runner._dag_task is not None
        assert not runner._dag_task.done()

        await runner.stop()

        assert runner._dag_task is None
        assert not runner._running
    # No exception raised — clean stop confirmed


# ---------------------------------------------------------------------------
# Test e: last_dag_tick is recorded after a successful tick
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_last_dag_tick_recorded():
    """last_dag_tick should be set after each successful orchestrator tick."""
    settings = _make_settings(dag_tick_interval=1, dag_tick_timeout=5)

    ticked = asyncio.Event()

    async def dag_tick():
        ticked.set()

    dag_orchestrator = MagicMock()
    dag_orchestrator.tick = AsyncMock(side_effect=dag_tick)

    runner = _make_runner(settings, dag_orchestrator)
    assert runner.last_dag_tick is None

    with patch.object(runner, "_detect_missed_checks", AsyncMock()):
        await runner.start()
        try:
            await asyncio.wait_for(ticked.wait(), timeout=4.0)
            await asyncio.sleep(0.1)  # let the timestamp assignment land
            assert runner.last_dag_tick is not None
            assert isinstance(runner.last_dag_tick, datetime)
        finally:
            await runner.stop()


# ---------------------------------------------------------------------------
# Test f: DAG tick fires even during heartbeat quiet hours
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dag_tick_fires_during_quiet_hours():
    """DAG loop must not be gated by quiet hours."""
    settings = _make_settings(
        dag_tick_interval=1,
        # Force quiet hours to cover the entire day
        heartbeat_quiet_start=0,
        heartbeat_quiet_end=23,
    )

    ticked = asyncio.Event()

    async def dag_tick():
        ticked.set()

    dag_orchestrator = MagicMock()
    dag_orchestrator.tick = AsyncMock(side_effect=dag_tick)

    runner = _make_runner(settings, dag_orchestrator)

    with patch.object(runner, "_detect_missed_checks", AsyncMock()):
        # Override _in_quiet_hours to always return True
        with patch.object(runner, "_in_quiet_hours", return_value=True):
            await runner.start()
            try:
                await asyncio.wait_for(ticked.wait(), timeout=4.0)
                assert ticked.is_set(), "DAG tick did not fire during quiet hours"
            finally:
                await runner.stop()


# ---------------------------------------------------------------------------
# Test g: DAG tick is skipped (not called) when orchestrator is None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dag_tick_skipped_when_no_orchestrator():
    """When dag_orchestrator is None the loop must not error or call tick."""
    settings = _make_settings(dag_tick_interval=1)

    runner = _make_runner(settings, dag_orchestrator=None)
    assert runner.dag_orchestrator is None

    with patch.object(runner, "_detect_missed_checks", AsyncMock()):
        await runner.start()
        try:
            # Let two tick intervals pass
            await asyncio.sleep(2.5)
            # No exception raised and loop is still running
            assert runner._dag_task is not None
            assert not runner._dag_task.done()
        finally:
            await runner.stop()
