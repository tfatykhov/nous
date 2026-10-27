"""F107 phase 3: run guards for heartbeat dynamic checks (nous/heartbeat/check_guard.py + DynamicCheck.run).

A check's ``metadata.guard`` names an entry of ``NOUS_SCHEDULE_GUARD_COMMANDS``, like a schedule's. The guard runs
once before the check's model turn; only ``mode: skip`` with an ``exit0_quiet`` result ends the run with no model
call. Every other result runs the model with the guard's receipt; the guard command never runs twice in one run.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nous.handlers import schedule_guard
from nous.heartbeat import check_guard
from nous.heartbeat.dynamic import DynamicCheck

pytestmark = pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "killpg"), reason="POSIX shell and process groups"
)

NAME = "gridbot_tick"
CLEAN = '{"has_findings": false, "findings": []}'
FOUND = '{"has_findings": true, "findings": [{"summary": "SOL_USDT tick failed", "urgency": "high"}]}'


class Recorder:
    """The guard context's emit and ledger hooks, recording what the check persisted."""

    def __init__(self, ledger: list | None = None) -> None:
        self.events: list[tuple[str, dict]] = []
        self.ledger_rows = ledger or []
        self.ledger_sessions: list[str] = []

    async def emit(self, event_type: str, data: dict, session_id: str) -> None:
        self.events.append((event_type, data))

    async def ledger(self, session_id: str) -> list:
        self.ledger_sessions.append(session_id)
        return self.ledger_rows

    def of(self, event_type: str) -> list[dict]:
        return [d for t, d in self.events if t == event_type]


def counted(tmp_path: Path, body: str = "") -> tuple[str, Path]:
    """A guard command that appends a line to a counter file each time it runs, then runs ``body``."""
    counter = tmp_path / "count"
    return f"echo x >> {counter}; {body}", counter


def runs(counter: Path) -> int:
    return len(counter.read_text().splitlines()) if counter.exists() else 0


def make(
    command: str,
    guard: dict | None = None,
    *,
    response: str = CLEAN,
    timeout: int = 300,
    rec: Recorder | None = None,
    enabled: bool = True,
    commands: dict | None = None,
    name: str = "gridbot-paper-tick",
    intention: dict | None = None,
) -> tuple[DynamicCheck, MagicMock, Recorder]:
    runner = MagicMock()
    runner.run_turn = AsyncMock(return_value=(response, None, {"input_tokens": 10, "output_tokens": 5}))
    runner.end_conversation = AsyncMock()
    rec = rec or Recorder()
    ctx = check_guard.CheckGuardContext(
        enabled=enabled,
        commands={NAME: command} if commands is None else commands,
        emit=rec.emit,
        ledger=rec.ledger,
    )
    check = DynamicCheck(
        check_id=str(uuid.uuid4()),
        name=name,
        prompt="Diagnose the gridbot paper tick and report per the rules.",
        tools=["bash"],
        timeout=timeout,
        runner=runner,
        guard={"name": NAME, "timeout_seconds": 30, **(guard or {})},
        guards=ctx,
        intention=intention,
    )
    return check, runner, rec


def instruction(runner: MagicMock) -> str:
    return runner.run_turn.await_args.args[1]


# --- skip mode ---------------------------------------------------------------------------------------------


async def test_a_quiet_guard_in_skip_mode_ends_the_run_with_no_model_call_and_records_it(tmp_path):
    command, counter = counted(tmp_path)
    check, runner, rec = make(command, {"mode": "skip"})

    result = await check.run()

    runner.run_turn.assert_not_awaited()
    assert result.skipped is False and result.findings == [] and result.tokens_used == 0
    assert runs(counter) == 1
    [ran] = rec.of(check_guard.RAN)
    assert ran["check_name"] == "gridbot-paper-tick" and ran["check_id"] == check.check_id
    assert ran["guard"]["skipped"] is True and ran["guard"]["would_skip"] is True and ran["guard"]["reason"] is None
    assert rec.of(check_guard.ACTED) == []


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("exit 1", "exit"),
        ("echo '!!! SOL_USDT tick FAILED (exit=1)'", "output"),
        ("echo 'warning: low disk' >&2", "stderr"),
        ("echo 'failures since last report: 2 (consecutive: 3)'", "output"),
    ],
)
async def test_a_guard_that_is_not_quiet_falls_through_once_with_the_action_receipt(tmp_path, body, reason):
    command, counter = counted(tmp_path, body)
    check, runner, rec = make(command, {"mode": "skip", "is_action": True}, response=FOUND)

    result = await check.run()

    runner.run_turn.assert_awaited_once()
    assert runs(counter) == 1  # the guard is the action: it ran exactly once
    text = instruction(runner)
    assert "already ran" in text and "Do NOT run it again" in text
    assert command in text
    assert [f.summary for f in result.findings] == ["SOL_USDT tick failed"]
    [ran] = rec.of(check_guard.RAN)
    assert ran["guard"]["skipped"] is False and ran["guard"]["reason"] == reason


