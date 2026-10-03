"""A background loop ends only when its own task is cancelled.

A ``CancelledError`` that comes out of something a loop awaited (a handler, a
turn, a query whose future was cancelled elsewhere) is that thing's failure,
not a request to stop. The loop logs it and goes on, the work of the
interrupted iteration is neither left open nor done twice, and ``stop()``
still ends the loop without raising or hanging.

Every test drives the real class through ``start()`` and ``stop()``; only its
collaborators are replaced. A cancellation "from within" is produced the way
it arises in production: the awaited code waits on a future that something
else cancels.

Each scenario runs on an event loop of its own, in a thread of its own. A loop
that ignored its own cancellation would keep that event loop from shutting
down; here that fails one test instead of hanging the whole session at exit.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

# The subtask worker imports nous.api.tools on a turn's first run, and
# nous.api.tools imports nous.api.runner on its first subtask prefix. Importing
# both here keeps those cold imports out of every test's timed window.
import nous.api.runner  # noqa: F401
import nous.api.tools  # noqa: F401
from nous.config import Settings
from nous.events import Event, EventBus
from nous.handlers.decision_reviewer import DecisionReviewer
from nous.handlers.session_monitor import SessionTimeoutMonitor
from nous.handlers.subtask_worker import SubtaskWorkerPool
from nous.handlers.task_scheduler import TaskScheduler

# How long a test waits for something that should happen at once. Only reached
# when the behaviour under test is broken.
WAIT = 5.0
# The interval of every loop in these tests.
TICK = 0.01


async def _cancelled_from_within() -> None:
    """Wait on a future that something else cancels: the CancelledError reaches
    the caller, and nobody has cancelled the caller's own task."""
    victim = asyncio.get_running_loop().create_future()
    victim.cancel()
    await victim


class _Once:
    """True the first time it is asked, if the loop is to be cancelled from within at all."""

    def __init__(self, armed: bool) -> None:
        self._armed = armed

    def __call__(self) -> bool:
        armed, self._armed = self._armed, False
        return armed


@dataclass
class _Loop:
    """One background loop under test, built on its real class."""

    name: str
    start: Callable[[], Awaitable[None]]
    stop: Callable[[], Awaitable[None]]
    tasks: Callable[[], list[asyncio.Task]]
    # Set by an iteration that ran after the one that was cancelled from within
    # (or by the first iteration, when nothing was cancelled).
    went_on: asyncio.Event
    logger: str
    says: str
    parts: dict[str, Any] = field(default_factory=dict)


def _run(scenario: Callable[[], Awaitable[None]]) -> None:
    """Run a scenario to its end on an event loop and a thread of its own."""
    raised: list[BaseException] = []

    def target() -> None:
        try:
            asyncio.run(scenario())
        except BaseException as exc:  # noqa: BLE001 - raised again below, in the test's own thread
            raised.append(exc)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(4 * WAIT)
    if thread.is_alive():
        pytest.fail("the event loop could not shut down: a loop did not end when its task was cancelled")
    if raised:
        raise raised[0]


async def _expect(event: asyncio.Event, otherwise: str) -> None:
    try:
        await asyncio.wait_for(event.wait(), WAIT)
    except TimeoutError:
        pytest.fail(otherwise)


async def _stop(loop: _Loop) -> None:
    try:
        await asyncio.wait_for(loop.stop(), WAIT)
    except TimeoutError:
        pytest.fail(f"stop() of the {loop.name} had not returned after {WAIT:.0f}s")
    except asyncio.CancelledError:
        pytest.fail(f"stop() of the {loop.name} raised CancelledError")


def _event_bus(cancelled: bool) -> _Loop:
    bus = EventBus()
    went_on = asyncio.Event()

    async def handler_cancelled_from_within(event: Event) -> None:
        await _cancelled_from_within()

    async def next_handler(event: Event) -> None:
        went_on.set()

    bus.on("cancelled", handler_cancelled_from_within)
    bus.on("next", next_handler)

    async def start() -> None:
        await bus.start()
        if cancelled:
            await bus.emit(Event(type="cancelled", agent_id="test"))
        await bus.emit(Event(type="next", agent_id="test"))

    return _Loop(
        "event bus (a handler)",
        start,
        bus.stop,
        lambda: [bus._task],
        went_on,
        "nous.events",
        "failed for event cancelled",
    )


