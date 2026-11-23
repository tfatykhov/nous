"""F118: the outside judge's hourly sweep (real Postgres, stub judges, no model).

With ``NOUS_OUTSIDE_JUDGE_REPORTS_ENABLED`` the service runs one bounded pass soon after it starts and then every hour.
A pass queues at most five subjects without a final row at the judge's name and version, in this order: decisions
graded by the owner, what a restart lost (decisions the agent resolved in the last 7 days, reports finished in the last
2 days), then decisions graded by a fixed signal. It queues nothing once today's calls of the judge reach half the
daily limit. It reads the claim of a decision from its row as it stands; the grade the reviewer gives from the agent's
own low stated confidence is stored under its own grader name. It never writes a decision or a task.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import text

from nous.config import Settings
from nous.handlers.decision_reviewer import ErrorSignal
from nous.judge import evidence, store
from nous.judge import service as service_module
from nous.judge.service import JudgeSet, OutsideJudge
from nous.judge.slot import JudgeInput, JudgeOutput

pytestmark = pytest.mark.postgres_only

FAST = {"settle_seconds": 0.0, "timeout_seconds": 2.0}
NOW = datetime.now(UTC)


class StubJudge:
    def __init__(self, *, name: str = "stub", version: str = "d1", verdict: str = "success") -> None:
        self.name, self.version, self.model = name, version, "stub-model"
        self.verdict = verdict
        self.records: list[JudgeInput] = []

    async def grade(self, record: JudgeInput, *, timeout: float, max_calls: int | None = None) -> JudgeOutput:
        self.records.append(record)
        return JudgeOutput(status="ok", verdict=self.verdict, confidence=0.6, calls=1)


@pytest_asyncio.fixture
async def agent(db):
    name = f"test-f118-swp-{uuid.uuid4().hex[:8]}"
    yield name
    async with db.session() as session:
        for table in ("brain.decisions", "heart.subtasks", "nous_system.tool_call_log", "nous_system.judge_verdicts"):
            await session.execute(text(f"DELETE FROM {table} WHERE agent_id LIKE :a"), {"a": f"{name}%"})
        await session.commit()


def _settings(agent: str, **over) -> Settings:
    return Settings(
        _env_file=None, agent_id=agent, outside_judge_mode="shadow", outside_judge_reports_enabled=True, **over
    )


def _service(db, agent: str, *, reports: bool = True, **over) -> tuple[OutsideJudge, StubJudge, StubJudge | None]:
    decisions = StubJudge()
    report_judge = StubJudge(version="r1", verdict="supported") if reports else None
    settings = _settings(agent, **{k: v for k, v in over.items() if k.startswith("outside_judge")})
    rest = {k: v for k, v in over.items() if not k.startswith("outside_judge")}
    return OutsideJudge(db, settings, decisions, report_judge=report_judge, **{**FAST, **rest}), decisions, report_judge


async def _decision(
    db,
    agent: str,
    reviewer: str | None,
    *,
    outcome: str = "success",
    reviewed_at: datetime | None = None,
    confidence: float = 0.7,
    note: str = "it ran",
) -> uuid.UUID:
    """A decision graded by ``reviewer`` with one call in its session (so it can be judged)."""
    decision_id = uuid.uuid4()
    reviewed = reviewed_at or NOW - timedelta(hours=1)
    session_id = f"sess-{decision_id.hex[:8]}"
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO brain.decisions (id, agent_id, description, confidence, category, stakes, session_id, "
                "created_at, outcome, outcome_result, reviewed_at, reviewer) VALUES (:id, :a, 'Move the backup', :c, "
                "'tooling', 'low', :s, :t, :o, :n, :r, :rv)"
            ),
            {
                "id": decision_id,
                "a": agent,
                "c": confidence,
                "s": session_id,
                "t": reviewed - timedelta(minutes=10),
                "o": outcome,
                "n": note,
                "r": reviewed,
                "rv": reviewer,
            },
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


async def _task(
    db, agent: str, *, completed_at: datetime | None = None, status: str = "completed", final_outcome: str = "completed"
) -> uuid.UUID:
    task_id = uuid.uuid4()
    done = completed_at or NOW - timedelta(hours=2)
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO heart.subtasks (id, agent_id, task, status, final_outcome, report_jsonb, created_at, "
                "completed_at) VALUES (:id, :a, 'Fix the link', :st, :fo, CAST(:r AS jsonb), :c, :d)"
            ),
            {
                "id": task_id,
                "a": agent,
                "st": status,
                "fo": final_outcome,
                "r": json.dumps({"summary": "Fixed the link."}),
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


async def _verdict(
    db, agent: str, subject_id: uuid.UUID, *, kind: str = "decision", version: str = "d1", **over
) -> None:
    values = {"status": "ok", "verdict": "success" if kind == "decision" else "supported", "attempts": 1, "calls": 1}
    values.update(over)
    judged_at = values.get("judged_at") or datetime.now(UTC)
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO nous_system.judge_verdicts (agent_id, subject_kind, subject_id, judge_name, "
                "judge_version, status, verdict, claim_at, calls, attempts, judged_at) VALUES (:a, :k, :s, 'stub', "
                ":v, :st, :vd, now(), :c, :at, :j)"
            ),
            {
                "a": agent,
                "k": kind,
                "s": subject_id,
                "v": version,
                "st": values["status"],
                "vd": values["verdict"],
                "c": values["calls"],
                "at": values["attempts"],
                "j": judged_at,
            },
        )
        await session.commit()


async def _rows(db, agent: str) -> list[dict]:
    async with db.session() as session:
        result = await session.execute(
            text(
                "SELECT subject_kind, subject_id, against_grader, against_grade, claim_at, status FROM "
                "nous_system.judge_verdicts WHERE agent_id = :a AND calls > 0 ORDER BY judged_at"
            ),
            {"a": agent},
        )
        return [dict(row._mapping) for row in result]


async def _until(predicate, seconds: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out waiting")
        await asyncio.sleep(0.02)


async def _pass(service: OutsideJudge) -> int:
    queued = await service.sweep_once()
    await _until(lambda: service.idle)
    return queued


# ---- on and off -----------------------------------------------------------------------------------------------------


async def test_the_sweep_is_off_unless_reports_are_enabled(db, agent):
    off = OutsideJudge(db, Settings(_env_file=None, agent_id=agent, outside_judge_mode="shadow"), StubJudge(), **FAST)
    off.start()
    assert off.sweep_numbers() is None and off._sweeper is None
    await off.close()
    on, _d, _r = _service(db, agent)
    on.start()
    assert on._sweeper is not None and not on._sweeper.done()
    await on.close()
    assert on._sweeper.done()


async def test_the_first_pass_runs_soon_after_the_start_and_then_every_interval(db, agent, monkeypatch):
    monkeypatch.setattr(service_module, "SWEEP_FIRST_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(service_module, "SWEEP_INTERVAL_SECONDS", 0.05)
    service, _d, _r = _service(db, agent)
    service.start()
    await _until(lambda: service.sweep_numbers()["passes"] >= 2)
    await service.close()
    passes = service.sweep_numbers()["passes"]
    await asyncio.sleep(0.2)
    assert service.sweep_numbers()["passes"] == passes  # stopped by close


# ---- order and bounds -----------------------------------------------------------------------------------------------


async def test_a_pass_takes_owner_grades_then_what_a_restart_lost_then_fixed_signals_at_most_five(db, agent):
    auto = [await _decision(db, agent, "auto", reviewed_at=NOW - timedelta(minutes=m)) for m in (1, 2, 3)]
    owner = [await _decision(db, agent, "a2ui"), await _decision(db, agent, "external")]
    lost = [await _decision(db, agent, "agent", reviewed_at=NOW - timedelta(days=d)) for d in (1, 2)]
    reports = [await _task(db, agent, completed_at=NOW - timedelta(hours=h)) for h in (1, 3)]
    service, _d, _r = _service(db, agent)
    service.start()
    assert await _pass(service) == 5
    first = {row["subject_id"] for row in await _rows(db, agent)}
    assert first == {*owner, *lost, reports[0]}
    assert await _pass(service) == 4
    second = {row["subject_id"] for row in await _rows(db, agent)} - first
    assert second == {reports[1], *auto}
    assert await _pass(service) == 0
    await service.close()
    numbers = service.sweep_numbers()
    assert numbers["passes"] == 3
    assert numbers["queued_decisions"] == {"a2ui": 1, "external": 1, "agent": 2, "auto": 3}
    assert numbers["queued_reports"] == 2


async def test_a_pass_keeps_to_its_windows_and_its_graders(db, agent):
    taken = await _decision(db, agent, "a2ui", reviewed_at=NOW - timedelta(days=29))
    for reviewer, days in (("a2ui", 31), ("agent", 8), ("auto", 31)):
        await _decision(db, agent, reviewer, reviewed_at=NOW - timedelta(days=days))
    for reviewer in ("agent-background", "f099", None):
        await _decision(db, agent, reviewer)
    await _decision(db, agent, "a2ui", outcome="noise")
    await _decision(db, agent, "a2ui", outcome="superseded")
    await _decision(db, agent, "a2ui", outcome="failure", confidence=0.0)  # the abandoned mark
    await _task(db, agent, completed_at=NOW - timedelta(days=3))
    await _task(db, agent, status="failed", final_outcome="errored")
    await _task(db, agent, status="completed", final_outcome="incomplete_blocked")
    await _decision(db, f"{agent}-other", "a2ui")
    await _task(db, f"{agent}-other")
    service, _d, _r = _service(db, agent)
    service.start()
    assert await _pass(service) == 1
    await service.close()
    assert [row["subject_id"] for row in await _rows(db, agent)] == [taken]


async def test_a_subject_with_a_final_row_is_not_taken_and_a_failed_one_is_until_its_third_attempt(db, agent):
    judged = await _decision(db, agent, "a2ui")
    await _verdict(db, agent, judged)
    other_version = await _decision(db, agent, "a2ui")
    await _verdict(db, agent, other_version, version="d0")
    retried = await _decision(db, agent, "a2ui")
    # A failed row is taken again once its last attempt is 6 hours old.
    await _verdict(db, agent, retried, status="error", verdict="cannot_tell", attempts=2, judged_at=NOW - timedelta(hours=7))
    waiting = await _decision(db, agent, "a2ui")
    await _verdict(db, agent, waiting, status="timeout", verdict="cannot_tell", attempts=1, judged_at=NOW - timedelta(hours=5))
    spent = await _decision(db, agent, "a2ui")
    await _verdict(db, agent, spent, status="timeout", verdict="cannot_tell", attempts=3, judged_at=NOW - timedelta(days=1))
    report_done = await _task(db, agent)
    await _verdict(db, agent, report_done, kind="task_report", version="r1")
    service, decisions, _r = _service(db, agent)
    service.start()
    assert await _pass(service) == 2
    await service.close()
    assert {record.subject_id for record in decisions.records} == {other_version, retried}


class FailingJudge(StubJudge):
    async def grade(self, record: JudgeInput, *, timeout: float, max_calls: int | None = None) -> JudgeOutput:
        self.records.append(record)
        return JudgeOutput(status="error", verdict="cannot_tell", calls=1)


async def test_hourly_passes_during_an_outage_do_not_spend_the_attempts(db, agent):
    decision_id = await _decision(db, agent, "a2ui")
    failing = FailingJudge()
    service = OutsideJudge(db, _settings(agent), failing, report_judge=StubJudge(version="r1"), **FAST)
    service.start()
    start = datetime.now(UTC)
    for hour in range(3):  # three hourly passes while the judge fails
        await service.sweep_once(now=start + timedelta(hours=hour))
        await _until(lambda: service.idle)
    async with db.session() as session:
        attempts = await session.scalar(
            text("SELECT attempts FROM nous_system.judge_verdicts WHERE agent_id = :a AND subject_id = :s"),
            {"a": agent, "s": decision_id},
        )
    assert attempts == 1 < store.MAX_ATTEMPTS and len(failing.records) == 1
    await service.sweep_once(now=start + timedelta(hours=7))  # six hours after the last attempt: tried again
    await _until(lambda: service.idle)
    await service.close()
    assert len(failing.records) == 2


async def test_a_pass_queues_nothing_at_half_the_days_limit(db, agent):
    await _decision(db, agent, "a2ui")
    service, _d, _r = _service(db, agent, outside_judge_daily_calls=10)
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO nous_system.judge_verdicts (agent_id, subject_kind, subject_id, judge_name, "
                "judge_version, status, verdict, claim_at, calls) VALUES (:a, 'decision', :s, 'stub', 'd0', 'ok', "
                "'success', now(), 5)"
            ),
            {"a": agent, "s": uuid.uuid4()},
        )
        await session.commit()
    assert await service.sweep_once() == 0
    assert service.sweep_numbers()["held_at_half_limit"] == 1 and service.runtime()["queued"] == 0
    under, _d2, _r2 = _service(db, agent, outside_judge_daily_calls=11)
    assert await under.sweep_once() == 1
    await under.close()
    zero, _d3, _r3 = _service(db, agent, outside_judge_daily_calls=0)
    assert await zero.sweep_once() == 0 and zero.sweep_numbers()["held_at_half_limit"] == 1


async def test_a_pass_that_cannot_count_todays_calls_queues_nothing_and_does_not_raise(db, agent, monkeypatch, caplog):
    await _decision(db, agent, "a2ui")

    async def broken(*args, **kwargs):
        raise RuntimeError("the count failed")

    monkeypatch.setattr(store, "calls_today", broken)
    service, _d, _r = _service(db, agent)
    assert await service.sweep_once() == 0
    assert service.sweep_numbers()["failed"] == 1 and service.runtime()["queued"] == 0
    assert "RuntimeError" in caplog.text


async def test_a_pass_that_fails_while_queueing_is_counted_and_the_next_pass_still_runs(db, agent, monkeypatch, caplog):
    await _decision(db, agent, "a2ui")
    real = service_module._sweep_rank
    ranked = {"n": 0}

    def fails_once(grader: str) -> int:
        ranked["n"] += 1
        if ranked["n"] == 1:
            raise RuntimeError("the ranking broke")
        return real(grader)

    monkeypatch.setattr(service_module, "_sweep_rank", fails_once)
    monkeypatch.setattr(service_module, "SWEEP_FIRST_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(service_module, "SWEEP_INTERVAL_SECONDS", 0.05)
    service, _d, _r = _service(db, agent)
    service.start()
    await _until(lambda: service.sweep_numbers()["queued_decisions"].get("a2ui") == 1)
    await _until(lambda: service.idle)
    assert service.sweep_numbers()["failed"] == 1 and not service._sweeper.done()
    await service.close()
    assert "RuntimeError" in caplog.text


async def test_a_subject_already_queued_is_not_queued_again_or_counted_as_dropped(db, agent):
    decision_id = await _decision(db, agent, "a2ui")
    service, _d, _r = _service(db, agent)
    service.submit_decision(decision_id, grade="success", note=None, grader="a2ui", resolved_at=NOW)
    assert await service.sweep_once() == 0
    assert service.runtime() == {"queued": 1, "dropped": 0, "over_cap": 0, "worker_alive": False}
    await service.close()


async def test_a_service_without_a_report_judge_sweeps_nothing_unless_it_is_given_a_sweep(db, agent):
    decision_id = await _decision(db, agent, "a2ui")
    await _task(db, agent)
    service, _d, _r = _service(db, agent, reports=False)  # the setting is on
    service.start()
    assert service.sweep_numbers() is None and service._sweeper is None
    await service.close()
    given, decisions, _r = _service(db, agent, reports=False, sweep=True)
    given.start()
    assert await _pass(given) == 1  # decisions only
    await given.close()
    assert [row["subject_kind"] for row in await _rows(db, agent)] == ["decision"]
    assert [record.subject_id for record in decisions.records] == [decision_id]
    assert given.sweep_numbers()["queued_reports"] == 0


async def test_with_two_judges_only_the_first_sweeps(db, agent):
    await _decision(db, agent, "external")
    task_id = await _task(db, agent)
    first, _d, first_reports = _service(db, agent)
    second_judge = StubJudge(name="second", version="j1")
    second = OutsideJudge(db, _settings(agent), second_judge, **FAST)  # as main.py builds it: no report judge
    both = JudgeSet(first, second)
    first.start()
    second.start()
    assert second._sweeper is None
    assert await first.sweep_once() == 2
    await _until(lambda: first.idle and second.idle)
    await both.close()
    assert [record.subject_id for record in first_reports.records] == [task_id]
    assert second_judge.records == []
    numbers = both.sweep_numbers()
    assert numbers["second"] is None and numbers["queued_reports"] == 1


# ---- the claim ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("reviewer", ["a2ui", "external", "auto"])
async def test_a_swept_decision_graded_by_another_is_judged_without_its_grade_and_compared_with_it(
    db, agent, reviewer
):
    reviewed = NOW - timedelta(hours=3)
    note = "NOTE-7c2e only half of the backup moved"
    decision_id = await _decision(db, agent, reviewer, outcome="partial", reviewed_at=reviewed, note=note)
    service, decisions, _r = _service(db, agent)
    service.start()
    await _pass(service)
    await service.close()
    [record] = decisions.records
    assert record.claim is None
    rendered = evidence.render_record(record)
    assert json.loads(rendered)["written_by_author"]["claim"] is None
    assert "NOTE-7c2e" not in rendered and "partial" not in rendered
    [row] = await _rows(db, agent)
    assert (row["against_grader"], row["against_grade"], row["claim_at"]) == (reviewer, "partial", reviewed)
    assert row["subject_id"] == decision_id


async def test_a_decision_the_agent_resolved_is_judged_on_its_claim_as_it_stands(db, agent):
    reviewed = NOW - timedelta(hours=3)
    await _decision(db, agent, "agent", outcome="partial", reviewed_at=reviewed, note="half of it")
    service, decisions, _r = _service(db, agent)
    service.start()
    await _pass(service)
    await service.close()
    [record] = decisions.records
    assert (record.claim.grader, record.claim.grade, record.claim.note) == ("agent", "partial", "half of it")
    [row] = await _rows(db, agent)
    assert (row["against_grader"], row["against_grade"], row["claim_at"]) == ("agent", "partial", reviewed)


async def test_the_reviewers_stated_confidence_grade_is_kept_apart_from_its_outcome_signals(db, agent):
    stated = await ErrorSignal().check(SimpleNamespace(confidence_raw=0.2, confidence=0.15))
    assert stated is not None and stated.result == "failure"
    assert store.grader_of("auto", stated.result, stated.explanation) == store.STATED_CONFIDENCE_GRADER
    assert store.grader_of("auto", "success", "PR #12 merged") == "auto"
    assert store.grader_of("auto", "failure", "PR #12 closed without merge") == "auto"
    assert store.grader_of("a2ui", "failure", stated.explanation) == "a2ui"
    assert store.grader_of("agent", "failure", stated.explanation) == "agent"

    low = await _decision(db, agent, "auto", outcome="failure", confidence=0.2, note=stated.explanation)
    merged = await _decision(db, agent, "auto", note="PR #12 merged")
    service, _d, _r = _service(db, agent)
    service.start()
    await _pass(service)
    await service.close()
    graders = {row["subject_id"]: row["against_grader"] for row in await _rows(db, agent)}
    assert graders == {low: "auto:stated_confidence", merged: "auto"}
    assert service.sweep_numbers()["queued_decisions"] == {"auto:stated_confidence": 1, "auto": 1}


# ---- it writes nothing but verdicts -------------------------------------------------------------------------------


async def test_the_sweep_never_writes_a_decision_or_a_task(db, agent):
    for reviewer in ("a2ui", "external", "agent", "auto"):
        await _decision(db, agent, reviewer)
    await _task(db, agent)

    async def snapshot() -> list:
        async with db.session() as session:
            rows = []
            for table in ("brain.decisions", "heart.subtasks", "nous_system.tool_call_log"):
                result = await session.execute(
                    text(f"SELECT to_jsonb(t) FROM {table} t WHERE agent_id = :a"), {"a": agent}
                )
                rows.append(sorted(json.dumps(r[0], sort_keys=True) for r in result))
            return rows

    before = await snapshot()
    service, _d, _r = _service(db, agent)
    service.start()
    assert await _pass(service) == 5
    await service.close()
    assert len(await _rows(db, agent)) == 5
    assert await snapshot() == before
