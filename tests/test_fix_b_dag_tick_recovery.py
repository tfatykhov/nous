"""The DAG tick loop recovers from a tick that fails, is cancelled, or never returns.

From the post-merge review of #656.

Every test drives the real HeartbeatRunner. The only collaborator replaced is
the orchestrator, whose ``tick()`` is scripted per test. Timing is gated on
events and on the (mutable) settings object rather than on sleeps: ``WAIT`` is
only ever reached when the behaviour under test is broken.
"""

from __future__ import annotations

import asyncio
import logging
import types
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from httpx import ASGITransport, AsyncClient, ConnectError

from nous.config import Settings
from nous.heartbeat.dynamic import DynamicCheck
from nous.heartbeat.registry import BaseCheck, CheckRegistry
from nous.heartbeat.runner import HeartbeatRunner, _await_chain
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
# A finished tick is read without raising, whatever it ended with
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
        inner = runner._dag_pending_task

        await _stop(runner)

        assert runner.last_dag_tick is None, "a tick that never finished was recorded as a successful one"
        assert inner.cancelled(), "stop() returned with the tick still running"


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
        try:
            await _expect(started, "the tick never started")
            stopping = asyncio.create_task(_stop(runner))
            await _until(
                lambda: runner._dag_shutdown_drain_deadline is not None,
                "stop() never reached the loop's shutdown drain",
            )
        finally:
            release.set()  # or a failure above leaves teardown draining this tick for 30 s
        await asyncio.wait_for(stopping, WAIT)

    assert runner.last_dag_tick is not None, "a tick that returned during the shutdown drain was not recorded"


# ---------------------------------------------------------------------------
# Only this task's own cancellation ends a loop
# ---------------------------------------------------------------------------


async def test_a_tick_that_finishes_cancelled_does_not_end_the_dag_loop(caplog):
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
    with caplog.at_level(logging.DEBUG, logger="nous.heartbeat.runner"):
        async with _started(runner):
            await _expect(second_started, "the DAG loop ended when a tick finished cancelled")

            assert last_dag_tick_seen_by_second == [None], "the cancelled tick was recorded as a successful one"
            assert runner._dag_shutdown_drain_deadline is None, "a tick's own cancellation spent stop()'s drain budget"
            assert not runner._dag_task.done()
            await _stop(runner)

    errors = [r.getMessage() for r in _runner_log(caplog) if r.levelno >= logging.ERROR]
    assert errors == ["F038: DAG orchestrator tick was cancelled from within — a failed tick"], (
        "a tick cancelled from within is reported once, as a failed tick"
    )


async def test_a_cancelled_error_raised_while_starting_a_tick_does_not_end_the_dag_loop(caplog):
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
    with caplog.at_level(logging.DEBUG, logger="nous.heartbeat.runner"):
        async with _started(runner):
            await _expect(second_called, "the DAG loop ended on a CancelledError that was not its own cancellation")

            assert not runner._dag_task.done()
            await _stop(runner)

    errors = [r.getMessage() for r in _runner_log(caplog) if r.levelno >= logging.ERROR]
    assert errors == ["F038: DAG tick loop iteration was cancelled from within — the loop continues"]


class _ScriptedCheck(BaseCheck):
    name = "scripted"
    interval = 0  # due on every heartbeat tick

    def __init__(self, run) -> None:
        super().__init__()
        self._run = run

    async def run(self) -> CheckResult:
        return await self._run()


async def test_a_cancelled_error_out_of_a_check_does_not_end_the_heartbeat_loop(caplog):
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
    with caplog.at_level(logging.DEBUG, logger="nous.heartbeat.runner"):
        async with _started(runner):
            await _expect(second_run, "the heartbeat check loop ended when a check raised CancelledError")

            assert not runner._task.done()
            await _stop(runner)

    errors = [r.getMessage() for r in _runner_log(caplog) if r.levelno >= logging.ERROR]
    assert errors == ["Heartbeat check 'scripted' was cancelled from within — a failed run"]


