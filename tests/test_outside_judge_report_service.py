"""F118: finished task reports in the outside judge's service (real Postgres, stub judges, no model).

``submit_report`` is synchronous, never raises and never waits; a service without a report judge takes no report. The
worker judges a report with the report judge only, as it stood at the task's completion (the claim's time is the
completion, whenever it was handed in), and writes one ``task_report`` row under the report judge's name and version.
A task that did not finish as completed with a report is never judged. The daily limit is per judge name, decisions
and reports together. ``JudgeSet`` hands a report to the first judge only. No task row is ever changed.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text

from nous.config import Settings
from nous.judge import service as service_module
from nous.judge.service import JudgeSet, OutsideJudge
from nous.judge.slot import JudgeInput, JudgeOutput

pytestmark = pytest.mark.postgres_only

FAST = {"settle_seconds": 0.0, "timeout_seconds": 2.0}
REPORT = {"summary": "Fixed the link and deployed the site.", "findings": [], "confidence": 0.9}


class StubJudge:
    """A judge that answers, raises or hangs, and records what it was given."""

    def __init__(
        self,
        behaviour: str = "ok",
        *,
        name: str = "stub",
        version: str = "stub-r1",
        verdict: str = "not_supported",
    ) -> None:
        self.name = name
        self.version = version
        self.model = "stub-model"
        self.behaviour = behaviour
        self.verdict = verdict
        self.records: list[JudgeInput] = []

    async def grade(self, record: JudgeInput, *, timeout: float, max_calls: int | None = None) -> JudgeOutput:
        self.records.append(record)
        if self.behaviour == "raise":
            raise RuntimeError("the stub judge broke")
        if self.behaviour == "hang":
            await asyncio.sleep(3600)
        if self.behaviour == "error":
            return JudgeOutput(status="error", verdict="cannot_tell", calls=1)
        return JudgeOutput(
            status="ok",
            verdict=self.verdict,
            confidence=0.7,
            labels=("error_in_evidence",),
            reason="The deploy command failed.",
            model="stub-model",
            tokens_in=900,
            tokens_out=30,
            calls=1,
        )


@pytest_asyncio.fixture
async def agent(db):
    name = f"test-f118-rsv-{uuid.uuid4().hex[:8]}"
    yield name
    async with db.session() as session:
        for table in (
            "heart.subtasks",
            "nous_system.tool_call_log",
            "nous_system.judge_verdicts",
            "brain.decisions",
        ):
            await session.execute(text(f"DELETE FROM {table} WHERE agent_id = :a"), {"a": name})
        await session.commit()


def _settings(agent: str, **over) -> Settings:
    return Settings(_env_file=None, agent_id=agent, outside_judge_mode="shadow", **over)


def _service(db, agent: str, report_judge=None, judge=None, **over) -> OutsideJudge:
    return OutsideJudge(
        db,
        _settings(agent),
        judge or StubJudge(name="stub", version="stub-d1", verdict="success"),
        report_judge=report_judge,
        **{**FAST, **over},
    )


async def _task(
    db,
    agent: str,
    *,
    status: str = "completed",
    final_outcome: str | None = "completed",
    with_call: bool = True,
    completed_at: datetime | None = None,
) -> tuple[uuid.UUID, datetime]:
    task_id = uuid.uuid4()
    done = completed_at or datetime.now(UTC) - timedelta(minutes=1)
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO heart.subtasks (id, agent_id, task, status, final_outcome, report_jsonb, metadata, "
                "created_at, completed_at) VALUES (:id, :a, 'Fix the link and deploy', :st, :fo, CAST(:r AS jsonb), "
                "CAST(:m AS jsonb), :c, :d)"
            ),
            {
                "id": task_id,
                "a": agent,
                "st": status,
                "fo": final_outcome,
                "r": json.dumps(REPORT),
                "m": json.dumps({"finish_check": {"verdict": "clean", "failed": []}}),
                "c": done - timedelta(minutes=5),
                "d": done,
            },
        )
        if with_call:
            await session.execute(
                text(
                    "INSERT INTO nous_system.tool_call_log (agent_id, session_id, subtask_id, call_index, work_kind, "
                    "tool_name, tool_name_called, outcome, is_error, input_text, result_preview, error_text, "
                    "started_at) VALUES (:a, :s, :id, 0, 'subtask', 'bash', 'bash', 'error', true, "
                    "'{\"command\": \"./deploy.sh\"}', '', 'login failed', :t)"
                ),
                {"a": agent, "s": f"subtask-{task_id.hex[:8]}", "id": task_id, "t": done - timedelta(minutes=2)},
            )
        await session.commit()
    return task_id, done


async def _rows(db, agent: str) -> list[dict]:
    async with db.session() as session:
        result = await session.execute(
            text(
                "SELECT subject_kind, subject_id, judge_name, judge_version, status, verdict, labels, against_grader, "
                "against_grade, claim_at, evidence, calls, attempts FROM nous_system.judge_verdicts "
                "WHERE agent_id = :a ORDER BY judged_at"
            ),
            {"a": agent},
        )
        return [dict(row._mapping) for row in result]


async def _task_row(db, task_id: uuid.UUID) -> str:
    async with db.session() as session:
        value = await session.scalar(text("SELECT to_jsonb(s)::text FROM heart.subtasks s WHERE id = :id"), {"id": task_id})
    return value


async def _until(predicate, seconds: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out waiting")
        await asyncio.sleep(0.02)


async def _drain(service, seconds: float = 10.0) -> None:
    await _until(lambda: service.idle, seconds)


# ---- submit ---------------------------------------------------------------------------------------------------------


def test_submit_report_is_synchronous():
    assert not inspect.iscoroutinefunction(OutsideJudge.submit_report)
    assert not inspect.iscoroutinefunction(JudgeSet.submit_report)


async def test_a_service_without_a_report_judge_takes_no_report_and_counts_nothing(db, agent):
    service = _service(db, agent)
    assert service.report_judge is None
    assert service.submit_report(uuid.uuid4(), claimed_at=datetime.now(UTC)) is False
    assert service.runtime() == {"queued": 0, "dropped": 0, "over_cap": 0, "worker_alive": False}


async def test_submit_report_drops_when_closed_full_or_already_queued_and_never_raises(db, agent):
    service = _service(db, agent, report_judge=StubJudge(), queue_max=2)
    task_id = uuid.uuid4()
    now = datetime.now(UTC)
    assert service.submit_report(task_id, claimed_at=now) is True
    assert service.submit_report(task_id, claimed_at=now) is False  # already queued
    assert service.submit_report(uuid.uuid4(), claimed_at=now) is True
    assert service.submit_report(uuid.uuid4(), claimed_at=now) is False  # the queue is full
    assert service.submit_report("not an id", claimed_at=now) is False
    assert service.submit_report(uuid.uuid4(), claimed_at="not a time") is False
    await service.close()
    assert service.submit_report(uuid.uuid4(), claimed_at=now) is False  # closed
    assert service.runtime()["dropped"] == 5


# ---- the worker -----------------------------------------------------------------------------------------------------


async def test_a_report_is_judged_by_the_report_judge_as_it_stood_at_the_completion(db, agent):
    decisions, reports = StubJudge(name="stub", version="stub-d1"), StubJudge(name="stub", version="stub-r1")
    service = _service(db, agent, report_judge=reports, judge=decisions)
    task_id, done = await _task(db, agent)
    before = await _task_row(db, task_id)
    service.start()
    later = datetime.now(UTC) + timedelta(minutes=30)  # handed in later: the claim's time is still the completion
    assert service.submit_report(task_id, claimed_at=later) is True
    await _drain(service)
    await service.close()

    assert decisions.records == []
    [record] = reports.records
    assert record.kind == "task_report" and record.subject_id == task_id
    assert (record.claim.grader, record.claim.grade, record.claim.note) == ("agent", "completed", REPORT["summary"])
    [row] = await _rows(db, agent)
    assert {k: row[k] for k in row if k not in ("evidence", "claim_at")} == {
        "subject_kind": "task_report",
        "subject_id": task_id,
        "judge_name": "stub",
        "judge_version": "stub-r1",
        "status": "ok",
        "verdict": "not_supported",
        "labels": ["error_in_evidence"],
        "against_grader": "agent",
        "against_grade": "completed",
        "calls": 1,
        "attempts": 1,
    }
    assert row["claim_at"] == done
    assert row["evidence"]["finish_check"] == "clean" and row["evidence"]["tool_calls"] == 1
    assert await _task_row(db, task_id) == before


@pytest.mark.parametrize(
    ("status", "final_outcome"),
    [("failed", "errored"), ("cancelled", "cancelled"), ("completed", "incomplete_blocked")],
)
async def test_a_task_that_did_not_finish_as_completed_is_never_judged(db, agent, status, final_outcome):
    reports = StubJudge()
    service = _service(db, agent, report_judge=reports)
    task_id, _done = await _task(db, agent, status=status, final_outcome=final_outcome)
    service.start()
    service.submit_report(task_id, claimed_at=datetime.now(UTC))
    await _drain(service)
    await service.close()
    assert reports.records == [] and await _rows(db, agent) == []


async def test_a_report_without_evidence_is_skipped_without_a_call(db, agent):
    reports = StubJudge()
    service = _service(db, agent, report_judge=reports)
    task_id, _done = await _task(db, agent, with_call=False)
    service.start()
    service.submit_report(task_id, claimed_at=datetime.now(UTC))
    await _drain(service)
    await service.close()
    [row] = await _rows(db, agent)
    assert reports.records == []
    assert (row["status"], row["verdict"], row["labels"], row["calls"]) == (
        "skipped",
        "cannot_tell",
        ["evidence_missing"],
        0,
    )


@pytest.mark.parametrize("verdict", ["success", "failure", "partial", "made_up"])
async def test_a_verdict_the_report_judge_may_not_give_is_stored_as_an_error(db, agent, verdict):
    service = _service(db, agent, report_judge=StubJudge(verdict=verdict))
    task_id, _done = await _task(db, agent)
    service.start()
    service.submit_report(task_id, claimed_at=datetime.now(UTC))
    await _drain(service)
    await service.close()
    [row] = await _rows(db, agent)
    assert (row["status"], row["verdict"], row["labels"]) == ("error", "cannot_tell", ["judge_error"])


async def test_a_report_verdict_given_for_a_decision_is_stored_as_an_error(db, agent):
    assert service_module._checked(JudgeOutput(status="ok", verdict="supported"), "decision").status == "error"
    assert service_module._checked(JudgeOutput(status="ok", verdict="success"), "task_report").status == "error"
    assert service_module._checked(JudgeOutput(status="ok", verdict="supported"), "task_report").status == "ok"


@pytest.mark.parametrize("behaviour", ["raise", "hang", "error"])
async def test_a_failed_report_judgement_is_tried_again_at_most_three_times_in_all(db, agent, behaviour):
    reports = StubJudge(behaviour)
    service = _service(db, agent, report_judge=reports, timeout_seconds=0.2)
    task_id, _done = await _task(db, agent)
    service.start()
    for _ in range(4):
        service.submit_report(task_id, claimed_at=datetime.now(UTC))
        await _drain(service)
    await service.close()
    [row] = await _rows(db, agent)
    assert row["status"] in ("error", "timeout") and row["attempts"] == 3
    assert len(reports.records) == 3


async def test_the_daily_limit_counts_decisions_and_reports_of_one_judge_name_together(db, agent):
    reports = StubJudge(name="shared", version="r")
    service = OutsideJudge(
        db,
        _settings(agent, outside_judge_daily_calls=2),
        StubJudge(name="shared", version="d", verdict="success"),
        report_judge=reports,
        **FAST,
    )
    async with db.session() as session:
        for _ in range(2):  # two decision calls of this judge name today
            await session.execute(
                text(
                    "INSERT INTO nous_system.judge_verdicts (agent_id, subject_kind, subject_id, judge_name, "
                    "judge_version, status, verdict, claim_at, calls) VALUES (:a, 'decision', :s, 'shared', 'd', "
                    "'ok', 'success', now(), 1)"
                ),
                {"a": agent, "s": uuid.uuid4()},
            )
        await session.commit()
    task_id, _done = await _task(db, agent)
    service.start()
    service.submit_report(task_id, claimed_at=datetime.now(UTC))
    await _drain(service)
    await service.close()
    assert reports.records == [] and service.runtime()["over_cap"] == 1
    assert len(await _rows(db, agent)) == 2  # no report row


# ---- two judges -----------------------------------------------------------------------------------------------------


def test_the_second_judge_grades_decisions_only():
    assert service_module.SECOND_JUDGE_GRADES_REPORTS is False


async def test_a_judge_set_hands_a_report_to_the_first_judge_only(db, agent):
    first_reports = StubJudge(name="first", version="r1")
    second_reports = StubJudge(name="second", version="r2")
    first = _service(db, agent, report_judge=first_reports, judge=StubJudge(name="first", version="d1"))
    # Even a second service that could judge reports is not handed one.
    second = _service(db, agent, report_judge=second_reports, judge=StubJudge(name="second", version="d2"))
    both = JudgeSet(first, second)
    first.start()
    second.start()
    task_id, _done = await _task(db, agent)
    assert both.submit_report(task_id, claimed_at=datetime.now(UTC)) is True
    assert second.idle and second.runtime()["queued"] == 0
    await _drain(first)
    await both.close()
    assert [r.subject_id for r in first_reports.records] == [task_id] and second_reports.records == []
    assert [(r["judge_name"], r["subject_kind"]) for r in await _rows(db, agent)] == [("first", "task_report")]


async def test_a_judge_set_whose_first_judge_raises_on_a_report_does_not_raise(db, agent, caplog):
    class Raising:
        def submit_report(self, *args, **kwargs):
            raise RuntimeError("broken")

        def submit_decision(self, *args, **kwargs):  # pragma: no cover - not called here
            raise RuntimeError("broken")

    both = JudgeSet(Raising(), None)
    assert both.submit_report(uuid.uuid4(), claimed_at=datetime.now(UTC)) is False
    assert "RuntimeError" in caplog.text


async def test_a_judge_set_reads_its_report_judge_as_the_first_judges(db, agent):
    reports = StubJudge()
    both = JudgeSet(_service(db, agent, report_judge=reports), None)
    assert both.report_judge is reports
    assert JudgeSet(_service(db, agent), None).report_judge is None