def _event_bus_dispatch(cancelled: bool) -> _Loop:
    """The dispatch itself is cancelled from within. Nothing a dispatch awaits
    can do that to the loop any more; the loop holds the rule all the same."""
    bus = EventBus()
    went_on = asyncio.Event()
    cancel = _Once(cancelled)
    dispatch = bus._dispatch

    async def dispatch_cancelled_once(event: Event) -> None:
        if cancel():
            await _cancelled_from_within()
        await dispatch(event)

    async def next_handler(event: Event) -> None:
        went_on.set()

    bus._dispatch = dispatch_cancelled_once
    bus.on("next", next_handler)

    async def start() -> None:
        await bus.start()
        if cancelled:
            await bus.emit(Event(type="lost to the cancellation", agent_id="test"))
        await bus.emit(Event(type="next", agent_id="test"))

    return _Loop(
        "event bus (its dispatch)",
        start,
        bus.stop,
        lambda: [bus._task],
        went_on,
        "nous.events",
        "cancelled from within",
    )


def _subtask(task: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        task=task,
        frame_type=None,
        timeout_seconds=60,
        model=None,
        notify=False,
        parent_session_id=None,
        dag_node_id=None,
        metadata_=None,
        agent_id="test",
        output_format=None,
        success_criteria=None,
        payload_schema=None,
    )


class _Subtasks:
    """Stands in for heart.subtasks: hands out the subtasks it was given, then nothing."""

    def __init__(self, pending: list[SimpleNamespace], went_on: asyncio.Event, cancel_a_dequeue: bool) -> None:
        self._pending = list(pending)
        self._last = pending[-1].id
        self._went_on = went_on
        self._cancel_a_dequeue = _Once(cancel_a_dequeue)
        # (subtask id, status, final_outcome, error), in the order the rows were written
        self.settled: list[tuple[Any, str, Any, str | None]] = []

    async def reclaim_stale(self) -> int:
        return 0

    async def dequeue(self, worker_id: str) -> SimpleNamespace | None:
        if self._cancel_a_dequeue():
            await _cancelled_from_within()
        return self._pending.pop(0) if self._pending else None

    async def complete(self, subtask_id: Any, result: str, **outcome: Any) -> None:
        self._settle(subtask_id, "completed", outcome, None)

    async def fail(self, subtask_id: Any, error: str, **outcome: Any) -> None:
        self._settle(subtask_id, "failed", outcome, error)

    def _settle(self, subtask_id: Any, status: str, outcome: dict, error: str | None) -> None:
        self.settled.append((subtask_id, status, outcome.get("final_outcome"), error))
        if subtask_id == self._last:
            self._went_on.set()


class _Turns:
    """Stands in for the AgentRunner: the turn of the subtask named "cancelled" is cancelled from within."""

    def __init__(self) -> None:
        self.ended: list[str] = []

    async def run_turn(self, *, user_message: str, **_: Any) -> tuple[str, None, dict]:
        if user_message == "cancelled":
            await _cancelled_from_within()
        return "done", None, {}

    async def end_conversation(self, session_id: str, **_: Any) -> None:
        self.ended.append(session_id)


class _TurnThatNeverEnds(_Turns):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def run_turn(self, **_: Any) -> None:
        self.started.set()
        await asyncio.Event().wait()


def _worker_settings(hardened: bool = False, poll: float = TICK) -> Settings:
    return Settings.model_construct(
        subtask_workers=1,
        subtask_poll_interval=poll,
        subtask_default_timeout=60,
        subtask_hardening_enabled=hardened,
        subtask_max_attempts=1,
        subtask_cleanup_timeout_seconds=5,
        agent_id="test",
        telegram_bot_token=None,
        telegram_chat_id=None,
    )


