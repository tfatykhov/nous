"""A write_file with no compensation snapshot bound to the call is a
plain in-place write.

Phase 2.8 (#652) routed every write through temp file + rename. That is the
right primitive for a write that may be reverted; for every other write it
changed what the tool does to the file (a new inode, broken hard links,
different refusals) with all flags off.
"""

from __future__ import annotations

import asyncio
import errno
import os
import shutil
import socket
import stat
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_fix_a_write_lock import _streamed_write
from test_runner_ledger import _FakeSnapStore, _FakeStore, _stream_runner

from nous.api import builtin_tools, compensation
from nous.api.builtin_tools import PreconditionFailed, atomic_replace_bytes, register_builtin_tools, write_file_tool
from nous.api.tools import ToolDispatcher

_FIFO = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFOs only")
_POSIX = pytest.mark.skipif(os.name != "posix", reason="POSIX links, file types, modes, dir_fd calls")
_NOT_REGULAR = "Refused to write '{0}': '{0}' is not a regular file; nothing was written."


def _after_validation(monkeypatch, swap) -> None:
    """Run ``swap`` once, right after write_file's own path validation has
    passed: from then on the path is trusted, and it has just stopped being
    what was validated."""
    real = builtin_tools._validate_path
    pending = [swap]

    def validate(path_str, workspace_dir):
        target = real(path_str, workspace_dir)
        if pending:
            pending.pop()()
        return target

    monkeypatch.setattr(builtin_tools, "_validate_path", validate)


def _hold_the_write(monkeypatch) -> tuple[threading.Event, threading.Event]:
    """Stop the in-place write's thread before it touches anything, until
    ``gate`` is set; ``entered`` says the thread got that far."""
    entered, gate = threading.Event(), threading.Event()
    real = builtin_tools._write_text_in_place

    def held(*args):
        entered.set()
        assert gate.wait(20)
        return real(*args)

    monkeypatch.setattr(builtin_tools, "_write_text_in_place", held)
    return entered, gate


def _tree(root: Path) -> dict[str, str]:
    """Every entry below ``root``: a link's destination, a directory, or a file's content."""
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_symlink():
            out[rel] = f"<link to {os.readlink(p)}>"
        elif p.is_dir():
            out[rel] = "<dir>"
        else:
            out[rel] = p.read_text(encoding="utf-8")
    return out


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
async def test_content_is_written_as_utf_8(tmp_path):
    """Whatever the locale's encoding, the file holds the content as UTF-8,
    as Path.write_text(encoding="utf-8") wrote it before #652."""
    content = "héllo 世界"

    result = await write_file_tool("u.txt", content, _workspace_dir=str(tmp_path))

    assert not result.get("is_error"), result
    assert (tmp_path / "u.txt").read_bytes() == content.encode("utf-8")


@pytest.mark.asyncio
async def test_unsnapshotted_write_keeps_a_held_path_lock_until_its_thread_ends(tmp_path, monkeypatch):
    """With compensation wired the runner holds the path's lock around every
    write_file. A cancelled call returns before its thread does, so the
    in-place write hands the runner that thread exactly as the snapshotted
    write does: the lock is released only once nothing can still land."""
    from nous.api import call_outcome
    from nous.api.call_outcome import CallOutcome
    from nous.api.compensation import release_write_path_lock_after, write_path_lock

    entered, gate = _hold_the_write(monkeypatch)
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