async def test_a_check_cancelled_from_within_is_a_failed_check_and_the_tick_goes_on(caplog):
    """A check that ends cancelled when nobody cancelled the loop has failed:
    its breaker counts the run and its stats record it, and the checks after
    it and the rest of the tick still run."""
    cancelled_runs = 0
    later_runs = 0
    later_ran_five_times = asyncio.Event()

    async def ends_cancelled() -> CheckResult:
        nonlocal cancelled_runs
        cancelled_runs += 1
        raise asyncio.CancelledError  # something the check awaited was cancelled elsewhere

    async def runs_after_it() -> CheckResult:
        nonlocal later_runs
        later_runs += 1
        if later_runs == 5:
            later_ran_five_times.set()
        return CheckResult()

    loader = MagicMock()  # where the runs of a dynamic check are recorded
    loader.sync = AsyncMock(return_value=0)
    loader.update_run_stats = AsyncMock()
    failing = DynamicCheck(check_id="failing-id", name="failing", prompt="", tools=[], interval=0)
    failing.run = ends_cancelled

    runner = _runner(_settings(heartbeat_tick_interval=0.01), tick=None)
    runner.dag_orchestrator = None  # the DAG loop idles
    runner._dynamic_loader = loader  # what HeartbeatRunner(dynamic_loader=...) sets
    runner.registry.register(failing)
    runner.registry.register(_ScriptedCheck(runs_after_it))
    with caplog.at_level(logging.DEBUG, logger="nous.heartbeat.runner"):
        async with _started(runner):
            await _expect(later_ran_five_times, "the check after one that ended cancelled never ran")
            await _stop(runner)

    assert cancelled_runs == failing.max_failures, "its breaker never opened: it ran on every tick"
    assert failing.consecutive_failures == failing.max_failures
    assert runner.last_tick is not None, "the tick was abandoned at the check that ended cancelled"
    errors = [r.getMessage() for r in _runner_log(caplog) if r.levelno >= logging.ERROR]
    assert errors == ["Heartbeat check 'failing' was cancelled from within — a failed run"] * failing.max_failures
    assert loader.update_run_stats.await_args_list == (
        [call("failing-id", success=False, error_msg="cancelled")] * failing.max_failures
    )


async def test_a_cancellation_that_lands_after_the_run_succeeded_is_still_a_failed_run():
    """The per-check handler also covers the one await that follows a
    successful run: the write of its stats. A cancellation from elsewhere there
    must not leave a run whose success was never recorded reading as one."""
    from nous.heartbeat.dynamic import RUN_OUTCOME

    stats: list[tuple[bool, str | None]] = []

    async def update_run_stats(check_id, *, success, error_msg=None):
        stats.append((success, error_msg))
        if success:
            raise asyncio.CancelledError  # the write was cancelled elsewhere

    async def final_run() -> CheckResult:
        RUN_OUTCOME.get()["final_run"] = True  # this run disabled its own check: its last run
        return CheckResult()

    loader = MagicMock()
    loader.update_run_stats = update_run_stats
    worker = DynamicCheck(check_id="worker-id", name="worker", prompt="", tools=[], interval=0)
    worker.run = final_run
    runner = _runner(_settings(), tick=None)
    runner._dynamic_loader = loader  # what HeartbeatRunner(dynamic_loader=...) sets
    runner.registry.register(worker)

    await runner._tick()

    assert stats == [(True, None), (False, "cancelled")]
    assert worker.consecutive_failures == 1
    assert runner.registry.self_disabled_run_failed("worker"), (
        "a final run whose success was never recorded read as completion"
    )


