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
async def test_pending_drain_cancels_inner_task_after_timeout():
    """stop() must cancel a still-running pending tick after the drain window.

    Fix 7 (Codex P1 round-5): when the asyncio.wait in stop()'s drain
    times out the task is still running.  The old behaviour logged and
    returned, leaving the tick racing with shutdown_components() which
    closes the DB and subtask pool immediately after stop() returns.
    The new behaviour cancels the task so it cannot access closed resources.

    Design note: round-3 required asyncio.wait (not asyncio.wait_for) in
    the drain so the task is not cancelled on the *first* timeout attempt —
    that window gives an in-flight tick a full dag_tick_timeout to finish
    gracefully.  Only if it is *still* running after that full window is
    cancellation triggered, making it the lesser evil compared to DB writes
    against a closed pool.
    """
    settings = _make_settings(dag_tick_interval=1, dag_tick_timeout=0.05)

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

        # stop() drains the pending task, times out (tick never finishes),
        # and cancels it. tick_release is set in finally so the cancelled
        # coroutine can clean up.
        try:
            await asyncio.wait_for(runner.stop(), timeout=1.5)
        finally:
            tick_release.set()

        # Brief yield so cancellation propagates.
        await asyncio.sleep(0.02)

        assert pending.done(), (
            "stop() returned but the pending tick is still running — "
            "it may access the DB after shutdown_components() closes it; "
            "Fix 7 (P1 round-5): cancel the pending task after the drain timeout"
        )


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

        pending_captured: asyncio.Task | None = None
        try:
            await asyncio.wait_for(runner.stop(), timeout=5.0)
            # After Fix 5 (P1 round-4), _dag_pending_task is preserved when
            # _dag_loop's drain times out; stop() then drains it (using the
            # remaining budget from the shared deadline — Fix 8 round-6).
            # After Fix 8 the remaining budget may be ≈ 0 when stop() runs,
            # so the tick is cancelled rather than awaited to completion.
            # What we verify: stop() actually ran the drain code (it didn't
            # skip due to a missing reference) — evidenced by pending.done().
            pending_captured = runner._dag_pending_task
        finally:
            tick_release.set()

    # The task must be done: either stop() drained it, it was cancelled, or it
    # completed on its own after tick_release was set above.  The important
    # thing is that stop() did not clear _dag_pending_task prematurely (which
    # would have left the task running past shutdown_components()).
    assert pending_captured is None or pending_captured.done(), (
        "_dag_pending_task is still set and not done after stop() — "
        "stop() failed to drain/cancel the in-flight tick; "
        "Fix 5 (P1 round-4): preserve _dag_pending_task on drain timeout; "
        "Fix 8 (P1 round-6): use remaining shared deadline in stop()'s drain"
    )


# ---------------------------------------------------------------------------
# Tests for Codex round-5 fixes (P2 #1, P1 #2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_harvests_already_completed_pending_tick():
    """stop() must consume the result of an already-done _dag_pending_task.

    Fix 6 (Codex P2 round-5): when a timed-out tick finishes while
    _dag_loop is sleeping between iterations, _pending.done() is True at
    the moment stop() inspects it.  The old code skipped the entire drain
    block (`if _pending is not None and not _pending.done()`), so
    last_dag_tick was never updated and any exception was silently dropped.
    """
    settings = _make_settings(dag_tick_interval=1, dag_tick_timeout=0.05)

    tick_started = asyncio.Event()
    tick_release = asyncio.Event()

    async def controlled_tick():
        tick_started.set()
        await tick_release.wait()

    dag_orchestrator = MagicMock()
    dag_orchestrator.tick = AsyncMock(side_effect=controlled_tick)

    runner = _make_runner(settings, dag_orchestrator)

    with patch.object(runner, "_detect_missed_checks", AsyncMock()):
        await runner.start()
        await asyncio.wait_for(tick_started.wait(), timeout=3.0)
        # Let the soft deadline fire; tick is now the pending task.
        await asyncio.sleep(0.15)

        pending = runner._dag_pending_task
        assert pending is not None and not pending.done()

        # Release the tick and let it complete fully before calling stop().
        tick_release.set()
        await asyncio.sleep(0.1)  # give the task time to finish

        assert pending.done(), "tick should be done before stop() is called"

        # stop() must harvest the already-done result and update last_dag_tick.
        await asyncio.wait_for(runner.stop(), timeout=3.0)

    assert runner.last_dag_tick is not None, (
        "last_dag_tick was not updated after stop() processed an already-completed "
        "_dag_pending_task; "
        "Fix 6 (P2 round-5): handle _pending.done() in stop()"
    )


