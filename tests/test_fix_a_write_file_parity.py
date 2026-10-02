"""A write_file with no compensation snapshot bound to the call is a
plain in-place write.

Phase 2.8 (#652) routed every write through temp file + rename. That is the
right primitive for a write that may be reverted; for every other write it
changed what the tool does to the file (a new inode, broken hard links,
different refusals) with all flags off.
"""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_fix_a_write_lock import _streamed_write
from test_runner_ledger import _FakeSnapStore, _FakeStore, _stream_runner

from nous.api import compensation
from nous.api.builtin_tools import register_builtin_tools, write_file_tool
from nous.api.tools import ToolDispatcher


@pytest.mark.asyncio
async def test_unsnapshotted_write_keeps_the_same_file(tmp_path):
    """Pins the pre-#652 behavior: the existing file is written in place, so
    its inode, owner, group and mode are untouched and a hard link to it sees
    the new content. On 236c110 the file was replaced by a new one."""
    target = tmp_path / "notes.txt"
    target.write_text("v1", encoding="utf-8")
    linked = tmp_path / "notes.hardlink"
    os.link(target, linked)
    before = target.stat()

    result = await write_file_tool("notes.txt", "v2", _workspace_dir=str(tmp_path))

    assert not result.get("is_error"), result
    after = target.stat()
    assert after.st_ino == before.st_ino
    assert (after.st_uid, after.st_gid, after.st_mode) == (before.st_uid, before.st_gid, before.st_mode)
    assert linked.read_text(encoding="utf-8") == "v2"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["notes.hardlink", "notes.txt"]


@pytest.mark.asyncio
async def test_unsnapshotted_write_keeps_a_held_path_lock_until_its_thread_ends(tmp_path, monkeypatch):
    """With compensation wired the runner holds the path's lock around every
    write_file. A cancelled call returns before its thread does, so the
    in-place write hands the runner that thread exactly as the snapshotted
    write does: the lock is released only once nothing can still land."""
    from nous.api import call_outcome
    from nous.api.call_outcome import CallOutcome
    from nous.api.compensation import release_write_path_lock_after, write_path_lock

    entered, gate = threading.Event(), threading.Event()
    real_write_text = Path.write_text

    def gated(self, *args, **kwargs):
        entered.set()
        assert gate.wait(10)
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", gated)
    lock = write_path_lock("f.txt", str(tmp_path))
    await lock.acquire()
    outcome = CallOutcome(write_lock=lock)
    token = call_outcome._current.set(outcome)
    try:
        task = asyncio.create_task(write_file_tool("f.txt", "late", _workspace_dir=str(tmp_path)))
    finally:
        call_outcome._current.reset(token)
    try:
        assert await asyncio.to_thread(entered.wait, 5), "write_file did not write in place"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # the runner's finally: the call has returned, its thread has not
        release_write_path_lock_after(lock, outcome.write_worker)
        assert lock.locked()
    finally:
        gate.set()
    await asyncio.wait_for(asyncio.shield(outcome.write_worker), 10)
    await asyncio.sleep(0)  # the done-callback runs on the next loop pass
    assert not lock.locked()
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "late"


def _stream_with_the_real_handler(tmp_path, store):
    """stream_chat with compensation wired, the real dispatcher and the real
    write_file handler, so the handler sees the CallOutcome the loop built.
    ``seen`` records, at dispatch: (the outcome's lock is held, its bound snapshot)."""
    real = ToolDispatcher()
    register_builtin_tools(real, SimpleNamespace(workspace_dir=str(tmp_path)))
    seen: list[tuple] = []

    async def dispatch(name, inp, **kw):
        outcome = kw["outcome"]
        seen.append((outcome.write_lock is not None and outcome.write_lock.locked(), outcome.write_target))
        return await real.dispatch(name, inp, **kw)

    runner = _stream_runner(store, dispatch)
    snaps = _FakeSnapStore()
    runner.set_snapshot_store(snaps, str(tmp_path))
    return runner, seen, snaps


@pytest.mark.asyncio
async def test_stream_compensation_on_writes_in_place_under_the_lock(tmp_path):
    """Every stream_chat write is unsnapshotted (the context is interactive).
    With compensation on it is still an in-place write, made while the loop
    holds the path's lock and with that lock on the call's CallOutcome."""
    store = _FakeStore()
    runner, seen, snaps = _stream_with_the_real_handler(tmp_path, store)
    runner._settings.tool_timeout = 5
    target = tmp_path / "notes.txt"
    target.write_text("v1", encoding="utf-8")
    inode = target.stat().st_ino
    key = compensation.write_path_key("notes.txt", str(tmp_path))
    runner._call_api_stream = _streamed_write("notes.txt")

    [e async for e in runner.stream_chat("s1", "go")]

    assert seen == [(True, None)]  # the lock is held and handed over; no snapshot is bound
    assert snaps.captured == []
    assert target.read_text(encoding="utf-8") == "c" and target.stat().st_ino == inode
    assert store.events[-1] == ("close", "id-write_file", "success")
    assert key not in compensation._write_path_locks


@pytest.mark.asyncio
async def test_stream_timeout_mid_write_keeps_the_lock_until_the_thread_ends(tmp_path, monkeypatch):
    """stream_chat is the loop that cuts a tool call off at tool_timeout, so
    it orphans a write's thread in ordinary operation. The path stays locked
    until that thread ends: the next write must not snapshot a half-written file."""
    entered, gate = threading.Event(), threading.Event()
    real_write_text = Path.write_text

    def gated(self, *args, **kwargs):
        entered.set()
        assert gate.wait(20)
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", gated)
    store = _FakeStore()
    runner, _, _ = _stream_with_the_real_handler(tmp_path, store)  # tool_timeout is 0.05 s in this harness
    key = compensation.write_path_key("late.txt", str(tmp_path))
    runner._call_api_stream = _streamed_write("late.txt")
    try:
        [e async for e in runner.stream_chat("s1", "go")]
        assert entered.is_set(), "write_file did not write in place"
        assert store.events[-1] == ("close", "id-write_file", "unknown")
        second_call_messages = runner._call_api_stream.call_args_list[1][0][1]
        assert "timed out" in str(second_call_messages[-1]["content"])
        lock = compensation._write_path_locks.get(key)
        # the call is over, its thread is not: the path must still be locked
        assert lock is not None and lock.locked()
        assert not (tmp_path / "late.txt").exists()
    finally:
        gate.set()
    for _ in range(300):
        if key not in compensation._write_path_locks:
            break
        await asyncio.sleep(0.01)
    assert key not in compensation._write_path_locks
    assert (tmp_path / "late.txt").read_text(encoding="utf-8") == "c"


@pytest.mark.asyncio
async def test_unencodable_content_leaves_the_target_alone(tmp_path):
    """Content that cannot be encoded as UTF-8 (a lone surrogate) is refused
    before anything is opened: an existing file keeps its content, and no
    file or directory is created. Written in place without that check, the
    file is truncated first and the encode fails afterwards."""
    target = tmp_path / "notes.txt"
    target.write_text("v1", encoding="utf-8")

    existing = await write_file_tool("notes.txt", "x\ud83dy", _workspace_dir=str(tmp_path))
    new = await write_file_tool("sub/new.txt", "x\ud83dy", _workspace_dir=str(tmp_path))

    assert existing.get("is_error") is True and new.get("is_error") is True
    assert target.read_text(encoding="utf-8") == "v1"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["notes.txt"]
