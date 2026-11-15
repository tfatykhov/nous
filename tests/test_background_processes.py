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
    stale, fresh = SimpleNamespace(pid=4_000_000, returncode=0), SimpleNamespace(pid=4_000_000, returncode=None)
    background_processes.track(stale)
    background_processes._entries[4_000_000].exited_at = 0.0  # it exited long ago
    background_processes.track(fresh)
    entry = background_processes._entries[4_000_000]
    assert entry.proc is fresh and entry.exited_at is None


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