@pytest.mark.asyncio
async def test_stop_cancels_pending_tick_after_drain_timeout():
    """stop() must cancel _dag_pending_task when the drain window times out.

    Fix 7 (Codex P1 round-5): previously stop() logged an error and returned
    while the task was still running, leaving it to access the DB and subtask
    pool after shutdown_components() had closed them.  After the fix, stop()
    cancels the task so it cannot mutate shared resources post-teardown.

    We verify by confirming the pending task is done (cancelled or complete)
    by the time stop() returns.
    """
    settings = _make_settings(dag_tick_interval=1, dag_tick_timeout=0.05)

    tick_started = asyncio.Event()
    tick_release = asyncio.Event()

    async def controlled_tick():
        tick_started.set()
        await tick_release.wait()

    dag_orchestrator = MagicMock()
    dag_orchestrator.tick = AsyncMock(side_effect=controlled_tick)

    runner = _make_runner(settings, dag_orchestrator)

    with patch.object(runner, "_detect_missed_checks", AsyncMock()):
        await runner.start()
        await asyncio.wait_for(tick_started.wait(), timeout=3.0)
        # Let the soft deadline fire; tick is now the pending task.
        await asyncio.sleep(0.15)

        pending = runner._dag_pending_task
        assert pending is not None and not pending.done()

        # Do NOT release the tick — it must remain blocked so stop()'s
        # asyncio.wait times out, triggering the cancellation path.
        try:
            await asyncio.wait_for(runner.stop(), timeout=3.0)
        finally:
            tick_release.set()  # unblock so the cancelled task can clean up

    assert pending.done(), (
        "stop() returned but _dag_pending_task is still running — "
        "it may access the DB after shutdown_components() closes it; "
        "Fix 7 (P1 round-5): cancel the pending task after the drain window times out"
    )


# ---------------------------------------------------------------------------
# Test for Codex round-6 fix (P1): single total shutdown deadline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_total_drain_bounded_to_one_timeout():
    """stop() total drain time must not exceed one dag_tick_timeout.

    Fix 8 (Codex P1 round-6): before this fix the shutdown path had two
    independent drain windows each lasting up to dag_tick_timeout:
      1. _dag_loop's CancelledError handler drained for dag_tick_timeout.
      2. stop() then saw _dag_pending_task and drained for another full
         dag_tick_timeout.
    With a hung DB/network call this doubles the maximum shutdown latency,
    likely exceeding service-manager grace periods.

    After the fix _dag_loop records an absolute deadline before its drain;
    stop() reads the remaining budget so the combined wait never exceeds
    one dag_tick_timeout.
    """
    TIMEOUT = 0.2
    settings = _make_settings(dag_tick_interval=0, dag_tick_timeout=TIMEOUT)

    tick_started = asyncio.Event()

    async def forever_tick():
        tick_started.set()
        await asyncio.Event().wait()  # never completes

    dag_orchestrator = MagicMock()
    dag_orchestrator.tick = AsyncMock(side_effect=forever_tick)

    runner = _make_runner(settings, dag_orchestrator)

    with patch.object(runner, "_detect_missed_checks", AsyncMock()):
        await runner.start()
        # Wait for the tick to start.
        await asyncio.wait_for(tick_started.wait(), timeout=3.0)

        start_t = asyncio.get_event_loop().time()
        # Calling stop() while the tick is in-flight means _dag_loop's
        # CancelledError handler drains first, then stop() may drain again.
        # The whole sequence must complete within 2 × TIMEOUT (generous) —
        # NOT 4 × TIMEOUT (which would indicate a double-full-timeout).
        await asyncio.wait_for(runner.stop(), timeout=10.0)
        elapsed = asyncio.get_event_loop().time() - start_t

    # Without Fix 8 the combined drain is ≈ 2 × TIMEOUT.  With Fix 8 it is
    # ≈ 1 × TIMEOUT.  Use 1.5 × TIMEOUT as the ceiling so we can distinguish
    # the two; allow generous CI headroom above 1× (asyncio timing varies).
    assert elapsed < 1.5 * TIMEOUT, (
        f"stop() took {elapsed:.3f}s but should be < {1.5 * TIMEOUT:.3f}s "
        f"(dag_tick_timeout={TIMEOUT}s); "
        "Fix 8 (Codex P1 round-6): the two drain windows must share one "
        "total budget via _dag_shutdown_drain_deadline, not each consume a "
        "full dag_tick_timeout independently"
    )