async def test_a_cancelled_error_out_of_the_loop_body_does_not_end_the_heartbeat_loop(caplog):
    """Pins the check loop's own handler. A check that ends cancelled is dealt
    with inside the tick and never reaches it, so the test raises one from the
    loop body after the tick: the tuning pass."""
    second_pass = asyncio.Event()
    calls = 0

    async def tune() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise asyncio.CancelledError  # something the loop awaited was cancelled elsewhere
        second_pass.set()

    runner = _runner(_settings(heartbeat_tick_interval=0.01), tick=None)
    runner.dag_orchestrator = None  # the DAG loop idles
    runner._maybe_tune = tune
    with caplog.at_level(logging.DEBUG, logger="nous.heartbeat.runner"):
        async with _started(runner):
            await _expect(
                second_pass, "the heartbeat check loop ended on a CancelledError that was not its own cancellation"
            )

            assert not runner._task.done()
            await _stop(runner)

    errors = [r.getMessage() for r in _runner_log(caplog) if r.levelno >= logging.ERROR]
    assert errors == ["Heartbeat tick was cancelled from within — the loop continues"]


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


async def test_cancelling_the_check_loop_while_a_check_is_running_still_ends_it():
    """The loop's own cancellation passes through the per-check handler: it is
    not a failed run of the check that happened to be running."""
    running, never = asyncio.Event(), asyncio.Event()

    async def run() -> CheckResult:
        running.set()
        await never.wait()
        return CheckResult()

    check = _ScriptedCheck(run)
    runner = _runner(_settings(heartbeat_tick_interval=0.01), tick=None)
    runner.dag_orchestrator = None  # the DAG loop idles
    runner.registry.register(check)
    async with _started(runner):
        await _expect(running, "the check never started")
        check_task = runner._task

        check_task.cancel()
        done, _ = await asyncio.wait({check_task}, timeout=WAIT)

        assert done == {check_task}, "the check loop swallowed its own cancellation"
        assert check.consecutive_failures == 0, "the loop's own cancellation was counted as a failed run of the check"


# ---------------------------------------------------------------------------
# A TimeoutError the tick raised is a failed tick, not a deadline
# ---------------------------------------------------------------------------


def _runner_log(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "nous.heartbeat.runner"]


async def test_a_timeout_error_raised_by_the_tick_is_logged_as_a_failed_tick(caplog):
    second_started = asyncio.Event()
    calls = 0
    last_dag_tick_seen_by_second: list = []

    async def tick():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("connect timed out")  # an asyncpg connect timeout, socket.timeout
        if calls == 2:
            last_dag_tick_seen_by_second.append(runner.last_dag_tick)
            second_started.set()

    runner = _runner(_settings(), tick)
    with caplog.at_level(logging.DEBUG, logger="nous.heartbeat.runner"):
        async with _started(runner):
            await _expect(second_started, "no tick ran after one raised TimeoutError")
            await _stop(runner)

    messages = [r.getMessage() for r in _runner_log(caplog)]
    assert not [m for m in messages if "still running in background" in m], (
        "a tick that had already finished was reported as still running past its deadline"
    )
    failed = [r for r in _runner_log(caplog) if r.getMessage() == "F038: DAG orchestrator tick failed"]
    assert len(failed) == 1 and isinstance(failed[0].exc_info[1], TimeoutError)
    assert not [m for m in messages if "raised after timing out" in m], "the same finished tick was recorded twice"
    assert last_dag_tick_seen_by_second == [None], "the failed tick was recorded as a successful one"


async def test_a_timeout_error_raised_during_the_shutdown_drain_is_logged_as_a_failed_tick(caplog):
    started, release = asyncio.Event(), asyncio.Event()

    async def tick():
        started.set()
        await release.wait()
        raise TimeoutError("connect timed out")

    runner = _runner(_settings(), tick)  # 30 s deadline: stop() finds the loop still awaiting this tick
    with caplog.at_level(logging.DEBUG, logger="nous.heartbeat.runner"):
        async with _started(runner):
            try:
                await _expect(started, "the tick never started")
                stopping = asyncio.create_task(_stop(runner))
                await _until(
                    lambda: runner._dag_shutdown_drain_deadline is not None,
                    "stop() never reached the loop's shutdown drain",
                )
            finally:
                release.set()  # or a failure above leaves teardown draining this tick for 30 s
            await asyncio.wait_for(stopping, WAIT)

    messages = [r.getMessage() for r in _runner_log(caplog)]
    assert not [m for m in messages if "did not finish within" in m], (
        "a tick that had already finished was reported as still running past the drain deadline"
    )
    assert "F038: DAG orchestrator tick failed during shutdown drain" in messages
    assert runner.last_dag_tick is None, "the failed tick was recorded as a successful one"


