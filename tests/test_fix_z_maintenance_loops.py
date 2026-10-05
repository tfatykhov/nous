"""The maintenance loops that nous/main.py starts end only when their own task is cancelled.

``create_components`` starts five loops (execution-ledger maintenance, two
retention sweeps, the companion surface sweep and the F098 result reconciler) and ``shutdown_components``
cancels them. Each test runs one of them, through the coroutine production
runs, with stand-ins for what it sweeps.

A ``CancelledError`` that comes out of something a loop awaited is that
thing's failure, not a request to stop: the loop logs it and goes on. It is
produced here the way it arises in production: the awaited code waits on a
future that something else cancels.

Each scenario runs on an event loop of its own, in a thread of its own. A loop
that ignored its own cancellation would keep that event loop from shutting
down; here that fails one test instead of hanging the whole session at exit.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import logging
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

import nous.main as main
from nous.cognitive.ledger_store import effective_orphan_threshold
from nous.heart import result_reconciler

# How long a test waits for something that should happen at once. Only reached
# when the behaviour under test is broken.
WAIT = 5.0
# The interval of every loop in these tests.
TICK = 0.01


def _short_intervals(monkeypatch: pytest.MonkeyPatch, seconds: float = TICK) -> None:
    """Production waits a day between two retention sweeps and a minute after
    a failed ledger pass; a test cannot."""
    monkeypatch.setattr(main, "_RETENTION_SWEEP_INTERVAL_SECONDS", seconds)
    monkeypatch.setattr(main, "_EXECUTION_LEDGER_RETRY_SECONDS", seconds)
    monkeypatch.setattr(result_reconciler, "RECONCILE_INTERVAL_SECONDS", seconds)


async def _cancelled_from_within() -> None:
    """Wait on a future that something else cancels: the CancelledError reaches
    the caller, and nobody has cancelled the caller's own task."""
    victim = asyncio.get_running_loop().create_future()
    victim.cancel()
    await victim


class _First:
    """What the first call of a stand-in does: nothing special, an error, or a
    cancellation from within. With ``every_time`` every call does it."""

    def __init__(self, how: str | None, every_time: bool = False) -> None:
        self._how, self._every_time = how, every_time
        self.calls = 0

    async def __call__(self) -> None:
        self.calls += 1
        await asyncio.sleep(0)  # a real call gives the event loop a turn
        how = self._how
        if not self._every_time:
            self._how = None
        if how == "raises":
            raise RuntimeError("the sweep failed")
        if how == "cancelled":
            await _cancelled_from_within()


@dataclass
class _Loop:
    """One maintenance loop under test: the production coroutine with stand-ins."""

    name: str
    # Its key among the components that shutdown_components stops.
    key: str
    run: Callable[[], Awaitable[None]]
    # Set by a pass that ran after the first one failed or was cancelled (or
    # by the first pass, when nothing happened to it).
    went_on: asyncio.Event
    # What the loop logs when a pass raises.
    failed: str
    parts: dict[str, Any] = field(default_factory=dict)
    task: asyncio.Task | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        """As production stops it."""
        await main.shutdown_components({self.key: self.task})


def _run(scenario: Callable[[], Awaitable[None]], seconds: float = 4 * WAIT) -> None:
    """Run a scenario to its end on an event loop and a thread of its own."""
    raised: list[BaseException] = []

    def target() -> None:
        try:
            asyncio.run(scenario())
        except BaseException as exc:  # noqa: BLE001 - raised again below, in the test's own thread
            raised.append(exc)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        pytest.fail(
            f"the scenario had not finished after {seconds:.0f}s: "
            "a loop that ignores its cancellation, or a machine too slow for this budget"
        )
    if raised:
        raise raised[0]


async def _expect(event: asyncio.Event, otherwise: str) -> None:
    try:
        await asyncio.wait_for(event.wait(), WAIT)
    except TimeoutError:
        pytest.fail(otherwise)


async def _until(condition: Callable[[], bool], otherwise: str) -> None:
    deadline = asyncio.get_running_loop().time() + WAIT
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail(otherwise)
        await asyncio.sleep(TICK)


async def _stop(loop: _Loop) -> None:
    try:
        await asyncio.wait_for(loop.stop(), WAIT)
    except TimeoutError:
        pytest.fail(f"shutdown_components had not stopped the {loop.name} after {WAIT:.0f}s")


