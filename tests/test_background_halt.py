"""Goals PR 0c: the halt object, its predicate, its epoch and its serialized transition (pure unit, a fake store)."""

from __future__ import annotations

import asyncio

import pytest

from nous import background_halt
from nous.api.execution_context import CONTEXT_KINDS
from nous.background_halt import BackgroundHalt, HaltRow


class FakeStore:
    """The store protocol: ``read``, ``write(row, audit)`` in one transaction, ``audit(entry)``."""

    def __init__(self, row: HaltRow | None = None) -> None:
        self.row = row
        self.audits: list = []
        self.fail_writes = 0
        self.gate: asyncio.Event | None = None
        self.entered = asyncio.Event()
        self.read_error: Exception | None = None

    async def read(self) -> HaltRow | None:
        if self.read_error is not None:
            raise self.read_error
        return self.row

    async def write(self, row: HaltRow, audit) -> None:
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.fail_writes:
            self.fail_writes -= 1
            raise RuntimeError("the database is down")
        self.row = row
        self.audits.append(audit)

    async def audit(self, entry) -> None:
        self.audits.append(entry)


def make(store: FakeStore | None = None, *, host_hold: bool = False, retry: float = 0.01) -> BackgroundHalt:
    return BackgroundHalt(host_hold=host_hold, store=store or FakeStore(), retry_seconds=retry)


FOREGROUND = {"interactive", "mcp"}


async def test_refuses_matrix():
    halt = make()
    epoch = halt.epoch
    for kind in CONTEXT_KINDS:
        assert not halt.refuses(kind, "s1", epoch), kind
        assert not halt.refuses(kind, "claude-abc", epoch), kind
    await halt.halt(actor="t", surface="dashboard")
    for kind in CONTEXT_KINDS:
        if kind == "approved_action":
            assert not halt.refuses(kind, "proposal-x", epoch)
            assert not halt.refuses(kind, "s1", None)
        elif kind in FOREGROUND:
            assert not halt.refuses(kind, "s1", epoch), kind
            assert not halt.refuses(kind, None, epoch), kind
            for session in ("claude-abc", "subtask-1", "proposal-x"):
                assert halt.refuses(kind, session, epoch), (kind, session)
        else:
            assert halt.refuses(kind, "s1", epoch), kind
            assert halt.refuses(kind, None, None), kind


async def test_a_turn_from_an_older_epoch_is_refused_after_resume():
    halt = make()
    e0 = halt.epoch
    await halt.halt(actor="t", surface="dashboard")
    outcome = await halt.resume(actor="t", via="dashboard")
    assert outcome.status == "resumed" and not halt.halted
    assert halt.refuses("subtask", "subtask-1", e0)
    assert halt.refuses("interactive", "claude-job", e0)
    assert not halt.refuses("interactive", "s1", e0)  # a chat turn is never refused
    assert not halt.refuses("subtask", "subtask-1", halt.epoch)  # a turn started after the resume runs
    assert not halt.refuses("subtask", "subtask-1", None)  # not captured: only the flag counts


async def test_resume_does_not_clear_a_halt_that_arrived_during_it():
    store = FakeStore()
    halt = make(store)
    await halt.halt(actor="t", surface="dashboard")
    store.gate = asyncio.Event()
    store.entered.clear()
    resuming = asyncio.create_task(halt.resume(actor="t", via="dashboard"))
    await store.entered.wait()  # the resume is inside its write
    second = asyncio.create_task(halt.halt(actor="t", surface="telegram"))
    await asyncio.sleep(0)
    assert halt.halted  # memory first: on at once
    store.gate.set()
    outcome = await resuming
    await second
    assert outcome.status == "raced"
    assert halt.halted
    assert store.row.halted  # the halt's write landed after the resume's
    assert halt.persisted


async def test_a_resume_queued_before_a_halt_does_not_clear_it():
    """The resume reads the sequence when it is called, not when it gets the lock: a halt that lands while the
    resume waits for the lock wins."""
    store = FakeStore()
    halt = make(store)
    await halt.halt(actor="t", surface="dashboard")
    store.gate = asyncio.Event()
    store.entered.clear()
    first = asyncio.create_task(halt.halt(actor="t", surface="dashboard"))  # holds the lock in its write
    await store.entered.wait()
    resuming = asyncio.create_task(halt.resume(actor="t", via="dashboard"))
    await asyncio.sleep(0)
    third = asyncio.create_task(halt.halt(actor="t", surface="telegram"))
    await asyncio.sleep(0)
    store.gate.set()
    await first
    outcome = await resuming
    await third
    assert outcome.status == "raced"
    assert halt.halted and store.row.halted


async def test_a_failed_halt_write_leaves_the_flag_on_and_retries():
    store = FakeStore()
    store.fail_writes = 2
    halt = make(store, retry=0.01)
    outcome = await halt.halt(actor="t", surface="dashboard")
    assert halt.halted and not outcome.persisted and not halt.persisted
    assert halt.snapshot()["persisted"] is False
    for _ in range(200):
        if halt.persisted:
            break
        await asyncio.sleep(0.01)
    assert halt.persisted and store.row.halted
    assert [a.action for a in store.audits] == ["halt"]  # one audit row, written with the state that landed
    assert halt.halted


async def test_a_retry_stops_after_a_newer_transition():
    store = FakeStore()
    store.fail_writes = 1
    halt = make(store, retry=0.05)
    await halt.halt(actor="t", surface="dashboard")
    assert not halt.persisted
    outcome = await halt.resume(actor="t", via="dashboard")  # lands, and supersedes the pending retry
    assert outcome.status == "resumed"
    await asyncio.sleep(0.15)
    assert not halt.halted and store.row.halted is False
    assert [a.action for a in store.audits] == ["resume"]


