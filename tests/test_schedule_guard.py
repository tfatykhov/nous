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
from nous.handlers import schedule_guard
from nous.handlers import subtask_worker as worker_mod
from nous.handlers.subtask_worker import SubtaskWorkerPool

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
    return {"name": NAME, "skip_when": "exit0_quiet", "timeout_seconds": 10, "_command": command, **kw}


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
    "name", "command", "skip_when", "timeout_seconds", "exit_code", "timed_out", "error",
    "duration_ms", "stdout_bytes", "stderr_bytes", "stderr_head", "skipped",
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
    assert _guard_ran(pool._bus)["exit_code"] == 0  # the skip was not written, so the run is recorded here


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
    task = await _falls_through(_guard("echo 'git: not found' >&2; true"))
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
    assert _spec("true", timeout_seconds=10_000).timeout_seconds == 300
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
            task=TASK, schedule_type="recurring", interval_seconds=3600, metadata={"guard": {"name": NAME, "skip_when": "exit0_quiet", "timeout_seconds": 10}}
        )
        fire = await create_fire(heart, settings, schedule)
        run = await heart.subtasks.dequeue("worker-0")
        assert run is not None and run.id == fire.id
        runner = MagicMock()
        runner.run_turn = AsyncMock()
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
            task=TASK, schedule_type="recurring", interval_seconds=3600,
            metadata={"guard": {"name": NAME, "skip_when": "exit0_quiet", "timeout_seconds": 10}},
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
                await session.execute(
                    select(EventRow).where(
                        EventRow.agent_id == settings.agent_id, EventRow.event_type == "subtask_guard_ran"
                    )
                )
            ).scalars().all()
        assert len(rows) == 1
        guard = rows[0].data["guard"]
        assert set(guard) == RECORD_FIELDS
        assert (guard["skipped"], guard["exit_code"], guard["stdout_bytes"]) == (False, 0, 16)
        assert rows[0].data["subtask_id"] == run.id.hex
    finally:
        await bus.stop()
        await brain.close()
        await heart.close()
