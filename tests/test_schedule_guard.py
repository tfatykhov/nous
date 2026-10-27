"""Run-level guard for scheduled script jobs (nous/handlers/schedule_guard.py + the worker hook).

The guard config is read from the schedule row's ``metadata.guard``, which names an entry of the
allowlist ``NOUS_SCHEDULE_GUARD_COMMANDS`` (the only source of a command). A guard that exits 0 with empty
stdout and stderr completes the run with no model call; every other result falls through to the model turn with a
receipt appended to the run's task text.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nous import utils as nous_utils
from nous.config import Settings
from nous.handlers import schedule_guard, task_scheduler
from nous.handlers import subtask_worker as worker_mod
from nous.handlers.subtask_worker import SubtaskWorkerPool

# The guard runs `/bin/sh` commands in their own session; the tests use /dev/zero, touch, setsid and killpg.
pytestmark = pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "killpg"), reason="POSIX shell and process groups"
)

TASK = "Run `bash /tmp/ledger_sweep.sh` and report what it logged."


def _settings(**overrides) -> Settings:
    base = {
        "telegram_bot_token": "test-token",
        "telegram_chat_id": "12345",
        "result_inbox_enabled": True,
        "result_memory_enabled": True,
    }
    return Settings(_env_file=None, **{**base, **overrides})


def _schedule(guard: dict | str | None, *, task: str = TASK) -> SimpleNamespace:
    """A schedule row; a ``_command`` key of ``guard`` becomes NAME's allowlisted command, not row data."""
    commands = {}
    if isinstance(guard, dict) and "_command" in guard:
        guard = dict(guard)
        commands[NAME] = guard.pop("_command")
    return SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=_settings().agent_id,
        task=task,
        metadata_={} if guard is None else {"guard": guard},
        commands=commands,
    )


def _run(schedule: SimpleNamespace, **meta) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        task=schedule.task,
        metadata_={"schedule_id": schedule.id.hex, **meta},
        timeout_seconds=120,
        notify=True,
        frame_type=None,
        model=None,
        dag_node_id=None,
        agent_id="test-agent",
    )


def _pool(schedule: SimpleNamespace, **overrides):
    runner = MagicMock()
    runner.run_turn = AsyncMock(return_value=("ok", None, {}))
    runner.end_conversation = AsyncMock()
    runner.root_cancelled = MagicMock(return_value=False)
    heart = MagicMock()
    heart.subtasks = AsyncMock()
    heart.schedules = AsyncMock()
    heart.schedules.get = AsyncMock(return_value=schedule)
    bus = AsyncMock()
    http = AsyncMock()
    settings = _settings(**{"schedule_guard_commands": schedule.commands, **overrides})
    pool = SubtaskWorkerPool(runner=runner, heart=heart, settings=settings, bus=bus, http_client=http)
    seen: dict = {}

    async def fake_execute(subtask):
        seen["task"] = subtask.task

    pool._execute_subtask = AsyncMock(side_effect=fake_execute)
    pool._record_inbox = AsyncMock()
    return pool, runner, heart, bus, http, seen


@pytest.fixture
def closed(monkeypatch):
    calls = []

    async def fake_close(store, settings, source_kind, source_id):
        calls.append(source_id)
        return None

    monkeypatch.setattr(worker_mod, "close_intention_quietly", fake_close)
    return calls


NAME = "sweep"


def _guard(command: str, **kw) -> dict:
    """A guard naming NAME, whose allowlisted command is ``command`` (see ``_schedule``)."""
    return {"name": NAME, "mode": "skip", "skip_when": "exit0_quiet", "timeout_seconds": 10, "_command": command, **kw}


def _spec(command: str, **kw) -> schedule_guard.GuardSpec:
    return schedule_guard.parse_guard({"name": NAME, **kw}, {NAME: command})


# ---- skip path -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["true", "printf '  \\n\\t'", "printf ' \\n' >&2"])
async def test_exit0_with_no_output_completes_the_run_without_a_model_call(command, closed):
    schedule = _schedule(_guard(command))
    run = _run(schedule)
    pool, runner, heart, bus, http, _ = _pool(schedule)

    await pool._process_subtask(run)

    runner.run_turn.assert_not_awaited()
    pool._execute_subtask.assert_not_awaited()
    heart.subtasks.complete.assert_awaited_once()
    args, kwargs = heart.subtasks.complete.await_args
    assert args[0] == run.id
    assert args[1].startswith(f"skipped_by_guard: {command} exit=0 stdout=empty stderr=empty (")
    assert kwargs["final_outcome"] == "completed"
    assert kwargs["report_jsonb"]["guard_skipped"] is True
    guard = kwargs["report_jsonb"]["guard"]
    assert set(guard) == RECORD_FIELDS
    assert guard["command"] == command and guard["name"] == NAME
    assert (guard["skipped"], guard["exit_code"], guard["timed_out"]) == (True, 0, False)
    assert guard["timeout_seconds"] == 10 and guard["skip_when"] == "exit0_quiet"
    assert kwargs["tokens_in"] == 0 and kwargs["tokens_out"] == 0
    heart.subtasks.fail.assert_not_awaited()
    # an event, no wake, no push, no inbox row or memory; the run's intention still closes
    assert [e.args[0].type for e in bus.emit.await_args_list] == ["subtask_guard_skipped"]
    http.post.assert_not_awaited()
    pool._record_inbox.assert_not_awaited()
    assert closed == [run.id]


async def test_a_failed_event_after_the_skip_never_sends_the_run_to_the_model(closed):
    schedule = _schedule(_guard("true"))
    pool, runner, heart, bus, *_ = _pool(schedule)
    bus.emit.side_effect = RuntimeError("bus down")
    await pool._process_subtask(_run(schedule))
    heart.subtasks.complete.assert_awaited_once()
    pool._execute_subtask.assert_not_awaited()
    runner.run_turn.assert_not_awaited()


