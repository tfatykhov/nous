"""A run_python script's processes do not outlive a call that timed out.

The deadline of a script is a trace hook, and a trace hook does not fire
while the worker thread is inside a blocking C call. A script waiting for a
process it had started therefore stayed where it was: the call returned as
timed out, while the thread, the process and one run slot stayed until the
process ended by itself.

Everything here runs the real tool, real worker threads and real processes.
A child is this interpreter running `_CHILD`: it sleeps, then writes a marker
file. A marker that never appears is a process that was killed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from nous.api import tools as T
from nous.api.tools import create_programmatic_tools, run_python_active_runs
from nous.config import Settings

_CHILD = "import pathlib, sys, time; time.sleep(float(sys.argv[2])); pathlib.Path(sys.argv[1]).write_text('survived')"
# A timed-out call returns about 3 s after it started: a 1 s deadline plus the
# grace. A child that is left alone writes its marker after this many seconds.
_NAP = 4.5
_TIMED_OUT = "Error: execution timed out (1s)"


def _run_python(heart=None, slots: int = 4):
    settings = Settings(
        programmatic_tools_enabled=True, programmatic_tools_timeout=1, programmatic_tools_max_concurrent=slots
    )
    return create_programmatic_tools(AsyncMock(), heart or AsyncMock(), settings)["run_python"]


def _child(marker, nap: float = _NAP) -> str:
    """Script source of the argv that starts one child."""
    return repr([sys.executable, "-c", _CHILD, str(marker), str(nap)])


async def _idle(limit: float = 3.0) -> bool:
    """Wait until no script holds a run slot; False if one still does."""
    end = time.monotonic() + limit
    while run_python_active_runs() and time.monotonic() < end:
        await asyncio.sleep(0.02)
    return run_python_active_runs() == 0


async def _after_the_nap(started: float) -> None:
    """Wait until a child started at `started` and left alone would have written its marker."""
    await asyncio.sleep(max(0.0, started + _NAP + 1.0 - time.monotonic()))


@pytest_asyncio.fixture(autouse=True)
async def no_script_running():
    assert await _idle(), "a script from another test is still running"
    yield
    assert await _idle(limit=_NAP + 3.0), "this test left a script running"


async def test_a_process_the_script_was_waiting_for_is_killed(tmp_path, caplog):
    marker = tmp_path / "survived"
    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="nous.api.tools"):
        result = await _run_python()(code=f"import subprocess\nsubprocess.run({_child(marker)})\n")

    assert result["is_error"] is True
    assert run_python_active_runs() == 0, "the timed-out script still holds its run slot"
    assert result["content"][0]["text"] == _TIMED_OUT + "; killed 1 process(es) the script had started"
    assert "killed 1 process(es) it had started" in caplog.text
    await _after_the_nap(started)
    assert not marker.exists(), "the process outlived the call that timed out"


async def test_every_slot_is_free_again_after_scripts_that_timed_out(tmp_path):
    run_python = _run_python(slots=2)
    code = f"import subprocess\nsubprocess.run({_child(tmp_path / 'survived')})\n"

    blocked = await asyncio.gather(run_python(code=code), run_python(code=code))
    after = await run_python(code="result = 'ran'")

    assert [r["is_error"] for r in blocked] == [True, True]
    assert after == {"content": [{"type": "text", "text": "ran"}]}


@pytest.mark.skipif(not os.path.isdir("/proc/self"), reason="what a process started is read from Linux /proc")
async def test_what_the_scripts_process_started_is_killed_with_it(tmp_path):
    """A shell pipeline holds the pipe the script reads: with only the shell
    killed, the worker would go on waiting for that pipe to close."""
    marker = tmp_path / "survived"
    command = f"(sleep {_NAP}; echo survived > {shlex.quote(str(marker))}) | cat"
    started = time.monotonic()
    result = await _run_python()(
        code=f"import subprocess\nsubprocess.run({command!r}, shell=True, capture_output=True)\n"
    )

    assert run_python_active_runs() == 0, "the timed-out script still holds its run slot"
    killed = re.search(r"killed (\d+) process", result["content"][0]["text"])
    assert killed and int(killed.group(1)) > 1
    await _after_the_nap(started)
    assert not marker.exists(), "part of the pipeline outlived the call that timed out"


async def test_a_process_the_script_did_not_wait_for_is_killed_when_the_deadline_stops_the_script(tmp_path):
    marker = tmp_path / "survived"
    started = time.monotonic()
    result = await _run_python()(
        code=f"import subprocess\np = subprocess.Popen({_child(marker)})\nwhile True:\n    pass\n"
    )

    await _after_the_nap(started)
    assert not marker.exists(), "the process outlived the call that timed out"
    assert result["content"][0]["text"] == _TIMED_OUT + "; killed 1 process(es) the script had started"


@pytest.mark.skipif(os.name == "nt", reason="on Windows `subprocess` does not keep a `Popen` its owner let go of")
async def test_a_process_whose_popen_the_script_let_go_of_is_killed_all_the_same(tmp_path):
    marker = tmp_path / "survived"
    started = time.monotonic()
    result = await _run_python()(code=f"import subprocess\nsubprocess.Popen({_child(marker)})\nwhile True:\n    pass\n")

    await _after_the_nap(started)
    assert not marker.exists(), "the process outlived the call that timed out"
    assert result["content"][0]["text"] == _TIMED_OUT + "; killed 1 process(es) the script had started"


async def test_a_popen_the_script_lets_go_of_is_released_as_it_was_before():
    """The hook must not keep the object alive: with it would stay the pipes
    the script left open and the entry of a process it never waited for."""
    code = (
        "import subprocess, sys, weakref\n"
        "proc = subprocess.Popen([sys.executable, '-c', 'pass'])\n"
        "proc.wait()\n"
        "released = []\n"
        "weakref.finalize(proc, released.append, True)\n"
        "del proc\n"
        "result = 'released' if released else 'still held'\n"
    )

    assert await _run_python()(code=code) == {"content": [{"type": "text", "text": "released"}]}


async def test_the_hook_does_not_raise_for_something_that_is_not_a_popen():
    """A trace hook that raises is removed, and with it the script's deadline."""
    code = (
        "import subprocess, time\n"
        "try:\n"
        "    subprocess.Popen.__init__(object(), ['true'])\n"  # no weak reference can point to an `object()`
        "except AttributeError:\n"
        "    pass\n"
        "end = time.monotonic() + 6\n"
        "while time.monotonic() < end:\n"
        "    pass\n"
    )
    result = await _run_python()(code=code)

    assert result["content"][0]["text"] == _TIMED_OUT