def _settings(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "agent_id": "test",
        "execution_ledger_retention_days": 90,
        "execution_ledger_sweep_interval_seconds": TICK,
        "execution_ledger_pending_unknown_after_seconds": 3600,
        "retrieval_telemetry_retention_days": 14,
        "context_log_retention_days": 30,
        "a2ui_sweep_interval_seconds": TICK,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _Ledger:
    """Stands in for the LedgerStore."""

    def __init__(self, went_on: asyncio.Event | None, first_prune: _First) -> None:
        self.pruned: list[int] = []
        self.swept: list[float | None] = []
        self._went_on, self._first_prune = went_on, first_prune

    async def prune(self, *, retention_days: int) -> int:
        await self._first_prune()
        self.pruned.append(retention_days)
        if self._went_on is not None:
            self._went_on.set()
        return 3

    async def mark_orphans_unknown(self, *, older_than_seconds: float | None) -> int:
        self.swept.append(older_than_seconds)
        return 0


class _Cards:
    """Stands in for the AgentRunner: the retry of pending compensation cards."""

    def __init__(self, went_on: asyncio.Event | None, first: _First) -> None:
        self.swept = 0
        self._went_on, self._first = went_on, first

    async def sweep_pending_cards(self) -> int:
        await self._first()
        self.swept += 1
        if self._went_on is not None:
            self._went_on.set()
        return 1


class _Database:
    """Stands in for the Database: its one session records what is executed and
    committed, and how each ``async with`` was left."""

    def __init__(self, went_on: asyncio.Event, first: _First) -> None:
        # (the statement, its parameters as sorted pairs), in the order they were executed
        self.executed: list[tuple[str, tuple]] = []
        self.commits = 0
        self.left_with: list[type[BaseException] | None] = []
        self._went_on, self._first = went_on, first

    def session(self) -> _Database:
        return self

    async def __aenter__(self) -> _Database:
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, *_: Any) -> bool:
        self.left_with.append(exc_type)
        return False

    async def execute(self, statement: Any, params: dict) -> None:
        await self._first()
        self.executed.append((str(statement), tuple(sorted(params.items()))))

    async def commit(self) -> None:
        self.commits += 1
        self._went_on.set()


class _Surfaces:
    """Stands in for the SurfaceService."""

    def __init__(self, went_on: asyncio.Event, first: _First) -> None:
        self.invalidated = 0
        self.expired = 0
        self._went_on, self._first = went_on, first

    async def invalidate_heartbeat_surfaces(self) -> int:
        self.invalidated += 1
        await asyncio.sleep(0)
        return 0

    async def expire_sweep(self) -> int:
        await self._first()
        self.expired += 1
        self._went_on.set()
        return 0


def _execution_ledger_cards(first: str | None) -> _Loop:
    went_on = asyncio.Event()
    settings, ledger, cards = _settings(), _Ledger(None, _First(None)), _Cards(went_on, _First(first))
    return _Loop(
        "execution-ledger maintenance loop (its card retry)",
        "execution_ledger_task",
        lambda: main._execution_ledger_maintenance_loop(settings, ledger, cards),
        went_on,
        "Harness: sweep_pending_cards failed",
        {"settings": settings, "ledger": ledger, "cards": cards},
    )


def _execution_ledger_prune(first: str | None) -> _Loop:
    went_on = asyncio.Event()
    settings, ledger, cards = _settings(), _Ledger(went_on, _First(first)), _Cards(None, _First(None))
    return _Loop(
        "execution-ledger maintenance loop (its prune)",
        "execution_ledger_task",
        lambda: main._execution_ledger_maintenance_loop(settings, ledger, cards),
        went_on,
        "Harness: execution ledger maintenance failed",
        {"settings": settings, "ledger": ledger, "cards": cards},
    )


def _retrieval_log_retention(first: str | None) -> _Loop:
    went_on = asyncio.Event()
    settings, database = _settings(), _Database(went_on, _First(first))
    return _Loop(
        "retrieval-log retention loop",
        "retrieval_log_retention_task",
        lambda: main._retrieval_log_retention_loop(settings, database),
        went_on,
        "F091: retrieval retention sweep failed",
        {"database": database},
    )