def _worker_pool(name: str, cancelled: bool, *, in_its_dequeue: bool = False, hardened: bool = False) -> _Loop:
    went_on = asyncio.Event()
    in_its_turn = cancelled and not in_its_dequeue
    pending = ([_subtask("cancelled")] if in_its_turn else []) + [_subtask("next")]
    subtasks = _Subtasks(pending, went_on, cancel_a_dequeue=cancelled and in_its_dequeue)
    turns = _Turns()
    pool = SubtaskWorkerPool(
        runner=turns, heart=SimpleNamespace(subtasks=subtasks), settings=_worker_settings(hardened)
    )
    if in_its_dequeue:
        says = "cancelled from within"
    else:
        says = f"Subtask {pending[0].id.hex[:8]} " + ("hardened-path errored" if hardened else "failed")
    return _Loop(
        name,
        pool.start,
        pool.stop,
        lambda: list(pool._workers),
        went_on,
        "nous.handlers.subtask_worker",
        says,
        {"subtasks": subtasks, "turns": turns, "pending": pending},
    )


def _subtask_worker_turn(cancelled: bool) -> _Loop:
    return _worker_pool("subtask worker (its turn)", cancelled)


def _subtask_worker_hardened_turn(cancelled: bool) -> _Loop:
    return _worker_pool("subtask worker (its hardened turn)", cancelled, hardened=True)


def _subtask_worker_dequeue(cancelled: bool) -> _Loop:
    return _worker_pool("subtask worker (its dequeue)", cancelled, in_its_dequeue=True)


def _session_monitor_sweep(cancelled: bool) -> _Loop:
    went_on = asyncio.Event()
    cancel = _Once(cancelled)

    class _WorkingMemory:
        async def cleanup_stale(self, **_: Any) -> None:
            if cancel():
                await _cancelled_from_within()
            went_on.set()

    settings = SimpleNamespace(
        sleep_check_interval=TICK,
        session_idle_timeout=3600,
        sleep_timeout=3600,
        agent_id="test",
        working_memory_ttl_hours=1,
        working_memory_sweep_interval_seconds=0,
        working_memory_sweep_batch_size=10,
    )
    monitor = SessionTimeoutMonitor(EventBus(), settings, heart=SimpleNamespace(working_memory=_WorkingMemory()))
    return _Loop(
        "session monitor (its sweep)",
        monitor.start,
        monitor.stop,
        lambda: [monitor._task],
        went_on,
        "nous.handlers.session_monitor",
        "cancelled from within",
    )


def _session_monitor_closure(cancelled: bool) -> _Loop:
    went_on = asyncio.Event()
    reached = asyncio.Event()
    closed: list[str] = []

    class _Runner:
        async def end_conversation(self, session_id: str, **_: Any) -> bool:
            closed.append(session_id)
            if session_id == "cancelled":
                reached.set()
                await _cancelled_from_within()
            if session_id == "next":
                went_on.set()
            return True

    # Idle for longer than -1 s: every tracked session is due at the next check.
    settings = SimpleNamespace(
        sleep_check_interval=TICK,
        session_idle_timeout=-1,
        sleep_timeout=3600,
        agent_id="test",
        working_memory_ttl_hours=0,
    )
    monitor = SessionTimeoutMonitor(EventBus(), settings, runner=_Runner())

    async def start() -> None:
        if cancelled:
            monitor.touch("cancelled", "test")
            monitor.touch("same check", "test")
        await monitor.start()
        if cancelled:
            await _expect(reached, "the monitor never tried to close the idle session")
        monitor.touch("next", "test")  # due at a later check than the one that was cancelled from within

    return _Loop(
        "session monitor (a closure)",
        start,
        monitor.stop,
        lambda: [monitor._task],
        went_on,
        "nous.handlers.session_monitor",
        "Failed to end timed-out session cancelled",
        {"monitor": monitor, "closed": closed},
    )


def _task_scheduler(cancelled: bool) -> _Loop:
    went_on = asyncio.Event()
    cancel = _Once(cancelled)

    class _Schedules:
        async def get_due(self, now: Any) -> list:
            if cancel():
                await _cancelled_from_within()
            went_on.set()
            return []

    scheduler = TaskScheduler(SimpleNamespace(schedules=_Schedules()), SimpleNamespace(schedule_check_interval=TICK))
    return _Loop(
        "task scheduler",
        scheduler.start,
        scheduler.stop,
        lambda: [scheduler._task],
        went_on,
        "nous.handlers.task_scheduler",
        "cancelled from within",
    )


