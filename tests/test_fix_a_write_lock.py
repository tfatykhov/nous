"""PR A: the per-path write lock (#652) engages only when compensation is
wired, can never raise out of a tool loop, and is never waited for forever.

On 236c110 every write_file -- with every flag off -- computed a lock key one
line before the loop's ``try`` (a NUL byte in the path killed the turn) and
then waited for that lock with no bound.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from test_runner_authorization import (
    AgentRunner,
    _MockBrain,
    _MockCognitive,
    _MockHeart,
    _one_tool_call_then_done_with,
    _run_loop,
    _settings,
)
from test_runner_ledger import _FakeSnapStore, _FakeStore, _stream_runner
from test_runner_ledger import _runner as _ledger_runner

from nous.api import compensation
from nous.api import runner as runner_module
from nous.api.builtin_tools import register_builtin_tools, write_file_tool
from nous.api.execution_context import ExecutionContext
from nous.api.tools import ToolDispatcher

_SUBTASK = {"is_background": True, "context": ExecutionContext(kind="subtask", session_id="s1")}

# Paths no lock key can be computed for: os.path.realpath raises ValueError.
_UNUSABLE = [
    pytest.param("a\x00b.txt", id="nul-byte"),
    pytest.param(
        "a\ud83d.txt",
        id="lone-surrogate",
        marks=pytest.mark.skipif(
            sys.platform == "win32", reason="a lone surrogate is a legal NTFS name; only POSIX cannot encode it"
        ),
    ),
]


def _streamed_write(path: str):
    """A streamed model turn that calls write_file(path) once, then finishes."""
    from nous.api.anthropic_client import StreamEvent

    calls = {"n": 0}

    async def fake_stream(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            yield StreamEvent(type="tool_start", tool_name="write_file", tool_id="t1", block_index=1)
            yield StreamEvent(type="tool_input_delta", text=json.dumps({"path": path, "content": "c"}), block_index=1)
            yield StreamEvent(type="block_stop", block_index=1)
            yield StreamEvent(type="done", stop_reason="tool_use")
        else:
            yield StreamEvent(type="text_delta", text="ok")
            yield StreamEvent(type="done", stop_reason="end_turn")

    return MagicMock(side_effect=fake_stream)


def _held_lock_recorder(key: str, seen: list[tuple[bool, bool]]):
    """A dispatch stand-in recording, at dispatch time, whether the path's
    lock is held and whether the call's CallOutcome carries that same lock."""

    async def dispatch(name, inp, **kw):
        held = compensation._write_path_locks.get(key)
        seen.append((held is not None and held.locked(), held is not None and kw["outcome"].write_lock is held))
        return "ok", False

    return dispatch


@pytest.mark.asyncio
@pytest.mark.parametrize("compensation_on", [False, True], ids=["flags-off", "compensation-on"])
@pytest.mark.parametrize("path", _UNUSABLE)
async def test_unusable_path_is_a_tool_error_not_a_dead_turn(tmp_path, path, compensation_on):
    """_tool_loop, the real dispatcher and the real write_file handler: the
    handler refuses the path, as it always did, and the ledger row closes.
    ``compensation-on`` wires auto-review and a card publisher as well, so
    the snapshot capture is engaged for this call and meets the same path."""
    store = _FakeStore()
    settings = _settings(
        workspace_dir=str(tmp_path),
        compensation_enabled=compensation_on,
        compensation_auto_review_enabled=compensation_on,
    )
    r = AgentRunner(_MockCognitive(), _MockBrain(), _MockHeart(), settings)
    d = ToolDispatcher()
    register_builtin_tools(d, settings)
    r.set_dispatcher(d)
    r.set_ledger_store(store)
    snaps = _FakeSnapStore()
    cards: list[str] = []

    async def publish_card(tool_name, entry_id, session_id):
        cards.append(tool_name)

    if compensation_on:
        r.set_snapshot_store(snaps, str(tmp_path))
        r.set_action_review_pusher(publish_card)
    r._call_api = _one_tool_call_then_done_with("write_file", {"path": path, "content": "x"})

    _, tool_results, *_ = await _run_loop(r, **_SUBTASK)

    assert len(tool_results) == 1 and tool_results[0].result is None
    assert tool_results[0].error  # the handler's own message about the path
    assert store.events == [("open", "write_file", "subtask"), ("close", "id-write_file", "error")]
    assert snaps.captured == [] and cards == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _UNUSABLE)