async def test_skip_does_not_close_the_intention_when_the_inbox_is_off(closed):
    schedule = _schedule(_guard("true"))
    pool, *_ = _pool(schedule, result_inbox_enabled=False)
    await pool._process_subtask(_run(schedule))
    assert closed == []


# ---- fall-through paths --------------------------------------------------------------------------------


RECORD_FIELDS = {
    "name",
    "command",
    "skip_when",
    "timeout_seconds",
    "exit_code",
    "timed_out",
    "error",
    "duration_ms",
    "stdout_bytes",
    "stderr_bytes",
    "stderr_head",
    "skipped",
    "mode",
    "would_skip",
    "reason",
}


def _guard_ran(bus) -> dict:
    """The guard record of the one ``subtask_guard_ran`` event the bus got."""
    events = [c.args[0] for c in bus.emit.await_args_list]
    assert [e.type for e in events] == ["subtask_guard_ran"]
    assert events[0].persist is True  # the bus's DB persister writes it to nous_system.events
    guard = events[0].data["guard"]
    assert set(guard) == RECORD_FIELDS and guard["skipped"] is False
    return guard


async def _falls_through(guard: dict, *, record: dict | None = None) -> str:
    schedule = _schedule(guard)
    pool, runner, heart, bus, _, seen = _pool(schedule)
    await pool._process_subtask(_run(schedule))
    pool._execute_subtask.assert_awaited_once()
    heart.subtasks.complete.assert_not_awaited()
    pool._record_inbox.assert_awaited_once()
    if record is not None:
        record.update(_guard_ran(bus))
    return seen["task"]


async def test_non_empty_stdout_falls_through_with_the_output_in_a_receipt():
    record: dict = {}
    task = await _falls_through(_guard("echo 'logged 3 commits'"), record=record)
    assert (record["exit_code"], record["stdout_bytes"], record["stderr_bytes"]) == (0, 17, 0)
    assert record["command"] == "echo 'logged 3 commits'"
    assert task.startswith(TASK)
    assert "exit: 0" in task
    assert "logged 3 commits" in task
    assert "already ran" in task


async def test_a_non_zero_exit_falls_through():
    record: dict = {}
    task = await _falls_through(_guard("echo boom >&2; exit 3"), record=record)
    assert (record["exit_code"], record["stderr_bytes"], record["stderr_head"]) == (3, 5, "boom\n")
    assert "exit: 3" in task
    assert "boom" in task


async def test_a_timeout_falls_through_and_kills_the_commands_children(tmp_path):
    marker = tmp_path / "survived"
    start = time.monotonic()
    record: dict = {}
    task = await _falls_through(_guard(f"(sleep 2; touch {marker}) & wait", timeout_seconds=1), record=record)
    assert time.monotonic() - start < 10
    assert (record["timed_out"], record["exit_code"], record["timeout_seconds"]) == (True, None, 1)
    assert "timed out after 1s" in task
    await asyncio.sleep(2)
    assert not marker.exists()


async def test_a_timeout_kills_a_background_child_after_its_shell_has_exited(tmp_path):
    # The shell is gone (returncode set) but its child holds stdout: the group is still killed.
    marker = tmp_path / "survived"
    record: dict = {}
    await _falls_through(_guard(f"(sleep 2; touch {marker}) & exit 0", timeout_seconds=1), record=record)
    assert record["timed_out"] is True
    await asyncio.sleep(2)
    assert not marker.exists()


async def test_a_timeout_returns_soon_when_an_escaped_child_holds_stdout(tmp_path, monkeypatch):
    # `setsid` puts the child outside the command's process group, so the group kill
    # misses it and it keeps stdout open; the wait for the pipes must still be bounded.
    monkeypatch.setattr(nous_utils, "_KILL_WAIT_SECONDS", 0.5)
    pidfile = tmp_path / "pid"
    spec = _spec(f"setsid sleep 30 & echo $! > {pidfile}; sleep 30", timeout_seconds=1)
    start = time.monotonic()
    try:
        result = await schedule_guard.run_guard(spec)
        assert time.monotonic() - start < 5
        assert result.timed_out
    finally:
        with contextlib.suppress(ValueError, OSError):
            os.kill(int(pidfile.read_text()), signal.SIGKILL)


async def test_a_timeout_and_a_cancellation_use_the_bounded_group_kill(monkeypatch):
    # The escaped-child test above relies on 3.12's proc.wait(); this holds on any version.
    killed = []

    async def spy(proc, **kw):
        killed.append(proc.pid)
        await nous_utils.kill_process_group(proc, **kw)

    monkeypatch.setattr(schedule_guard, "kill_process_group", spy)
    assert (await schedule_guard.run_guard(_spec("sleep 30", timeout_seconds=1))).timed_out
    task = asyncio.create_task(schedule_guard.run_guard(_spec("sleep 30", timeout_seconds=30)))
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(killed) == 2


async def test_a_cancelled_guard_returns_soon_when_an_escaped_child_holds_stdout(tmp_path, monkeypatch):
    monkeypatch.setattr(nous_utils, "_KILL_WAIT_SECONDS", 0.5)
    pidfile = tmp_path / "pid"
    spec = _spec(f"setsid sleep 30 & echo $! > {pidfile}; sleep 30", timeout_seconds=30)
    task = asyncio.create_task(schedule_guard.run_guard(spec))
    try:
        for _ in range(100):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            await asyncio.sleep(0.05)
        start = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)
        assert time.monotonic() - start < 5
    finally:
        with contextlib.suppress(ValueError, OSError):
            os.kill(int(pidfile.read_text()), signal.SIGKILL)


@pytest.mark.parametrize(
    ("command", "level"),
    [("exit 1", "INFO"), ("exit 127", "WARNING"), ("kill -9 $$", "WARNING"), ("echo oops >&2", "WARNING")],
)
async def test_a_guard_that_looks_broken_falls_through_at_warning(command, level, caplog):
    caplog.set_level("INFO", logger=worker_mod.logger.name)
    await _falls_through(_guard(command))
    [rec] = [r for r in caplog.records if "did not certify a no-op" in r.getMessage()]
    assert rec.levelname == level


