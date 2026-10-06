"""F099 Phase 2c-2: with continuation off, forced off, or on prod's exact flags, nothing of 2c exists or runs."""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest
from f099_support import CONT, env_factory, runner_env  # noqa: F401

import nous.main as main
from nous.brain import continuation
from nous.config import Settings
from nous.handlers.continuation_publisher import OwnerPublisher
from nous.handlers.continuation_runner import ContinuationRunner
from nous.heart.result_reconciler import ContinuationWakePass, build_reconciler, repair_missing_results

PROD = {"result_inbox_enabled": True, "intentions_enabled": True}  # prod: inbox ON, intentions ON, continuation OFF


class Untouchable:
    """A collaborator that fails the test on any use: proof that something did not touch it."""

    def __getattr__(self, name):
        raise AssertionError(f"touched .{name}")


UNTOUCHED = {name: Untouchable() for name in ("database", "runner", "heart", "brain", "bus", "dispatcher")}


def test_the_runner_is_not_ready_in_this_pr():  # PIN: 2e flips this assertion, and nothing else flips the constant
    assert continuation.CONTINUATION_RUNNER_READY is False


async def test_a_requested_flag_is_forced_off_and_no_runner_is_built():  # PIN (from 2c-2 on)
    settings = Settings(_env_file=None, **CONT)
    assert settings.continuation_enabled is True  # as an operator set it
    main._gate_continuation_flag(settings)
    assert settings.continuation_enabled is False
    assert await main._build_continuation_runner(settings, **UNTOUCHED) is None


async def test_the_second_guard_holds_even_if_the_gate_were_bypassed():  # PIN (from 2c-2 on)
    settings = Settings(_env_file=None, **CONT)  # the flag on, the gate never run
    assert await main._build_continuation_runner(settings, **UNTOUCHED) is None


async def test_prod_flags_build_nothing_even_when_the_constant_is_flipped(monkeypatch):  # PIN (from 2c-2 on)
    monkeypatch.setattr(continuation, "CONTINUATION_RUNNER_READY", True)
    assert await main._build_continuation_runner(Settings(_env_file=None, **PROD), **UNTOUCHED) is None


@pytest.mark.postgres_only
async def test_with_the_constant_flipped_and_the_flag_on_the_runner_is_built_wired_and_started(
    runner_env,  # noqa: F811
    monkeypatch,
):
    """The rehearsal of 2e's flip: everything 2e switches on already works."""
    monkeypatch.setattr(continuation, "CONTINUATION_RUNNER_READY", True)
    env = await runner_env()
    bus = MagicMock()
    built = await main._build_continuation_runner(
        env.settings,
        database=env.db,
        runner=env.runner,
        heart=env.heart,
        brain=env.brain,
        bus=bus,
        dispatcher=env.dispatcher,
    )
    try:
        assert isinstance(built, ContinuationRunner) and built._task is None  # built, wired, NOT started
        await built.start()  # create_components does this as its LAST statement
        assert built._task is not None
        bus.on.assert_called_once_with("intention.result_ready", built.on_result_ready)
        assert built._publisher is not None
    finally:
        await built.stop()


@pytest.mark.postgres_only
async def test_with_no_event_bus_the_runner_still_builds(runner_env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(continuation, "CONTINUATION_RUNNER_READY", True)
    env = await runner_env()
    built = await main._build_continuation_runner(
        env.settings,
        database=env.db,
        runner=env.runner,
        heart=env.heart,
        brain=env.brain,
        bus=None,
        dispatcher=env.dispatcher,
    )
    try:
        assert built is not None  # the sweep is the backstop for the hint it cannot receive
    finally:
        await built.stop()


class NoDatabase:
    def session(self):
        raise AssertionError("touched the database")


async def test_every_entry_point_is_inert_on_prods_flags():  # PIN (from 2c-2 on)
    settings = Settings(_env_file=None, telegram_bot_token="test-token", **PROD)
    runner = ContinuationRunner(
        database=NoDatabase(), settings=settings, runner=Untouchable(), heart=Untouchable(), brain=Untouchable()
    )
    report = await runner.run_once()
    assert (report.released, report.expired_roots, report.expired_proposals, report.pushed) == (0, 0, 0, 0)
    assert report.launched == () and report.next_due is None
    await runner.start()
    assert runner._task is None and runner.running_roots == frozenset()  # no loop was created
    await runner.stop()
    http = MagicMock()
    assert await OwnerPublisher(database=NoDatabase(), settings=settings, http_client=http).push_due() == 0
    http.post.assert_not_called()
    wake = MagicMock()
    assert await ContinuationWakePass(NoDatabase(), Untouchable(), settings, wake).run(limit=50) == 0
    wake.assert_not_called()
    assert await repair_missing_results(NoDatabase(), Untouchable(), settings, limit=50) == 0


def test_prods_flags_register_exactly_the_reconciler_passes_of_2b():  # PIN (from 2c-2 on)
    reconciler = build_reconciler(
        MagicMock(), MagicMock(), Settings(_env_file=None, **PROD), continuation_wake=MagicMock()
    )
    assert [p.name for p in reconciler._passes] == ["inbox", "dag", "intentions"]


def test_create_components_builds_the_runner_before_the_reconciler_and_returns_it():
    source = inspect.getsource(main.create_components)
    assert source.index("_build_continuation_runner(") < source.index("build_reconciler(")
    assert "continuation_wake=" in source
    assert '"continuation_runner": continuation_runner' in source
    assert source.index("continuation_runner.start()") > source.index("register_dag_tools(")  # tools first
    assert source.index("continuation_runner.start()") > source.index("a2ui_sweep_task = ")
    assert source.index("continuation_runner.start()") < source.rindex("return {")


def test_shutdown_stops_the_runner_before_the_heartbeat():
    source = inspect.getsource(main.shutdown_components)
    assert source.index("continuation_runner.stop()") < source.index("heartbeat_runner.stop()")