# ---------------------------------------------------------------------------
# Status says when the in-flight tick started
# ---------------------------------------------------------------------------


async def test_dag_tick_pending_since_is_the_start_of_the_in_flight_tick_and_none_otherwise():
    settings = _settings(dag_tick_timeout=0.05)
    started, release = asyncio.Event(), asyncio.Event()

    async def tick():
        started.set()
        settings.dag_tick_interval = 3600  # after this tick's deadline the loop sleeps until stop()
        await release.wait()

    runner = _runner(settings, tick)
    assert runner.dag_tick_pending_since is None, "no tick has started yet"
    before = datetime.now(UTC)
    async with _started(runner):
        await _expect(started, "the tick never started")
        since = runner.dag_tick_pending_since
        assert since is not None and before <= since <= datetime.now(UTC)

        await _until(lambda: not runner._dag_tick_lock.locked(), "the tick's deadline never passed")
        assert runner.dag_tick_pending_since == since, "a tick past its deadline is still the in-flight tick"

        pending = runner._dag_pending_task
        release.set()
        await asyncio.wait({pending}, timeout=WAIT)
        assert runner.dag_tick_pending_since is None, "a finished tick is not in flight"
        await _stop(runner)
    assert runner.dag_tick_pending_since is None


async def _get_json(app, path: str) -> dict:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(path)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_heartbeat_status_reports_the_in_flight_dag_tick():
    from nous.api.rest import create_app

    settings = _settings()
    started, release = asyncio.Event(), asyncio.Event()

    async def tick():
        started.set()
        await release.wait()

    runner = _runner(settings, tick)
    app = create_app(
        runner=MagicMock(),
        brain=MagicMock(),
        heart=MagicMock(),
        cognitive=MagicMock(),
        database=MagicMock(),
        settings=settings,
        heartbeat_runner=runner,
    )

    assert (await _get_json(app, "/heartbeat/status"))["dag_tick_pending_since"] is None
    async with _started(runner):
        try:
            await _expect(started, "the tick never started")

            body = await _get_json(app, "/heartbeat/status")

            assert body["dag_tick_pending_since"] == runner.dag_tick_pending_since.isoformat()
            assert body["last_dag_tick"] is None
        finally:
            release.set()  # or a failure above leaves teardown draining this tick for 30 s


async def test_dashboard_heartbeat_reports_the_in_flight_dag_tick(db):
    from nous.api.rest import create_app

    settings = _settings()
    started, release = asyncio.Event(), asyncio.Event()

    async def tick():
        started.set()
        await release.wait()

    runner = _runner(settings, tick)
    app = create_app(
        runner=MagicMock(),
        brain=MagicMock(),
        heart=MagicMock(),
        cognitive=MagicMock(),
        database=db,
        settings=settings,
        heartbeat_runner=runner,
    )

    assert (await _get_json(app, "/dashboard/heartbeat"))["status"]["dag_tick_pending_since"] is None
    async with _started(runner):
        try:
            await _expect(started, "the tick never started")

            status = (await _get_json(app, "/dashboard/heartbeat"))["status"]

            assert status["dag_tick_pending_since"] == runner.dag_tick_pending_since.isoformat()
            assert status["last_dag_tick"] is None
        finally:
            release.set()  # or a failure above leaves teardown draining this tick for 30 s