async def test_stream_unusable_path_is_a_tool_error_not_a_dead_turn(tmp_path, path):
    """stream_chat: the same call, the same outcome."""
    store = _FakeStore()
    results: list[tuple[str, bool]] = []

    async def real_write_file(name, inp, **kw):
        result = await write_file_tool(inp["path"], inp["content"], _workspace_dir=str(tmp_path))
        results.append((result["content"][0]["text"], bool(result.get("is_error"))))
        return results[-1]

    runner = _stream_runner(store, real_write_file)
    runner._workspace_dir = str(tmp_path)
    runner._call_api_stream = _streamed_write(path)

    [e async for e in runner.stream_chat("s1", "go")]

    assert [is_error for _, is_error in results] == [True]
    assert store.events[-1] == ("close", "id-write_file", "error")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="ntpath.realpath computes a key even for a NUL byte")
@pytest.mark.parametrize("path", ["a\x00b.txt", "a\ud83d.txt"], ids=["nul-byte", "lone-surrogate"])
async def test_acquire_write_lock_takes_no_lock_for_a_path_without_a_key(tmp_path, path):
    """The helper's own contract with compensation wired: no key, no lock.
    The only thing it raises is SnapshotBlocksDispatch, for a lock timeout."""
    runner = object.__new__(AgentRunner)
    runner._snap_store = _FakeSnapStore()
    runner._workspace_dir = str(tmp_path)
    runner._dispatcher = SimpleNamespace()  # no repaired_args: the input is used as is

    assert await runner._acquire_write_lock("write_file", {"path": path, "content": "x"}) is None


@pytest.mark.asyncio
async def test_a_lock_that_cannot_be_taken_does_not_let_an_undoable_write_through(tmp_path, monkeypatch, caplog):
    """The helper is total. A failure nobody expected while taking the lock is
    logged and means "no lock", so the snapshot capture still runs -- and it
    refuses an undoable write it cannot serialize. Raised into the loop
    instead, the failure would be dropped there together with the capture,
    and the write dispatched with neither lock nor snapshot."""

    def unavailable(path, workspace_dir):
        raise RuntimeError("lock table unavailable")

    monkeypatch.setattr(runner_module, "write_path_lock", unavailable)
    store = _FakeStore()
    r, d = _ledger_runner(store, compensation_enabled=True)
    snaps = _FakeSnapStore()
    r.set_snapshot_store(snaps, str(tmp_path))
    r._call_api = _one_tool_call_then_done_with("write_file", {"path": "notes.txt", "content": "x"})

    with caplog.at_level(logging.WARNING, logger="nous.api.runner"):
        _, tool_results, *_ = await _run_loop(
            r, is_background=True, context=ExecutionContext(kind="dag_node", session_id="s1", undoable=True)
        )

    assert d.calls == []  # never dispatched
    assert tool_results[0].result is None and "write_file refused" in tool_results[0].error
    assert store.events == [("open", "write_file", "dag_node"), ("close", "id-write_file", "blocked")]
    assert snaps.captured == []
    assert [rec.getMessage() for rec in caplog.records if "no path lock" in rec.getMessage()] == [
        "Harness Phase 2.8: no path lock for write_file 'notes.txt' (RuntimeError: lock table unavailable)"
    ]


@pytest.mark.asyncio
async def test_flags_off_write_file_takes_no_path_lock(tmp_path):
    """With compensation off nothing can be reverted, so nothing is
    serialized: no lock object exists while the write is dispatched."""
    store = _FakeStore()
    r, d = _ledger_runner(store)
    r._workspace_dir = str(tmp_path)
    key = compensation.write_path_key("plain.txt", str(tmp_path))
    seen: list[tuple[bool, object]] = []

    async def dispatch(name, inp, **kw):
        seen.append((key in compensation._write_path_locks, kw["outcome"].write_lock))
        return "ok", False

    d.dispatch = dispatch
    r._call_api = _one_tool_call_then_done_with("write_file", {"path": "plain.txt", "content": "x"})

    await _run_loop(r, **_SUBTASK)

    assert seen == [(False, None)]


@pytest.mark.asyncio
async def test_tool_loop_hands_the_held_lock_to_the_call(tmp_path):
    """Guard (green on 236c110): with compensation wired, the lock the loop
    holds is the one on the call's CallOutcome -- what the snapshot capture
    checks before it trusts a snapshot, and what the handler is given."""
    store = _FakeStore()
    r, d = _ledger_runner(store, compensation_enabled=True)
    r.set_snapshot_store(_FakeSnapStore(), str(tmp_path))
    key = compensation.write_path_key("plain.txt", str(tmp_path))
    seen: list[tuple[bool, bool]] = []
    d.dispatch = _held_lock_recorder(key, seen)
    r._call_api = _one_tool_call_then_done_with("write_file", {"path": "plain.txt", "content": "x"})

    await _run_loop(r, **_SUBTASK)

    assert seen == [(True, True)]
    assert key not in compensation._write_path_locks  # released once the call is over