def _context_log_retention(first: str | None) -> _Loop:
    went_on = asyncio.Event()
    settings, database = _settings(), _Database(went_on, _First(first))
    return _Loop(
        "context-log retention loop",
        "context_log_retention_task",
        lambda: main._context_log_retention_loop(settings, database),
        went_on,
        "OB-1: retention sweep failed",
        {"database": database},
    )


def _surface_sweep(first: str | None) -> _Loop:
    went_on = asyncio.Event()
    settings, surfaces = _settings(), _Surfaces(went_on, _First(first))
    return _Loop(
        "companion surface sweep loop",
        "a2ui_sweep_task",
        lambda: main._a2ui_sweep_loop(settings, surfaces),
        went_on,
        "F092: expiry sweep failed",
        {"surfaces": surfaces},
    )


class _Reconciler:
    """Stands in for the TerminalSubtaskReconciler."""

    def __init__(self, went_on: asyncio.Event, first: _First) -> None:
        self.ticks = 0
        self._went_on, self._first = went_on, first

    async def run_once(self) -> dict[str, int]:
        await self._first()
        self.ticks += 1
        self._went_on.set()
        return {}


def _result_reconciler(first: str | None) -> _Loop:
    went_on = asyncio.Event()
    reconciler = _Reconciler(went_on, _First(first))
    return _Loop(
        "result reconciler loop",
        "result_reconciler_task",
        lambda: main._result_reconciler_loop(reconciler),
        went_on,
        "F098: result reconciler tick failed",
        {"reconciler": reconciler},
    )


LOOPS = [
    _execution_ledger_cards,
    _execution_ledger_prune,
    _retrieval_log_retention,
    _context_log_retention,
    _surface_sweep,
    _result_reconciler,
]
every_loop = pytest.mark.parametrize(
    "build", LOOPS, ids=[build.__name__.strip("_").replace("_", " ") for build in LOOPS]
)


# ---------------------------------------------------------------------------
# What each loop does, and that it still ends when it is told to
# ---------------------------------------------------------------------------


@every_loop
def test_shutdown_ends_a_loop(build, monkeypatch):
    _short_intervals(monkeypatch)
    loop = build(None)

    async def scenario() -> None:
        await loop.start()
        await _expect(loop.went_on, f"the {loop.name} never ran")
        await _stop(loop)
        assert loop.task.done(), f"shutdown_components left the {loop.name} running"

    _run(scenario)


@every_loop
def test_cancelling_its_task_ends_a_loop(build, monkeypatch):
    """Nothing but the cancellation tells the loop to end here."""
    _short_intervals(monkeypatch)
    loop = build(None)

    async def scenario() -> None:
        await loop.start()
        await _expect(loop.went_on, f"the {loop.name} never ran")
        loop.task.cancel()
        _, pending = await asyncio.wait({loop.task}, timeout=WAIT)
        assert not pending, f"the cancelled {loop.name} was still running after {WAIT:.0f}s"

    _run(scenario)


@every_loop
def test_event_loop_teardown_ends_a_loop(build, monkeypatch):
    """The loop is left running when the scenario returns, so it is
    ``asyncio.run()`` that cancels it, as at interpreter exit."""
    _short_intervals(monkeypatch)
    loop = build(None)

    async def scenario() -> None:
        await loop.start()
        await _expect(loop.went_on, f"the {loop.name} never ran")

    _run(scenario)


@every_loop
def test_a_loop_goes_on_after_a_pass_that_raised(build, monkeypatch, caplog):
    _short_intervals(monkeypatch)
    loop = build("raises")

    async def scenario() -> None:
        await loop.start()
        try:
            await _expect(loop.went_on, f"the {loop.name} did nothing more after a pass that raised")
            assert not loop.task.done(), f"the {loop.name} has ended"
        finally:
            await _stop(loop)

    with caplog.at_level(logging.DEBUG, logger="nous.main"):
        _run(scenario)

    said = [record.getMessage() for record in caplog.records if record.name == "nous.main"]
    assert any(loop.failed in line for line in said), said


