"""A shell command that reached its timeout is stopped as a whole, and its caller gets control back.

Every test runs a real shell command through the production coroutine
(``DAGOrchestrator._run_completion_check`` or ``bash_tool``). The shell, the
timeout and the kill are all real. Each command writes the pids that matter
to ``*.pid`` files in its working directory, and the ``workspace`` fixture
kills whatever is recorded there when the test ends, so a failing test leaves
no process running.

Process groups are POSIX, so the module is skipped on Windows.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import signal
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from nous.config import Settings
from nous.dag import orchestrator as orchestrator_module
from nous.dag.orchestrator import CheckResult, DAGOrchestrator

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX; CI (Linux) proves this")

# With a 1 s timeout, a call that has not returned after this long is not
# coming back until its command ends. Only reached when the behaviour is broken.
LIMIT = 5.0

# The shell is still waiting for its child when the timeout fires:
FOREGROUND = "echo $$ > shell.pid; sh -c 'echo $$ > child.pid; exec sleep 30'; true"
# The same, with a job in the background as well:
FOREGROUND_WITH_JOB = "sleep 30 & echo $! > job.pid; " + FOREGROUND
# The shell has exited, and the job it left runs on, holding the command's output:
BACKGROUND = "sleep 30 & echo $! > job.pid"
# The same job with its output redirected: it holds nothing, so the command ends when its shell does:
REDIRECTED = "sleep 30 > /dev/null 2>&1 & echo $! > job.pid"
# The child started a session of its own, so it is no longer in the shell's process group:
ESCAPED = f"{shlex.quote(sys.executable)} -c 'import os, time; os.setsid(); time.sleep(30)' & echo $! > child.pid; wait"

# The two commands whose shell is still running at the timeout, and what each records.
SHELL_STILL_RUNNING = pytest.mark.parametrize(
    ("command", "recorded"),
    [
        (FOREGROUND, ["shell.pid", "child.pid"]),
        (FOREGROUND_WITH_JOB, ["shell.pid", "child.pid", "job.pid"]),
    ],
    ids=["waiting for its child", "waiting for its child, a job in the background"],
)


def _gone(pid: int) -> bool:
    """Whether the process has exited. A zombie counts: it has exited and only
    waits to be collected, which PID 1 of a container may never do."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:
        with open(f"/proc/{pid}/stat") as stat:
            return stat.read().rsplit(")", 1)[1].split()[0] == "Z"
    except OSError:
        return False


def _recorded_pids(workspace: Path) -> list[int]:
    pids = []
    for pidfile in workspace.rglob("*.pid"):
        text = pidfile.read_text().strip()
        if text:
            pids.append(int(text))
    return pids


@pytest.fixture
async def workspace(tmp_path: Path):
    """The directory a command runs under; whatever it recorded is killed afterwards.

    One pid at a time, never a group: a completion check whose shell was not
    given a session of its own runs in pytest's own process group.
    """
    yield tmp_path
    pids = _recorded_pids(tmp_path)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 2.0
    while not all(_gone(pid) for pid in pids) and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.05)  # the loop sees the closed pipes; no subprocess transport outlives the test


async def _recorded_pid(workspace: Path, name: str = "child.pid") -> int:
    deadline = time.monotonic() + LIMIT
    while time.monotonic() < deadline:
        for pidfile in workspace.rglob(name):
            text = pidfile.read_text().strip()
            if text:
                return int(text)
        await asyncio.sleep(0.02)
    pytest.fail(f"the command never wrote {name}")


async def _assert_gone(pid: int, what: str) -> None:
    deadline = time.monotonic() + 2.0  # a SIGKILL takes effect a moment after it is sent
    while not _gone(pid):
        if time.monotonic() > deadline:
            pytest.fail(what)
        await asyncio.sleep(0.02)


async def _assert_still_running(pid: int, what: str) -> None:
    # A SIGKILL takes effect a moment after it is sent: one that should not
    # have been sent must have time to show.
    await asyncio.sleep(0.5)
    assert not _gone(pid), what


def _check_node(workspace: Path, command: str) -> tuple[DAGOrchestrator, SimpleNamespace]:
    """A real DAGOrchestrator, and a node whose completion check is ``command``.

    Only the store is replaced: _run_completion_check asks it for the node's
    DAG, to derive the directory the command runs in.
    """
    dag = SimpleNamespace(id=uuid4())

    async def get_dag(dag_id):
        return dag

    orchestrator = DAGOrchestrator(
        store=SimpleNamespace(get_dag=get_dag),
        settings=Settings.model_construct(dag_workspace_root=workspace),
    )
    node = SimpleNamespace(id=uuid4(), dag_id=dag.id, name="check", completion_check=command)
    return orchestrator, node


