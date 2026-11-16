"""Goals PR 0c: the shell commands of background turns are tracked, and a halt signals their process groups."""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

from nous import background_processes
from nous.api.builtin_tools import bash_tool
from nous.api.execution_context import ExecutionContext
from nous.api.tools import ToolDispatcher

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX only")


@pytest.fixture(autouse=True)
def _empty_registry():
    background_processes._reset()
    yield
    background_processes.kill_all()
    background_processes._reset()


def _text(result: dict) -> str:
    return result["content"][0]["text"]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            return f.read().split(") ", 1)[1][0] != "Z"  # a zombie has ended
    except OSError:
        return True


async def _until(predicate, seconds: float = 5.0) -> None:
    for _ in range(int(seconds / 0.02)):
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


@posix_only
async def test_kill_all_against_a_command_whose_communicate_is_pending(tmp_path):
    first = asyncio.create_task(bash_tool("sleep 30", 60, _workspace_dir=str(tmp_path), _background=True))
    second = asyncio.create_task(bash_tool("sleep 30; echo late", 60, _workspace_dir=str(tmp_path), _background=True))
    await _until(lambda: len(background_processes.tracked()) == 2)
    assert background_processes.kill_all() == 2  # signals only: it neither raises nor stops after the first
    results = await asyncio.wait_for(asyncio.gather(first, second), timeout=10)
    for result in results:
        assert _text(result).splitlines()[-1] == "Exit code: -9"
    assert "late" not in _text(results[1])


@posix_only
async def test_a_detached_nohup_child_is_killed_after_the_shell_exits(tmp_path):
    result = await asyncio.wait_for(
        bash_tool("nohup sleep 30 > /dev/null 2>&1 & echo $!", 10, _workspace_dir=str(tmp_path), _background=True),
        timeout=10,
    )
    child = int(_text(result).splitlines()[0])
    assert _alive(child)
    assert background_processes.tracked() != []  # the shell exited, its group still has the sleeper
    assert background_processes.kill_all() == 1
    await _until(lambda: not _alive(child))


def test_a_new_child_with_a_reused_pid_replaces_the_stale_entry():
    if not hasattr(os, "killpg"):  # no process groups: entries are kept by handle
        stale, fresh = SimpleNamespace(pid=4_000_000, returncode=0), SimpleNamespace(pid=4_000_000, returncode=None)
        background_processes.track(stale)
        background_processes._entries[4_000_000].exited_at = 0.0  # it exited long ago
        background_processes.track(fresh)
        entry = background_processes._entries[4_000_000]
        assert entry.proc is fresh and entry.exited_at is None
        return
    import subprocess

    fresh = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        stale = background_processes._Entry(fresh.pid, None, exited_at=0.0, start=1)  # an old command's entry
        background_processes._entries[fresh.pid] = stale
        background_processes.track(fresh)
        entry = background_processes._entries[fresh.pid]
        assert (
            entry.proc is fresh and entry.exited_at is None and entry.start == background_processes._stat(fresh.pid)[2]
        )
    finally:
        fresh.kill()
        fresh.wait()


@posix_only
async def test_untrack_on_timeout_and_a_finished_empty_group_is_pruned(tmp_path):
    result = await bash_tool("sleep 30", 1, _workspace_dir=str(tmp_path), _background=True)
    assert "timed out" in _text(result)
    assert background_processes.tracked() == []  # killed, so its group is empty
    await bash_tool("true", 5, _workspace_dir=str(tmp_path), _background=True)
    assert background_processes.tracked() == []
    assert background_processes.kill_all() == 0


@posix_only
async def test_a_chat_command_and_an_approved_proposal_command_are_not_tracked(tmp_path, monkeypatch):
    from nous.api.builtin_tools import register_builtin_tools
    from nous.config import Settings

    tracked: list[bool] = []
    real_track = background_processes.track

    def spy(proc):
        tracked.append(True)
        return real_track(proc)

    monkeypatch.setattr(background_processes, "track", spy)
    dispatcher = ToolDispatcher()
    register_builtin_tools(dispatcher, Settings(_env_file=None, workspace_dir=str(tmp_path)))
    cases = [
        (ExecutionContext(kind="interactive", session_id="s1"), "s1", False),
        (ExecutionContext(kind="mcp", session_id="m1"), "m1", False),
        (
            ExecutionContext(
                kind="approved_action",
                session_id="proposal-1",
                proposal_id=__import__("uuid").uuid4(),
                declared_tools=("bash",),
            ),
            "proposal-1",
            False,
        ),
        (ExecutionContext(kind="interactive", session_id="claude-job"), "claude-job", True),
        (ExecutionContext(kind="subtask", session_id="subtask-1"), "subtask-1", True),
        (ExecutionContext(kind="heartbeat_check", session_id="dynamic-check-x"), "dynamic-check-x", True),
    ]
    for ctx, session, expected in cases:
        tracked.clear()
        await dispatcher.dispatch("bash", {"command": "true"}, session_id=session, context=ctx)
        assert tracked == ([True] if expected else []), ctx.kind


linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")


@linux_only
def test_a_group_whose_leader_has_another_start_time_or_none_readable_is_dropped_not_signalled():
    import subprocess

    other = subprocess.Popen(["sleep", "30"], start_new_session=True)  # a group this registry never started
    try:
        _state, _group, started = background_processes._stat(other.pid)
        # Their calls have returned (released): an entry not shown to be the command's is dropped.
        background_processes._entries[other.pid] = background_processes._Entry(
            other.pid, None, start=started + 1, released=True
        )
        background_processes._entries[4_000_000] = background_processes._Entry(4_000_000, None, start=1, released=True)
        assert background_processes.kill_all() == 0
        assert other.poll() is None  # untouched
        assert other.pid not in background_processes._entries and 4_000_000 not in background_processes._entries
        background_processes._entries[other.pid] = background_processes._Entry(other.pid, None, start=started)
        assert background_processes.kill_all() == 1  # the same leader: signalled
        assert other.wait(timeout=5) == -9
    finally:
        if other.poll() is None:
            other.kill()
            other.wait()


async def test_a_background_command_is_not_spawned_while_halted(tmp_path, monkeypatch):
    from nous import background_halt

    spawned: list[str] = []

    async def fake_spawn(command, **kwargs):
        spawned.append(command)
        raise RuntimeError("spawned")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_spawn)
    halt = background_halt.BackgroundHalt(host_hold=False)
    background_halt.install(halt)
    await halt.halt(actor="t", surface="dashboard")
    refused = await bash_tool("echo hi", 5, _workspace_dir=str(tmp_path), _background=True)
    assert _text(refused) == background_halt.HALTED_TOOL_TEXT and spawned == []
    await bash_tool("echo hi", 5, _workspace_dir=str(tmp_path), _background=False)  # a chat command still runs
    assert spawned == ["echo hi"]


async def test_a_dispatched_background_command_is_checked_with_its_calls_epoch(tmp_path, monkeypatch):
    """A command of a turn that started before a halt is refused after the resume, when the epoch reaches the call
    only through its scope (the streaming loop sets no TURN_EPOCH in the task it dispatches in)."""
    from nous import background_halt

    spawned: list[str] = []

    async def fake_spawn(command, **kwargs):
        spawned.append(command)
        raise RuntimeError("spawned")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_spawn)
    halt = background_halt.BackgroundHalt(host_hold=False)
    background_halt.install(halt)
    before = halt.epoch
    await halt.halt(actor="t", surface="dashboard")
    await halt.resume(actor="t", via="dashboard")
    token = background_halt.CALL_SCOPE.set(("interactive", "claude-job-1", before))
    try:
        refused = await bash_tool("echo hi", 5, _workspace_dir=str(tmp_path), _background=True)
    finally:
        background_halt.CALL_SCOPE.reset(token)
    assert _text(refused) == background_halt.HALTED_TOOL_TEXT and spawned == []


@linux_only
async def test_a_group_whose_members_all_started_after_the_leaders_exit_is_dropped_not_signalled(tmp_path):
    result = await bash_tool(
        "nohup sleep 30 > /dev/null 2>&1 & echo $!", 10, _workspace_dir=str(tmp_path), _background=True
    )
    child = int(_text(result).splitlines()[0])
    try:
        (entry,) = background_processes._entries.values()
        assert entry.exited_ticks is not None and background_processes._stat(entry.pgid) is None  # the leader ended
        _state, _group, member_started = background_processes._stat(child)
        kept_ticks, kept_members = entry.exited_ticks, entry.members
        entry.exited_ticks = member_started - 1  # as if the leader's exit was seen before this member started
        entry.members = {}  # and no member was recorded at track
        assert background_processes.kill_all() == 0 and _alive(child)  # not shown to be ours: dropped
        assert entry.pgid not in background_processes._entries
        entry.exited_ticks, entry.members = kept_ticks, kept_members
        background_processes._entries[entry.pgid] = entry  # an older member: ours, signalled
        assert background_processes.kill_all() == 1
        await _until(lambda: not _alive(child))
    finally:
        if _alive(child):
            os.kill(child, 9)