@pytest.mark.asyncio
async def test_a_write_with_no_lock_held_is_awaited_by_the_call(tmp_path):
    """Both loops give every call an outcome; with compensation off it holds
    no lock. Such a write is awaited by the call itself, as before #652, and
    not handed to a task of its own: a call cancelled before its thread has
    started then never writes."""
    from nous.api import call_outcome
    from nous.api.call_outcome import CallOutcome

    outcome = CallOutcome()
    token = call_outcome._current.set(outcome)
    try:
        result = await write_file_tool("f.txt", "x", _workspace_dir=str(tmp_path))
    finally:
        call_outcome._current.reset(token)

    assert not result.get("is_error"), result
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "x"
    assert outcome.write_worker is None


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
    entered, gate = _hold_the_write(monkeypatch)
    store = _FakeStore()
    runner, _, _ = _stream_with_the_real_handler(tmp_path, store)
    runner._settings.tool_timeout = 1
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


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [123, None, True], ids=["a-number", "null", "a-bool"])
async def test_content_that_is_not_a_string_keeps_its_error(tmp_path, content):
    """A model can send a number, null or a bool as the content, and the
    dispatcher passes it on. It is refused with the words the write had for
    it before #652, before anything is opened or made."""
    target = tmp_path / "notes.txt"
    target.write_text("v1", encoding="utf-8")

    existing = await write_file_tool("notes.txt", content, _workspace_dir=str(tmp_path))
    new = await write_file_tool("sub/new.txt", content, _workspace_dir=str(tmp_path))

    said = f"Error writing file: data must be str, not {type(content).__name__}"
    assert existing.get("is_error") is True and new.get("is_error") is True
    assert [existing["content"][0]["text"], new["content"][0]["text"]] == [said, said]
    assert target.read_text(encoding="utf-8") == "v1"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["notes.txt"]


@_FIFO
@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["pipe", "link"], ids=["fifo", "link-to-the-fifo"])
async def test_in_place_write_to_a_fifo_is_refused_at_once(tmp_path, path):
    """A FIFO with no reader blocks whoever opens it for writing, for good.
    The in-place write opens without waiting and refuses it with the refusal
    a snapshotted write gives, so the call returns at once and no thread is
    left in open() -- also when the path is a link to the FIFO."""
    pipe = tmp_path / "pipe"
    os.mkfifo(pipe)
    (tmp_path / "link").symlink_to(pipe)
    try:
        result = await asyncio.wait_for(write_file_tool(path, "x", _workspace_dir=str(tmp_path)), timeout=2)
    finally:
        # A reader, so that a write that did block in open() can end and a
        # failure here cannot hang the whole run.
        os.close(os.open(pipe, os.O_RDONLY | os.O_NONBLOCK))

    assert result.get("is_error") is True
    assert result["content"][0]["text"] == (
        f"Refused to write '{path}': 'pipe' is not a regular file; nothing was written."
    )
    assert stat.S_ISFIFO(pipe.lstat().st_mode)


@_FIFO
def test_the_compare_and_replace_write_gives_the_same_refusal(tmp_path):
    """One definition: the primitive a snapshot-bound write goes through
    refuses a FIFO with the same words, and does not block on it either."""
    os.mkfifo(tmp_path / "pipe")

    with pytest.raises(PreconditionFailed, match="^'pipe' is not a regular file$"):
        atomic_replace_bytes(tmp_path / "pipe", b"x", root=tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["sub", ""], ids=["a-directory", "the-workspace-itself"])
async def test_a_directory_as_the_target_is_still_the_writes_own_error(tmp_path, path):
    """Guard: the refusal is for files that can block or misbehave when
    opened. A directory never did, nor does the workspace itself, which an
    empty path names: the write reports it with the text it had before #652,
    the error of the open with the whole path in it."""
    ws = tmp_path.resolve()
    (ws / "sub").mkdir()

    result = await write_file_tool(path, "x", _workspace_dir=str(ws))

    said = "[Errno 21] Is a directory" if os.name == "posix" else "[Errno 13] Permission denied"
    assert result.get("is_error") is True
    assert result["content"][0]["text"] == f"Error writing file: {said}: {str(ws / path)!r}"


@pytest.mark.asyncio
async def test_a_workspace_removed_under_the_write_is_the_writes_own_error(tmp_path, monkeypatch):
    """The workspace directory disappears right after the write made sure it
    is there. That ends as it does for a write by path: the file's path does
    not exist."""
    ws = tmp_path.resolve() / "ws"
    real_mkdir = Path.mkdir

    def made_then_removed(self, *args, **kwargs):
        real_mkdir(self, *args, **kwargs)
        if self == ws:
            ws.rmdir()

    monkeypatch.setattr(Path, "mkdir", made_then_removed)

    result = await write_file_tool("f.txt", "x", _workspace_dir=str(ws))

    assert result.get("is_error") is True
    assert result["content"][0]["text"] == (
        f"Error writing file: [Errno 2] No such file or directory: {str(ws / 'f.txt')!r}"
    )