def test_the_ledger_loop_prunes_once_and_then_sweeps_orphans_and_retries_cards_every_interval(monkeypatch):
    _short_intervals(monkeypatch)
    loop = _execution_ledger_cards(None)
    settings, ledger, cards = loop.parts["settings"], loop.parts["ledger"], loop.parts["cards"]

    async def scenario() -> None:
        await loop.start()
        await _until(lambda: cards.swept >= 3, "the loop did not go on sweeping")
        await _stop(loop)

    _run(scenario)

    assert ledger.pruned == [90]  # at startup, and not again within the day
    assert len(ledger.swept) >= 3
    assert set(ledger.swept) == {effective_orphan_threshold(settings)}


def test_the_ledger_loop_does_not_prune_when_retention_is_off(monkeypatch):
    _short_intervals(monkeypatch)
    went_on = asyncio.Event()
    ledger = _Ledger(None, _First(None))

    async def scenario() -> None:
        task = asyncio.create_task(
            main._execution_ledger_maintenance_loop(
                _settings(execution_ledger_retention_days=0), ledger, _Cards(went_on, _First(None))
            )
        )
        await _expect(went_on, "the loop never ran")
        await main.shutdown_components({"execution_ledger_task": task})

    _run(scenario)

    assert ledger.pruned == []
    assert ledger.swept


def test_a_ledger_pass_that_keeps_failing_is_tried_again_once_per_retry_interval(monkeypatch):
    _short_intervals(monkeypatch, 0.05)
    always_fails = _First("raises", every_time=True)

    async def scenario() -> None:
        task = asyncio.create_task(
            main._execution_ledger_maintenance_loop(
                _settings(), _Ledger(None, always_fails), _Cards(None, _First(None))
            )
        )
        await asyncio.sleep(0.5)
        await main.shutdown_components({"execution_ledger_task": task})

    _run(scenario)

    # One try per retry interval: about ten in half a second, not one and not thousands.
    assert 2 <= always_fails.calls < 50, always_fails.calls


def test_the_retrieval_log_sweep_deletes_this_agents_old_rows_at_startup_and_again_every_interval(monkeypatch):
    _short_intervals(monkeypatch)
    loop = _retrieval_log_retention(None)
    database = loop.parts["database"]

    async def scenario() -> None:
        await loop.start()
        await _until(lambda: database.commits >= 3, "the sweep was not run again")
        await _stop(loop)

    _run(scenario)

    assert set(database.executed) == {
        (
            "DELETE FROM nous_system.retrieval_log WHERE agent_id = :agent_id "
            "AND timestamp < now() - make_interval(days => :d)",
            (("agent_id", "test"), ("d", 14)),
        )
    }


def test_the_context_log_sweep_deletes_old_rows_of_both_tables_every_interval(monkeypatch):
    _short_intervals(monkeypatch)
    loop = _context_log_retention(None)
    database = loop.parts["database"]

    async def scenario() -> None:
        await loop.start()
        await _until(lambda: database.commits >= 3, "the sweep was not run again")
        await _stop(loop)

    _run(scenario)

    assert database.executed[:2] == [
        ("DELETE FROM nous_system.context_log WHERE timestamp < now() - make_interval(days => :d)", (("d", 30),)),
        (
            "DELETE FROM nous_system.behavior_snapshots WHERE timestamp < now() - make_interval(days => :d)",
            (("d", 30),),
        ),
    ]


def test_the_surface_sweep_invalidates_heartbeat_surfaces_once_and_expires_every_interval(monkeypatch):
    _short_intervals(monkeypatch)
    loop = _surface_sweep(None)
    surfaces = loop.parts["surfaces"]

    async def scenario() -> None:
        await loop.start()
        await _until(lambda: surfaces.expired >= 3, "the sweep was not run again")
        await _stop(loop)

    _run(scenario)

    assert surfaces.invalidated == 1


@every_loop
def test_a_loop_waits_its_interval_between_two_passes(build, monkeypatch):
    """About thirty passes in 0.3 s at an interval of 0.01 s, and never a
    hundred: a loop that lost its wait runs thousands."""
    _short_intervals(monkeypatch)
    loop = build(None)
    passes = {
        "execution_ledger_task": lambda: loop.parts["cards"].swept,
        "retrieval_log_retention_task": lambda: loop.parts["database"].commits,
        "context_log_retention_task": lambda: loop.parts["database"].commits,
        "a2ui_sweep_task": lambda: loop.parts["surfaces"].expired,
        "result_reconciler_task": lambda: loop.parts["reconciler"].ticks,
    }[loop.key]

    async def scenario() -> None:
        await loop.start()
        await _until(lambda: passes() >= 1, f"the {loop.name} never ran")
        await asyncio.sleep(0.3)
        await _stop(loop)

    _run(scenario)

    assert passes() < 100, passes()


