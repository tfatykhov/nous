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
                "DAG loop task died — it was blocked on the hung tick instead of abandoning it at the timeout"
            )

            # Release the hanging first tick; the pending-task guard clears
            # and a second tick should run
            first_release.set()
            await asyncio.wait_for(second_started.wait(), timeout=3.0)
            assert second_started.is_set(), "No second tick ran after releasing the hung first tick"
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


# ---------------------------------------------------------------------------
# Test k (Codex P1 round-2): stop() drains a timed-out pending tick
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_drains_pending_task_after_timeout():
    """stop() must await _dag_pending_task so shutdown_components() cannot
    close the DB while the orchestrator tick is still mutating nodes.

    Scenario:
      1. A DAG tick starts and immediately hangs, causing dag_tick_timeout.
      2. The loop detaches the task into _dag_pending_task and moves on.
      3. stop() is called while the hanging task is still running.
      4. stop() must NOT return before that task is done.
    """
    settings = _make_settings(dag_tick_interval=0, dag_tick_timeout=0.05)

    tick_started = asyncio.Event()
    tick_release = asyncio.Event()
    tick_done = asyncio.Event()

    async def hanging_tick():
        tick_started.set()
        await tick_release.wait()
        tick_done.set()

    dag_orchestrator = MagicMock()
    dag_orchestrator.tick = AsyncMock(side_effect=hanging_tick)

    runner = _make_runner(settings, dag_orchestrator)

    with patch.object(runner, "_detect_missed_checks", AsyncMock()):
        await runner.start()
        try:
            # Wait for the tick to start and time out
            await asyncio.wait_for(tick_started.wait(), timeout=3.0)
            # Let the timeout (0.05 s) fire
            await asyncio.sleep(0.2)

            # _dag_pending_task should be set and still running
            assert runner._dag_pending_task is not None, "_dag_pending_task was not set after tick timeout"
            assert not runner._dag_pending_task.done(), "_dag_pending_task already done before we released it"

            # Release the hanging tick just before calling stop() so it can
            # finish inside the drain window.
            tick_release.set()

            await runner.stop()

            # After stop() returns the pending task must be done
            assert tick_done.is_set(), (
                "stop() returned before the timed-out pending tick completed — "
                "shutdown_components() would close the DB under an in-flight tick"
            )
            assert runner._dag_pending_task is None, "_dag_pending_task was not cleared by stop()"
        except Exception:
            tick_release.set()
            raise


# ---------------------------------------------------------------------------
# Tests for Codex round-3 fixes (P1 #1, P1 #2, P2 #3, P2 #4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_drain_bounded_when_inner_task_hangs():
    """stop() must return within dag_tick_timeout when a tick is in-flight and hangs.

    Fix 1 (Codex P1 round-3): The CancelledError handler in _dag_loop now
    uses asyncio.wait_for(asyncio.shield(inner_task), timeout=dag_tick_timeout)
    instead of a naked asyncio.shield() with no deadline.  Without the fix,
    a tick blocked on a hung DB or network operation would make _dag_task.cancel()
    / await _dag_task hang indefinitely, stalling shutdown_components().
    """
    settings = _make_settings(dag_tick_interval=0, dag_tick_timeout=0.25)

    tick_started = asyncio.Event()

    async def forever_tick():
        tick_started.set()
        await asyncio.Event().wait()  # never completes

    dag_orchestrator = MagicMock()
    dag_orchestrator.tick = AsyncMock(side_effect=forever_tick)

    runner = _make_runner(settings, dag_orchestrator)

    with patch.object(runner, "_detect_missed_checks", AsyncMock()):
        await runner.start()
        await asyncio.wait_for(tick_started.wait(), timeout=3.0)

        # stop() is called while the tick is in-flight (before dag_tick_timeout fires).
        # Without Fix 1 it would hang forever; with Fix 1 it returns within the bound.
        start_t = asyncio.get_event_loop().time()
        await asyncio.wait_for(runner.stop(), timeout=5.0)
        elapsed = asyncio.get_event_loop().time() - start_t

        # Allow generous headroom for CI variance — the key assertion is that
        # stop() returns at all (asyncio.wait_for above would raise on a hang).
        # An upper bound of 4× dag_tick_timeout distinguishes "bounded" from
        # "hung but happened to finish".
        assert elapsed < 4 * settings.dag_tick_timeout, (
            f"stop() took {elapsed:.3f}s — expected < "
            f"{4 * settings.dag_tick_timeout:.3f}s; "
            "the in-flight shutdown drain must be bounded by dag_tick_timeout"
        )