def _decision_review_sweep(cancelled: bool) -> _Loop:
    went_on = asyncio.Event()
    cancel = _Once(cancelled)

    class _Brain:
        async def get_unreviewed(self, max_age_days: int = 30) -> list:
            if cancel():
                await _cancelled_from_within()
            went_on.set()
            return []

    settings = SimpleNamespace(decision_sweep_interval=TICK, github_token="")
    reviewer = DecisionReviewer(_Brain(), settings, EventBus())
    return _Loop(
        "decision review sweep",
        reviewer.start,
        reviewer.stop,
        lambda: [reviewer._sweep_task],
        went_on,
        "nous.handlers.decision_reviewer",
        "cancelled from within",
    )


LOOPS = [
    _event_bus,
    _event_bus_dispatch,
    _subtask_worker_turn,
    _subtask_worker_hardened_turn,
    _subtask_worker_dequeue,
    _session_monitor_sweep,
    _session_monitor_closure,
    _task_scheduler,
    _decision_review_sweep,
]
every_loop = pytest.mark.parametrize(
    "build", LOOPS, ids=[build.__name__.strip("_").replace("_", " ") for build in LOOPS]
)


# ---------------------------------------------------------------------------
# A cancellation from within does not end a loop
# ---------------------------------------------------------------------------


@every_loop
def test_a_loop_goes_on_after_a_cancellation_from_within(build, caplog):
    loop = build(cancelled=True)

    async def scenario() -> None:
        await loop.start()
        tasks = loop.tasks()
        try:
            await _expect(loop.went_on, f"the {loop.name} did nothing more after something it awaited was cancelled")
            assert not any(task.done() for task in tasks), f"a task of the {loop.name} has ended"
        finally:
            await _stop(loop)
        assert all(task.done() for task in tasks), f"stop() left a task of the {loop.name} running"

    with caplog.at_level(logging.ERROR, logger=loop.logger):
        _run(scenario)

    said = [record.getMessage() for record in caplog.records if record.name == loop.logger]
    assert any(loop.says in line for line in said), said


# ---------------------------------------------------------------------------
# Its own cancellation still ends a loop
# ---------------------------------------------------------------------------


@every_loop
def test_stop_ends_a_loop(build):
    """Parity pin: green before this change too."""
    loop = build(cancelled=False)

    async def scenario() -> None:
        await loop.start()
        tasks = loop.tasks()
        await _expect(loop.went_on, f"the {loop.name} never ran")
        await _stop(loop)
        assert all(task.done() for task in tasks), f"stop() left a task of the {loop.name} running"

    _run(scenario)


@every_loop
def test_cancelling_its_task_ends_a_loop(build):
    """Parity pin: green before this change too. Nothing but the cancellation
    tells the loop to end here: stop() is called only afterwards."""
    loop = build(cancelled=False)

    async def scenario() -> None:
        await loop.start()
        tasks = loop.tasks()
        try:
            await _expect(loop.went_on, f"the {loop.name} never ran")
            for task in tasks:
                task.cancel()
            _, pending = await asyncio.wait(tasks, timeout=WAIT)
            assert not pending, f"a cancelled task of the {loop.name} was still running after {WAIT:.0f}s"
        finally:
            await _stop(loop)

    _run(scenario)


@every_loop
def test_event_loop_teardown_ends_a_loop(build):
    """Parity pin: green before this change too. The loop is left running when
    the scenario returns, so it is ``asyncio.run()`` that cancels it, as at
    interpreter exit."""
    loop = build(cancelled=False)

    async def scenario() -> None:
        await loop.start()
        await _expect(loop.went_on, f"the {loop.name} never ran")

    _run(scenario)


# ---------------------------------------------------------------------------
# Whose cancellation it is
# ---------------------------------------------------------------------------


def test_outside_a_task_a_cancellation_counts_as_requested():
    """Where there is no task, nothing could ever ask it to stop: the answer
    that ends a loop is the safe one."""
    from nous.cancellation import cancel_requested

    answers: list[bool] = []

    async def scenario() -> None:
        asyncio.get_running_loop().call_soon(lambda: answers.append(cancel_requested()))
        await asyncio.sleep(TICK)

    _run(scenario)

    assert answers == [True]


# ---------------------------------------------------------------------------
# The event bus: the event that was being dispatched
# ---------------------------------------------------------------------------