async def test_what_never_started_and_what_the_script_let_go_of_are_not_looked_for():
    code = (
        "import subprocess, sys\n"
        "try:\n"
        "    subprocess.Popen(['true'], bufsize='not a number')\n"  # refused before anything is started
        "except TypeError as refused:\n"
        "    kept = refused\n"  # its traceback keeps the object that never got a process
        "subprocess.Popen([sys.executable, '-c', 'pass']).wait()\n"  # ended, and nothing holds it any more
        "while True:\n"
        "    pass\n"
    )
    result = await _run_python()(code=code)

    assert result["content"][0]["text"] == _TIMED_OUT


async def test_a_popen_subclass_the_script_wrote_is_left_alone(tmp_path):
    """The kill runs on the event loop, which never runs a script's code: the
    `poll` and `kill` of a class the script wrote are not called there."""
    code = (
        "import subprocess\n"
        "class Own(subprocess.Popen):\n"
        "    def poll(self):\n"
        "        raise ValueError('a poll of its own')\n"
        "    def kill(self):\n"
        "        raise ValueError('a kill of its own')\n"
        f"Own({_child(tmp_path / 'survived', nap=4.0)}).wait()\n"
    )
    result = await _run_python()(code=code)

    text = result["content"][0]["text"]
    assert text.startswith(_TIMED_OUT) and "killed" not in text


@pytest.mark.skipif(not hasattr(signal, "SIGKILL"), reason="needs POSIX signals")
def test_a_process_that_may_not_be_signalled_is_skipped(monkeypatch):
    running = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])

    def refused(pid, sig):
        raise PermissionError(pid)

    monkeypatch.setattr(T, "_descendants", lambda pids: [])
    monkeypatch.setattr(T.os, "kill", refused)  # what `Popen.kill` signals with

    try:
        assert T._kill_script_processes([running]) == []
    finally:
        monkeypatch.undo()
        running.kill()
        running.wait()