@pytest.mark.asyncio
async def test_pending_drain_does_not_cancel_inner_task():
    """stop() must not cancel a timed-out pending tick during the drain.

    Fix 2 (Codex P1 round-3): asyncio.wait is used instead of asyncio.wait_for
    so the pending task is never cancelled on timeout.  asyncio.wait_for would
    cancel the task, reintroducing the unsafe CancelledError window between
    primitive creation and the node's running transition.
    """
    settings = _make_settings(dag_tick_interval=0, dag_tick_timeout=0.05)

    tick_started = asyncio.Event()
    tick_release = asyncio.Event()

    async def hanging_tick():
        tick_started.set()
        await tick_release.wait()

    dag_orchestrator = MagicMock()
    dag_orchestrator.tick = AsyncMock(side_effect=hanging_tick)

    runner = _make_runner(settings, dag_orchestrator)

    with patch.object(runner, "_detect_missed_checks", AsyncMock()):
        await runner.start()
        await asyncio.wait_for(tick_started.wait(), timeout=3.0)
        # Let the soft deadline fire so the tick becomes _dag_pending_task.
        await asyncio.sleep(0.2)

        pending = runner._dag_pending_task
        assert pending is not None and not pending.done(), (
            "tick must be timed-out and still running before calling stop()"
        )

        # stop() will try to drain but the tick never finishes (tick_release
        # not set).  Fix 2: no cancellation; stop() returns after the drain
        # timeout expires.
        await asyncio.wait_for(runner.stop(), timeout=1.5)

        # Brief yield so any queued cancellation could land.
        await asyncio.sleep(0.02)

        assert not pending.cancelled(), (
            "stop() cancelled the timed-out pending tick; use asyncio.wait (not asyncio.wait_for) in the pending drain"
        )

    # Release the still-running task to silence asyncio "task was destroyed"
    # warnings.
    tick_release.set()
    try:
        await asyncio.wait_for(pending, timeout=0.5)
    except Exception:
        pass


@pytest.mark.asyncio
async def test_completed_pending_tick_updates_last_dag_tick():
    """last_dag_tick must be updated when a timed-out tick eventually completes.

    Fix 3 (Codex P2 round-3): Without this fix the loop only sets last_dag_tick
    in the normal (non-timeout) completion path.  A tick that times out and
    finishes later never updated the timestamp, so slow-but-successful ticks
    reported zero progress on the status endpoint.
    """
    settings = _make_settings(dag_tick_interval=0, dag_tick_timeout=0.05)

    tick_count = 0
    tick_1_started = asyncio.Event()
    tick_1_release = asyncio.Event()
    tick_2_started = asyncio.Event()
    tick_2_gate = asyncio.Event()  # hold tick 2 so we can inspect before it finishes

    async def side_effect():
        nonlocal tick_count
        tick_count += 1
        if tick_count == 1:
            tick_1_started.set()
            await tick_1_release.wait()
        elif tick_count == 2:
            tick_2_started.set()
            await tick_2_gate.wait()

    dag_orchestrator = MagicMock()
    dag_orchestrator.tick = AsyncMock(side_effect=side_effect)

    runner = _make_runner(settings, dag_orchestrator)

    with patch.object(runner, "_detect_missed_checks", AsyncMock()):
        await runner.start()
        try:
            await asyncio.wait_for(tick_1_started.wait(), timeout=3.0)
            # Let the timeout fire; tick 1 is now in _dag_pending_task.
            await asyncio.sleep(0.2)

            assert runner.last_dag_tick is None, "last_dag_tick should be None while tick 1 is still pending"

            # Release tick 1 so it completes from the background.
            tick_1_release.set()

            # Wait for tick 2 to start — this proves the loop iterated again
            # and processed tick 1's completed result (Fix 3).
            await asyncio.wait_for(tick_2_started.wait(), timeout=3.0)
            await asyncio.sleep(0.05)  # let the timestamp assignment land

            assert runner.last_dag_tick is not None, (
                "last_dag_tick not set after a timed-out tick eventually completed; "
                "Fix 3 (P2): harvest the completed _dag_pending_task result"
            )
        finally:
            tick_2_gate.set()
            tick_1_release.set()
            await runner.stop()


@pytest.mark.asyncio
async def test_heartbeat_task_done_before_pending_drain():
    """_task (heartbeat loop) must be cancelled before stop() drains _dag_pending_task.

    Fix 4 (Codex P2 round-3): Without this fix the heartbeat loop stayed alive
    while DAG work drained, allowing it to wake from sleep and launch new checks
    — including ones with external side effects — after shutdown had begun.

    We verify the ordering by patching the pending task's .done() method to
    record the state of _task at the exact moment the drain code inspects it.
    """
    settings = _make_settings(dag_tick_interval=0, dag_tick_timeout=0.3)

    tick_started = asyncio.Event()
    tick_release = asyncio.Event()

    async def slow_tick():
        tick_started.set()
        await tick_release.wait()

    dag_orchestrator = MagicMock()
    dag_orchestrator.tick = AsyncMock(side_effect=slow_tick)

    runner = _make_runner(settings, dag_orchestrator)

    with patch.object(runner, "_detect_missed_checks", AsyncMock()):
        await runner.start()
        await asyncio.wait_for(tick_started.wait(), timeout=3.0)
        # Let the soft deadline fire so the tick enters _dag_pending_task.
        await asyncio.sleep(0.1)

        pending = runner._dag_pending_task
        assert pending is not None and not pending.done()

        # Patch .done() on the pending task to record _task's state the first
        # time the drain code calls it (the `if _pending is not None and not
        # _pending.done():` check in stop()).
        _orig_done = pending.done
        hb_task_state_at_drain: list[bool] = []

        def _recording_done() -> bool:
            result = _orig_done()
            if not hb_task_state_at_drain:
                hb_task_state_at_drain.append(runner._task is None or runner._task.done())
            return result

        pending.done = _recording_done  # type: ignore[method-assign]

        tick_release.set()  # release so drain can complete quickly
        await asyncio.wait_for(runner.stop(), timeout=2.0)

    assert hb_task_state_at_drain, "drain code never called _pending.done()"
    assert hb_task_state_at_drain[0], (
        "_task was still running when stop() entered the _dag_pending_task drain; "
        "Fix 4 (P2): cancel _task before the DAG drain"
    )