async def test_a_guard_naming_no_allowlist_entry_warns_with_the_name(caplog):
    schedule = _schedule({"name": "ledger_swep", "skip_when": "exit0_quiet"})
    schedule.commands = {NAME: "true"}
    await _unguarded(schedule, _run(schedule))
    [rec] = [r for r in caplog.records if r.levelname == "WARNING" and "ledger_swep" in r.getMessage()]
    assert NAME in rec.getMessage()


async def test_a_guard_that_raises_falls_through(monkeypatch):
    async def boom(*a, **kw):
        raise OSError("no shell")

    monkeypatch.setattr(schedule_guard.asyncio, "create_subprocess_shell", boom)
    record: dict = {}
    task = await _falls_through(_guard("true"), record=record)
    assert "error: OSError: no shell" in task
    assert record["error"] == "OSError: no shell"


async def test_a_failed_complete_falls_through_to_the_turn():
    schedule = _schedule(_guard("true"))
    pool, runner, heart, *_rest, seen = _pool(schedule)
    heart.subtasks.complete.side_effect = RuntimeError("db down")
    await pool._process_subtask(_run(schedule))
    pool._execute_subtask.assert_awaited_once()
    pool._record_inbox.assert_awaited_once()
    guard = _guard_ran(pool._bus)  # the skip was not written, so the run is recorded here
    assert (guard["exit_code"], guard["would_skip"], guard["reason"]) == (0, True, "error")


async def test_the_records_stderr_head_is_cut_at_512_bytes_and_redacted():
    record: dict = {}
    await _falls_through(_guard("echo password=hunter2 >&2; head -c 2000 /dev/zero | tr '\\0' e >&2"), record=record)
    assert record["stderr_bytes"] == 2017
    assert "hunter2" not in record["stderr_head"] and "[REDACTED]" in record["stderr_head"]
    assert len(record["stderr_head"].encode()) <= 512 + len("[REDACTED]")


async def test_a_failed_record_never_fails_the_turn():
    schedule = _schedule(_guard("echo hi"))
    pool, runner, heart, bus, *_rest, seen = _pool(schedule)
    bus.emit.side_effect = RuntimeError("bus down")
    await pool._process_subtask(_run(schedule))
    pool._execute_subtask.assert_awaited_once()
    assert "hi" in seen["task"]


async def test_stderr_alone_with_exit_0_falls_through_with_it_in_the_receipt():
    # a `...; true` script reports a failure only on stderr
    record: dict = {}
    task = await _falls_through(_guard("echo 'git: not found' >&2; true"), record=record)
    assert (record["exit_code"], record["stderr_head"], record["stderr_bytes"]) == (0, "git: not found\n", 15)
    assert "exit: 0" in task
    assert "git: not found" in task


async def test_the_old_exit0_empty_stdout_mode_is_unknown_and_falls_through():
    task = await _falls_through(_guard("true", skip_when="exit0_empty_stdout"))
    assert "unknown skip_when" in task


async def test_an_unknown_skip_when_falls_through():
    task = await _falls_through(_guard("true", skip_when="exit0"))
    assert "exit: 0" in task
    assert "unknown skip_when" in task


async def test_output_after_2kb_of_whitespace_is_not_empty():
    task = await _falls_through(_guard("head -c 5000 /dev/zero | tr '\\0' ' '; echo logged"))
    assert "exit: 0" in task


async def test_the_receipt_keeps_at_most_2kb_of_output():
    task = await _falls_through(_guard("head -c 10000 /dev/zero | tr '\\0' x"))
    assert "x" * 2048 in task
    assert "x" * 2049 not in task


# ---- no guard: unchanged behaviour ---------------------------------------------------------------------


async def _unguarded(schedule, run, **overrides):
    pool, runner, heart, *_rest, seen = _pool(schedule, **overrides)
    await pool._process_subtask(run)
    pool._execute_subtask.assert_awaited_once()
    heart.subtasks.complete.assert_not_awaited()
    assert seen["task"] == run.task
    return heart


async def test_the_kill_switch_ignores_guards():
    schedule = _schedule(_guard("true"))
    heart = await _unguarded(schedule, _run(schedule), schedule_guards_enabled=False)
    heart.schedules.get.assert_not_awaited()


async def test_a_schedule_without_guard_metadata_is_unchanged():
    schedule = _schedule(None)
    await _unguarded(schedule, _run(schedule))


async def test_a_run_that_is_not_a_scheduled_fire_is_unchanged():
    schedule = _schedule(_guard("true"))
    run = _run(schedule)
    run.metadata_ = {}
    heart = await _unguarded(schedule, run)
    heart.schedules.get.assert_not_awaited()


async def test_a_check_and_fix_run_is_never_guarded():
    schedule = _schedule(_guard("true"))
    await _unguarded(schedule, _run(schedule, job_fix=True))


async def test_a_schedule_of_another_agent_is_never_read_for_a_guard():
    schedule = _schedule(_guard("true"))
    schedule.agent_id = "someone-else"
    await _unguarded(schedule, _run(schedule))


async def test_a_run_whose_text_is_not_the_schedules_task_is_never_guarded():
    schedule = _schedule(_guard("true"))
    run = _run(schedule)
    run.task = "Check a background job that keeps going wrong"
    await _unguarded(schedule, run)


@pytest.mark.parametrize("guard", [{"skip_when": "exit0_quiet"}, {"name": ""}, "true", {"name": 5}])
async def test_a_malformed_guard_is_ignored(guard):
    schedule = _schedule(guard)
    await _unguarded(schedule, _run(schedule))


# ---- the allowlist -------------------------------------------------------------------------------------


async def test_a_name_that_is_not_in_the_allowlist_falls_through():
    schedule = _schedule({"name": "not-allowlisted", "skip_when": "exit0_quiet"})
    schedule.commands = {NAME: "true"}
    await _unguarded(schedule, _run(schedule))


async def test_an_empty_allowlist_falls_through():
    schedule = _schedule(_guard("true"))
    schedule.commands = {}
    await _unguarded(schedule, _run(schedule))