def test_the_other_handlers_of_an_event_finish_before_the_next_event_when_one_is_cancelled_from_within():
    bus = EventBus()
    went_on = asyncio.Event()
    order: list[str] = []

    async def handler_cancelled_from_within(event: Event) -> None:
        await _cancelled_from_within()

    async def slower_handler(event: Event) -> None:
        await asyncio.sleep(TICK)
        order.append("the other handler of the first event")

    async def next_handler(event: Event) -> None:
        order.append("the next event")
        went_on.set()

    bus.on("first", handler_cancelled_from_within)
    bus.on("first", slower_handler)
    bus.on("next", next_handler)

    async def scenario() -> None:
        await bus.start()
        await bus.emit(Event(type="first", agent_id="test"))
        await bus.emit(Event(type="next", agent_id="test"))
        try:
            await _expect(went_on, "the bus never dispatched the next event")
        finally:
            await asyncio.wait_for(bus.stop(), WAIT)

    _run(scenario)

    assert order == ["the other handler of the first event", "the next event"]
    first = next(event for event in bus.stats.recent_events() if event.type == "first")
    assert (first.handlers_invoked, first.handlers_failed) == (2, 1)
    failed = bus.stats.to_dict()["handlers"][handler_cancelled_from_within.__qualname__]
    assert (failed["errors"], failed["last_error_msg"]) == (1, "CancelledError")


def test_an_event_whose_persist_was_cancelled_from_within_still_reaches_its_handlers(caplog):
    bus = EventBus()
    went_on = asyncio.Event()
    handled: list[str] = []

    async def persist_cancelled_from_within(event: Event) -> None:
        await _cancelled_from_within()

    async def handler(event: Event) -> None:
        handled.append(event.type)
        went_on.set()

    bus.set_db_persister(persist_cancelled_from_within)
    bus.on("event", handler)

    async def scenario() -> None:
        await bus.start()
        await bus.emit(Event(type="event", agent_id="test"))
        try:
            await _expect(went_on, "the event never reached its handler")
        finally:
            await asyncio.wait_for(bus.stop(), WAIT)

    with caplog.at_level(logging.WARNING, logger="nous.events"):
        _run(scenario)

    assert handled == ["event"]
    said = [record.getMessage() for record in caplog.records if record.name == "nous.events"]
    assert any("DB persist was cancelled from within for event event" in line for line in said), said


def test_cancelling_the_bus_while_it_persists_an_event_ends_it():
    """Parity pin: green before this change too. The bus's own cancellation is
    not taken for a failed persist."""
    bus = EventBus()
    persisting = asyncio.Event()

    async def persist_that_never_ends(event: Event) -> None:
        persisting.set()
        await asyncio.Event().wait()

    bus.set_db_persister(persist_that_never_ends)

    async def scenario() -> None:
        await bus.start()
        task = bus._task
        await bus.emit(Event(type="event", agent_id="test"))
        try:
            await _expect(persisting, "the bus never persisted the event")
            task.cancel()
            _, pending = await asyncio.wait({task}, timeout=WAIT)
            assert not pending, "the bus went on after its task was cancelled"
        finally:
            await asyncio.wait_for(bus.stop(), WAIT)

    _run(scenario)


def test_stopping_the_event_bus_drains_past_a_handler_cancelled_from_within():
    bus = EventBus()
    busy, drained = asyncio.Event(), []

    async def keeps_the_loop_busy(event: Event) -> None:
        busy.set()
        await asyncio.Event().wait()

    async def handler_cancelled_from_within(event: Event) -> None:
        await _cancelled_from_within()

    async def last_handler(event: Event) -> None:
        drained.append(event.type)

    bus.on("busy", keeps_the_loop_busy)
    bus.on("cancelled", handler_cancelled_from_within)
    bus.on("last", last_handler)

    async def scenario() -> None:
        await bus.start()
        await bus.emit(Event(type="busy", agent_id="test"))
        await _expect(busy, "the bus never dispatched the first event")
        # Both stay in the queue: the loop is inside the first event's dispatch.
        await bus.emit(Event(type="cancelled", agent_id="test"))
        await bus.emit(Event(type="last", agent_id="test"))
        try:
            await asyncio.wait_for(bus.stop(), WAIT)
        except asyncio.CancelledError:
            pytest.fail("stop() raised the CancelledError of a handler it was draining")

    _run(scenario)

    assert drained == ["last"]
    errors = {name: stat["errors"] for name, stat in bus.stats.to_dict()["handlers"].items()}
    # The handler cancelled from within failed. The one stop() itself cancelled did not.
    assert errors.get(handler_cancelled_from_within.__qualname__) == 1, errors
    assert not errors.get(keeps_the_loop_busy.__qualname__), errors