@pytest.mark.skipif(not hasattr(signal, "SIGKILL"), reason="needs POSIX signals")
def test_what_has_already_ended_is_not_signalled(monkeypatch):
    ended = subprocess.Popen([sys.executable, "-c", "pass"])
    ended.wait()
    monkeypatch.setattr(T, "_descendants", lambda pids: [ended.pid])  # a pid nothing has any more
    running = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])

    try:
        assert T._kill_script_processes([ended, running]) == [running.pid]
        assert running.wait(timeout=5) == -signal.SIGKILL
    finally:
        running.kill()
        running.wait()


def test_where_there_is_no_proc_nothing_is_found(monkeypatch):
    def no_proc(path):
        raise FileNotFoundError(path)

    monkeypatch.setattr(T.os, "listdir", no_proc)

    assert T._descendants([os.getpid()]) == []


@pytest.mark.skipif(not os.path.isdir("/proc/self"), reason="reads Linux /proc")
def test_a_process_that_ends_during_the_scan_is_skipped(monkeypatch):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    listed = os.listdir("/proc")
    monkeypatch.setattr(T.os, "listdir", lambda path: [*listed, "4194999"])  # above pid_max: no such process

    try:
        assert child.pid in T._descendants([os.getpid()])
    finally:
        child.kill()
        child.wait()


async def test_a_process_started_after_the_call_gave_up_is_killed_at_birth(tmp_path):
    late = tmp_path / "late"
    code = (
        "import subprocess\n"
        f"subprocess.run({_child(tmp_path / 'first')})\n"  # the call times out while the script waits here
        f"subprocess.run({_child(late, nap=0.5)})\n"  # started by the script once its first process is gone
    )
    result = await _run_python()(code=code)
    await asyncio.sleep(1.5)  # a second child left alone writes its marker after half a second

    assert result["is_error"] is True
    assert not late.exists(), "a process started after the call had timed out went on running"


async def test_a_script_the_call_gave_up_on_gets_no_memory_function(tmp_path):
    import gc
    import warnings

    refusal = tmp_path / "refusal"
    code = (
        "import time\n"
        "time.sleep(4.5)\n"
        "try:\n"
        "    learn_fact('written after the call had returned')\n"
        "except BaseException as exc:\n"
        f"    open({str(refusal)!r}, 'w').write(type(exc).__name__)\n"
    )
    heart = AsyncMock()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = await _run_python(heart)(code=code)
        assert await _idle(limit=4.0)
        gc.collect()

    assert result["is_error"] is True
    heart.learn.assert_not_awaited()
    assert refusal.read_text() == "ScriptDeadlineExceeded"  # not one `except Exception` would catch
    assert not [w for w in caught if "never awaited" in str(w.message)]


async def test_a_refused_memory_call_does_not_turn_the_deadline_off(tmp_path):
    """A script can catch the refusal (`except:`). It keeps its deadline and the
    hook that kills what it starts: `_fail_trace` turns the trace hook back on
    after a refused recall, as it does after one that failed."""
    from nous.observability.retrieval_logger import RetrievalLogger, get_active, set_active

    caught, late, looped = tmp_path / "caught", tmp_path / "late", tmp_path / "looped"
    code = (
        "import subprocess, time\n"
        "time.sleep(4.0)\n"  # the call returns as timed out while the script sleeps
        "try:\n"
        "    recall_deep('anything')\n"
        "except:\n"
        # Inside the handler: past its deadline, the first line after an
        # `except` block is itself a point where the trace hook raises.
        f"    open({str(caught)!r}, 'w').write('refused')\n"
        f"    subprocess.run({_child(late, nap=0.5)})\n"
        "    end = time.monotonic() + 3\n"
        "    while time.monotonic() < end:\n"
        "        pass\n"
        f"    open({str(looped)!r}, 'w').write('looped')\n"
    )
    previous = get_active()
    set_active(RetrievalLogger(db_writer=None, enabled=True))  # main.py wires one by default
    try:
        result = await _run_python()(code=code)
        stopped = await _idle(limit=4.0)
    finally:
        set_active(previous)

    assert result["is_error"] is True
    assert caught.exists(), "the recall was not refused: the handler never ran"
    assert stopped, "the script ran on past its deadline"
    assert not looped.exists(), "the loop ran to its end: the script had lost its deadline"
    assert not late.exists(), "a process started after the call had given up went on running"