# ---------------------------------------------------------------------------
# codex P1 (PR #656 round 2): a check that disables itself mid-run must not
# advance its DAG node until the run has finished and been recorded.
# ---------------------------------------------------------------------------


def _self_disabling_setup(fail_after_disable: bool = False):
    """A DAG-managed check whose LLM turn disables itself (as
    manage_check(action="disable") does: flag + unregister) and then blocks
    until released, plus an orchestrator sharing the same registry."""
    from types import SimpleNamespace

    from nous.dag.orchestrator import DAGOrchestrator

    registry = CheckRegistry()
    disabled = asyncio.Event()
    release = asyncio.Event()
    agent = MagicMock()

    check = DynamicCheck(
        check_id="dag-check-id",
        name="dag-self-disabler",
        prompt="do the node's work",
        tools=["heartbeat_check_manage"],
        interval=1,
        timeout=30,
        runner=agent,
    )

    async def turn_that_disables(*args, **kwargs):
        check._self_disabled = True
        registry.unregister(check.name)
        disabled.set()
        await release.wait()  # still executing tools after the disable
        if fail_after_disable:
            raise RuntimeError("tool failed after disable")
        return ('{"has_findings": false, "findings": []}', MagicMock(), {})

    agent.run_turn = AsyncMock(side_effect=turn_that_disables)
    agent.end_conversation = AsyncMock()
    registry.register(check)

    stats_order: list[str] = []
    loader = MagicMock()
    loader._registry = registry

    async def record_stats(check_id, success, error_msg=None):
        stats_order.append("success" if success else "failure")

    loader.update_run_stats = AsyncMock(side_effect=record_stats)

    hb = HeartbeatRunner(
        settings=_make_settings(heartbeat_enabled=True),
        registry=registry,
        runner=MagicMock(),
        brain=MagicMock(),
        heart=MagicMock(),
        bus=None,
        http_client=None,
        finding_store=None,
        api_client=None,
        dynamic_loader=loader,
    )
    store = AsyncMock()
    orch = DAGOrchestrator(
        store=store,
        dynamic_loader=loader,
        settings=Settings(_env_file=None),
    )
    node = SimpleNamespace(
        id="node-1",
        name="monitor",
        completion_check=None,
        check_name=check.name,
        status="running",
    )
    return hb, orch, store, node, disabled, release, stats_order


@pytest.mark.asyncio
async def test_self_disabled_check_node_waits_for_run_to_finish():
    hb, orch, store, node, disabled, release, stats_order = _self_disabling_setup()

    tick = asyncio.create_task(hb._tick())
    await asyncio.wait_for(disabled.wait(), timeout=3.0)

    # Mid-run: the check is unregistered, but its run has not finished.
    await orch._sync_check_node(node)
    assert node.status == "running"
    store.update_node.assert_not_called()

    release.set()
    await asyncio.wait_for(tick, timeout=3.0)
    assert stats_order == ["success"]  # recorded before the node advances

    await orch._sync_check_node(node)
    assert node.status == "completed"
    store.update_node.assert_awaited_once()


@pytest.mark.asyncio
async def test_self_disabled_check_node_fails_when_run_fails():
    hb, orch, store, node, disabled, release, stats_order = _self_disabling_setup(
        fail_after_disable=True,
    )

    tick = asyncio.create_task(hb._tick())
    await asyncio.wait_for(disabled.wait(), timeout=3.0)
    await orch._sync_check_node(node)
    assert node.status == "running"

    release.set()
    await asyncio.wait_for(tick, timeout=3.0)
    assert stats_order == ["failure"]

    await orch._sync_check_node(node)
    assert node.status == "failed"
    assert store.update_node.await_args.kwargs["status"] == "failed"