# ---------------------------------------------------------------------------
# A completion check that timed out is stopped, and the tick gets control back
# ---------------------------------------------------------------------------


@SHELL_STILL_RUNNING
async def test_a_timed_out_completion_check_returns_and_stops_its_command(workspace, monkeypatch, command, recorded):
    monkeypatch.setattr(orchestrator_module, "_CHECK_CMD_TIMEOUT", 1.0)
    orchestrator, node = _check_node(workspace, command)

    try:
        result = await asyncio.wait_for(orchestrator._run_completion_check(node), LIMIT)
    except TimeoutError:
        pytest.fail(f"the completion check had not returned after {LIMIT:.0f}s, with a 1s timeout")

    assert result == CheckResult("pending", "command timed out")
    for name in recorded:
        await _assert_gone(await _recorded_pid(workspace, name), f"{name} was still running after the timeout")


async def test_a_command_that_ignores_sigterm_is_still_stopped(workspace, monkeypatch):
    """The group is sent SIGKILL: a command that ignores SIGTERM cannot hold the caller."""
    monkeypatch.setattr(orchestrator_module, "_CHECK_CMD_TIMEOUT", 1.0)
    orchestrator, node = _check_node(workspace, "trap '' TERM; " + FOREGROUND)

    try:
        result = await asyncio.wait_for(orchestrator._run_completion_check(node), LIMIT)
    except TimeoutError:
        pytest.fail(f"the completion check had not returned after {LIMIT:.0f}s, with a 1s timeout")

    assert result == CheckResult("pending", "command timed out")
    await _assert_gone(await _recorded_pid(workspace), "the command's child was still running after the timeout")


async def test_a_job_that_outlived_its_shell_survives_the_timeout_of_its_check(workspace, monkeypatch):
    """Parity pin: green before this change too. The shell has exited by the
    time the timeout fires, so nothing is killed: the job it left behind runs
    on, and the caller gets control back at the timeout."""
    monkeypatch.setattr(orchestrator_module, "_CHECK_CMD_TIMEOUT", 1.0)
    orchestrator, node = _check_node(workspace, BACKGROUND)

    result = await asyncio.wait_for(orchestrator._run_completion_check(node), LIMIT)

    assert result == CheckResult("pending", "command timed out")
    job = await _recorded_pid(workspace, "job.pid")
    await _assert_still_running(job, "a job that had outlived its shell was killed at the timeout")


async def test_a_descendant_outside_the_group_cannot_hold_the_caller(workspace, monkeypatch, caplog):
    """The group kill cannot reach a process that started its own session, and
    that process still holds the command's pipes. The wait for the killed
    shell is bounded, so the caller gets control back anyway."""
    monkeypatch.setattr(orchestrator_module, "_CHECK_CMD_TIMEOUT", 1.0)
    # raising=False: if the constant is ever gone, the test has to fail on
    # what the code does, not on this line.
    monkeypatch.setattr("nous.utils._KILL_WAIT_SECONDS", 0.5, raising=False)
    orchestrator, node = _check_node(workspace, ESCAPED)

    with caplog.at_level(logging.WARNING, logger="nous.utils"):
        try:
            result = await asyncio.wait_for(orchestrator._run_completion_check(node), LIMIT)
        except TimeoutError:
            pytest.fail(f"the completion check had not returned after {LIMIT:.0f}s, with a 1s timeout")

    assert result == CheckResult("pending", "command timed out")
    assert not _gone(await _recorded_pid(workspace)), "the escaped child died: this run did not test the bounded wait"
    warnings = [r.getMessage() for r in caplog.records if r.name == "nous.utils"]
    assert len(warnings) == 1 and "may still be running" in warnings[0], warnings


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("sleep 1; exit 0", CheckResult("success")),
        ("echo not yet >&2; exit 1", CheckResult("failed", "not yet")),
        ("exit 2", CheckResult("pending")),
    ],
    ids=["passes after a second", "fails", "still pending"],
)
async def test_a_completion_check_that_ends_before_its_timeout_keeps_its_exit_status(workspace, command, expected):
    """Parity pin: green before this change too. The timeout is the module's
    own 10 s here; a check that ends inside it never reaches the kill."""
    orchestrator, node = _check_node(workspace, command)

    assert await orchestrator._run_completion_check(node) == expected


async def test_a_job_that_holds_no_output_outlives_a_check_that_ended_in_time(workspace):
    """Parity pin: green before this change too. Nothing is killed unless the
    timeout fires, and it does not fire for a job that holds none of the pipes."""
    orchestrator, node = _check_node(workspace, REDIRECTED)

    assert await asyncio.wait_for(orchestrator._run_completion_check(node), LIMIT) == CheckResult("success")
    job = await _recorded_pid(workspace, "job.pid")
    await _assert_still_running(job, "a job that held none of the command's output was killed")