async def test_a_raw_command_in_the_row_is_never_executed(tmp_path):
    marker = tmp_path / "ran"
    schedule = _schedule({"command": f"touch {marker}", "skip_when": "exit0_quiet"})
    schedule.commands = {NAME: "true"}
    await _unguarded(schedule, _run(schedule))
    assert not marker.exists()


async def test_a_named_guard_runs_the_allowlisted_command_not_the_rows(tmp_path, closed):
    marker = tmp_path / "ran"
    schedule = _schedule({**_guard("true"), "command": f"touch {marker}"})
    pool, runner, heart, *_ = _pool(schedule)
    await pool._process_subtask(_run(schedule))
    heart.subtasks.complete.assert_awaited_once()  # "true" ran and certified a no-op
    assert heart.subtasks.complete.await_args.args[1].startswith("skipped_by_guard: true ")
    assert not marker.exists()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"a": "bash /x.sh", "b": "true"}', {"a": "bash /x.sh", "b": "true"}),
        ("", {}),
        ("not json", {}),
        ('["bash /x.sh"]', {}),
        ('{"a": "true", "b": "", "c": 5, "": "true"}', {"a": "true"}),
    ],
)
def test_the_allowlist_env_is_parsed_and_never_fails_boot(monkeypatch, raw, expected):
    monkeypatch.setenv("NOUS_SCHEDULE_GUARD_COMMANDS", raw)
    assert Settings(_env_file=None).schedule_guard_commands == expected


async def test_a_failed_schedule_read_is_ignored():
    schedule = _schedule(_guard("true"))
    pool, runner, heart, *_rest, seen = _pool(schedule)
    heart.schedules.get = AsyncMock(side_effect=RuntimeError("db down"))
    run = _run(schedule)
    await pool._process_subtask(run)
    pool._execute_subtask.assert_awaited_once()
    assert seen["task"] == run.task


# ---- the command runner --------------------------------------------------------------------------------


def test_parse_clamps_the_timeout():
    # well below F101's 300 s active-run grace (task_scheduler._STALE_GRACE_SECONDS)
    assert _spec("true", timeout_seconds=10_000).timeout_seconds == 120
    assert _spec("true", timeout_seconds=300).timeout_seconds == 120
    assert schedule_guard.MAX_TIMEOUT_SECONDS + 5 < task_scheduler._STALE_GRACE_SECONDS / 2
    assert _spec("true", timeout_seconds=0).timeout_seconds == 1
    assert _spec("true").timeout_seconds == 60


async def test_a_cancelled_guard_kills_its_command(tmp_path):
    started, survived = tmp_path / "started", tmp_path / "survived"
    spec = _spec(f"touch {started}; sleep 1; touch {survived}", timeout_seconds=30)
    task = asyncio.create_task(schedule_guard.run_guard(spec))
    for _ in range(100):
        if started.exists():
            break
        await asyncio.sleep(0.05)
    assert started.exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(1.5)
    assert not survived.exists()


# ---- end to end on a real database ---------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_a_guarded_fire_completes_its_row_and_closes_its_intention(db):
    from nous.brain import intentions
    from nous.handlers.task_scheduler import create_fire
    from nous.heart.heart import Heart

    settings = Settings(
        _env_file=None,
        agent_id=f"test-guard-{uuid.uuid4().hex[:8]}",
        intentions_enabled=True,
        result_inbox_enabled=True,
        schedule_guard_commands={NAME: "true"},
    )
    heart = Heart(db, settings)
    try:
        schedule = await heart.schedules.create(
            task=TASK,
            schedule_type="recurring",
            interval_seconds=3600,
            metadata={"guard": {"name": NAME, "mode": "skip", "skip_when": "exit0_quiet", "timeout_seconds": 10}},
        )
        fire = await create_fire(heart, settings, schedule)
        run = await heart.subtasks.dequeue("worker-0")
        assert run is not None and run.id == fire.id
        runner = MagicMock()
        runner.run_turn = AsyncMock()
        runner.root_cancelled = MagicMock(return_value=False)
        bus = AsyncMock()
        await SubtaskWorkerPool(runner=runner, heart=heart, settings=settings, bus=bus)._process_subtask(run)

        runner.run_turn.assert_not_awaited()
        row = await heart.subtasks.get(run.id)
        assert row.status == "completed" and row.final_outcome == "completed"
        assert row.result.startswith("skipped_by_guard: true exit=0 stdout=empty stderr=empty")
        assert row.report_jsonb["guard_skipped"] is True
        assert set(row.report_jsonb["guard"]) == RECORD_FIELDS and row.report_jsonb["guard"]["skipped"] is True
        assert (row.tokens_in, row.tokens_out, row.attempts) == (0, 0, 0)
        intention = await heart.intentions.get_for_source(intentions.SOURCE_SUBTASK, run.id)
        assert intention is not None and intention.state != "open"
        assert [e.args[0].type for e in bus.emit.await_args_list] == ["subtask_guard_skipped"]
    finally:
        await heart.close()