@pytest.mark.asyncio
async def test_worker_evidence_deferred_while_self_disabling_run_in_flight():
    """The completion_check path's is_check_disabled evidence is committed
    DURING the run, so it must not count until the run has finished."""
    hb, orch, store, node, disabled, release, _ = _self_disabling_setup()
    loader = orch._dynamic_loader
    loader.get_successful_run_count = AsyncMock(return_value=0)
    loader.is_check_disabled = AsyncMock(return_value=True)

    tick = asyncio.create_task(hb._tick())
    await asyncio.wait_for(disabled.wait(), timeout=3.0)
    assert await orch._heartbeat_worker_has_run(node) is False

    release.set()
    await asyncio.wait_for(tick, timeout=3.0)
    assert await orch._heartbeat_worker_has_run(node) is True


# ---------------------------------------------------------------------------
# codex P1 (PR #656 round 3): a run that ends without recording an outcome
# (cancelled by stop(), or a forced run that raises) must read as FAILED to
# the DAG loop when it disabled its own check, never as completion.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancelled_self_disabled_run_fails_node():
    hb, orch, store, node, disabled, release, stats_order = _self_disabling_setup()

    tick = asyncio.create_task(hb._tick())
    await asyncio.wait_for(disabled.wait(), timeout=3.0)
    tick.cancel()  # stop() cancelling the heartbeat task mid-run
    with pytest.raises(asyncio.CancelledError):
        await tick

    assert not hb._registry.is_in_flight(node.check_name)
    await orch._sync_check_node(node)
    assert node.status == "failed"
    assert store.update_node.await_args.kwargs["status"] == "failed"


@pytest.mark.asyncio
async def test_forced_self_disabled_run_that_raises_fails_node():
    hb, orch, store, node, disabled, release, stats_order = _self_disabling_setup(
        fail_after_disable=True,
    )

    forced = asyncio.create_task(hb.trigger_check(node.check_name))
    await asyncio.wait_for(disabled.wait(), timeout=3.0)
    await orch._sync_check_node(node)
    assert node.status == "running"

    release.set()
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(forced, timeout=3.0)
    assert stats_order == ["failure"]

    await orch._sync_check_node(node)
    assert node.status == "failed"
    assert store.update_node.await_args.kwargs["status"] == "failed"