async def test_the_receipt_of_a_guard_that_is_the_action_carries_its_output(tmp_path):
    command, _ = counted(tmp_path, "echo '!!! BTC_USDT tick FAILED (exit=124)'; exit 1")
    check, runner, _ = make(command, {"mode": "skip", "is_action": True})

    await check.run()

    text = instruction(runner)
    assert "!!! BTC_USDT tick FAILED (exit=124)" in text and "exit: 1" in text
    # The check's own instructions come first, the receipt after them.
    assert text.index("Diagnose the gridbot") < text.index("[Run guard receipt")


# --- shadow mode -------------------------------------------------------------------------------------------


async def test_shadow_is_the_default_and_a_quiet_guard_still_runs_the_model(tmp_path):
    command, counter = counted(tmp_path)
    check, runner, rec = make(command, {"is_action": True})

    await check.run()

    runner.run_turn.assert_awaited_once()
    assert runs(counter) == 1
    assert "Do NOT run it again" in instruction(runner)
    [ran] = rec.of(check_guard.RAN)
    assert ran["guard"]["mode"] == "shadow" and ran["guard"]["would_skip"] is True
    assert ran["guard"]["skipped"] is False and ran["guard"]["reason"] is None
    [acted] = rec.of(check_guard.ACTED)
    assert acted["acted"] is False and acted["acted_by"] == []
    assert acted["guard"] == {"name": NAME, "mode": "shadow", "would_skip": True}
    assert acted["run_id"] == ran["run_id"] and acted["run_status"] == "completed"


async def test_a_shadow_run_whose_model_reported_findings_is_recorded_as_acted(tmp_path):
    command, _ = counted(tmp_path)
    check, _, rec = make(command, response=FOUND)

    await check.run()

    [acted] = rec.of(check_guard.ACTED)
    assert acted["acted"] is True and acted["acted_by"] == ["finding"]


async def test_acted_uses_the_runs_ledger_and_ignores_a_bash_run_of_the_guards_own_command(tmp_path):
    command, _ = counted(tmp_path)
    own = schedule_guard.command_digest(command)
    rec = Recorder(ledger=[("bash", "completed", {"command_sha256": own})])
    check, _, _ = make(command, rec=rec)

    await check.run()
    [acted] = rec.of(check_guard.ACTED)
    assert acted["acted"] is False
    assert rec.ledger_sessions and rec.ledger_sessions[0].startswith("dynamic-check-gridbot-paper-tick-")

    rec2 = Recorder(ledger=[("send_email", "completed", {})])
    check2, _, _ = make(command, rec=rec2)
    await check2.run()
    [acted2] = rec2.of(check_guard.ACTED)
    assert acted2["acted"] is True and acted2["acted_by"] == ["ledger"]


async def test_a_failed_shadow_turn_is_recorded_with_its_status(tmp_path):
    command, _ = counted(tmp_path)
    check, runner, rec = make(command)
    runner.run_turn = AsyncMock(side_effect=RuntimeError("API error"))

    with pytest.raises(RuntimeError):
        await check.run()

    [acted] = rec.of(check_guard.ACTED)
    # no answer to read, so "did nothing" could not be seen: unknown, as phase 2 reads an unobserved run
    assert acted["run_status"] == "failed" and acted["acted"] is None and acted["observed"] is False