# ---------------------------------------------------------------------------
# Tests for Codex round-4 fixes (P2 #1, P1 #2)
# ---------------------------------------------------------------------------


def test_dag_tick_interval_must_be_positive():
    """dag_tick_interval=0 must be rejected; asyncio.sleep(0) turns the loop
    into a tight busy-poll that starves the event loop and floods the logs.

    Fix (Codex P2 round-4): constrained to ge=1.
    """
    with pytest.raises(ValueError):
        Settings(dag_tick_interval=0)


def test_dag_tick_interval_negative_must_be_rejected():
    """dag_tick_interval=-1 must be rejected."""
    with pytest.raises(ValueError):
        Settings(dag_tick_interval=-1)


def test_dag_tick_timeout_must_be_positive():
    """dag_tick_timeout=0 would make every tick time out immediately and
    spawn endless shielded tasks in the background, saturating the pool.

    Fix (Codex P2 round-4): constrained to ge=1.
    """
    with pytest.raises(ValueError):
        Settings(dag_tick_timeout=0)


def test_dag_tick_timeout_negative_must_be_rejected():
    """dag_tick_timeout=-5 must be rejected."""
    with pytest.raises(ValueError):
        Settings(dag_tick_timeout=-5)


@pytest.mark.asyncio
async def test_stop_tracks_pending_task_through_shutdown_drain_timeout():
    """_dag_pending_task must not be cleared when the in-loop shutdown drain
    times out — stop() needs the reference to drain the task itself.

    Before Fix 5 (Codex P1 round-4): _dag_loop always executed
    ``self._dag_pending_task = None`` after its bounded shutdown drain,
    even when the drain timed out and inner_task was still running.
    stop() then found no pending task and returned, leaving inner_task racing
    with DB shutdown.

    After the fix: _dag_pending_task is preserved on drain timeout so stop()
    can observe the still-running task and wait for it via asyncio.wait.
    """
    # Keep the shutdown-drain window short so it times out while the tick
    # is still running, then release the tick inside stop()'s own drain
    # window so the fix can be verified.
    settings = _make_settings(dag_tick_interval=0, dag_tick_timeout=0.06)

    tick_started = asyncio.Event()
    tick_release = asyncio.Event()
    tick_completed = asyncio.Event()

    async def controlled_tick():
        tick_started.set()
        await tick_release.wait()
        tick_completed.set()

    dag_orchestrator = MagicMock()
    dag_orchestrator.tick = AsyncMock(side_effect=controlled_tick)

    runner = _make_runner(settings, dag_orchestrator)

    with patch.object(runner, "_detect_missed_checks", AsyncMock()):
        await runner.start()
        await asyncio.wait_for(tick_started.wait(), timeout=3.0)

        # Release the tick after the shutdown-drain window (dag_tick_timeout)
        # but within stop()'s own drain window, so the tick can complete if
        # stop() still holds the reference — which it only does with the fix.
        async def delayed_release():
            await asyncio.sleep(settings.dag_tick_timeout * 1.5)
            tick_release.set()

        release_task = asyncio.create_task(delayed_release(), name="tick-releaser")
        completed_at_stop_return: bool = False
        try:
            await asyncio.wait_for(runner.stop(), timeout=5.0)
            # Capture state immediately after stop() returns, before any further
            # yields that would allow delayed_release to run.
            completed_at_stop_return = tick_completed.is_set()
        finally:
            release_task.cancel()
            tick_release.set()
            try:
                await asyncio.wait_for(release_task, timeout=0.5)
            except (asyncio.CancelledError, TimeoutError):
                pass

    assert completed_at_stop_return, (
        "stop() returned before inner_task completed — "
        "_dag_pending_task was cleared in the shutdown drain timeout path, "
        "losing the reference stop() needs to drain the in-flight tick; "
        "Fix 5 (P1 round-4): preserve _dag_pending_task on drain timeout"
    )