def test_the_retrieval_log_sweep_runs_at_startup_before_its_first_wait(monkeypatch):
    """A process restarted every day would never prune under a loop that
    waits first."""
    _short_intervals(monkeypatch, 3600)
    loop = _retrieval_log_retention(None)
    database = loop.parts["database"]

    async def scenario() -> None:
        await loop.start()
        await _expect(loop.went_on, "no sweep at startup")
        await _stop(loop)

    _run(scenario)

    assert database.commits == 1


def test_production_waits_a_day_between_two_retention_sweeps_and_a_minute_after_a_failed_ledger_pass():
    assert (main._RETENTION_SWEEP_INTERVAL_SECONDS, main._EXECUTION_LEDGER_RETRY_SECONDS) == (86400, 60)


def test_create_components_starts_each_loop_behind_its_own_switch_with_its_own_collaborators():
    """The loops are module-level coroutines. What starts them, and when, is
    still decided in create_components, each loop once."""
    tree = ast.parse(inspect.getsource(main.create_components))
    started: list[tuple[str, list[str], list[str]]] = []

    def visit(node: ast.AST, switches: list[str]) -> None:
        if isinstance(node, ast.If):
            for child in node.body:
                visit(child, [*switches, ast.unparse(node.test)])
            for child in node.orelse:
                visit(child, [*switches, f"not ({ast.unparse(node.test)})"])
            return
        if (
            isinstance(node, ast.Call)
            and ast.unparse(node.func) == "asyncio.create_task"
            and node.args
            and isinstance(node.args[0], ast.Call)
            and ast.unparse(node.args[0].func).endswith("_loop")
        ):
            loop = node.args[0]
            started.append((ast.unparse(loop.func), [ast.unparse(arg) for arg in loop.args], switches))
        for child in ast.iter_child_nodes(node):
            visit(child, switches)

    visit(tree, [])

    assert sorted(started) == [
        ("_a2ui_sweep_loop", ["settings", "surface_service"], ["settings.a2ui_enabled"]),
        (
            "_context_log_retention_loop",
            ["settings", "database"],
            ["settings.context_log_enabled", "getattr(settings, 'context_log_retention_days', 0) > 0"],
        ),
        (
            "_execution_ledger_maintenance_loop",
            ["settings", "ledger_store", "runner"],
            ["settings.execution_ledger_persist_enabled"],
        ),
        ("_result_reconciler_loop", ["result_reconciler"], ["settings.result_inbox_enabled"]),
        (
            "_retrieval_log_retention_loop",
            ["settings", "database"],
            ["settings.retrieval_telemetry_enabled", "getattr(settings, 'retrieval_telemetry_retention_days', 0) > 0"],
        ),
    ], f"create_components starts its loops differently: {started}"


# ---------------------------------------------------------------------------
# A cancellation from within does not end a loop
# ---------------------------------------------------------------------------


@every_loop
def test_a_loop_goes_on_after_a_cancellation_from_within(build, monkeypatch, caplog):
    _short_intervals(monkeypatch)
    loop = build("cancelled")

    async def scenario() -> None:
        await loop.start()
        try:
            await _expect(loop.went_on, f"the {loop.name} did nothing more after something it awaited was cancelled")
            assert not loop.task.done(), f"the {loop.name} has ended"
        finally:
            await _stop(loop)
        assert loop.task.done(), f"shutdown_components left the {loop.name} running"

    with caplog.at_level(logging.ERROR, logger="nous.main"):
        _run(scenario)

    said = [record for record in caplog.records if record.name == "nous.main"]
    # With its traceback: the only way to find where the cancellation came from.
    assert any("cancelled from within" in record.getMessage() and record.exc_info for record in said), [
        (record.getMessage(), bool(record.exc_info)) for record in said
    ]