# ---------------------------------------------------------------------------
# A tick that never returns is escalated, not skipped forever
# ---------------------------------------------------------------------------


def _skips(caplog) -> int:
    return sum("DAG tick skipped" in r.getMessage() for r in _runner_log(caplog))


def _criticals(caplog) -> list[str]:
    return [r.getMessage() for r in _runner_log(caplog) if r.levelno == logging.CRITICAL]


@pytest.mark.parametrize("wired", [True, False], ids=["stall action wired", "no stall action"])
async def test_a_tick_that_never_returns_is_escalated_once_after_three_timeouts(caplog, wired):
    settings = _settings(dag_tick_timeout=0.2)
    loop = asyncio.get_running_loop()
    never = asyncio.Event()
    ticks_started = 0
    fired: list[tuple[float, str]] = []

    async def _stuck_in_the_database():
        await never.wait()

    async def tick():
        nonlocal ticks_started
        ticks_started += 1
        await _stuck_in_the_database()

    runner = _runner(settings, tick)
    if wired:
        runner.dag_stall_action = lambda reason: fired.append((loop.time(), reason))
    with caplog.at_level(logging.DEBUG, logger="nous.heartbeat.runner"):
        before_start = loop.time()
        async with _started(runner):
            await _until(lambda: _criticals(caplog), "a tick that never returns was never escalated")
            # "Once": the loop keeps iterating behind the hung tick; let it.
            skips = _skips(caplog)
            await _until(lambda: _skips(caplog) >= skips + 20, "the loop stopped iterating after the escalation")

            assert len(_criticals(caplog)) == 1, "the same hung tick was escalated more than once"
            assert ticks_started == 1, "a second tick started behind the hung one"
            assert not runner._dag_task.done()
            await _stop(runner)

    (critical,) = _criticals(caplog)
    assert "_stuck_in_the_database" in critical, "the log does not say where the tick is waiting"
    assert "(3 x dag_tick_timeout=0.2s)" in critical, "the report does not state the documented threshold"
    if wired:
        assert [reason for _, reason in fired] == [critical]
        # 3 = the documented contract (runner._DAG_STALL_TIMEOUTS). The text
        # above pins the number; this pins that the wait really happened. It is
        # measured from before start(), so a slow machine only adds.
        assert fired[0][0] - before_start >= 3 * settings.dag_tick_timeout, "escalated too early"
    else:
        assert fired == []


async def test_a_hung_tick_is_escalated_at_three_timeouts_not_before_and_not_later(caplog):
    settings = _settings(dag_tick_timeout=0.05)
    loop = asyncio.get_running_loop()
    started, never = asyncio.Event(), asyncio.Event()

    async def tick():
        # The loop stamps the start before this runs. From here the stall clock is the test's.
        runner._dag_pending_started = loop.time() + 1_000_000
        started.set()
        await never.wait()

    runner = _runner(settings, tick)
    with caplog.at_level(logging.DEBUG, logger="nous.heartbeat.runner"):
        async with _started(runner):
            try:
                await _expect(started, "the tick never started")
                await _until(lambda: not runner._dag_tick_lock.locked(), "the tick's deadline never passed")
                settings.dag_tick_timeout = 1000  # the threshold is now 3000 s on the stall clock

                runner._dag_pending_started = loop.time() - 2990
                skips = _skips(caplog)
                await _until(lambda: _skips(caplog) >= skips + 5, "the loop stopped iterating")
                assert not _criticals(caplog), "escalated before 3 x dag_tick_timeout"

                runner._dag_pending_started = loop.time() - 3000
                skips = _skips(caplog)
                await _until(lambda: _skips(caplog) >= skips + 5, "the loop stopped iterating")
                assert len(_criticals(caplog)) == 1, "not escalated at 3 x dag_tick_timeout"
            finally:
                settings.dag_tick_timeout = 0.05  # or stop() drains this tick for 1000 s
            await _stop(runner)