# ---------------------------------------------------------------------------
# The subtask worker: the subtask it had claimed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("build", "the_following_ends_as"),
    [
        (_subtask_worker_turn, ("completed", "completed")),
        (_subtask_worker_hardened_turn, ("failed", "incomplete_no_terminal")),
    ],
    ids=["legacy path", "hardened path"],
)
def test_a_subtask_whose_turn_was_cancelled_from_within_is_failed_not_left_running(build, the_following_ends_as):
    loop = build(cancelled=True)
    subtasks, turns = loop.parts["subtasks"], loop.parts["turns"]
    cancelled, following = loop.parts["pending"]

    async def scenario() -> None:
        await loop.start()
        try:
            await _expect(loop.went_on, "the worker never ran the subtask that followed")
        finally:
            await _stop(loop)

    _run(scenario)

    assert [row[:3] for row in subtasks.settled] == [
        (cancelled.id, "failed", "errored"),
        (following.id, *the_following_ends_as),
    ]
    assert subtasks.settled[0][3] == "CancelledError: a call inside the turn was cancelled; the worker was not stopped"
    assert turns.ended == [f"subtask-{cancelled.id.hex[:8]}", f"subtask-{following.id.hex[:8]}"]


def test_a_worker_cancelled_from_within_again_and_again_waits_between_its_tries():
    tries = 0

    class _NeverAnswers:
        async def reclaim_stale(self) -> int:
            return 0

        async def dequeue(self, worker_id: str) -> None:
            nonlocal tries
            tries += 1
            await asyncio.sleep(0)  # a real query gives the event loop a turn before it fails
            await _cancelled_from_within()

    pool = SubtaskWorkerPool(
        runner=_Turns(), heart=SimpleNamespace(subtasks=_NeverAnswers()), settings=_worker_settings(poll=0.05)
    )

    async def scenario() -> None:
        await pool.start()
        await asyncio.sleep(0.5)
        await asyncio.wait_for(pool.stop(), WAIT)

    _run(scenario)

    # One try per poll interval: about ten in half a second, not one and not thousands.
    assert 2 <= tries < 50, tries


BOTH_PATHS = pytest.mark.parametrize("hardened", [False, True], ids=["legacy path", "hardened path"])


@BOTH_PATHS
def test_stopping_the_pool_mid_turn_leaves_the_subtask_for_the_next_start(hardened):
    """Parity pin: green before this change too. A worker stopped mid-turn
    does not fail its subtask: the row stays as it is and the next start
    reclaims it."""
    turns = _TurnThatNeverEnds()
    subtasks = _Subtasks([_subtask("never ends")], asyncio.Event(), cancel_a_dequeue=False)
    pool = SubtaskWorkerPool(
        runner=turns, heart=SimpleNamespace(subtasks=subtasks), settings=_worker_settings(hardened)
    )

    async def scenario() -> None:
        await pool.start()
        await _expect(turns.started, "the worker never started the turn")
        await asyncio.wait_for(pool.stop(), WAIT)

    _run(scenario)

    assert subtasks.settled == []


@BOTH_PATHS
def test_a_turn_that_runs_out_of_time_is_still_recorded_as_timed_out(hardened):
    """Parity pin: green before this change too. The timeout cancels the turn
    through the worker's own task, which is not a cancellation from within."""
    slow = _subtask("never ends")
    slow.timeout_seconds = 0.05
    went_on = asyncio.Event()
    subtasks = _Subtasks([slow], went_on, cancel_a_dequeue=False)
    pool = SubtaskWorkerPool(
        runner=_TurnThatNeverEnds(), heart=SimpleNamespace(subtasks=subtasks), settings=_worker_settings(hardened)
    )

    async def scenario() -> None:
        await pool.start()
        try:
            await _expect(went_on, "the subtask that ran out of time was never settled")
        finally:
            await asyncio.wait_for(pool.stop(), WAIT)

    _run(scenario)

    assert [row[:3] for row in subtasks.settled] == [(slow.id, "failed", "timed_out")]


