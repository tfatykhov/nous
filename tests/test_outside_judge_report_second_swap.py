"""F118 task reports beside the second judge's dashboard settings: a second judge the settings manager swaps in gets no
report and no swept subject, and the first judge's sweep runs on across the swap (real Postgres, stub first judges, the
real second judge with a transport that fails the test if a request is made).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from test_judge_second_manager import BOX, KEY, FixedKeyFile, _sealed

from nous.config import Settings
from nous.judge import second_settings
from nous.judge.second_settings import SecondJudgeManager
from nous.judge.service import JudgeSet, OutsideJudge
from nous.judge.slot import JudgeInput, JudgeOutput

pytestmark = pytest.mark.postgres_only

FAST = {"settle_seconds": 0.0, "timeout_seconds": 2.0}


@pytest.fixture(autouse=True)
def _no_request_leaves_the_process(monkeypatch):
    async def refuse(self: Any, request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"a real request was attempted to {request.url.host}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)


def _no_request(request: httpx.Request) -> httpx.Response:
    raise AssertionError("the second judge was handed a subject")


class StubJudge:
    def __init__(self, version: str, verdict: str) -> None:
        self.name, self.version, self.model = "background_model", version, "claude-test"
        self.verdict = verdict
        self.records: list[JudgeInput] = []

    async def grade(self, record: JudgeInput, *, timeout: float, max_calls: int | None = None) -> JudgeOutput:
        self.records.append(record)
        return JudgeOutput(status="ok", verdict=self.verdict, confidence=0.6, calls=1)


@pytest_asyncio.fixture
async def agent(db):
    name = f"test-f118-rswap-{uuid.uuid4().hex[:8]}"
    yield name
    async with db.session() as session:
        for table in (
            "nous_system.judge_second_settings",
            "nous_system.judge_verdicts",
            "brain.decisions",
            "heart.subtasks",
            "nous_system.tool_call_log",
        ):
            await session.execute(text(f"DELETE FROM {table} WHERE agent_id = :a"), {"a": name})
        await session.commit()


async def _task(db, agent: str) -> uuid.UUID:
    task_id = uuid.uuid4()
    done = datetime.now(UTC) - timedelta(hours=1)
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO heart.subtasks (id, agent_id, task, status, final_outcome, report_jsonb, created_at, "
                "completed_at) VALUES (:id, :a, 'Fix the link', 'completed', 'completed', CAST(:r AS jsonb), :c, :d)"
            ),
            {
                "id": task_id,
                "a": agent,
                "r": json.dumps({"summary": "Fixed it."}),
                "c": done - timedelta(minutes=5),
                "d": done,
            },
        )
        await session.execute(
            text(
                "INSERT INTO nous_system.tool_call_log (agent_id, session_id, subtask_id, call_index, work_kind, "
                "tool_name, tool_name_called, outcome, input_text, result_preview, started_at) VALUES (:a, :s, :id, "
                "0, 'subtask', 'write_file', 'write_file', 'ok', '{}', 'written', :t)"
            ),
            {"a": agent, "s": f"subtask-{task_id.hex[:8]}", "id": task_id, "t": done - timedelta(minutes=1)},
        )
        await session.commit()
    return task_id


async def _owner_graded_decision(db, agent: str) -> uuid.UUID:
    decision_id = uuid.uuid4()
    session_id = f"sess-{decision_id.hex[:8]}"
    reviewed = datetime.now(UTC) - timedelta(hours=1)
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO brain.decisions (id, agent_id, description, confidence, category, stakes, session_id, "
                "created_at, outcome, outcome_result, reviewed_at, reviewer) VALUES (:id, :a, 'Move the backup', 0.7, "
                "'tooling', 'low', :s, :t, 'success', 'done', :r, 'a2ui')"
            ),
            {"id": decision_id, "a": agent, "s": session_id, "t": reviewed - timedelta(minutes=10), "r": reviewed},
        )
        await session.execute(
            text(
                "INSERT INTO nous_system.tool_call_log (agent_id, session_id, call_index, work_kind, tool_name, "
                "tool_name_called, outcome, input_text, result_preview, started_at) VALUES (:a, :s, 0, "
                "'conversation', 'bash', 'bash', 'ok', '{}', 'moved', :t)"
            ),
            {"a": agent, "s": session_id, "t": reviewed - timedelta(minutes=5)},
        )
        await session.commit()
    return decision_id


async def _until(predicate, seconds: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out waiting")
        await asyncio.sleep(0.02)


async def test_a_second_judge_swapped_in_by_the_settings_gets_no_report_and_no_swept_subject(db, agent):
    settings = Settings(
        _env_file=None,
        agent_id=agent,
        outside_judge_mode="shadow",
        outside_judge_reports_enabled=True,
        outside_judge_settings_enabled=True,
    )
    decisions, reports = StubJudge("dj-2", "success"), StubJudge("rj-1", "supported")
    primary = OutsideJudge(db, settings, decisions, report_judge=reports, **FAST)
    primary.start()
    judges = JudgeSet(primary, None)  # as main.py installs it with the settings on
    async with db.session() as session:
        await second_settings.upsert_row(session, agent, {"enabled": True, **_sealed(agent, KEY)}, "owner")
        await session.commit()
    manager = SecondJudgeManager(
        db, settings, judges, transport=httpx.MockTransport(_no_request), key_file=FixedKeyFile(BOX)
    )
    try:
        await manager.load()
        first_second = judges.second
        assert isinstance(first_second, OutsideJudge)
        sweeper = primary._sweeper

        swapped = await manager.patch({"model": "jev-1.15.0"}, confirmed=None, actor="owner")
        assert swapped.status == 200
        second = judges.second
        assert isinstance(second, OutsideJudge) and second is not first_second
        for service in (first_second, second):  # the second judge's service judges no report and does not sweep
            assert service.report_judge is None and service.sweep_numbers() is None and service._sweeper is None
        assert primary._sweeper is sweeper and not sweeper.done()  # the first judge's sweep runs on across the swap

        task_id = await _task(db, agent)
        assert judges.submit_report(task_id, claimed_at=datetime.now(UTC)) is True
        assert second.idle and second.runtime()["queued"] == 0
        await _until(lambda: primary.idle)
        decision_id = await _owner_graded_decision(db, agent)
        assert await primary.sweep_once() == 1
        await _until(lambda: primary.idle)
        assert second.idle and second.runtime() == {"queued": 0, "dropped": 0, "over_cap": 0, "worker_alive": True}
        assert [record.subject_id for record in reports.records] == [task_id]
        assert [record.subject_id for record in decisions.records] == [decision_id]
        assert judges.sweep_numbers() == primary.sweep_numbers()
    finally:
        await manager.close()
        old = judges.replace_second(None)
        if old is not None:
            await old.close()
        await primary.close()
    async with db.session() as session:
        rows = (
            await session.execute(
                text("SELECT judge_name, subject_kind FROM nous_system.judge_verdicts WHERE agent_id = :a"),
                {"a": agent},
            )
        ).all()
    assert sorted(tuple(row) for row in rows) == [("background_model", "decision"), ("background_model", "task_report")]