async def test_a_shadow_turn_whose_answer_does_not_parse_is_unobserved_unless_it_wrote(tmp_path):
    command, _ = counted(tmp_path)
    check, _, rec = make(command, response="I could not decide.")
    await check.run()
    [acted] = rec.of(check_guard.ACTED)
    assert (acted["acted"], acted["observed"], acted["run_status"]) == (None, False, "completed")

    rec2 = Recorder(ledger=[("send_email", "completed", {})])
    check2, _, _ = make(command, response="I could not decide.", rec=rec2)
    await check2.run()
    [acted2] = rec2.of(check_guard.ACTED)
    assert (acted2["acted"], acted2["acted_by"]) == (True, ["ledger"])  # a write is seen either way


async def test_no_code_path_turns_shadow_into_skip(tmp_path):
    command, _ = counted(tmp_path)
    for guard in ({}, {"mode": "shadow"}, {"mode": "SKIP"}, {"mode": "bogus"}, {"skip_when": "always"}):
        check, runner, _ = make(command, guard)
        await check.run()
        runner.run_turn.assert_awaited_once()


# --- timeouts ----------------------------------------------------------------------------------------------


async def test_a_guard_timeout_falls_through_as_timeout_and_kills_the_process_group(tmp_path):
    pidfile = tmp_path / "child.pid"
    command, counter = counted(tmp_path, f"sleep 60 & echo $! > {pidfile}; wait")
    check, runner, rec = make(command, {"mode": "skip", "is_action": True, "timeout_seconds": 1})

    started = asyncio.get_running_loop().time()
    await check.run()
    assert asyncio.get_running_loop().time() - started < 10  # the bounded wait

    runner.run_turn.assert_awaited_once()
    assert runs(counter) == 1
    [ran] = rec.of(check_guard.RAN)
    assert ran["guard"]["reason"] == "timeout" and ran["guard"]["timed_out"] is True
    text = instruction(runner)
    assert "timed out after 1s (killed)" in text and "state is unknown" in text
    child = int(pidfile.read_text())
    await asyncio.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)


def test_the_largest_guard_timeout_leaves_the_turn_its_reserve_inside_the_check_timeout():
    assert check_guard.max_guard_timeout(600) == 600 - check_guard.TURN_RESERVE_SECONDS - check_guard.KILL_BOUND_SECONDS
    assert check_guard.max_guard_timeout(30) < 1


async def test_a_guard_whose_timeout_does_not_fit_the_check_timeout_is_not_run(tmp_path):
    command, counter = counted(tmp_path)
    check, runner, rec = make(command, {"mode": "skip", "is_action": True, "timeout_seconds": 290}, timeout=300)

    await check.run()

    assert runs(counter) == 0  # never started: the check's own timeout could kill it mid-way
    runner.run_turn.assert_awaited_once()
    text = instruction(runner)
    assert "did NOT run" in text and "290" in text
    [unresolved] = rec.of(check_guard.UNRESOLVED)
    assert unresolved["guard"]["reason"] == "invalid" and unresolved["guard"]["name"] == NAME
    assert "290" in unresolved["guard"]["detail"]
    assert rec.of(check_guard.RAN) == []


async def test_a_guard_timeout_above_the_schedule_cap_is_kept_when_it_fits_the_check(tmp_path):
    command, _ = counted(tmp_path)
    check, _, rec = make(command, {"timeout_seconds": 540}, timeout=900)

    await check.run()

    [ran] = rec.of(check_guard.RAN)
    assert ran["guard"]["timeout_seconds"] == 540


# --- unresolved and refused guards -------------------------------------------------------------------------


async def test_a_guard_naming_no_allowlisted_command_is_not_run_and_is_recorded(tmp_path):
    command, counter = counted(tmp_path)
    check, runner, rec = make(command, {"mode": "skip"}, commands={})

    await check.run()

    assert runs(counter) == 0
    runner.run_turn.assert_awaited_once()
    [unresolved] = rec.of(check_guard.UNRESOLVED)
    assert unresolved["guard"] == {"name": NAME, "reason": "not_allowlisted", "detail": None}


async def test_a_command_in_the_row_is_never_run(tmp_path):
    command, counter = counted(tmp_path)
    check, _, rec = make("true", {"command": command})

    await check.run()

    assert runs(counter) == 0
    [ran] = rec.of(check_guard.RAN)
    assert ran["guard"]["command"] == "true"