@pytest.mark.asyncio
async def test_stream_hands_the_held_lock_to_the_call(tmp_path):
    """Guard (green on 236c110): stream_chat, the same hand-over."""
    store = _FakeStore()
    key = compensation.write_path_key("plain.txt", str(tmp_path))
    seen: list[tuple[bool, bool]] = []
    runner = _stream_runner(store, _held_lock_recorder(key, seen))
    runner.set_snapshot_store(_FakeSnapStore(), str(tmp_path))
    runner._call_api_stream = _streamed_write("plain.txt")

    [e async for e in runner.stream_chat("s1", "go")]

    assert seen == [(True, True)]
    assert key not in compensation._write_path_locks


@pytest.mark.asyncio
async def test_contended_path_lock_refuses_the_call_after_a_bounded_wait(tmp_path, monkeypatch):
    """_tool_loop, compensation on: a path whose lock is still held after the
    bounded wait (an earlier write's thread that never returned) refuses the
    call -- a tool error and a closed ledger row -- instead of waiting
    forever. The bound is its own constant: ``tool_timeout`` stays at its
    120 s default here, and nothing times a _tool_loop dispatch anyway."""
    monkeypatch.setattr(runner_module, "_WRITE_LOCK_WAIT_SECONDS", 0.05, raising=False)
    store = _FakeStore()
    r, d = _ledger_runner(store, compensation_enabled=True)
    r.set_snapshot_store(_FakeSnapStore(), str(tmp_path))
    held = compensation.write_path_lock("busy.txt", str(tmp_path))
    await held.acquire()
    try:
        r._call_api = _one_tool_call_then_done_with("write_file", {"path": "busy.txt", "content": "x"})
        _, tool_results, *_ = await asyncio.wait_for(_run_loop(r, **_SUBTASK), timeout=5)
    finally:
        compensation.release_write_path_lock(held)

    assert d.calls == []  # never dispatched
    assert tool_results[0].result is None
    assert "another write to 'busy.txt' still held its lock after 0.05s" in tool_results[0].error
    assert store.events == [("open", "write_file", "subtask"), ("close", "id-write_file", "blocked")]
    assert compensation.write_path_key("busy.txt", str(tmp_path)) not in compensation._write_path_locks


@pytest.mark.asyncio
async def test_stream_contended_path_lock_refuses_the_call_after_a_bounded_wait(tmp_path, monkeypatch):
    """stream_chat: the same refusal, long before the tool timeout."""
    monkeypatch.setattr(runner_module, "_WRITE_LOCK_WAIT_SECONDS", 0.05, raising=False)
    store = _FakeStore()
    dispatched: list[str] = []

    async def dispatch(name, inp, **kw):
        dispatched.append(name)
        return "ok", False

    runner = _stream_runner(store, dispatch)
    runner._settings.tool_timeout = 60
    runner.set_snapshot_store(_FakeSnapStore(), str(tmp_path))
    held = compensation.write_path_lock("busy.txt", str(tmp_path))
    await held.acquire()

    async def drain():
        return [e async for e in runner.stream_chat("s1", "go")]

    try:
        runner._call_api_stream = _streamed_write("busy.txt")
        await asyncio.wait_for(drain(), timeout=5)
    finally:
        compensation.release_write_path_lock(held)

    assert dispatched == []
    assert store.events[-1] == ("close", "id-write_file", "blocked")
    second_call_messages = runner._call_api_stream.call_args_list[1][0][1]
    assert "still held its lock" in str(second_call_messages[-1]["content"])


@pytest.mark.asyncio
async def test_revert_does_not_wait_forever_for_the_path_lock(tmp_path, monkeypatch):
    """The revert takes the same lock. Tapped while a write to that path is
    still in flight -- or pinned by a thread that never returns -- it reports
    failure and leaves the card live for a retry, instead of holding its
    surface's lock (and the HTTP request) forever."""
    import hashlib
    import uuid

    from nous.api.compensation import compensate_write_file

    monkeypatch.setattr(compensation, "_REVERT_LOCK_WAIT_SECONDS", 0.05, raising=False)
    target = tmp_path / "busy.txt"
    target.write_text("ours", encoding="utf-8")
    snap = {
        "full_path": str(target.resolve()),
        "workspace_root": str(tmp_path.resolve()),
        "existed": False,
        "prior_b64": None,
        "written_content_hash": hashlib.sha256(b"ours").hexdigest(),
        "written_size": 4,
    }
    held = compensation.write_path_lock("busy.txt", str(tmp_path))
    await held.acquire()
    try:
        result = await asyncio.wait_for(compensate_write_file(uuid.uuid4(), snap, None), timeout=5)
    finally:
        compensation.release_write_path_lock(held)

    assert not result.success and "still in flight" in result.message
    assert target.read_text(encoding="utf-8") == "ours"  # nothing was changed
    assert compensation.write_path_key("busy.txt", str(tmp_path)) not in compensation._write_path_locks