@_POSIX
@pytest.mark.asyncio
async def test_a_failure_of_the_write_itself_is_reported_as_it_is(tmp_path, monkeypatch):
    """An error of the write itself -- an I/O error, a full disk -- names no
    file. It is reported as a write by path reported it, without a path."""

    def fails(fd, length):
        raise OSError(errno.EIO, os.strerror(errno.EIO))

    (tmp_path / "f.txt").write_text("v1", encoding="utf-8")
    monkeypatch.setattr(os, "ftruncate", fails)

    result = await write_file_tool("f.txt", "x", _workspace_dir=str(tmp_path))

    assert result.get("is_error") is True
    assert result["content"][0]["text"] == f"Error writing file: [Errno {errno.EIO}] {os.strerror(errno.EIO)}"


@_POSIX
@pytest.mark.asyncio
async def test_a_workspace_named_through_a_symlink_is_written(tmp_path):
    """The workspace setting may reach the workspace through a symlink (a
    linked mount; /tmp on macOS). The walk starts from the workspace as
    resolved, so that link is no reason to refuse."""
    (tmp_path / "real").mkdir()
    (tmp_path / "ws").symlink_to(tmp_path / "real", target_is_directory=True)

    result = await write_file_tool("d/f.txt", "x", _workspace_dir=str(tmp_path / "ws"))

    assert not result.get("is_error"), result
    assert (tmp_path / "real" / "d" / "f.txt").read_text(encoding="utf-8") == "x"


@_POSIX
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "swapped", "said"),
    [
        pytest.param("a/b/c.txt", "ancestor", "on the way", id="ancestor-of-an-existing-file"),
        pytest.param("a/b/new.txt", "ancestor", "on the way", id="ancestor-of-a-new-file"),
        pytest.param("a/x/y/new.txt", "ancestor", "on the way", id="ancestor-of-new-directories"),
        pytest.param("f.txt", "victim.txt", "link", id="file-for-a-link-to-an-outside-file"),
        pytest.param("f.txt", "not-there-yet.txt", "link", id="file-for-a-link-to-a-new-outside-path"),
    ],
)
async def test_a_path_swapped_for_a_symlink_after_validation_is_refused(tmp_path, monkeypatch, path, swapped, said):
    """The path is inside the workspace when it is validated. Before the write
    opens it, a directory on the way -- or the file itself -- becomes a symlink
    that leads out of the workspace. The write does not follow it: it is
    refused with the snapshotted write's words, and nothing outside the
    workspace is written, created or changed."""
    ws, outside = tmp_path / "ws", tmp_path / "outside"
    (ws / "a" / "b").mkdir(parents=True)
    (ws / "a" / "b" / "c.txt").write_text("inside", encoding="utf-8")
    (ws / "f.txt").write_text("inside", encoding="utf-8")
    (outside / "b").mkdir(parents=True)
    (outside / "b" / "c.txt").write_text("theirs", encoding="utf-8")
    (outside / "victim.txt").write_text("theirs", encoding="utf-8")
    before = _tree(outside)

    def swap():
        if swapped == "ancestor":
            shutil.rmtree(ws / "a")
            (ws / "a").symlink_to(outside, target_is_directory=True)
        else:
            (ws / "f.txt").unlink()
            (ws / "f.txt").symlink_to(outside / swapped)

    _after_validation(monkeypatch, swap)

    result = await write_file_tool(path, "written by the tool", _workspace_dir=str(ws))

    refusal = {
        "on the way": f"a directory on the way to {ws.resolve() / path} is a symlink or not a directory; refused",
        "link": "'f.txt' is a symlink",
    }[said]
    assert _tree(outside) == before
    assert result.get("is_error") is True
    assert result["content"][0]["text"] == f"Refused to write '{path}': {refusal}; nothing was written."