async def test_guards_off_ignore_the_guard(tmp_path):
    command, counter = counted(tmp_path)
    check, runner, rec = make(command, {"mode": "skip"}, enabled=False)

    await check.run()

    assert runs(counter) == 0 and rec.events == []
    assert "[Run guard" not in instruction(runner)


@pytest.mark.parametrize("kw", [{"name": "dag-abc-node"}, {"intention": {"intention_id": "x"}}])
async def test_a_dag_owned_check_refuses_its_guard(tmp_path, kw):
    """A skip counts as a successful run, which the DAG reads as the node's completion."""
    command, counter = counted(tmp_path)
    check, runner, rec = make(command, {"mode": "skip"}, **kw)

    await check.run()

    assert runs(counter) == 0
    runner.run_turn.assert_awaited_once()
    [unresolved] = rec.of(check_guard.UNRESOLVED)
    assert unresolved["guard"]["reason"] == "invalid" and "DAG" in unresolved["guard"]["detail"]


async def test_a_check_without_a_guard_is_unchanged(tmp_path):
    runner = MagicMock()
    runner.run_turn = AsyncMock(return_value=(CLEAN, None, {}))
    runner.end_conversation = AsyncMock()
    rec = Recorder()
    ctx = check_guard.CheckGuardContext(enabled=True, commands={NAME: "true"}, emit=rec.emit, ledger=rec.ledger)
    check = DynamicCheck(check_id="c", name="plain", prompt="p", tools=[], runner=runner, guards=ctx)

    await check.run()

    assert rec.events == [] and "[Run guard" not in instruction(runner)


async def test_a_failing_emit_never_blocks_the_run(tmp_path):
    command, _ = counted(tmp_path)
    check, runner, _ = make(command)
    check._guards = check_guard.CheckGuardContext(
        enabled=True, commands={NAME: command}, emit=AsyncMock(side_effect=RuntimeError("bus")), ledger=AsyncMock()
    )

    await check.run()

    runner.run_turn.assert_awaited_once()


# --- concurrency and disable -------------------------------------------------------------------------------


async def test_a_second_run_while_the_guard_runs_is_skipped_and_the_guard_runs_once(tmp_path):
    command, counter = counted(tmp_path, "sleep 1")
    check, runner, _ = make(command, {"mode": "skip"})

    first = asyncio.create_task(check.run())
    await asyncio.sleep(0.3)
    second = await check.run()
    await first

    assert second.skipped is True
    assert runs(counter) == 1


async def test_a_disable_during_the_guard_lets_the_guard_finish_and_runs_no_turn(tmp_path):
    command, counter = counted(tmp_path, "sleep 1; echo done-tick")
    check, runner, rec = make(command, {"is_action": True})
    active_runs: dict = {}
    check._active_runs = active_runs

    task = asyncio.create_task(check.run())
    await asyncio.sleep(0.3)
    assert check in active_runs.get(check.name, set())
    check.cancel_run()
    result = await task

    assert result.skipped is True
    runner.run_turn.assert_not_awaited()
    assert runs(counter) == 1
    [ran] = rec.of(check_guard.RAN)
    assert ran["guard"]["reason"] == "output"  # the tick's record is kept
    assert active_runs == {}


def test_the_guard_is_part_of_the_signature():
    a = DynamicCheck(check_id="c", name="n", prompt="p", tools=[], guard={"name": NAME})
    b = DynamicCheck(check_id="c", name="n", prompt="p", tools=[], guard={"name": NAME, "mode": "skip"})
    assert a.signature() != b.signature()


async def test_the_loader_hands_each_check_its_rows_guard_and_the_shared_context(tmp_path):
    from nous.heartbeat.dynamic import DynamicCheckLoader
    from nous.heartbeat.registry import CheckRegistry

    command, counter = counted(tmp_path)
    rec = Recorder()
    ctx = check_guard.CheckGuardContext(enabled=True, commands={NAME: command}, emit=rec.emit, ledger=rec.ledger)
    runner = MagicMock()
    runner.run_turn = AsyncMock(return_value=(CLEAN, None, {}))
    runner.end_conversation = AsyncMock()
    registry = CheckRegistry()
    loader = DynamicCheckLoader(db=MagicMock(), registry=registry, runner=runner, agent_id="a", guards=ctx)
    row = MagicMock(
        id="row-1", prompt="p", tools=["bash"], interval_seconds=300, timeout_seconds=300, urgent=False,
        on_complete_prompt=None, on_complete_tools=[], cron_expr=None,
        metadata_={"guard": {"name": NAME, "mode": "skip", "timeout_seconds": 30}},
    )  # fmt: skip
    row.name = "gridbot-paper-tick"
    loader._fetch_enabled = AsyncMock(return_value=[row])

    await loader.sync()
    await registry.get_check("gridbot-paper-tick").run()

    assert runs(counter) == 1
    runner.run_turn.assert_not_awaited()  # skip mode, quiet guard
    assert [t for t, _ in rec.events] == [check_guard.RAN]