@pytest.mark.postgres_only
async def test_a_guard_that_falls_through_persists_its_record_as_an_event(db):
    from sqlalchemy import select

    from nous.brain.brain import Brain
    from nous.events import EventBus
    from nous.handlers.task_scheduler import create_fire
    from nous.heart.heart import Heart
    from nous.storage.models import Event as EventRow

    settings = Settings(
        _env_file=None,
        agent_id=f"test-guard-{uuid.uuid4().hex[:8]}",
        schedule_guard_commands={NAME: "echo 'logged 1 commit'"},
    )
    heart, brain, bus = Heart(db, settings), Brain(db, settings), EventBus()

    async def persist(event):  # the persister main.py wires
        await brain.emit_event(event.type, {**event.data}, session_id=event.session_id, event_id=event.event_id)

    bus.set_db_persister(persist)
    await bus.start()
    try:
        schedule = await heart.schedules.create(
            task=TASK,
            schedule_type="recurring",
            interval_seconds=3600,
            metadata={"guard": {"name": NAME, "mode": "skip", "skip_when": "exit0_quiet", "timeout_seconds": 10}},
        )
        await create_fire(heart, settings, schedule)
        run = await heart.subtasks.dequeue("worker-0")
        pool = SubtaskWorkerPool(runner=MagicMock(), heart=heart, settings=settings, bus=bus)
        pool._execute_subtask = AsyncMock()
        pool._record_inbox = AsyncMock()
        await pool._process_subtask(run)
        pool._execute_subtask.assert_awaited_once()
        await bus.stop()  # drains the queue through the persister
        async with db.session() as session:
            rows = (
                (
                    await session.execute(
                        select(EventRow).where(
                            EventRow.agent_id == settings.agent_id, EventRow.event_type == "subtask_guard_ran"
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1
        guard = rows[0].data["guard"]
        assert set(guard) == RECORD_FIELDS
        assert (guard["skipped"], guard["exit_code"], guard["stdout_bytes"]) == (False, 0, 16)
        assert rows[0].data["subtask_id"] == run.id.hex
    finally:
        await bus.stop()
        await brain.close()
        await heart.close()


def test_the_envelope_of_a_guard_skipped_run_is_none():
    from nous.heart.result_inbox import subtask_envelope

    row = SimpleNamespace(
        id=uuid.uuid4(),
        task=TASK,
        status="completed",
        result="skipped_by_guard: true exit=0 stdout=empty",
        error=None,
        final_outcome="completed",
        report_jsonb={"guard_skipped": True},
    )
    assert subtask_envelope(row, 2000) is None
    row.report_jsonb = {}
    assert subtask_envelope(row, 2000).msg_type == "INFORM"  # the same row without the flag has something to say


@pytest.mark.postgres_only
async def test_the_reconciler_writes_no_inbox_row_for_a_guard_skipped_run(db, monkeypatch):
    from sqlalchemy import select, update

    from nous.handlers.task_scheduler import create_fire
    from nous.heart import result_reconciler
    from nous.heart.heart import Heart
    from nous.storage.models import ResultInbox, Subtask

    settings = Settings(
        _env_file=None,
        agent_id=f"test-guard-{uuid.uuid4().hex[:8]}",
        result_inbox_enabled=True,
        schedule_guard_commands={NAME: "true"},
    )
    heart = Heart(db, settings)
    selected = []
    real_envelope = result_reconciler.subtask_envelope

    def spy(subtask, body_max):  # what the pass selected
        selected.append(subtask.id)
        return real_envelope(subtask, body_max)

    monkeypatch.setattr(result_reconciler, "subtask_envelope", spy)
    try:
        await heart.result_inbox.ensure_enabled_at()
        guarded = await heart.schedules.create(
            task=TASK,
            schedule_type="recurring",
            interval_seconds=3600,
            session_id="chat-1",
            metadata={"guard": {"name": NAME, "mode": "skip", "skip_when": "exit0_quiet", "timeout_seconds": 10}},
        )
        plain = await heart.schedules.create(
            task="Summarise the day.", schedule_type="recurring", interval_seconds=3600, session_id="chat-1"
        )
        await create_fire(heart, settings, guarded)
        skipped = await heart.subtasks.dequeue("worker-0")
        runner = MagicMock()
        runner.root_cancelled = MagicMock(return_value=False)
        await SubtaskWorkerPool(runner=runner, heart=heart, settings=settings, bus=AsyncMock())._process_subtask(
            skipped
        )
        await create_fire(heart, settings, plain)
        ran = await heart.subtasks.dequeue("worker-0")
        await heart.subtasks.complete(ran.id, "3 commits logged")
        row = await heart.subtasks.get(skipped.id)
        assert row.parent_session_id == "chat-1" and row.report_jsonb["guard_skipped"] is True
        assert row.delivered is True  # settled in the skip's own write

        async def inbox_ids():
            async with db.session() as session:
                rows = await session.execute(
                    select(ResultInbox.source_id).where(ResultInbox.agent_id == settings.agent_id)
                )
                return list(rows.scalars().all())

        reconcile = result_reconciler.InboxSubtaskPass(db, heart.result_inbox, settings)
        assert await reconcile.run(limit=10) == 1
        assert await inbox_ids() == [ran.id]
        # A skipped row whose settle was lost is still never selected, so never written nor rescanned.
        async with db.session() as session:
            await session.execute(update(Subtask).where(Subtask.id == skipped.id).values(delivered=False))
            await session.commit()
        selected.clear()
        assert await reconcile.run(limit=10) == 0
        assert skipped.id not in selected
        assert await inbox_ids() == [ran.id]
    finally:
        await heart.close()


def test_the_runners_root_cancelled_reads_the_view_tool_calls_are_refused_on():
    from nous.api.runner import AgentRunner

    runner = AgentRunner.__new__(AgentRunner)
    runner._forks = []
    cancelled = uuid.uuid4()
    runner.set_cancelled_roots({cancelled}.__contains__)
    assert runner.root_cancelled(cancelled) is True
    assert runner.root_cancelled(uuid.uuid4()) is False


@pytest.mark.postgres_only
@pytest.mark.parametrize("cancel", [True, False])
async def test_a_root_cancelled_before_the_guard_starts_runs_no_command(db, monkeypatch, cancel):
    from nous.api.execution_context import ExecutionContext
    from nous.brain import continuation
    from nous.handlers.task_scheduler import create_fire
    from nous.heart.heart import Heart

    settings = Settings(
        _env_file=None,
        agent_id=f"test-guard-{uuid.uuid4().hex[:8]}",
        intentions_enabled=True,
        result_inbox_enabled=True,
        schedule_guard_commands={NAME: "true"},
    )
    heart = Heart(db, settings)
    cancelled: set = set()
    runner = MagicMock()
    runner.run_turn = AsyncMock()
    runner.root_cancelled = cancelled.__contains__
    launched = []
    real_run_guard = schedule_guard.run_guard

    async def spy(spec):
        launched.append(spec.command)
        return await real_run_guard(spec)

    monkeypatch.setattr(schedule_guard, "run_guard", spy)
    try:
        schedule = await heart.schedules.create(
            task=TASK,
            schedule_type="recurring",
            interval_seconds=3600,
            metadata={"guard": {"name": NAME, "mode": "skip", "skip_when": "exit0_quiet", "timeout_seconds": 10}},
        )
        await create_fire(heart, settings, schedule)
        run = await heart.subtasks.dequeue("worker-0")
        root = ExecutionContext.for_subtask(run, "").root_intention_id
        assert root is not None
        pool = SubtaskWorkerPool(runner=runner, heart=heart, settings=settings, bus=AsyncMock())
        pool._execute_subtask = AsyncMock()
        real_spec = pool._guard_spec

        async def spec_then_cancel(subtask):  # the owner cancels while the schedule is read
            spec = await real_spec(subtask)
            if cancel:
                async with db.session() as session:
                    outcome = await continuation.cancel_root(
                        session, settings.agent_id, root, reason="stop", actor="owner"
                    )
                    await session.commit()
                cancelled.update(outcome.root_ids)  # as ContinuationRunner.cancel_root fills the view
            return spec

        pool._guard_spec = spec_then_cancel
        await pool._process_subtask(run)

        runner.run_turn.assert_not_awaited()
        pool._execute_subtask.assert_not_awaited()
        row = await heart.subtasks.get(run.id)
        if cancel:
            assert launched == []
            assert (row.status, row.final_outcome, row.report_jsonb) == ("cancelled", "cancelled", None)
        else:
            assert launched == ["true"]
            assert row.status == "completed" and row.report_jsonb["guard_skipped"] is True
    finally:
        await heart.close()


# ---- phase 2: shadow mode ------------------------------------------------------------------------------


def _shadow(command: str, **kw) -> dict:
    """A guard naming NAME with no ``mode``: shadow, the default for a newly guarded job."""
    guard = _guard(command, **kw)
    guard.pop("mode")
    return guard


def _row(run, *, status="completed", report=None) -> SimpleNamespace:
    """The re-read row of a run whose turn finished."""
    return SimpleNamespace(id=run.id, status=status, report_jsonb=report, result="done", metadata_=run.metadata_)


def _events(bus, event_type: str) -> list:
    return [c.args[0] for c in bus.emit.await_args_list if c.args[0].type == event_type]


async def _shadow_run(guard: dict, *, report=None, inbox=False, ledger=(), status="completed", push=False):
    """Process one fire of a shadow-guarded schedule; the model turn is faked."""
    schedule = _schedule(guard)
    pool, runner, heart, bus, http, seen = _pool(schedule)
    run = _run(schedule)
    heart.subtasks.get = AsyncMock(return_value=_row(run, status=status, report=report))
    heart.job_runs.ledger_effects = AsyncMock(return_value=list(ledger))
    pool._record_inbox = AsyncMock(return_value=inbox)

    async def fake_execute(subtask):
        seen["task"] = subtask.task
        if push:
            await pool._notify_telegram(subtask, result="3 commits logged")

    pool._execute_subtask = AsyncMock(side_effect=fake_execute)
    http.post = AsyncMock(return_value=SimpleNamespace(is_success=True))
    await pool._process_subtask(run)
    return pool, heart, bus, http, seen, run


def test_a_guard_without_a_mode_is_shadow_and_skip_must_be_written():
    assert _spec("true").mode == schedule_guard.MODE_SHADOW == "shadow"
    assert _spec("true", mode="skip").mode == schedule_guard.MODE_SKIP == "skip"
    assert _spec("true", mode="dry_run").mode == "dry_run"  # kept, so the worker can see it is unknown


async def test_a_shadow_guard_that_would_skip_still_runs_the_model_with_the_receipt(closed):
    pool, heart, bus, _http, seen, run = await _shadow_run(_shadow("true"))
    pool._execute_subtask.assert_awaited_once()
    heart.subtasks.complete.assert_not_awaited()  # never a guard_skipped row: a real run for the scorer
    assert seen["task"].startswith(TASK) and "[Run guard receipt]" in seen["task"]
    assert closed == []
    (ran,) = _events(bus, "subtask_guard_ran")
    guard = ran.data["guard"]
    assert set(guard) == RECORD_FIELDS
    assert (guard["mode"], guard["would_skip"], guard["skipped"], guard["reason"]) == ("shadow", True, False, None)
    assert ran.data["schedule_id"] == run.metadata_["schedule_id"]
    assert _events(bus, "subtask_guard_skipped") == []


async def test_a_shadow_guard_that_would_not_skip_records_its_reason():
    _pool_, _heart, bus, *_rest = await _shadow_run(_shadow("echo 'logged 2 commits'"))
    guard = _events(bus, "subtask_guard_ran")[0].data["guard"]
    assert (guard["mode"], guard["would_skip"], guard["reason"]) == ("shadow", False, "output")


@pytest.mark.parametrize("mode", ["dry_run", "", 5, "SKIP"])
async def test_an_unknown_mode_falls_through_at_warning(mode, caplog):
    schedule = _schedule(_guard("true", mode=mode))
    pool, _runner, heart, bus, _http, seen = _pool(schedule)
    with caplog.at_level("WARNING"):
        await pool._process_subtask(_run(schedule))
    pool._execute_subtask.assert_awaited_once()
    heart.subtasks.complete.assert_not_awaited()
    assert "[Run guard receipt]" in seen["task"] and "unknown mode" in seen["task"]
    guard = _guard_ran(bus)  # the command certified a no-op, but no known mode acts on it: a config error
    assert (guard["mode"], guard["would_skip"], guard["reason"]) == (str(mode), True, "error")
    assert any("mode" in r.getMessage() and r.levelname == "WARNING" for r in caplog.records)


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        ("echo hi", "output"),
        ("echo oops >&2; true", "stderr"),
        ("exit 3", "exit"),
        ("echo hi; exit 3", "exit"),
    ],
)
async def test_each_fall_through_records_its_reason(command, reason):
    record: dict = {}
    await _falls_through(_guard(command), record=record)
    assert (record["mode"], record["would_skip"], record["reason"]) == ("skip", False, reason)


async def test_a_timeout_and_an_error_record_their_reasons(monkeypatch):
    record: dict = {}
    await _falls_through(_guard("sleep 5", timeout_seconds=1), record=record)
    assert record["reason"] == "timeout"

    async def boom(*a, **kw):
        raise OSError("no shell")

    monkeypatch.setattr(schedule_guard.asyncio, "create_subprocess_shell", boom)
    record.clear()
    await _falls_through(_guard("true"), record=record)
    assert record["reason"] == "error"


async def test_a_skip_mode_skip_records_mode_and_would_skip(closed):
    schedule = _schedule(_guard("true"))
    pool, _runner, heart, *_rest = _pool(schedule)
    await pool._process_subtask(_run(schedule))
    guard = heart.subtasks.complete.await_args.kwargs["report_jsonb"]["guard"]
    assert (guard["mode"], guard["would_skip"], guard["skipped"], guard["reason"]) == ("skip", True, True, None)


# ---- phase 2: did the model act? ----------------------------------------------------------------------


def _acted(bus) -> dict:
    (event,) = _events(bus, "subtask_guard_acted")
    assert event.persist is True
    return event.data


async def test_a_shadow_run_that_did_nothing_records_not_acted():
    *_rest, bus, _http, _seen, run = await _shadow_run(_shadow("true"), report={"summary": ""})
    data = _acted(bus)
    assert (data["acted"], data["acted_by"]) == (False, [])
    assert data["subtask_id"] == run.id.hex and data["schedule_id"] == run.metadata_["schedule_id"]
    assert (data["guard"]["name"], data["guard"]["mode"], data["guard"]["would_skip"]) == (NAME, "shadow", True)
    assert data["run_status"] == "completed" and data["ledger_persisted"] is True


@pytest.mark.parametrize(
    ("kw", "signal"),
    [
        ({"report": {"summary": "Logged 3 commits."}}, "report"),
        ({"report": {"summary": " ", "findings": ["a"]}}, "finding"),
        ({"report": {"summary": " ", "next_actions": ["merge #1"]}}, "finding"),
        ({"report": {"summary": " ", "incomplete": True, "blocked_reason": "no access"}}, "finding"),
        ({"inbox": True}, "inbox"),
        ({"push": True}, "push"),
        ({"ledger": [("write_file", "success", {"path": "/x"})]}, "ledger"),
        ({"ledger": [("bash", "unknown", {"command_sha256": "0" * 16})]}, "ledger"),
    ],
)
async def test_each_kind_of_action_counts_as_acted(kw, signal):
    *_rest, bus, _http, _seen, _run_ = await _shadow_run(_shadow("true"), **kw)
    data = _acted(bus)
    assert data["acted"] is True and data["acted_by"] == [signal]


async def test_the_guards_own_command_and_a_blocked_call_are_not_acting():
    own = schedule_guard.command_digest("true")
    ledger = [("bash", "success", {"command_sha256": own, "command_len": "4"}), ("send_email", "blocked", {})]
    *_rest, bus, _http, _seen, _run_ = await _shadow_run(_shadow("true"), ledger=ledger)
    assert _acted(bus)["acted"] is False


def test_acted_signals_is_the_one_definition():
    row = SimpleNamespace(status="completed", report_jsonb={"summary": "x", "findings": ["f"]})
    signals = schedule_guard.acted_signals(
        row, inbox_written=True, pushed=True, ledger=[("write_file", "error", {})], guard_command="true"
    )
    assert signals == ["report", "finding", "inbox", "push", "ledger"]
    assert schedule_guard.acted_signals(None, inbox_written=False, pushed=False, ledger=[], guard_command="t") == []
    legacy = SimpleNamespace(report_jsonb=None)  # the legacy path writes no report, only a result text
    assert schedule_guard.acted_signals(legacy, inbox_written=False, pushed=False, ledger=[], guard_command="t") == []


async def test_a_push_is_counted_only_when_it_was_sent():
    *_rest, bus, http, _seen, _run_ = await _shadow_run(_shadow("true"), push=True)
    http.post.assert_awaited_once()
    assert _acted(bus)["acted_by"] == ["push"]


async def test_the_inbox_row_and_push_of_a_failed_run_are_not_acting():
    # A failure notice is the system reporting, not the model acting; the run's status is recorded beside it.
    *_rest, bus, http, _seen, _run_ = await _shadow_run(_shadow("true"), inbox=True, push=True, status="failed")
    http.post.assert_awaited_once()
    data = _acted(bus)
    assert (data["acted"], data["run_status"]) == (False, "failed")


@pytest.mark.parametrize("post", [RuntimeError("telegram down"), SimpleNamespace(is_success=False)])
async def test_a_failed_push_is_not_counted(post):
    schedule = _schedule(_shadow("true"))
    pool, _runner, heart, bus, http, _seen = _pool(schedule)
    run = _run(schedule)
    heart.subtasks.get = AsyncMock(return_value=_row(run))
    heart.job_runs.ledger_effects = AsyncMock(return_value=[])
    pool._record_inbox = AsyncMock(return_value=False)
    http.post = AsyncMock(side_effect=post) if isinstance(post, Exception) else AsyncMock(return_value=post)

    async def fake_execute(subtask):
        await pool._notify_telegram(subtask, result="x")

    pool._execute_subtask = AsyncMock(side_effect=fake_execute)
    await pool._process_subtask(run)
    assert _acted(bus)["acted"] is False
    assert pool._shadow == {} and pool._pushed == set()  # nothing left behind for the next run


async def test_a_skip_mode_fall_through_records_no_acted_event():
    schedule = _schedule(_guard("echo hi"))
    pool, _runner, _heart, bus, *_rest = _pool(schedule)
    await pool._process_subtask(_run(schedule))
    assert _events(bus, "subtask_guard_acted") == []


async def test_a_turn_that_timed_out_still_records_acted():
    schedule = _schedule(_shadow("true"))
    pool, _runner, heart, bus, _http, _seen = _pool(schedule)
    run = _run(schedule)
    run.timeout_seconds = 1
    heart.subtasks.get = AsyncMock(return_value=_row(run, status="failed"))
    heart.job_runs.ledger_effects = AsyncMock(return_value=[("write_file", "success", {})])
    pool._record_inbox = AsyncMock(return_value=False)

    async def slow(subtask):
        await asyncio.sleep(5)

    pool._execute_subtask = AsyncMock(side_effect=slow)
    await pool._process_subtask(run)
    data = _acted(bus)
    assert (data["acted"], data["acted_by"], data["run_status"]) == (True, ["ledger"], "failed")


async def test_a_run_left_running_by_a_shutdown_records_nothing():
    *_rest, bus, _http, _seen, _run_ = await _shadow_run(_shadow("true"), status="running", inbox=True)
    assert _events(bus, "subtask_guard_acted") == []


async def test_a_failed_acted_record_never_fails_the_run(caplog):
    schedule = _schedule(_shadow("true"))
    pool, _runner, heart, bus, _http, _seen = _pool(schedule)
    run = _run(schedule)
    heart.subtasks.get = AsyncMock(side_effect=RuntimeError("db down"))
    heart.job_runs.ledger_effects = AsyncMock(return_value=[])
    pool._record_inbox = AsyncMock(return_value=False)
    await pool._process_subtask(run)  # does not raise
    assert _events(bus, "subtask_guard_acted") == []
    assert "could not record whether the model acted" in caplog.text
    assert pool._shadow == {}


# ---- phase 2: an unresolved guard leaves a persisted record ---------------------------------------------


@pytest.mark.parametrize(
    ("guard", "name"), [({"name": "nope", "skip_when": "exit0_quiet"}, "nope"), ({"skip_when": "x"}, None)]
)
async def test_a_guard_that_names_no_allowlisted_command_emits_unresolved(guard, name):
    schedule = _schedule(guard)
    pool, _runner, _heart, bus, *_rest = _pool(schedule)
    await pool._process_subtask(_run(schedule))
    pool._execute_subtask.assert_awaited_once()
    (event,) = _events(bus, "subtask_guard_unresolved")
    assert event.persist is True
    assert event.data["schedule_id"] == schedule.id.hex
    assert (event.data["guard"]["name"], event.data["guard"]["reason"]) == (name, "not_allowlisted")
    assert _events(bus, "subtask_guard_ran") == []


@pytest.mark.postgres_only
async def test_an_unresolved_guard_and_a_shadow_run_persist_their_events(db):
    from sqlalchemy import select

    from nous.brain.brain import Brain
    from nous.events import EventBus
    from nous.handlers.task_scheduler import create_fire
    from nous.heart.heart import Heart
    from nous.storage.models import Event as EventRow
    from nous.storage.models import ExecutionLedgerEntry

    settings = Settings(
        _env_file=None,
        agent_id=f"test-guard-{uuid.uuid4().hex[:8]}",
        schedule_guard_commands={NAME: "true"},
        result_inbox_enabled=True,
    )
    heart, brain, bus = Heart(db, settings), Brain(db, settings), EventBus()

    async def persist(event):  # the persister main.py wires
        await brain.emit_event(event.type, {**event.data}, session_id=event.session_id, event_id=event.event_id)

    bus.set_db_persister(persist)
    await bus.start()
    try:
        await heart.result_inbox.ensure_enabled_at()
        unresolved = await heart.schedules.create(
            task=TASK, schedule_type="recurring", interval_seconds=3600, metadata={"guard": {"name": "nope"}}
        )
        shadow = await heart.schedules.create(
            task=TASK + " (2)",
            schedule_type="recurring",
            interval_seconds=3600,
            session_id="chat-1",  # so its result goes to the inbox
            metadata={"guard": {"name": NAME}},
        )
        pool = SubtaskWorkerPool(runner=MagicMock(), heart=heart, settings=settings, bus=bus)

        async def turn(subtask):  # the model re-runs the guard's command and writes one file
            async with db.session() as session:
                for tool, key_args in (
                    ("bash", {"command_sha256": schedule_guard.command_digest("true"), "command_len": "4"}),
                    ("write_file", {"path": "/tmp/x"}),
                ):
                    session.add(
                        ExecutionLedgerEntry(
                            agent_id=settings.agent_id,
                            context_kind="subtask",
                            subtask_id=subtask.id,
                            tool_name=tool,
                            side_effect_type="write",
                            key_args=key_args,
                            status="success",
                        )
                    )
                await session.commit()
            await heart.subtasks.complete(subtask.id, "wrote x", final_outcome="completed")

        pool._execute_subtask = AsyncMock(side_effect=turn)
        for schedule in (unresolved, shadow):
            await create_fire(heart, settings, schedule)
            await pool._process_subtask(await heart.subtasks.dequeue("worker-0"))
        assert pool._execute_subtask.await_count == 2
        await bus.stop()  # drains the queue through the persister
        async with db.session() as session:
            rows = (
                (await session.execute(select(EventRow).where(EventRow.agent_id == settings.agent_id))).scalars().all()
            )
        by_type = {r.event_type: r.data for r in rows}
        assert by_type["subtask_guard_unresolved"]["schedule_id"] == unresolved.id.hex
        assert by_type["subtask_guard_unresolved"]["guard"]["reason"] == "not_allowlisted"
        assert by_type["subtask_guard_ran"]["guard"]["would_skip"] is True
        acted = by_type["subtask_guard_acted"]
        assert acted["schedule_id"] == shadow.id.hex and acted["acted_by"] == ["inbox", "ledger"]  # not the bash re-run
        assert not any(r.event_type == "subtask_guard_skipped" for r in rows)
    finally:
        await bus.stop()
        await brain.close()
        await heart.close()
