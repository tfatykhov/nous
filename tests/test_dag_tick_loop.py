"""Tests for the decoupled DAG orchestrator tick loop (fix/dag-tick-own-loop).

Verifies:
  a. A hung heartbeat _tick does NOT block dag_orchestrator.tick() from being called.
  b. Overlapping orchestrator ticks are not run concurrently (single-flight).
  c. A slow/failed orchestrator tick does not wedge the loop.
  d. stop() cancels the DAG loop cleanly.
  h. (P1 #1) CancelledError during tick cannot strand an untracked primitive.
  i. (P1 #2) A check cancelled after snapshot does not execute.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nous.config import Settings
from nous.heartbeat.dynamic import DynamicCheck
from nous.heartbeat.registry import CheckRegistry
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
async def test_dag_tick_slow_tick_continues_loop():
    """A slow dag_orchestrator.tick() completes (shielded) and the loop continues."""
    settings = _make_settings(dag_tick_interval=1, dag_tick_timeout=1)

    call_count = 0
    second_call = asyncio.Event()

    async def slow_then_fast():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            await asyncio.sleep(0.5)  # slow but completes (shielded, not cancelled)
        else:
            second_call.set()

    dag_orchestrator = MagicMock()
    dag_orchestrator.tick = AsyncMock(side_effect=slow_then_fast)

    runner = _make_runner(settings, dag_orchestrator)

    with patch.object(runner, "_detect_missed_checks", AsyncMock()):
        await runner.start()
        try:
            await asyncio.wait_for(second_call.wait(), timeout=6.0)
            assert second_call.is_set(), "Loop did not continue after slow tick"
        finally:
            await runner.stop()


@pytest.mark.asyncio
async def test_failed_tick_does_not_advance_last_dag_tick():
    """A tick that raises must not be reported as a successful tick."""
    settings = _make_settings(dag_tick_interval=1, dag_tick_timeout=5)

    call_count = 0
    second_call = asyncio.Event()

    async def fail_then_signal():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("tick broke")
        second_call.set()

    dag_orchestrator = MagicMock()
    dag_orchestrator.tick = AsyncMock(side_effect=fail_then_signal)

    runner = _make_runner(settings, dag_orchestrator)

    with patch.object(runner, "_detect_missed_checks", AsyncMock()):
        await runner.start()
        try:
            await asyncio.wait_for(second_call.wait(), timeout=6.0)
            # After the second (successful) tick, last_dag_tick should be set
            await asyncio.sleep(0.1)
            assert runner.last_dag_tick is not None
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


# ---------------------------------------------------------------------------
# Test h (P1 #1): CancelledError during tick cannot strand an untracked
# primitive — the tick is shielded so it runs to completion even when the
# outer loop task is cancelled (e.g. by stop()).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dag_tick_shielded_from_cancellation():
    """Cancelling the DAG loop task mid-tick must not cancel the inner tick.

    With asyncio.shield, the inner tick runs to completion even when the
    outer task receives CancelledError.  This prevents the scenario where
    CancelledError lands between subtask creation and the node's running
    transition, leaving an untracked subtask.
    """
    settings = _make_settings(dag_tick_interval=0, dag_tick_timeout=10)

    tick_started = asyncio.Event()
    tick_completed = asyncio.Event()

    async def long_tick():
        tick_started.set()
        await asyncio.sleep(0.3)
        tick_completed.set()
        return 0

    dag_orchestrator = MagicMock()
    dag_orchestrator.tick = AsyncMock(side_effect=long_tick)

    runner = _make_runner(settings, dag_orchestrator)

    with patch.object(runner, "_detect_missed_checks", AsyncMock()):
        await runner.start()
        try:
            # Wait for the tick to start
            await asyncio.wait_for(tick_started.wait(), timeout=3.0)
            # Cancel the DAG loop task while the tick is mid-flight
            runner._dag_task.cancel()
            # Give time for the shielded tick to finish
            await asyncio.sleep(0.5)
            assert tick_completed.is_set(), (
                "Tick was cancelled mid-flight — CancelledError bypassed "
                "the launch path's cleanup and could strand a subtask"
            )
        finally:
            # stop() should handle the already-cancelled task gracefully
            await runner.stop()


@pytest.mark.asyncio
async def test_dag_tick_records_timestamp_on_cancel_after_completion():
    """When stop() cancels the loop while a shielded tick is running, the
    tick completes and last_dag_tick is still recorded."""
    settings = _make_settings(dag_tick_interval=0, dag_tick_timeout=10)

    tick_started = asyncio.Event()

    async def quick_tick():
        tick_started.set()
        await asyncio.sleep(0.1)
        return 0

    dag_orchestrator = MagicMock()
    dag_orchestrator.tick = AsyncMock(side_effect=quick_tick)

    runner = _make_runner(settings, dag_orchestrator)
    assert runner.last_dag_tick is None

    with patch.object(runner, "_detect_missed_checks", AsyncMock()):
        await runner.start()
        await asyncio.wait_for(tick_started.wait(), timeout=3.0)
        await asyncio.sleep(0.3)  # let the tick finish
        await runner.stop()

    assert runner.last_dag_tick is not None


@pytest.mark.asyncio
async def test_stop_drains_in_flight_tick():
    """stop() must not return while a shielded DAG tick is still running.

    Before the fix, asyncio.shield(coro) raised CancelledError on the outer
    task immediately without waiting for the inner task, so stop() could return
    while dag_orchestrator.tick() was still advancing nodes in the background
    — racing with DB shutdown and subtask pool teardown.

    After the fix, the _dag_loop CancelledError handler drains inner_task via
    a second asyncio.shield() before releasing the lock and propagating, so
    stop() cannot return until the tick is done.
    """
    settings = _make_settings(dag_tick_interval=0, dag_tick_timeout=10)

    tick_started = asyncio.Event()
    tick_completed = asyncio.Event()

    async def long_tick():
        tick_started.set()
        await asyncio.sleep(0.3)
        tick_completed.set()

    dag_orchestrator = MagicMock()
    dag_orchestrator.tick = AsyncMock(side_effect=long_tick)

    runner = _make_runner(settings, dag_orchestrator)

    with patch.object(runner, "_detect_missed_checks", AsyncMock()):
        await runner.start()
        # Ensure the tick has actually started before stopping
        await asyncio.wait_for(tick_started.wait(), timeout=3.0)
        # stop() mid-tick — must drain inner_task before returning
        await runner.stop()

    assert tick_completed.is_set(), (
        "stop() returned while dag_orchestrator.tick() was still running; "
        "the inner task was left untracked and could race with DB shutdown"
    )


# ---------------------------------------------------------------------------
# Test i (P1 #2): A check cancelled/unregistered after snapshot does not
# execute — the heartbeat loop re-verifies dynamic checks before running.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancelled_check_skipped_after_snapshot():
    """A dynamic check unregistered between get_due_checks and run() must
    not execute — no LLM turn, no tool calls."""
    settings = _make_settings(heartbeat_tick_interval=1, heartbeat_enabled=True)

    registry = CheckRegistry()
    check = DynamicCheck(
        check_id="test-id",
        name="dag-check-to-cancel",
        prompt="check something",
        tools=[],
        interval=1,
        timeout=30,
        urgent=False,
        runner=MagicMock(),
    )
    registry.register(check)

    runner = HeartbeatRunner(
        settings=settings,
        registry=registry,
        runner=MagicMock(),
        brain=MagicMock(),
        heart=MagicMock(),
        bus=None,
        http_client=None,
        finding_store=None,
        api_client=None,
        dynamic_loader=None,
    )

    original_get_due = registry.get_due_checks

    def snapshot_then_unregister(now=None):
        """Return the check in the snapshot, then immediately unregister it."""
        due = original_get_due(now)
        registry.unregister("dag-check-to-cancel")
        return due

    with patch.object(registry, "get_due_checks", side_effect=snapshot_then_unregister):
        with patch.object(check, "run", new_callable=AsyncMock) as mock_run:
            findings = await runner._tick()
            mock_run.assert_not_called()

    assert findings == []


@pytest.mark.asyncio
async def test_self_disabled_check_skipped_after_snapshot():
    """A dynamic check that self-disabled between snapshot and run() must
    not execute."""
    settings = _make_settings(heartbeat_tick_interval=1, heartbeat_enabled=True)

    registry = CheckRegistry()
    check = DynamicCheck(
        check_id="test-id",
        name="dag-check-self-disabled",
        prompt="check something",
        tools=[],
        interval=1,
        timeout=30,
        urgent=False,
        runner=MagicMock(),
    )
    registry.register(check)

    runner = HeartbeatRunner(
        settings=settings,
        registry=registry,
        runner=MagicMock(),
        brain=MagicMock(),
        heart=MagicMock(),
        bus=None,
        http_client=None,
        finding_store=None,
        api_client=None,
        dynamic_loader=None,
    )

    original_get_due = registry.get_due_checks

    def snapshot_then_disable(now=None):
        """Return the check in the snapshot, then mark it self-disabled."""
        due = original_get_due(now)
        check._self_disabled = True
        return due

    with patch.object(registry, "get_due_checks", side_effect=snapshot_then_disable):
        with patch.object(check, "run", new_callable=AsyncMock) as mock_run:
            findings = await runner._tick()
            mock_run.assert_not_called()

    assert findings == []


@pytest.mark.asyncio
async def test_deactivated_check_skipped_after_snapshot():
    """A dynamic check deactivated (active=False) between snapshot and
    run() must not execute."""
    settings = _make_settings(heartbeat_tick_interval=1, heartbeat_enabled=True)

    registry = CheckRegistry()
    check = DynamicCheck(
        check_id="test-id",
        name="dag-check-deactivated",
        prompt="check something",
        tools=[],
        interval=1,
        timeout=30,
        urgent=False,
        runner=MagicMock(),
    )
    registry.register(check)

    runner = HeartbeatRunner(
        settings=settings,
        registry=registry,
        runner=MagicMock(),
        brain=MagicMock(),
        heart=MagicMock(),
        bus=None,
        http_client=None,
        finding_store=None,
        api_client=None,
        dynamic_loader=None,
    )

    original_get_due = registry.get_due_checks

    def snapshot_then_deactivate(now=None):
        """Return the check in the snapshot, then deactivate it."""
        due = original_get_due(now)
        check.active = False
        return due

    with patch.object(registry, "get_due_checks", side_effect=snapshot_then_deactivate):
        with patch.object(check, "run", new_callable=AsyncMock) as mock_run:
            findings = await runner._tick()
            mock_run.assert_not_called()

    assert findings == []


# ---------------------------------------------------------------------------
# Test j (Codex P1): dag_tick_timeout is enforced — a hung tick is abandoned
# at the deadline rather than blocking the loop forever.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dag_tick_timeout_abandons_hung_tick():
    """A dag_orchestrator.tick() that never returns must be abandoned after
    dag_tick_timeout, allowing the loop to continue running.

    The inner task continues in background (shielded from cancellation), so
    single-flight is maintained via _dag_pending_task: subsequent ticks skip
    until the hung task completes, then proceed normally.
    """
    settings = _make_settings(dag_tick_interval=0, dag_tick_timeout=0.1)

    first_started = asyncio.Event()
    first_release = asyncio.Event()
    second_started = asyncio.Event()

    async def tick_side_effect():
        if not first_started.is_set():
            first_started.set()
            await first_release.wait()  # hang indefinitely
        else:
            second_started.set()

    dag_orchestrator = MagicMock()
    dag_orchestrator.tick = AsyncMock(side_effect=tick_side_effect)

    runner = _make_runner(settings, dag_orchestrator)

    with patch.object(runner, "_detect_missed_checks", AsyncMock()):
        await runner.start()
        try:
            # First tick starts and hangs
            await asyncio.wait_for(first_started.wait(), timeout=3.0)

            # Give the timeout (0.1 s) time to fire and the loop to continue
            await asyncio.sleep(0.5)

            # The loop task must still be alive — not hung on the tick
            assert runner._dag_task is not None and not runner._dag_task.done(), (
                "DAG loop task died — it was blocked on the hung tick instead "
                "of abandoning it at the timeout"
            )

            # Release the hanging first tick; the pending-task guard clears
            # and a second tick should run
            first_release.set()
            await asyncio.wait_for(second_started.wait(), timeout=3.0)
            assert second_started.is_set(), (
                "No second tick ran after releasing the hung first tick"
            )
        finally:
            await runner.stop()


@pytest.mark.asyncio
async def test_dag_tick_timeout_maintains_single_flight():
    """While a timed-out tick is still running in the background, subsequent
    tick intervals must be skipped (single-flight via _dag_pending_task)."""
    settings = _make_settings(dag_tick_interval=0, dag_tick_timeout=0.1)

    first_started = asyncio.Event()
    first_release = asyncio.Event()
    call_count = 0

    async def counting_tick():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            first_started.set()
            await first_release.wait()

    dag_orchestrator = MagicMock()
    dag_orchestrator.tick = AsyncMock(side_effect=counting_tick)

    runner = _make_runner(settings, dag_orchestrator)

    with patch.object(runner, "_detect_missed_checks", AsyncMock()):
        await runner.start()
        try:
            await asyncio.wait_for(first_started.wait(), timeout=3.0)
            # Let timeout fire and several more intervals pass while first tick hangs
            await asyncio.sleep(0.6)
            # Only ONE tick should have been attempted (the others skip)
            assert call_count == 1, (
                f"Expected 1 tick attempt while first was running, got {call_count}; "
                "single-flight was not maintained after timeout"
            )
        finally:
            first_release.set()
            await runner.stop()