async def test_a_timed_out_script_that_is_still_running_is_reported_as_such(caplog):
    with caplog.at_level(logging.WARNING, logger="nous.api.tools"):
        result = await _run_python()(code="import time\ntime.sleep(4.0)\n")

    assert result["content"][0]["text"] == _TIMED_OUT + (
        "; the script is still running: it is blocked in a call that cannot be "
        "interrupted and keeps its run slot until that call returns"
    )
    assert run_python_active_runs() == 1
    assert "is still running" in caplog.text and "(1/4 in use)" in caplog.text


async def test_a_timed_out_call_waits_for_its_worker_only_until_it_is_back(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "_KILL_SETTLE_SECONDS", 30.0)  # far longer than a worker needs to come back
    started = time.monotonic()
    result = await _run_python()(code=f"import subprocess\nsubprocess.run({_child(tmp_path / 'survived')})\n")

    assert time.monotonic() - started < 10, "the call waited out the whole settle time"
    assert result["content"][0]["text"] == _TIMED_OUT + "; killed 1 process(es) the script had started"


async def test_a_class_the_script_puts_in_place_of_subprocess_popen_is_left_alone(tmp_path):
    """The kill compares with the `Popen` of before any script ran: a script
    can rebind `subprocess.Popen` for the whole process."""
    code = (
        "import subprocess\n"
        "class Own(subprocess.Popen):\n"
        "    def poll(self):\n"
        "        raise ValueError('a poll of its own')\n"
        "subprocess.Popen = Own\n"
        f"subprocess.run({_child(tmp_path / 'survived', nap=4.0)})\n"
    )
    real = subprocess.Popen
    try:
        result = await _run_python()(code=code)
    finally:
        subprocess.Popen = real

    text = result["content"][0]["text"]
    assert text.startswith(_TIMED_OUT) and "killed" not in text


async def test_the_wait_for_a_worker_that_does_not_come_back_keeps_the_event_loop_turning(tmp_path):
    """After the kill the script goes on blocking: the call waits the whole
    settle time for it, and does so without holding the event loop."""
    gaps: list[float] = []

    async def heartbeat() -> None:
        while True:
            before = time.monotonic()
            await asyncio.sleep(0.01)
            gaps.append(time.monotonic() - before)

    run_python = _run_python()
    await run_python(code="result = 'warm'")  # the first call of a process also pays for imports
    beat = asyncio.ensure_future(heartbeat())
    started = time.monotonic()
    try:
        result = await run_python(
            code=f"import subprocess, time\nsubprocess.run({_child(tmp_path / 'survived')})\ntime.sleep(3)\n"
        )
        answered = time.monotonic() - started
        await asyncio.sleep(0.1)  # the heartbeat notes the gap it is in
    finally:
        beat.cancel()

    assert result["content"][0]["text"].startswith(
        _TIMED_OUT + "; killed 1 process(es) the script had started; the script is still running"
    )
    assert answered < 1 + T._TIMEOUT_GRACE + 1.5, "the call waited far longer than half a second for its worker"
    assert max(gaps) < 0.25, "the event loop stopped turning while the call waited for its worker"