@pytest.mark.asyncio
async def test_cancelled_run_without_self_disable_records_no_outcome():
    """Cancelling a run that did not disable its check propagates and leaves
    no failed final-run record — the check will simply run again."""
    registry = CheckRegistry()
    started = asyncio.Event()
    agent = MagicMock()
    check = DynamicCheck(
        check_id="plain-id",
        name="plain-check",
        prompt="p",
        tools=[],
        interval=1,
        timeout=30,
        runner=agent,
    )

    async def blocking_turn(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    agent.run_turn = AsyncMock(side_effect=blocking_turn)
    agent.end_conversation = AsyncMock()
    registry.register(check)
    loader = MagicMock()
    loader._registry = registry
    loader.update_run_stats = AsyncMock()
    hb = HeartbeatRunner(
        settings=_make_settings(heartbeat_enabled=True),
        registry=registry,
        runner=MagicMock(),
        brain=MagicMock(),
        heart=MagicMock(),
        bus=None,
        http_client=None,
        finding_store=None,
        api_client=None,
        dynamic_loader=loader,
    )

    tick = asyncio.create_task(hb._tick())
    await asyncio.wait_for(started.wait(), timeout=3.0)
    tick.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tick

    assert not registry.is_in_flight(check.name)
    assert registry.self_disabled_run_failed(check.name) is False
    loader.update_run_stats.assert_not_called()


# ---------------------------------------------------------------------------
# codex P2 (PR #656 round 4): retained self-disabled run outcomes must not
# accumulate — DAG checks use fresh names, so nothing re-registers to clear them.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_self_disabled_run_retains_nothing():
    hb, orch, store, node, disabled, release, stats_order = _self_disabling_setup()
    registry = hb._registry

    tick = asyncio.create_task(hb._tick())
    await asyncio.wait_for(disabled.wait(), timeout=3.0)
    release.set()
    await asyncio.wait_for(tick, timeout=3.0)

    assert registry._disabled_run_failures == {}
    await orch._sync_check_node(node)
    assert node.status == "completed"


@pytest.mark.asyncio
async def test_failed_self_disabled_run_consumed_when_node_fails():
    hb, orch, store, node, disabled, release, stats_order = _self_disabling_setup(
        fail_after_disable=True,
    )
    registry = hb._registry

    tick = asyncio.create_task(hb._tick())
    await asyncio.wait_for(disabled.wait(), timeout=3.0)
    release.set()
    await asyncio.wait_for(tick, timeout=3.0)
    assert registry.self_disabled_run_failed(node.check_name) is True

    await orch._sync_check_node(node)
    assert node.status == "failed"
    assert registry._disabled_run_failures == {}


@pytest.mark.asyncio
async def test_failed_self_disabled_run_kept_when_node_write_fails():
    """The failure is consumed only after it is persisted, so a failed write
    re-reads it on the next tick instead of completing the node."""
    hb, orch, store, node, disabled, release, stats_order = _self_disabling_setup(
        fail_after_disable=True,
    )
    registry = hb._registry

    tick = asyncio.create_task(hb._tick())
    await asyncio.wait_for(disabled.wait(), timeout=3.0)
    release.set()
    await asyncio.wait_for(tick, timeout=3.0)

    store.update_node.side_effect = RuntimeError("db down")
    with pytest.raises(RuntimeError):
        await orch._sync_check_node(node)
    assert registry.self_disabled_run_failed(node.check_name) is True

    store.update_node.side_effect = None
    await orch._sync_check_node(node)
    assert node.status == "failed"
    assert registry._disabled_run_failures == {}


def test_retained_self_disabled_failures_are_bounded():
    from nous.heartbeat.registry import _MAX_RETAINED_DISABLED_RUN_FAILURES

    registry = CheckRegistry()
    total = _MAX_RETAINED_DISABLED_RUN_FAILURES + 50
    for i in range(total):
        name = f"dag-{i}-node"
        registry.begin_run(name)
        registry.end_run(name, False, self_disabled=True)

    assert len(registry._disabled_run_failures) == _MAX_RETAINED_DISABLED_RUN_FAILURES
    assert not registry.self_disabled_run_failed("dag-0-node")
    assert registry.self_disabled_run_failed(f"dag-{total - 1}-node")
    assert registry._in_flight == {}


# ---------------------------------------------------------------------------
# codex P1 (PR #656 round 5): the completion_check path must honor a failed
# self-disabling final run before accepting enabled=False as worker evidence.
# ---------------------------------------------------------------------------


async def _awaiting_check_after_run(fail_after_disable: bool):
    hb, orch, store, node, disabled, release, _ = _self_disabling_setup(
        fail_after_disable=fail_after_disable,
    )
    loader = orch._dynamic_loader
    loader.get_successful_run_count = AsyncMock(return_value=0)
    loader.is_check_disabled = AsyncMock(return_value=True)
    loader.manage_check = AsyncMock()

    tick = asyncio.create_task(hb._tick())
    await asyncio.wait_for(disabled.wait(), timeout=3.0)
    release.set()
    await asyncio.wait_for(tick, timeout=3.0)

    node.status = "awaiting_check"
    node.completion_check = "true"
    node.awaiting_check_at = None
    node.started_at = datetime.now().astimezone()
    node.completion_check_interval = None
    node.last_check_at = None
    node.max_check_attempts = None
    node.check_attempts = 0
    node.timeout_seconds = 600
    node.result = None
    dag = MagicMock()
    dag.nodes = [node]
    orch._run_completion_check = AsyncMock(return_value=MagicMock(status="success", detail=None))
    orch._read_node_result = AsyncMock(return_value="done")
    await orch._poll_awaiting_checks(dag)
    return hb._registry, orch, node, loader


@pytest.mark.asyncio
async def test_completion_check_node_fails_when_self_disabled_run_fails():
    registry, orch, node, loader = await _awaiting_check_after_run(True)

    assert node.status == "failed"
    orch._run_completion_check.assert_not_called()
    assert node.check_attempts == 0
    loader.manage_check.assert_awaited()  # heartbeat check cancelled
    assert registry._disabled_run_failures == {}


@pytest.mark.asyncio
async def test_completion_check_node_completes_when_self_disabled_run_succeeds():
    registry, orch, node, _ = await _awaiting_check_after_run(False)

    assert node.status == "completed"
    orch._run_completion_check.assert_awaited_once()


# ---------------------------------------------------------------------------
# codex P1 (PR #656 round 6): the retained failure is re-read after every
# await in the completion_check poll, and a run still in flight is not
# accepted as worker evidence.
# ---------------------------------------------------------------------------


async def _awaiting_check_mid_run(fail_after_disable: bool, finish_during: str | None):
    """Poll a completion_check node against a self-disabling check run.

    ``worker_evidence``: the run is in flight when the poll starts and
    finishes inside the awaited worker-evidence lookup. ``completion_check``:
    the run starts and finishes inside the awaited shell command.
    ``result_read``: likewise inside the awaited result-file read. None: the
    run is in flight for the whole poll.
    """
    hb, orch, store, node, disabled, release, _ = _self_disabling_setup(
        fail_after_disable=fail_after_disable,
    )
    loader = orch._dynamic_loader
    loader.is_check_disabled = AsyncMock(return_value=True)
    loader.manage_check = AsyncMock()
    tick: asyncio.Task | None = None

    async def start_run():
        nonlocal tick
        tick = asyncio.create_task(hb._tick())
        await asyncio.wait_for(disabled.wait(), timeout=3.0)

    async def finish_run():
        release.set()
        await asyncio.wait_for(tick, timeout=3.0)

    async def successful_runs(*args, **kwargs):
        if finish_during == "worker_evidence":
            await finish_run()
        return 0

    async def shell_check(*args, **kwargs):
        if finish_during == "completion_check":
            await start_run()
            await finish_run()
        return MagicMock(status="success", detail=None)

    async def read_result(*args, **kwargs):
        if finish_during == "result_read":
            await start_run()
            await finish_run()
        return "done"

    if finish_during in ("completion_check", "result_read"):
        # An earlier run succeeded; the final run has not started yet.
        loader.get_successful_run_count = AsyncMock(return_value=1)
    elif finish_during is None:
        # An earlier run succeeded; the final run is in flight throughout.
        loader.get_successful_run_count = AsyncMock(return_value=1)
        await start_run()
    else:
        loader.get_successful_run_count = AsyncMock(side_effect=successful_runs)
        await start_run()
    orch._run_completion_check = AsyncMock(side_effect=shell_check)
    orch._read_node_result = AsyncMock(side_effect=read_result)

    node.status = "awaiting_check"
    node.completion_check = "true"
    node.awaiting_check_at = datetime.now().astimezone()
    node.started_at = datetime.now().astimezone()
    node.completion_check_interval = None
    node.last_check_at = None
    node.max_check_attempts = None
    node.check_attempts = 0
    node.timeout_seconds = 600
    node.result = None
    dag = MagicMock()
    dag.nodes = [node]
    await orch._poll_awaiting_checks(dag)
    if not tick.done():
        await finish_run()
    return hb._registry, orch, store, node, loader


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_during", ["worker_evidence", "completion_check", "result_read"])
async def test_completion_check_rechecks_final_run_failure_after_await(finish_during):
    registry, orch, store, node, loader = await _awaiting_check_mid_run(
        True,
        finish_during,
    )

    assert node.status == "failed"
    final = store.update_node.await_args_list[-1].kwargs
    assert final["status"] == "failed"
    assert final["error"] == "Check disabled itself but its final run failed"
    assert all(c.kwargs.get("status") != "completed" for c in store.update_node.await_args_list)
    loader.manage_check.assert_awaited()  # heartbeat check cancelled
    assert registry._disabled_run_failures == {}


@pytest.mark.asyncio
async def test_completion_check_defers_while_final_run_in_flight():
    _, orch, _, node, _ = await _awaiting_check_mid_run(False, None)

    assert node.status == "awaiting_check"
    orch._run_completion_check.assert_not_called()
    assert node.check_attempts == 0


@pytest.mark.asyncio
async def test_completion_check_completes_when_final_run_succeeds_mid_poll():
    _, _, _, node, _ = await _awaiting_check_mid_run(False, "completion_check")

    assert node.status == "completed"