@posix_only
async def test_a_halt_landing_between_the_check_and_the_track_kills_the_command(tmp_path, monkeypatch):
    from nous import background_halt

    halt = background_halt.BackgroundHalt(host_hold=False)
    background_halt.install(halt)
    real_spawn = asyncio.create_subprocess_shell
    started: list = []

    async def spawn_then_the_halt_lands(command, **kwargs):
        proc = await real_spawn(command, **kwargs)
        started.append(proc)
        await halt.halt(actor="t", surface="dashboard")
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_shell", spawn_then_the_halt_lands)
    result = await asyncio.wait_for(
        bash_tool("sleep 5; echo finished", 10, _workspace_dir=str(tmp_path), _background=True), timeout=4
    )
    assert _text(result) == background_halt.HALTED_TOOL_TEXT
    (proc,) = started
    assert proc.returncode is not None and proc.returncode != 0  # killed, not finished


def _detached(*, keep_shell: bool):
    """A shell in its own session that starts ``sleep 60`` detached and prints its pid; with ``keep_shell`` it then
    waits on stdin, so it is still alive until the test closes it."""
    import subprocess

    script = "sleep 60 </dev/null >/dev/null 2>&1 & echo $!" + ("; read _" if keep_shell else "")
    shell = subprocess.Popen(
        ["bash", "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, start_new_session=True, text=True
    )
    child = int(shell.stdout.readline())
    return shell, child


@linux_only
async def test_a_detached_child_is_killed_when_the_shell_is_still_alive_at_track():
    shell, child = _detached(keep_shell=True)
    try:
        assert background_processes.track(shell) == shell.pid
        assert background_processes._entries[shell.pid].start is not None
        shell.stdin.close()
        shell.wait(timeout=5)  # the shell exits and is collected
        background_processes.release(shell.pid)
        assert background_processes.kill_all() == 1
        await _until(lambda: not _alive(child))
    finally:
        if _alive(child):
            os.kill(child, 9)


@linux_only
async def test_a_detached_child_is_killed_when_the_shell_was_already_collected_at_track():
    shell, child = _detached(keep_shell=False)
    try:
        shell.wait(timeout=5)  # collected before the track, as a fast command can be
        assert background_processes.track(SimpleNamespace(pid=shell.pid, returncode=0)) == shell.pid
        entry = background_processes._entries[shell.pid]
        assert entry.start is None and child in entry.members  # known by its members
        assert background_processes.kill_all() == 1
        await _until(lambda: not _alive(child))
    finally:
        if _alive(child):
            os.kill(child, 9)


@linux_only
def test_a_collected_shell_with_no_member_left_is_not_tracked():
    import subprocess

    shell = subprocess.Popen(["true"], start_new_session=True)
    shell.wait(timeout=5)
    assert background_processes.track(SimpleNamespace(pid=shell.pid, returncode=0)) is None
    assert shell.pid not in background_processes._entries


@linux_only
def test_a_group_whose_only_members_are_zombies_is_empty():
    import subprocess
    import time

    # A killed process stays a zombie in its group until its parent (this test) collects it.
    parent = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        background_processes.track(parent)
        os.kill(parent.pid, 9)
        for _ in range(250):
            stat = background_processes._stat(parent.pid)
            if stat is not None and stat[0] == "Z":
                break
            time.sleep(0.02)
        assert background_processes._stat(parent.pid)[0] == "Z"  # not collected yet: a zombie in its group
        background_processes.release(parent.pid)
        assert background_processes.tracked() == []  # the group counts as empty
    finally:
        parent.wait()


@linux_only
async def test_a_child_left_holding_the_pipes_is_signalled_by_the_first_halt(tmp_path):
    """``cmd &`` with no redirect: the shell exits and is collected while the call still waits on the pipes its child
    holds. The child is the command's, and the first halt signals it."""
    call = asyncio.create_task(bash_tool("sleep 47 &", 20, _workspace_dir=str(tmp_path), _background=True))
    child = None
    try:
        await _until(lambda: len(background_processes._entries) == 1)
        (pgid,) = list(background_processes._entries)
        await _until(lambda: background_processes._stat(pgid) is None)  # the shell is collected
        (child,) = background_processes._members(pgid)
        await asyncio.sleep(0.3)  # the collection is seen
        assert not call.done()  # the call still waits on the pipes the child holds
        assert background_processes.kill_all() == 1
        await _until(lambda: not _alive(child))
        await asyncio.wait_for(call, 5)  # the pipes closed with the child
    finally:
        call.cancel()
        if child is not None and _alive(child):
            os.kill(child, 9)


@linux_only
def test_an_entry_not_shown_to_be_ours_is_kept_while_its_call_runs_and_dropped_once_it_returned():
    import time

    for pgid in (4_000_011, 4_000_012):  # no such process or group: nothing shows them to be the command's
        background_processes._entries[pgid] = background_processes._Entry(pgid, SimpleNamespace(pid=pgid), start=1)
    background_processes._entries[4_000_012].released = True
    background_processes._entries[4_000_012].exited_at = time.monotonic() - 10
    background_processes._entries[4_000_012].exited_ticks = 1
    assert background_processes.kill_all() == 0
    assert list(background_processes._entries) == [4_000_011]  # still there for the next halt
    assert background_processes.kill_all() == 0 and list(background_processes._entries) == [4_000_011]


@linux_only
def test_a_member_recorded_at_track_shows_the_group_is_ours_after_the_leader_ended():
    import subprocess
    import time

    shell = subprocess.Popen(
        ["sh", "-c", "sleep 30 & echo $!"], start_new_session=True, stdout=subprocess.PIPE, text=True
    )
    child = int(shell.stdout.readline())
    shell.wait()
    try:
        _state, _group, started = background_processes._stat(child)
        background_processes._entries[shell.pid] = background_processes._Entry(
            shell.pid,
            shell,
            start=1,  # the leader's: it has ended
            exited_at=time.monotonic(),
            exited_ticks=started - 1,  # as if the exit was seen before the child started: that rule does not hold
            members={child: started},  # but the child was recorded at track
            released=True,
        )
        assert background_processes.kill_all() == 1
        for _ in range(250):
            if not _alive(child):
                break
            time.sleep(0.02)
        assert not _alive(child)
    finally:
        if _alive(child):
            os.kill(child, 9)
        shell.stdout.close()


@linux_only
def test_pruning_scans_proc_once_however_many_entries_have_exited(monkeypatch):
    import time

    for pgid in (4_000_001, 4_000_002, 4_000_003):  # groups with no member: each entry is dropped
        background_processes._entries[pgid] = background_processes._Entry(
            pgid, SimpleNamespace(pid=pgid), exited_at=time.monotonic()
        )
    scans: list[str] = []
    listdir = os.listdir

    def counting(path="."):
        if path == "/proc":
            scans.append(path)
        return listdir(path)

    monkeypatch.setattr(background_processes.os, "listdir", counting)
    assert background_processes.tracked() == [] and len(scans) == 1


@linux_only
def test_kill_all_scans_proc_once_however_many_leaders_are_gone(monkeypatch):
    for pgid in (4_000_021, 4_000_022, 4_000_023):  # leaders gone, calls still running: each needs the members
        background_processes._entries[pgid] = background_processes._Entry(pgid, SimpleNamespace(pid=pgid), start=1)
    scans: list[str] = []
    listdir = os.listdir

    def counting(path="."):
        if path == "/proc":
            scans.append(path)
        return listdir(path)

    monkeypatch.setattr(background_processes.os, "listdir", counting)
    assert background_processes.kill_all() == 0 and len(scans) == 1


def test_the_exit_moment_is_rounded_to_the_tick_not_truncated(monkeypatch):
    import builtins
    import io

    real_open = builtins.open

    def fake_open(path, *args, **kwargs):
        if path == "/proc/uptime":
            return io.StringIO("4.35 0.00")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)
    monkeypatch.setattr(background_processes, "_CLK_TCK", 100)
    # 4.35 * 100 is 434.99999999999994 in floating point: a truncated tick would read a member started in that
    # very tick as started after the exit.
    assert background_processes._now_ticks() == 435