@pytest.mark.skipif(not os.path.isdir("/proc/self"), reason="reads Linux /proc")
def test_a_process_whose_name_holds_a_parenthesis_is_still_found():
    """`/proc/<pid>/stat` is "pid (comm) state ppid ...", and a process may name itself anything."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; open('/proc/self/comm', 'w').write('a) b 1'); time.sleep(30)"]
    )
    try:
        renamed = time.monotonic() + 5
        while time.monotonic() < renamed:
            with open(f"/proc/{child.pid}/stat", "rb") as stat:
                if b"(a) b 1)" in stat.read():
                    break
            time.sleep(0.01)

        assert child.pid in T._descendants([os.getpid()])
    finally:
        child.kill()
        child.wait()


async def test_a_process_started_while_the_call_is_killing_is_killed_at_birth(tmp_path, monkeypatch):
    """The call marks the script as given up on before it looks for processes."""
    real, calls = T._descendants, []

    def slow(pids):
        calls.append(pids)
        if len(calls) == 1:
            time.sleep(1.0)  # the call's own scan: the script wakes meanwhile and starts a process
        return real(pids)

    monkeypatch.setattr(T, "_descendants", slow)
    late = tmp_path / "late"
    run_python = _run_python()
    await run_python(code="result = 'warm'")  # the first call of a process also pays for imports
    result = await run_python(
        code=f"import subprocess, time\ntime.sleep(3.4)\nsubprocess.run({_child(late, nap=2.0)})\n"
    )
    await asyncio.sleep(2.5)  # a late child left alone writes its marker 2 s after it started

    assert result["is_error"] is True
    assert not late.exists(), "a process started while the call was killing went on running"


async def test_a_worker_reported_back_has_already_freed_its_slot(tmp_path, monkeypatch):
    real = T._release_run_slot

    def slow_release():
        time.sleep(0.1)  # a slot that takes a moment to come back
        real()

    monkeypatch.setattr(T, "_release_run_slot", slow_release)
    result = await _run_python()(code=f"import subprocess\nsubprocess.run({_child(tmp_path / 'survived')})\n")

    assert result["content"][0]["text"] == _TIMED_OUT + "; killed 1 process(es) the script had started"
    assert run_python_active_runs() == 0, "the call answered before its worker had freed the slot"


async def test_a_memory_call_running_when_the_call_gives_up_does_not_land_after_it(monkeypatch):
    """A memory call that is on the event loop when the call gives up is
    stopped before the call answers, so it cannot land after "timed out"."""
    gate, landed = asyncio.Event(), []

    async def slow_learn(*args, **kwargs):
        await gate.wait()
        landed.append(True)

    heart = AsyncMock()
    heart.learn.side_effect = slow_learn
    # The call gives up while the write waits on the loop: a grace below zero
    # makes that moment come before the script's own deadline.
    monkeypatch.setattr(T, "_TIMEOUT_GRACE", -0.5)
    result = await _run_python(heart)(code="learn_fact('written while the call gave up')\n")
    gate.set()
    await asyncio.sleep(0.2)

    assert result["is_error"] is True
    assert landed == [], "a memory write landed after the call had answered"


@pytest.mark.skipif(not os.path.isdir("/proc/self"), reason="what a process started is read from Linux /proc")
async def test_what_processes_start_while_the_kill_looks_is_killed_too(tmp_path, monkeypatch):
    """Processes that keep starting others are stopped before the kill looks
    again, so nothing they start meanwhile is left running. The script's own
    process starts processes and a second starter, which starts processes."""
    mark = "spawned-by-" + tmp_path.name
    forks = (
        "while True:\n"  # a sleep every 50 ms, each marked in its environment
        "    subprocess.Popen(['sleep', '30'], env={'SPAWNED_BY': sys.argv[1]})\n"
        "    time.sleep(0.05)\n"
    )
    second = "import subprocess, sys, time\n" + forks
    first = (
        "import subprocess, sys, time\n"
        "time.sleep(2.5)\n"
        f"subprocess.Popen([sys.executable, '-c', {second!r}, sys.argv[1]])\n" + forks
    )

    def marked() -> list[int]:
        found = []
        for entry in os.listdir("/proc"):
            if entry.isdigit():
                for name in ("cmdline", "environ"):
                    try:
                        with open(f"/proc/{entry}/{name}", "rb") as f:
                            if mark.encode() in f.read():
                                found.append(int(entry))
                                break
                    except OSError:
                        pass
        return found

    real, looks = T._descendants, []

    def slow(pids):
        found = real(pids)
        looks.append(found)
        time.sleep(0.5)  # a process that is not stopped starts more meanwhile
        return found

    monkeypatch.setattr(T, "_descendants", slow)
    try:
        result = await _run_python()(
            code=f"import subprocess, sys\nsubprocess.run([sys.executable, '-c', {first!r}, {mark!r}])\n"
        )
        await asyncio.sleep(0.5)

        assert result["is_error"] is True
        assert marked() == [], "processes started while the kill was looking went on running"
        assert len(looks) < 8, "the kill went on looking after a look had found nothing new"
    finally:
        for pid in marked():
            os.kill(pid, signal.SIGKILL)


@pytest.mark.skipif(os.name == "nt", reason="preexec_fn is POSIX only")
async def test_a_process_whose_popen_has_not_returned_yet_is_killed(tmp_path):
    """`Popen.__init__` returns once the child has exec'd. A child held before
    its exec, here by a slow `preexec_fn`, is found and killed all the same."""
    code = (
        "import subprocess, time\n"
        f"subprocess.run({_child(tmp_path / 'survived', nap=0.5)}, preexec_fn=lambda: time.sleep(6))\n"
    )
    result = await _run_python()(code=code)

    assert result["content"][0]["text"] == _TIMED_OUT + "; killed 1 process(es) the script had started"
    assert run_python_active_runs() == 0, "the timed-out script still holds its run slot"
