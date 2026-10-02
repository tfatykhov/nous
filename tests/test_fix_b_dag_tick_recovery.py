"""PR B: the DAG tick loop recovers from a tick that fails, is cancelled, or never returns.

Every test drives the real HeartbeatRunner. The only collaborator replaced is
the orchestrator, whose ``tick()`` is scripted per test. Timing is gated on
events and on the (mutable) settings object rather than on sleeps: ``WAIT`` is
only ever reached when the behaviour under test is broken.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from nous.config import Settings
from nous.heartbeat.registry import BaseCheck, CheckRegistry
from nous.heartbeat.runner import HeartbeatRunner
from nous.heartbeat.schemas import CheckResult

WAIT = 10.0


def _settings(**overrides) -> Settings:
    defaults = {
        "dag_tick_interval": 0.01,
        "dag_tick_timeout": 30,
        "heartbeat_tick_interval": 3600,  # the check loop stays asleep
        "heartbeat_quiet_start": 0,
        "heartbeat_quiet_end": 0,
        "heartbeat_daily_token_budget": 10_000,
        "heartbeat_dynamic_sync_ticks": 0,
    }
    defaults.update(overrides)
    return Settings.model_construct(**defaults)


def _runner(settings: Settings, tick) -> HeartbeatRunner:
    runner = HeartbeatRunner(
        settings=settings,
        registry=CheckRegistry(),
        runner=MagicMock(),
        brain=MagicMock(),
        heart=MagicMock(),
        bus=None,
        http_client=None,
    )
    runner.dag_orchestrator = SimpleNamespace(tick=tick)
    return runner


async def _expect(event: asyncio.Event, what: str) -> None:
    try:
        await asyncio.wait_for(event.wait(), WAIT)
    except TimeoutError:
        pytest.fail(f"{what} (waited {WAIT:.0f}s)")


async def _until(predicate, what: str) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WAIT
    while not predicate():
        if loop.time() > deadline:
            pytest.fail(f"{what} (waited {WAIT:.0f}s)")
        await asyncio.sleep(0.005)


async def _stop(runner: HeartbeatRunner) -> None:
    try:
        await runner.stop()
    except asyncio.CancelledError:
        pytest.fail("stop() raised CancelledError")


@asynccontextmanager
async def _started(runner: HeartbeatRunner):
    await runner.start()
    try:
        yield
    finally:
        # Teardown only, so a failure in the test body is not masked by a
        # second one from here. Tests assert stop() themselves, with _stop().
        try:
            await runner.stop()
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# Task B1: a finished tick is read without raising, whatever it ended with
# ---------------------------------------------------------------------------


async def test_stop_returns_when_the_pending_tick_already_finished_cancelled():
    settings = _settings(dag_tick_timeout=0.05)
    started, finish = asyncio.Event(), asyncio.Event()

    async def tick():
        started.set()
        settings.dag_tick_interval = 3600  # after this tick's deadline the loop sleeps until stop()
        await finish.wait()
        raise asyncio.CancelledError  # something the tick awaited was cancelled elsewhere

    runner = _runner(settings, tick)
    async with _started(runner):
        await _expect(started, "the tick never started")
        pending = runner._dag_pending_task
        await _until(lambda: not runner._dag_tick_lock.locked(), "the tick's deadline never passed")
        finish.set()
        await asyncio.wait({pending}, timeout=WAIT)
        assert pending.cancelled()

        await _stop(runner)

        assert runner.last_dag_tick is None, "a cancelled tick was recorded as a successful one"


@pytest.mark.parametrize("ends", ["cancelled", "successfully"])
async def test_stop_records_a_pending_tick_that_finishes_during_its_drain(ends):
    settings = _settings(dag_tick_timeout=0.05)
    started, finish = asyncio.Event(), asyncio.Event()

    async def tick():
        started.set()
        settings.dag_tick_interval = 3600
        await finish.wait()
        if ends == "cancelled":
            raise asyncio.CancelledError

    runner = _runner(settings, tick)
    async with _started(runner):
        await _expect(started, "the tick never started")
        await _until(lambda: not runner._dag_tick_lock.locked(), "the tick's deadline never passed")
        settings.dag_tick_timeout = 30  # stop() now waits this long for the pending tick

        stopping = asyncio.create_task(_stop(runner))
        await _until(lambda: runner._dag_task is None, "stop() never reached its pending-tick drain")
        finish.set()
        await asyncio.wait_for(stopping, WAIT)

        if ends == "successfully":
            assert runner.last_dag_tick is not None, "a tick that succeeded during stop()'s drain was not recorded"
        else:
            assert runner.last_dag_tick is None, "a cancelled tick was recorded as a successful one"


async def test_a_timed_out_tick_that_finishes_cancelled_is_harvested_and_ticking_resumes():
    settings = _settings(dag_tick_timeout=0.05)
    first_started, finish_first, second_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = 0
    last_dag_tick_seen_by_second: list = []

    async def tick():
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            await finish_first.wait()
            raise asyncio.CancelledError
        last_dag_tick_seen_by_second.append(runner.last_dag_tick)
        second_started.set()

    runner = _runner(settings, tick)
    async with _started(runner):
        await _expect(first_started, "the first tick never started")
        await _until(lambda: not runner._dag_tick_lock.locked(), "the first tick's deadline never passed")
        finish_first.set()

        await _expect(second_started, "no tick ran after the timed-out tick finished cancelled")

        assert last_dag_tick_seen_by_second == [None], "the cancelled tick was recorded as a successful one"
        await _stop(runner)


async def test_shutdown_drain_that_times_out_does_not_stamp_last_dag_tick():
    # 1 s: stop() must catch the loop while it still awaits the tick's first
    # deadline, and its shutdown drain then waits this long for the tick.
    settings = _settings(dag_tick_timeout=1)
    started, never = asyncio.Event(), asyncio.Event()

    async def tick():
        started.set()
        await never.wait()

    runner = _runner(settings, tick)
    async with _started(runner):
        await _expect(started, "the tick never started")

        await _stop(runner)

        assert runner.last_dag_tick is None, "a tick that never finished was recorded as a successful one"


async def test_a_loop_cancelled_in_the_step_its_tick_finishes_cancelled_still_ends():
    """Event-loop teardown cancels the tick and the loop together. The tick can
    finish cancelled before the loop sees its own CancelledError; the loop must
    not read that as a tick cancelled from within and carry on."""
    started, never = asyncio.Event(), asyncio.Event()

    async def tick():
        started.set()
        await never.wait()

    runner = _runner(_settings(), tick)
    async with _started(runner):
        await _expect(started, "the tick never started")
        dag_task, inner = runner._dag_task, runner._dag_pending_task

        inner.cancel()  # the tick first: it is done-cancelled when the loop's handler runs
        dag_task.cancel()
        done, _ = await asyncio.wait({dag_task}, timeout=WAIT)

        assert done == {dag_task}, "the DAG loop swallowed its own cancellation"
        await _stop(runner)
        assert runner.last_dag_tick is None


async def test_shutdown_drain_that_sees_the_tick_return_records_it():
    """Parity pin: green before this change too, when the drain stamped whatever
    happened. It pins the stamp that now sits after the drain's await: without
    it a tick that returned during the drain is not recorded."""
    started, release = asyncio.Event(), asyncio.Event()

    async def tick():
        started.set()
        await release.wait()

    runner = _runner(_settings(), tick)  # 30 s deadline: stop() finds the loop still awaiting this tick
    async with _started(runner):
        await _expect(started, "the tick never started")
        stopping = asyncio.create_task(_stop(runner))
        await _until(
            lambda: runner._dag_shutdown_drain_deadline is not None,
            "stop() never reached the loop's shutdown drain",
        )
        release.set()
        await asyncio.wait_for(stopping, WAIT)

    assert runner.last_dag_tick is not None, "a tick that returned during the shutdown drain was not recorded"


# ---------------------------------------------------------------------------
# Task B2: only this task's own cancellation ends a loop
# ---------------------------------------------------------------------------


async def test_a_tick_that_finishes_cancelled_does_not_end_the_dag_loop():
    second_started = asyncio.Event()
    calls = 0
    last_dag_tick_seen_by_second: list = []

    async def tick():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise asyncio.CancelledError  # something the tick awaited was cancelled elsewhere
        last_dag_tick_seen_by_second.append(runner.last_dag_tick)
        second_started.set()

    runner = _runner(_settings(), tick)
    async with _started(runner):
        await _expect(second_started, "the DAG loop ended when a tick finished cancelled")

        assert last_dag_tick_seen_by_second == [None], "the cancelled tick was recorded as a successful one"
        assert runner._dag_shutdown_drain_deadline is None, "a tick's own cancellation spent stop()'s drain budget"
        assert not runner._dag_task.done()
        await _stop(runner)


async def test_a_cancelled_error_raised_while_starting_a_tick_does_not_end_the_dag_loop():
    """Pins the loop's outermost handler. Nothing in the loop body reaches it
    today except the task's own cancellation, so the test injects one at the
    only seam there is: the call that creates the tick coroutine."""
    second_called = asyncio.Event()
    calls = 0

    def tick():  # not a coroutine function: raises when called, before any task exists
        nonlocal calls
        calls += 1
        if calls == 1:
            raise asyncio.CancelledError
        second_called.set()
        return asyncio.sleep(0)

    runner = _runner(_settings(), tick)
    async with _started(runner):
        await _expect(second_called, "the DAG loop ended on a CancelledError that was not its own cancellation")

        assert not runner._dag_task.done()
        await _stop(runner)


class _ScriptedCheck(BaseCheck):
    name = "scripted"
    interval = 0  # due on every heartbeat tick

    def __init__(self, run) -> None:
        super().__init__()
        self._run = run

    async def run(self) -> CheckResult:
        return await self._run()


async def test_a_cancelled_error_out_of_a_check_does_not_end_the_heartbeat_loop():
    second_run = asyncio.Event()
    calls = 0

    async def run() -> CheckResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise asyncio.CancelledError  # something the check awaited was cancelled elsewhere
        second_run.set()
        return CheckResult()

    runner = _runner(_settings(heartbeat_tick_interval=0.01), tick=None)
    runner.dag_orchestrator = None  # the DAG loop idles
    runner.registry.register(_ScriptedCheck(run))
    async with _started(runner):
        await _expect(second_run, "the heartbeat check loop ended when a check raised CancelledError")

        assert not runner._task.done()
        await _stop(runner)


async def test_cancelling_a_loop_task_still_ends_it():
    """The guard must not swallow a real cancellation: stop() is not the only
    one (the event loop cancels every task on teardown)."""
    started = asyncio.Event()

    async def tick():
        started.set()

    runner = _runner(_settings(), tick)
    async with _started(runner):
        await _expect(started, "the tick never started")
        dag_task, check_task = runner._dag_task, runner._task

        dag_task.cancel()
        check_task.cancel()
        done, _ = await asyncio.wait({dag_task, check_task}, timeout=WAIT)

        assert done == {dag_task, check_task}, "a cancelled loop task kept running"