@pytest.mark.parametrize(
    ("guard", "detail"),
    [
        ("gridbot_tick", "not a JSON object"),
        ({"name": NAME, "is_action": True}, "integer timeout_seconds"),
        ({"name": NAME, "is_action": True, "timeout_seconds": "600"}, "integer timeout_seconds"),
        ({"name": NAME, "is_action": True, "timeout_seconds": 0}, "at least 1"),
        ({"name": NAME, "is_action": True, "timeout_seconds": -5}, "at least 1"),
        ({"name": NAME, "timeout_seconds": 0}, "at least 1"),
        ({"name": NAME, "is_action": "true", "timeout_seconds": 240}, "is_action must be true or false"),
    ],
)
async def test_a_guard_that_cannot_be_read_safely_is_refused_not_ignored(tmp_path, guard, detail):
    command, counter = counted(tmp_path)
    check, runner, rec = make(command)
    check._guard = guard  # as the row holds it

    await check.run()

    assert runs(counter) == 0
    runner.run_turn.assert_awaited_once()
    assert "did NOT run" in instruction(runner)
    [unresolved] = rec.of(check_guard.UNRESOLVED)
    assert unresolved["guard"]["reason"] == "invalid" and detail in unresolved["guard"]["detail"]


async def test_the_loaders_context_persists_records_through_the_bus_with_a_clipped_session_id():
    from nous.config import Settings

    bus = MagicMock()
    bus.emit = AsyncMock()
    settings = Settings(_env_file=None, agent_id="a1", schedule_guard_commands={NAME: "true"})
    ctx = check_guard.context_for(settings, bus, MagicMock())
    long_run = "dynamic-check-" + "x" * 120 + "-abcd1234"

    await ctx.emit(check_guard.RAN, {"run_id": long_run}, long_run)

    [event] = [c.args[0] for c in bus.emit.await_args_list]
    assert (event.type, event.agent_id, event.session_id) == (check_guard.RAN, "a1", long_run[:100])
    assert event.data == {"run_id": long_run}  # the whole run id stays in the data
    assert ctx.commands == {NAME: "true"} and ctx.enabled is True
    assert check_guard.context_for(settings, None, MagicMock()).emit is not None  # no bus: nothing to persist


async def test_a_check_disabled_while_its_refused_guard_is_recorded_runs_no_turn(tmp_path):
    command, _ = counted(tmp_path)
    check, runner, _ = make(command, commands={})  # not allowlisted: refused, recorded, then the turn

    async def emit(event_type: str, data: dict, session_id: str) -> None:
        await asyncio.sleep(0)
        check._self_disabled = True  # a disable's gate landing during the record's await

    check._guards.emit = emit

    result = await check.run()

    assert result.skipped is True
    runner.run_turn.assert_not_awaited()


async def test_a_run_whose_guard_is_running_counts_as_a_run_a_disable_would_cancel(tmp_path):
    """So a disable that must not cancel a run (refuse_if_running) is refused, and one that does is not undoable."""
    from nous.heartbeat.dynamic import DynamicCheckLoader
    from nous.heartbeat.registry import CheckRegistry

    command, _ = counted(tmp_path, "sleep 1")
    check, _, _ = make(command)
    loader = DynamicCheckLoader(db=MagicMock(), registry=CheckRegistry(), runner=MagicMock(), agent_id="a")
    check._active_runs = loader._active_runs

    task = asyncio.create_task(check.run())
    await asyncio.sleep(0.3)
    assert loader._has_cancellable_run(check.name) is True
    await task
    assert loader._has_cancellable_run(check.name) is False