def test_a_ledger_pass_cancelled_from_within_again_and_again_is_tried_again_once_per_retry_interval(monkeypatch):
    _short_intervals(monkeypatch, 0.05)
    always_cancelled = _First("cancelled", every_time=True)

    async def scenario() -> None:
        task = asyncio.create_task(
            main._execution_ledger_maintenance_loop(
                _settings(), _Ledger(None, always_cancelled), _Cards(None, _First(None))
            )
        )
        await asyncio.sleep(0.5)
        await asyncio.wait_for(main.shutdown_components({"execution_ledger_task": task}), WAIT)

    _run(scenario)

    # One try per retry interval: about ten in half a second, not one and not thousands.
    assert 2 <= always_cancelled.calls < 50, always_cancelled.calls


@pytest.mark.parametrize(
    "build", [_retrieval_log_retention, _context_log_retention], ids=["retrieval log", "context log"]
)
def test_a_retention_sweep_cancelled_from_within_commits_nothing_and_is_run_again_after_the_interval(
    build, monkeypatch
):
    _short_intervals(monkeypatch)
    loop = build("cancelled")
    database = loop.parts["database"]

    async def scenario() -> None:
        await loop.start()
        try:
            await _expect(loop.went_on, "the sweep was never run again")
        finally:
            await _stop(loop)

    _run(scenario)

    # The session of the cancelled sweep was left by the CancelledError, which
    # is a rollback; the sweep after it went through and committed.
    assert database.left_with[:2] == [asyncio.CancelledError, None]
    assert database.commits >= 1


@pytest.mark.postgres_only
def test_the_loops_create_components_starts_go_on_after_a_cancellation_from_within(monkeypatch):
    """Through the real create_components, with the parts these loops do not
    need switched off. The ledger loop and the surface sweep call the runner
    and the surface service on every pass, so the cancellation goes in there."""
    from uuid import uuid4

    from sqlalchemy import text

    from nous.config import Settings

    agent_id = f"test-maintenance-loops-{uuid4().hex[:8]}"

    async def already_migrated(engine: Any) -> None:
        """The test database is migrated before the run. The boot writes rows:
        they go to an agent of this test's own."""
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO nous_system.agents (id, name, config) VALUES (:id, 'x', '{}')"), {"id": agent_id}
            )

    monkeypatch.setattr(main, "run_migrations", already_migrated)
    monkeypatch.setattr(main, "_EXECUTION_LEDGER_RETRY_SECONDS", TICK, raising=False)
    settings = Settings(
        _env_file=None,
        ANTHROPIC_API_KEY="test-key",
        agent_id=agent_id,
        heartbeat_enabled=False,
        subtask_enabled=False,
        schedule_enabled=False,
        dag_enabled=False,
        mcp_enabled=False,
    )
    # Below the fields' minimums, so set after validation; read on every pass.
    settings.execution_ledger_sweep_interval_seconds = 0.05
    settings.a2ui_sweep_interval_seconds = 0.05
    keys = ("execution_ledger_task", "retrieval_log_retention_task", "context_log_retention_task", "a2ui_sweep_task")

    async def boot_and_cancel() -> None:
        components = await main.create_components(settings)
        tasks = {key: components[key] for key in keys}
        ledger, sweep = tasks["execution_ledger_task"], tasks["a2ui_sweep_task"]
        cards, surfaces = _First("cancelled", every_time=True), _First("cancelled", every_time=True)
        try:
            components["runner"].sweep_pending_cards = cards
            components["surface_service"].expire_sweep = surfaces
            await _until(
                lambda: (cards.calls >= 2 or ledger.done()) and (surfaces.calls >= 2 or sweep.done()),
                "the ledger loop or the surface sweep neither went on nor ended",
            )
            ended = [key for key, task in tasks.items() if task.done()]
            assert not ended, f"ended by a cancellation from within: {ended}"
        finally:
            await main.shutdown_components(components)
        assert all(task.done() for task in tasks.values())

    async def scenario() -> None:
        try:
            await boot_and_cancel()
        finally:
            # The two rows the boot wrote: its agent and that agent's rubric version.
            from nous.storage.database import Database

            database = Database(settings)
            try:
                async with database.session() as session:
                    await session.execute(
                        text("DELETE FROM heart.rubric_versions WHERE agent_id = :a"), {"a": agent_id}
                    )
                    await session.execute(text("DELETE FROM nous_system.agents WHERE id = :a"), {"a": agent_id})
                    await session.commit()
            finally:
                await database.disconnect()

    _run(scenario, seconds=60)
