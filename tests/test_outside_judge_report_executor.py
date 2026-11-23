"""F118: a finished task's report is handed to the outside judge from ``execute_hardened``, and that changes nothing.

The hand-off is one synchronous call after the result is persisted. It adds no await to the ``finally`` block, and the
stored row, the outcome event, the notification and the returned value are the same whether the judge is off, the
setting is off, or the judge answers, raises or hangs. A run is recorded whole with ``record`` of
``test_finish_checks_executor`` and compared with the golden captured before any of this existed. Only a validated,
completed report is handed in: a failed, blocked or cancelled run hands nothing.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import textwrap
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import text
from test_finish_checks_executor import (
    GOLDEN,
    OWN_SESSION,
    PLACEHOLDER,
    SCENARIOS,
    SUBTASK_ID,
    VALID,
    _runner,
    _settings,
    _subtask,
    golden_view,
    record,
)

from nous import judge as judge_slot
from nous.config import Settings
from nous.handlers import subtask_executor
from nous.handlers.subtask_executor import execute_hardened
from nous.judge.service import OutsideJudge
from nous.judge.slot import JudgeInput, JudgeOutput

SUBMITTED = ("valid_first", "empty_then_valid", "placeholder_then_valid")


@pytest.fixture(autouse=True)
def _no_judge_installed():
    judge_slot.set_current(None)
    yield
    judge_slot.set_current(None)


class SpyService:
    """Records what it is handed; ``raise`` makes its submit raise."""

    def __init__(self, behaviour: str = "ok", heart=None) -> None:
        self.behaviour = behaviour
        self.heart = heart
        self.reports: list[tuple] = []
        self.persisted_before: list[int] = []

    def submit_report(self, subtask_id, *, claimed_at):
        if self.heart is not None:
            self.persisted_before.append(self.heart.subtasks.complete.await_count)
        if self.behaviour == "raise":
            raise RuntimeError("the judge's queue broke")
        self.reports.append((subtask_id, claimed_at))
        return True

    def submit_decision(self, *args, **kwargs):  # pragma: no cover - a task hands no decision
        raise AssertionError("a finished task hands no decision")


def _golden(name: str) -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))["scenarios"][name]


# ---- off ------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", list(SCENARIOS))
@pytest.mark.parametrize("setting", [None, False])
async def test_with_reports_off_nothing_is_handed_in_and_the_run_is_the_base_commits(name, setting):
    spy = SpyService()
    judge_slot.set_current(spy)
    settings = _settings() if setting is None else _settings(outside_judge_reports_enabled=setting)
    run = await record(SCENARIOS[name], settings)
    assert spy.reports == []
    assert golden_view(run) == _golden(name)


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_with_reports_on_but_no_judge_running_the_run_is_the_base_commits(name):
    run = await record(SCENARIOS[name], _settings(outside_judge_reports_enabled=True))
    assert golden_view(run) == _golden(name)


# ---- on: what is handed in, and that the run does not change --------------------------------------------------------


@pytest.mark.parametrize("name", list(SCENARIOS))
@pytest.mark.parametrize("behaviour", ["ok", "raise"])
async def test_with_reports_on_the_run_is_the_same_as_with_them_off(name, behaviour):
    off = await record(SCENARIOS[name], _settings())
    spy = SpyService(behaviour)
    judge_slot.set_current(spy)
    on = await record(SCENARIOS[name], _settings(outside_judge_reports_enabled=True))
    assert on == off
    assert golden_view(on) == _golden(name)


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_only_a_completed_report_is_handed_in_with_the_tasks_id(name):
    spy = SpyService()
    judge_slot.set_current(spy)
    before = datetime.now(UTC)
    await record(SCENARIOS[name], _settings(outside_judge_reports_enabled=True))
    if name in SUBMITTED:
        [(subtask_id, claimed_at)] = spy.reports
        assert subtask_id == SUBTASK_ID and before <= claimed_at <= datetime.now(UTC)
    else:  # blocked, errored, out of attempts: not handed in
        assert spy.reports == []


async def test_a_placeholder_only_run_hands_nothing_in():
    spy = SpyService()
    judge_slot.set_current(spy)
    await record([PLACEHOLDER, PLACEHOLDER], _settings(outside_judge_reports_enabled=True))
    assert spy.reports == []


@pytest.mark.parametrize(
    "metadata",
    [{"session_id": "schedule-abc"}, {"goal_id": str(uuid.uuid4()), "goal_session": 2}, {}],
)
async def test_a_task_in_a_shared_session_or_a_goal_session_is_handed_in_like_any_task(metadata):
    spy = SpyService()
    judge_slot.set_current(spy)
    await record([VALID], _settings(outside_judge_reports_enabled=True), subtask=_subtask(metadata_=metadata))
    assert [report[0] for report in spy.reports] == [SUBTASK_ID]


def _heart(complete_raises: bool = False):
    heart = MagicMock()
    heart.subtasks.complete = AsyncMock(side_effect=RuntimeError("the database is gone") if complete_raises else None)
    heart.subtasks.fail = AsyncMock()
    return heart


async def test_the_report_is_handed_in_after_the_row_is_persisted():
    heart = _heart()
    spy = SpyService(heart=heart)
    judge_slot.set_current(spy)
    await execute_hardened(
        _subtask(),
        OWN_SESSION,
        runner=_runner([VALID]),
        heart=heart,
        settings=_settings(outside_judge_reports_enabled=True),
    )
    assert spy.persisted_before == [1]


async def test_a_persist_that_fails_hands_nothing_in():
    heart = _heart(complete_raises=True)
    spy = SpyService(heart=heart)
    judge_slot.set_current(spy)
    await execute_hardened(
        _subtask(),
        OWN_SESSION,
        runner=_runner([VALID]),
        heart=heart,
        settings=_settings(outside_judge_reports_enabled=True),
    )
    assert spy.reports == [] and spy.persisted_before == []


async def test_a_cancelled_run_hands_nothing_in():
    spy = SpyService()
    judge_slot.set_current(spy)
    runner = MagicMock()
    runner.run_turn = AsyncMock(side_effect=asyncio.CancelledError())
    heart = _heart()
    with pytest.raises(asyncio.CancelledError):
        await execute_hardened(
            _subtask(), OWN_SESSION, runner=runner, heart=heart, settings=_settings(outside_judge_reports_enabled=True)
        )
    assert spy.reports == []


async def test_handing_in_adds_no_thread_and_no_sleep(monkeypatch):
    async def no_thread(*_args, **_kwargs):
        raise AssertionError("the finish path must not use a thread")

    async def no_sleep(*_args, **_kwargs):
        raise AssertionError("the finish path must not sleep")

    spy = SpyService()
    judge_slot.set_current(spy)
    monkeypatch.setattr(asyncio, "to_thread", no_thread)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    run = await record([VALID], _settings(outside_judge_reports_enabled=True))
    assert golden_view(run) == _golden("valid_first") and len(spy.reports) == 1


# ---- the shape of the hook ----------------------------------------------------------------------------------------


def _finally_awaits() -> int:
    tree = ast.parse(textwrap.dedent(inspect.getsource(execute_hardened)))
    function = tree.body[0]
    [outer] = [node for node in function.body if isinstance(node, ast.Try) and node.finalbody]
    return sum(isinstance(node, ast.Await) for stmt in outer.finalbody for node in ast.walk(stmt))


def test_the_finally_block_gains_no_await():  # PIN: the persist and the outcome event, as before
    assert _finally_awaits() == 2


def test_the_hook_is_synchronous_and_called_once_after_the_persist():
    assert not inspect.iscoroutinefunction(subtask_executor._submit_report_to_outside_judge)
    source = inspect.getsource(execute_hardened)
    assert source.count("_submit_report_to_outside_judge(") == 1
    assert source.index("_persist_outcome(") < source.index("_submit_report_to_outside_judge(")
    assert source.index("_submit_report_to_outside_judge(") < source.index("emit_event(\n")


def test_the_executor_loads_only_the_judges_accessor():
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    code = (
        "import sys, nous.handlers.subtask_executor\n"
        "print(' '.join(m for m in sys.modules if m.startswith('nous.judge')))"
    )
    loaded = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=root, timeout=120, check=True
    ).stdout.split()
    assert loaded == ["nous.judge"]


# ---- a live judge (real Postgres): it answers, raises or hangs, and the run is the same ----------------------------


class StubReportJudge:
    name, version, model = "stub", "stub-r1", "stub-model"

    def __init__(self, behaviour: str) -> None:
        self.behaviour = behaviour
        self.records: list[JudgeInput] = []

    async def grade(self, record: JudgeInput, *, timeout: float, max_calls: int | None = None) -> JudgeOutput:
        self.records.append(record)
        if self.behaviour == "raise":
            raise RuntimeError("the stub judge broke")
        if self.behaviour == "hang":
            await asyncio.sleep(3600)
        return JudgeOutput(status="ok", verdict="supported", confidence=0.7, calls=1)


class StubDecisionJudge:
    name, version = "stub", "stub-d1"

    async def grade(self, record, *, timeout, max_calls=None):  # pragma: no cover - no decision here
        raise AssertionError("no decision is judged here")


@pytest_asyncio.fixture
async def finished_task(db):
    """A completed task row with a report and one call of its own, as the persist and the recorder leave them."""
    agent = f"test-f118-rex-{uuid.uuid4().hex[:8]}"
    task_id = uuid.uuid4()
    done = datetime.now(UTC) - timedelta(seconds=5)
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO heart.subtasks (id, agent_id, task, status, final_outcome, report_jsonb, created_at, "
                "completed_at) VALUES (:id, :a, 'research X', 'completed', 'completed', CAST(:r AS jsonb), :c, :d)"
            ),
            {"id": task_id, "a": agent, "r": json.dumps(VALID), "c": done - timedelta(minutes=1), "d": done},
        )
        await session.execute(
            text(
                "INSERT INTO nous_system.tool_call_log (agent_id, session_id, subtask_id, call_index, work_kind, "
                "tool_name, tool_name_called, outcome, input_text, result_preview, started_at) VALUES (:a, :s, :id, 0, "
                "'subtask', 'web_search', 'web_search', 'ok', '{}', 'three candidates', :t)"
            ),
            {"a": agent, "s": f"subtask-{task_id.hex[:8]}", "id": task_id, "t": done - timedelta(seconds=30)},
        )
        await session.commit()
    yield SimpleNamespace(agent=agent, id=task_id)
    async with db.session() as session:
        for table in ("heart.subtasks", "nous_system.tool_call_log", "nous_system.judge_verdicts"):
            await session.execute(text(f"DELETE FROM {table} WHERE agent_id = :a"), {"a": agent})
        await session.commit()


async def _until(predicate, seconds: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out waiting")
        await asyncio.sleep(0.02)


@pytest.mark.postgres_only
@pytest.mark.parametrize("behaviour", ["ok", "raise", "hang"])
async def test_with_a_live_judge_that_answers_raises_or_hangs_the_run_is_the_same(db, finished_task, behaviour):
    subtask = _subtask(id=finished_task.id)
    session_id = f"subtask-{finished_task.id.hex[:8]}"
    off = await record([VALID], _settings(agent_id=finished_task.agent), subtask=subtask, session_id=session_id)

    stub = StubReportJudge(behaviour)
    settings = Settings(
        _env_file=None, agent_id=finished_task.agent, outside_judge_mode="shadow", outside_judge_reports_enabled=True
    )
    service = OutsideJudge(
        db, settings, StubDecisionJudge(), report_judge=stub, settle_seconds=0.0, timeout_seconds=0.5
    )
    service.start()
    judge_slot.set_current(service)
    try:
        on = await record(
            [VALID],
            _settings(agent_id=finished_task.agent, outside_judge_reports_enabled=True),
            subtask=subtask,
            session_id=session_id,
        )
        assert on == off
        await _until(lambda: len(stub.records) == 1)  # the judge was reached, after the run had returned
        await _until(lambda: service.idle)
    finally:
        await service.close()
    async with db.session() as session:
        status = await session.scalar(
            text("SELECT status FROM nous_system.judge_verdicts WHERE agent_id = :a AND subject_id = :s"),
            {"a": finished_task.agent, "s": finished_task.id},
        )
    assert status == {"ok": "ok", "raise": "error", "hang": "timeout"}[behaviour]