async def test_each_hung_tick_is_escalated_once():
    settings = _settings(dag_tick_timeout=0.1)
    release_first = asyncio.Event()
    never = asyncio.Event()
    escalated = [asyncio.Event(), asyncio.Event()]
    calls = 0
    fired_during_call: list[int] = []

    async def tick():
        nonlocal calls
        calls += 1
        await (release_first if calls == 1 else never).wait()

    def action(reason: str) -> None:
        fired_during_call.append(calls)
        escalated[len(fired_during_call) - 1].set()

    runner = _runner(settings, tick)
    runner.dag_stall_action = action
    async with _started(runner):
        await _expect(escalated[0], "the first hung tick was never escalated")
        release_first.set()

        await _expect(escalated[1], "a second hung tick in the same process was never escalated")

        assert fired_during_call == [1, 2]
        await _stop(runner)


async def test_a_failed_read_of_the_await_chain_costs_the_detail_not_the_report(caplog, monkeypatch):
    def _unreadable(task):
        raise RuntimeError("this coroutine object has no cr_await")

    monkeypatch.setattr("nous.heartbeat.runner._await_chain", _unreadable)
    never = asyncio.Event()
    fired: list[str] = []

    async def tick():
        await never.wait()

    runner = _runner(_settings(dag_tick_timeout=0.1), tick)
    runner.dag_stall_action = fired.append
    with caplog.at_level(logging.DEBUG, logger="nous.heartbeat.runner"):
        async with _started(runner):
            await _until(lambda: fired, "a hung tick was not escalated when its await chain could not be read")
            await _stop(runner)

    (critical,) = _criticals(caplog)
    assert critical.endswith("Waiting at: unknown")
    assert fired == [critical]
    unread = [r for r in _runner_log(caplog) if r.getMessage().startswith("F038: could not read where")]
    assert len(unread) == 1 and isinstance(unread[0].exc_info[1], RuntimeError)


async def test_the_await_chain_walks_through_a_generator_based_awaitable():
    """A generator-based awaitable (types.coroutine, which older libraries
    still use) keeps its frame and what it waits on in gi_frame and
    gi_yieldfrom, where a coroutine has cr_frame and cr_await."""
    reached, never = asyncio.Event(), asyncio.Event()

    async def _leaf():
        reached.set()
        await never.wait()

    @types.coroutine
    def _generator_based():
        yield from _leaf()

    async def _outermost():
        await _generator_based()

    task = asyncio.create_task(_outermost())
    try:
        await _expect(reached, "the chain never reached its innermost coroutine")
        chain = _await_chain(task)
    finally:
        task.cancel()
        await asyncio.wait({task}, timeout=WAIT)

    names = [hop.split(" (", 1)[0] for hop in chain.split(" > ")]
    assert names[:3] == ["_outermost", "_generator_based", "_leaf"], chain


# ---------------------------------------------------------------------------
# The report of a hung tick reaches a person
# ---------------------------------------------------------------------------


class _TelegramRecorder:
    """Stands in for the runner's shared httpx client and records every post.

    A post answers with ``response``, or raises ``error`` if one is given. With
    ``hold`` it first waits for that event: the send stays in flight until then.
    """

    def __init__(
        self, error: Exception | None = None, response: object = None, hold: asyncio.Event | None = None
    ) -> None:
        self.posts: list[dict] = []
        self._error = error
        self._response = response
        self._hold = hold

    async def post(self, url: str, *, json: dict, timeout: float) -> object:
        self.posts.append({"url": url, **json})
        if self._hold is not None:
            await self._hold.wait()
        if self._error is not None:
            raise self._error
        return self._response


def _hung_tick_runner(telegram: _TelegramRecorder, **settings) -> HeartbeatRunner:
    """A runner whose first tick never returns, with ``telegram`` as its HTTP client."""
    never = asyncio.Event()

    async def tick():
        await never.wait()

    runner = _runner(_settings(dag_tick_timeout=0.1, **settings), tick)
    runner._http = telegram  # what HeartbeatRunner(http_client=...) sets
    return runner


