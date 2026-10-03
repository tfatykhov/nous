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
from typing import Any

import pytest

from nous.events import Event, EventBus

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


LOOPS = [
    _event_bus,
    _event_bus_dispatch,
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
