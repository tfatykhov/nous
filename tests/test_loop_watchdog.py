"""Event-loop watchdog — follow-up to the 2026-09-26 prod wedge.

The event loop sat parked on `threading._active_limbo_lock` for 2.5 h while
the container reported `unhealthy` and nothing acted on it. The watchdog must
fire in exactly that state: the loop thread blocked on a lock that will never
be released, with no Python code able to run on it.
"""

import asyncio
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from nous.config import Settings

REPO = Path(__file__).resolve().parents[1]


def test_wedged_loop_dumps_every_stack_and_exits():
    script = textwrap.dedent(
        """
        import asyncio, threading
        from nous.loop_watchdog import run_event_loop_watchdog

        leaked = threading.Lock()
        leaked.acquire()  # held forever, like the leaked _active_limbo_lock

        async def main():
            asyncio.create_task(run_event_loop_watchdog(1, rearm=0.1))
            await asyncio.sleep(0.3)
            leaked.acquire()  # the loop thread parks here for good

        asyncio.run(main())
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": str(REPO)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 1, proc.stderr
    assert "Timeout (0:00:01" in proc.stderr  # 1 s + the 0.1 s re-arm slack
    # The frame the loop is stuck in is in the dump — the evidence py-spy had
    # to be brought in for during the incident.
    assert "in main" in proc.stderr


@pytest.mark.asyncio
async def test_rearms_while_the_loop_turns_and_disarms_when_stopped(monkeypatch):
    from nous import loop_watchdog as W

    calls: list[tuple] = []
    monkeypatch.setattr(
        W.faulthandler, "dump_traceback_later",
        lambda timeout, **kw: calls.append(("arm", timeout, kw)),
    )
    monkeypatch.setattr(
        W.faulthandler, "cancel_dump_traceback_later", lambda: calls.append(("cancel",)),
    )

    task = asyncio.create_task(W.run_event_loop_watchdog(45, rearm=0.01))
    await asyncio.sleep(0.1)
    await W.stop_event_loop_watchdog(task)

    arms = [c for c in calls if c[0] == "arm"]
    assert len(arms) >= 2, "the timer must be re-armed while the loop turns"
    # Codex P2 on #655: the timer runs from the last re-arm, not from when the
    # stall began, so it must cover `timeout + rearm` — otherwise a stall that
    # starts just before a re-arm is killed after only `timeout - rearm`.
    assert all(c[1] == 45 + 0.01 and c[2]["exit"] is True for c in arms)
    assert calls[-1] == ("cancel",), "stopping must disarm, or shutdown gets killed"


@pytest.mark.asyncio
async def test_start_honours_the_settings(monkeypatch):
    from nous import loop_watchdog as W

    monkeypatch.setattr(W.faulthandler, "dump_traceback_later", lambda *a, **kw: None)
    monkeypatch.setattr(W.faulthandler, "cancel_dump_traceback_later", lambda: None)

    off = Settings(_env_file=None, event_loop_watchdog_enabled=False)
    assert W.start_event_loop_watchdog(off) is None
    await W.stop_event_loop_watchdog(None)  # no-op

    task = W.start_event_loop_watchdog(Settings(_env_file=None))
    assert task is not None
    await W.stop_event_loop_watchdog(task)
    assert task.done()


@pytest.mark.asyncio
async def test_lifespan_arms_after_startup_and_disarms_before_shutdown(monkeypatch):
    """Armed too early, a slow startup kills Nous; disarmed too late, a slow
    shutdown does."""
    import nous.main as M

    order: list = []

    async def fake_create(settings):
        order.append("startup")
        return {}

    async def fake_shutdown(components):
        order.append("shutdown")

    async def fake_stop(task):
        order.append(("disarm", task))

    monkeypatch.setattr(M, "create_components", fake_create)
    monkeypatch.setattr(M, "shutdown_components", fake_shutdown)
    monkeypatch.setattr(M, "start_event_loop_watchdog", lambda s: order.append("arm") or "wd")
    monkeypatch.setattr(M, "stop_event_loop_watchdog", fake_stop)

    app = M.build_app(Settings(_env_file=None, mcp_enabled=False))
    async with app.router.lifespan_context(app):
        order.append("serving")
    assert order == ["startup", "arm", "serving", ("disarm", "wd"), "shutdown"]

    # Codex P2 on #655: an exception thrown in at `yield` must still disarm,
    # or the process-global timer outlives the app it was guarding.
    order.clear()
    with pytest.raises(RuntimeError):
        async with app.router.lifespan_context(app):
            raise RuntimeError("serving failed")
    assert ("disarm", "wd") in order


def test_defaults_and_bounds():
    s = Settings(_env_file=None)
    assert s.event_loop_watchdog_enabled is True
    assert s.event_loop_watchdog_timeout_seconds == 120
    # Below this, a legitimately slow tick could kill a healthy process.
    with pytest.raises(ValidationError):
        Settings(_env_file=None, event_loop_watchdog_timeout_seconds=29)