async def test_a_hung_tick_is_reported_on_telegram_once(caplog):
    telegram = _TelegramRecorder()
    runner = _hung_tick_runner(telegram, telegram_bot_token="token", telegram_chat_id="chat")
    with caplog.at_level(logging.DEBUG, logger="nous.heartbeat.runner"):
        async with _started(runner):
            await _until(lambda: telegram.posts, "a hung tick was never reported on Telegram")
            # "Once": the loop keeps iterating behind the hung tick; let it.
            skips = _skips(caplog)
            await _until(lambda: _skips(caplog) >= skips + 20, "the loop stopped iterating after the report")

            assert len(telegram.posts) == 1, "the same hung tick was reported more than once"
            await _stop(runner)

    (critical,) = _criticals(caplog)
    assert telegram.posts == [
        {"url": "https://api.telegram.org/bottoken/sendMessage", "chat_id": "chat", "text": f"[Heartbeat] {critical}"}
    ]


async def test_a_failed_telegram_send_stops_neither_the_stall_action_nor_the_loop(caplog):
    telegram = _TelegramRecorder(error=ConnectError("telegram is unreachable"))
    fired: list[str] = []
    posts_when_fired: list[int] = []

    def action(reason: str) -> None:
        fired.append(reason)
        posts_when_fired.append(len(telegram.posts))

    runner = _hung_tick_runner(telegram, telegram_bot_token="token", telegram_chat_id="chat")
    runner.dag_stall_action = action
    with caplog.at_level(logging.DEBUG, logger="nous.heartbeat.runner"):
        async with _started(runner):
            await _until(lambda: telegram.posts, "a hung tick was never reported on Telegram")
            await _until(lambda: fired, "the stall action did not run after the Telegram send failed")
            skips = _skips(caplog)
            await _until(lambda: _skips(caplog) >= skips + 3, "the DAG loop stopped after the Telegram send failed")

            assert not runner._dag_task.done()
            await _stop(runner)

    assert fired == _criticals(caplog)
    # The message first: a stall action that ends the process must not get there before it.
    assert posts_when_fired == [1], "the stall action ran before the message was sent"
    assert len(telegram.posts) == 1
    assert "F038: DAG tick loop iteration failed" not in [r.getMessage() for r in _runner_log(caplog)]


@pytest.mark.parametrize(
    "configured",
    [{}, {"telegram_bot_token": "token"}, {"telegram_chat_id": "chat"}],
    ids=["neither", "no chat id", "no token"],
)
async def test_nothing_is_sent_when_telegram_is_not_configured(caplog, configured):
    """Control: green before this change too. It pins that the report goes
    through the runner's own sender, which returns when either value is missing."""
    telegram = _TelegramRecorder()
    runner = _hung_tick_runner(telegram, **configured)
    with caplog.at_level(logging.DEBUG, logger="nous.heartbeat.runner"):
        async with _started(runner):
            await _until(lambda: _criticals(caplog), "a tick that never returns was never escalated")
            skips = _skips(caplog)
            await _until(lambda: _skips(caplog) >= skips + 3, "the loop stopped iterating after the escalation")
            await _stop(runner)

    assert telegram.posts == []


async def test_the_telegram_text_is_cut_to_what_telegram_accepts(caplog, monkeypatch):
    monkeypatch.setattr("nous.heartbeat.runner._await_chain", lambda task: " > ".join(["hop (file.py:1)"] * 400))
    telegram = _TelegramRecorder()
    runner = _hung_tick_runner(telegram, telegram_bot_token="token", telegram_chat_id="chat")
    with caplog.at_level(logging.DEBUG, logger="nous.heartbeat.runner"):
        async with _started(runner):
            await _until(lambda: telegram.posts, "a hung tick was never reported on Telegram")
            await _stop(runner)

    (critical,) = _criticals(caplog)
    text = telegram.posts[0]["text"]
    assert len(critical) > 4096, "the scripted await chain is too short to need cutting"
    assert len(text) <= 4096, "Telegram rejects a text over 4096 characters"
    assert f"[Heartbeat] {critical}".startswith(text)