@_FIFO
@pytest.mark.asyncio
async def test_a_file_swapped_for_a_fifo_after_validation_is_refused_not_waited_for(tmp_path, monkeypatch):
    """Guard for the same stretch: the file becomes a FIFO nobody reads after
    the path was validated. The open cannot block on it, so the call is
    refused at once instead of holding its thread until a reader appears."""
    target = tmp_path / "f.txt"
    target.write_text("v1", encoding="utf-8")

    def swap():
        target.unlink()
        os.mkfifo(target)

    _after_validation(monkeypatch, swap)
    try:
        result = await asyncio.wait_for(write_file_tool("f.txt", "x", _workspace_dir=str(tmp_path)), timeout=2)
    finally:
        # A reader, so that a write that did block in open() can end and a
        # failure here cannot hang the whole run.
        os.close(os.open(target, os.O_RDONLY | os.O_NONBLOCK))

    assert result["content"][0]["text"] == _NOT_REGULAR.format("f.txt")
    assert stat.S_ISFIFO(target.lstat().st_mode)


@_FIFO
@pytest.mark.asyncio
async def test_a_fifo_somebody_reads_is_refused_and_receives_nothing(tmp_path):
    """With a reader on its other end a FIFO can be opened for writing. Its
    type is checked on the open descriptor before anything is truncated or
    written: the call is refused and the reader gets no byte."""
    pipe = tmp_path / "pipe"
    os.mkfifo(pipe)
    reader = os.open(pipe, os.O_RDONLY | os.O_NONBLOCK)
    try:
        result = await asyncio.wait_for(write_file_tool("pipe", "x", _workspace_dir=str(tmp_path)), timeout=2)
        received = os.read(reader, 16)
    finally:
        os.close(reader)

    assert result["content"][0]["text"] == _NOT_REGULAR.format("pipe")
    assert received == b""  # the writer came and went without writing


@_POSIX
@pytest.mark.asyncio
async def test_a_socket_is_refused_with_the_same_words(tmp_path, monkeypatch):
    """A socket cannot be opened at all, so its type is read from the name."""
    monkeypatch.chdir(tmp_path)  # a relative name: a socket path is short
    with socket.socket(socket.AF_UNIX) as bound:
        bound.bind("sock")

        result = await write_file_tool("sock", "x", _workspace_dir=str(tmp_path))

    assert result["content"][0]["text"] == _NOT_REGULAR.format("sock")
    assert stat.S_ISSOCK((tmp_path / "sock").lstat().st_mode)


@pytest.mark.skipif(not hasattr(os, "mknod") or os.geteuid() != 0, reason="making a device node takes root")
@pytest.mark.asyncio
async def test_a_device_node_is_refused_and_stays_what_it_was(tmp_path):
    """A device node opens without blocking, too: it is refused on its type
    and stays the device it was."""
    node = tmp_path / "null"
    try:
        os.mknod(node, 0o666 | stat.S_IFCHR, os.makedev(1, 3))
    except PermissionError:
        pytest.skip("this root may not make device nodes")

    result = await write_file_tool("null", "x", _workspace_dir=str(tmp_path))

    assert result["content"][0]["text"] == _NOT_REGULAR.format("null")
    after = node.lstat()
    assert stat.S_ISCHR(after.st_mode) and after.st_rdev == os.makedev(1, 3)


@_POSIX
@pytest.mark.asyncio
async def test_a_new_file_and_its_directories_get_the_default_modes(tmp_path):
    """What the in-place write creates has the mode any created file or
    directory gets, the umask applied: it was never the temp file's 0600."""
    umask = os.umask(0o022)
    os.umask(umask)

    result = await write_file_tool("made/new.txt", "x", _workspace_dir=str(tmp_path))

    assert not result.get("is_error"), result
    assert stat.S_IMODE((tmp_path / "made" / "new.txt").stat().st_mode) == 0o666 & ~umask
    assert stat.S_IMODE((tmp_path / "made").stat().st_mode) == 0o777 & ~umask