async def test_a_failed_resume_write_keeps_the_halt():
    store = FakeStore()
    halt = make(store)
    await halt.halt(actor="t", surface="dashboard")
    store.fail_writes = 1
    outcome = await halt.resume(actor="t", via="dashboard")
    assert outcome.status == "failed" and halt.halted and store.row.halted


async def test_host_hold_wins_and_resume_refuses():
    store = FakeStore(HaltRow(halted=False))
    halt = make(store, host_hold=True)
    await halt.load()
    assert halt.halted and halt.source == "host"
    assert halt.refuses("scheduled", None, halt.epoch)
    outcome = await halt.resume(actor="t", via="dashboard")
    assert outcome.status == "host_hold" and halt.halted
    assert [a.action for a in store.audits] == ["resume_refused"]


async def test_unreadable_state_starts_halted():
    store = FakeStore()
    store.read_error = RuntimeError("no such table")
    halt = make(store)
    await halt.load()
    assert halt.halted and halt.source == "unreadable"
    outcome = await halt.resume(actor="t", via="dashboard")
    assert outcome.status == "resumed" and not halt.halted and halt.source == "none"


async def test_a_stored_halt_is_restored_with_who_and_when():
    halt = make()
    await halt.halt(actor="telegram:1", surface="telegram", reason="  stop\nnow ", verified=True)
    again = BackgroundHalt(host_hold=False, store=halt._store)
    await again.load()
    snap = again.snapshot()
    assert again.halted and snap["source"] == "stored" and snap["by"] == "telegram:1"
    assert snap["surface"] == "telegram" and snap["verified"] is True and snap["reason"] == "stop now"
    assert snap["since"] is not None and snap["persisted"] is True


async def test_texts_are_cut_and_surfaces_are_a_fixed_set():
    halt = make()
    await halt.halt(actor="x" * 500, surface="somewhere", reason="r" * 900)
    snap = halt.snapshot()
    assert len(snap["by"]) == 100 and len(snap["reason"]) == 200 and snap["surface"] == "unknown"


def test_default_current_is_never_halted_until_installed():
    background_halt.install(None)
    default = background_halt.current()
    assert not default.halted and not background_halt.background_halted()
    for kind in CONTEXT_KINDS:
        assert not default.refuses(kind, "claude-x", None)
    with pytest.raises(RuntimeError):
        asyncio.run(default.halt(actor="t", surface="agent"))


async def test_background_halted_reads_the_installed_object():
    halt = make()
    background_halt.install(halt)
    try:
        assert background_halt.current() is halt and not background_halt.background_halted()
        await halt.halt(actor="t", surface="agent")
        assert background_halt.background_halted()
    finally:
        background_halt.install(None)
    assert not background_halt.background_halted()


def test_the_turn_epoch_scope_restores_the_outer_value():
    assert background_halt.TURN_EPOCH.get() is None
    with background_halt.epoch_scope(3):
        assert background_halt.turn_epoch() == 3
        with background_halt.epoch_scope(5):
            assert background_halt.TURN_EPOCH.get() == 5
        assert background_halt.TURN_EPOCH.get() == 3
    assert background_halt.TURN_EPOCH.get() is None
    assert background_halt.turn_epoch() == background_halt.current().epoch


def test_inside_a_tool_call_the_turn_epoch_is_the_calls_unless_an_outer_one_is_set():
    token = background_halt.CALL_SCOPE.set(("interactive", "claude-job-1", 4))  # as the dispatcher sets it
    try:
        assert background_halt.turn_epoch() == 4
        with background_halt.epoch_scope(2):
            assert background_halt.turn_epoch() == 2  # the earliest start wins
    finally:
        background_halt.CALL_SCOPE.reset(token)
    token = background_halt.CALL_SCOPE.set(("subtask", "subtask-1", None))  # a call handed no epoch
    try:
        assert background_halt.turn_epoch() == background_halt.current().epoch
    finally:
        background_halt.CALL_SCOPE.reset(token)


async def test_announce_false_records_and_gates_and_tells_the_caller_to_send_nothing():
    store = FakeStore()
    halt = make(store)
    quiet = await halt.halt(actor="t", surface="agent", reason="r", announce=False)
    assert halt.halted and halt.refuses("subtask", None, None)
    assert [a.action for a in store.audits] == ["halt"] and store.row.halted
    assert quiet.announce is False
    assert (await halt.halt(actor="t", surface="agent")).announce is True
    assert (await halt.halt(actor="t", surface="host")).announce is True
    assert (await halt.halt(actor="t", surface="dashboard")).announce is False


async def test_a_repeat_halt_writes_a_new_log_row_and_keeps_the_first_since():
    store = FakeStore()
    halt = make(store)
    first = await halt.halt(actor="telegram:1", surface="telegram", reason="the bill")
    since = halt.snapshot()["since"]
    again = await halt.halt(actor="dashboard", surface="dashboard", reason="still the bill")
    assert (first.already, again.already) == (False, True)
    rows = [a for a in store.audits if a.action == "halt"]
    assert [(r.actor, r.reason) for r in rows] == [("telegram:1", "the bill"), ("dashboard", "still the bill")]
    snap = halt.snapshot()
    assert (snap["since"], snap["by"], snap["reason"]) == (since, "telegram:1", "the bill")
    assert store.row.halted_by == "telegram:1" and store.row.halted_at is not None


async def test_persisted_is_known_right_after_the_first_write():
    landed = await make(FakeStore()).halt(actor="t", surface="dashboard")
    assert landed.persisted is True
    store = FakeStore()
    store.fail_writes = 1
    halt = make(store, retry=60)
    failed = await halt.halt(actor="t", surface="dashboard")
    assert failed.persisted is False and halt.persisted is False and halt.snapshot()["persisted"] is False
    await halt.close()