async def test_a_report_that_telegram_rejects_is_logged_once_and_never_with_the_bot_token(caplog):
    """Telegram answers a wrong token, a rate limit or a bad request with an
    HTTP error, not an exception. The URL of the send holds the bot token, so
    the log line carries the status and nothing else."""
    secret = "7351:AAH-never-in-a-log"
    telegram = _TelegramRecorder(response=SimpleNamespace(status_code=401))
    runner = _hung_tick_runner(telegram, telegram_bot_token=secret, telegram_chat_id="chat")
    with caplog.at_level(logging.DEBUG):  # every logger, not only the runner's
        async with _started(runner):
            await _until(lambda: telegram.posts, "a hung tick was never reported on Telegram")
            skips = _skips(caplog)
            await _until(lambda: _skips(caplog) >= skips + 3, "the DAG loop stopped after Telegram rejected the report")
            await _stop(runner)

    assert secret in telegram.posts[0]["url"], "the send did not go out with the configured token"
    about_telegram = [(r.levelname, r.getMessage()) for r in _runner_log(caplog) if "Telegram" in r.getMessage()]
    assert about_telegram == [("WARNING", "Heartbeat Telegram notification rejected: HTTP 401")]
    assert secret not in caplog.text, "the bot token reached a log line"
    assert "api.telegram.org" not in caplog.text, "the URL of the send reached a log line"


@pytest.mark.parametrize(
    ("response", "logged"),
    [
        (SimpleNamespace(status_code=400), ["Heartbeat Telegram notification rejected: HTTP 400"]),
        (SimpleNamespace(status_code=200), []),
        (MagicMock(), []),
        (None, []),
    ],
    ids=["an error status", "a success status", "the answer of a mocked client", "no answer object"],
)
async def test_the_telegram_sender_logs_an_error_status_and_nothing_else(caplog, response, logged):
    """Only an integer status of 400 or more is a rejection. A send that
    succeeds logs nothing, and neither does a test double whose answer has no
    integer status, such as the mocked http client other tests pass in."""
    runner = _runner(_settings(telegram_bot_token="token", telegram_chat_id="chat"), tick=None)
    runner._http = _TelegramRecorder(response=response)  # what HeartbeatRunner(http_client=...) sets
    with caplog.at_level(logging.DEBUG, logger="nous.heartbeat.runner"):
        await runner._send_telegram("text")

    assert [r.getMessage() for r in _runner_log(caplog)] == logged


async def test_a_tick_that_returns_while_its_report_is_being_sent_is_not_logged_as_still_running(caplog):
    release, send_done, second_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = 0

    async def tick():
        nonlocal calls
        calls += 1
        if calls == 1:
            await release.wait()
        else:
            second_started.set()

    telegram = _TelegramRecorder(hold=send_done)
    runner = _runner(_settings(dag_tick_timeout=0.1, telegram_bot_token="token", telegram_chat_id="chat"), tick)
    runner._http = telegram  # what HeartbeatRunner(http_client=...) sets
    with caplog.at_level(logging.DEBUG, logger="nous.heartbeat.runner"):
        async with _started(runner):
            await _until(lambda: telegram.posts, "a hung tick was never reported on Telegram")
            # The DAG loop is inside the send now: it logs nothing more until the send returns.
            skips = _skips(caplog)
            reported = runner._dag_pending_task
            release.set()
            await asyncio.wait({reported}, timeout=WAIT)
            assert reported.done(), "the reported tick did not return"
            send_done.set()

            await _expect(second_started, "ticking did not resume after the reported tick returned")
            assert _skips(caplog) == skips, "a tick that had returned was logged as still running in background"
            await _stop(runner)