# ---------------------------------------------------------------------------
# The session monitor: the other sessions of the same check
# ---------------------------------------------------------------------------


def test_a_closure_cancelled_from_within_fails_alone_and_its_check_is_not_repeated():
    loop = _session_monitor_closure(cancelled=True)
    monitor, closed = loop.parts["monitor"], loop.parts["closed"]

    async def scenario() -> None:
        await loop.start()
        try:
            await _expect(loop.went_on, "the monitor never ran a later check")
            await asyncio.sleep(5 * TICK)  # a few more checks: none of them may close a session again
        finally:
            await _stop(loop)

    _run(scenario)

    assert sorted(closed) == ["cancelled", "next", "same check"], closed
    assert monitor.get_stats()["tracked_sessions"] == 0


# ---------------------------------------------------------------------------
# The task scheduler: the other schedules of the same check
# ---------------------------------------------------------------------------


def _schedule(task: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        task=task,
        created_by_session=None,
        timeout_seconds=60,
        notify=False,
        model=None,
        frame_type=None,
        schedule_type="once",
        continuation_turns=0,
        continuation_session_id=None,
        continuation_count=0,
        fire_count=0,
    )


def test_a_schedule_cancelled_from_within_fails_alone_and_the_other_due_schedules_still_fire():
    first, second = _schedule("first"), _schedule("second")
    created: list[str] = []
    deactivated: list[str] = []
    cancel = _Once(True)

    class _Subtasks:
        async def create(self, *, task: str, **_: Any) -> None:
            if task == "first" and cancel():
                await _cancelled_from_within()
            created.append(task)

    class _Schedules:
        async def get_due(self, now: Any) -> list:
            return [first, second]

        async def deactivate(self, schedule_id: Any) -> None:
            deactivated.append(schedule_id)

    # No database: the check for a still-active subtask fails open, as it does on any error.
    scheduler = TaskScheduler(
        SimpleNamespace(schedules=_Schedules(), subtasks=_Subtasks()),
        SimpleNamespace(schedule_check_interval=TICK, schedule_continuation_enabled=False, agent_id="test"),
    )
    fired: list[int] = []

    async def scenario() -> None:
        try:
            fired.append(await scheduler._fire_due_tasks())
        except asyncio.CancelledError:
            pytest.fail("the check ended with the first schedule's cancellation; the second was never fired")

    _run(scenario)

    assert (created, deactivated, fired) == (["second"], [second.id], [1])


def test_cancelling_the_scheduler_while_it_fires_a_schedule_ends_it():
    """Parity pin: green before this change too. The scheduler's own
    cancellation, landing inside one schedule, is not taken for that
    schedule's failure."""
    creating = asyncio.Event()

    class _Subtasks:
        async def create(self, **_: Any) -> None:
            creating.set()
            await asyncio.Event().wait()

    class _Schedules:
        async def get_due(self, now: Any) -> list:
            return [_schedule("never created")]

    scheduler = TaskScheduler(
        SimpleNamespace(schedules=_Schedules(), subtasks=_Subtasks()),
        SimpleNamespace(schedule_check_interval=TICK, schedule_continuation_enabled=False, agent_id="test"),
    )

    async def scenario() -> None:
        await scheduler.start()
        task = scheduler._task
        try:
            await _expect(creating, "the scheduler never fired the schedule")
            task.cancel()
            _, pending = await asyncio.wait({task}, timeout=WAIT)
            assert not pending, "the scheduler went on after its task was cancelled"
        finally:
            await asyncio.wait_for(scheduler.stop(), WAIT)

    _run(scenario)


# ---------------------------------------------------------------------------
# One definition
# ---------------------------------------------------------------------------


def test_the_heartbeat_loops_ask_the_same_predicate():
    """The heartbeat and DAG tick loops follow the same rule. They ask the one
    predicate, not a copy of it that could drift."""
    from nous import cancellation
    from nous.heartbeat import runner

    assert runner._cancel_requested is cancellation.cancel_requested, "the heartbeat runner has a definition of its own"
