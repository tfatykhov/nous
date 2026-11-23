"""F118 task reports in GET /dashboard/outside-judge (owner-guarded).

With ``NOUS_OUTSIDE_JUDGE_REPORTS_ENABLED`` off the answer is exactly the one without reports, key for key, even with
report rows in the table. With it on the answer adds ``reports`` (the report judge's verdicts, how they compare with
the finish checks and with the owner's spot checks, and their cost) and ``sweep`` (the agreement with grades from
outcomes, the stated-confidence grade apart, and what the sweep did in this process). A block that cannot be read is
left out; the rest is still given.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from nous import judge as judge_slot
from nous.config import Settings
from nous.judge import store

OWNER_VALUE = "owner-only-test-value"
OWNER = {"X-Nous-Owner-Secret": OWNER_VALUE}
NOW = datetime(2026, 11, 22, 15, 0, tzinfo=UTC)
GROUP_KEYS = {"n", "agree", "judge_harsher", "judge_kinder", "cannot_tell", "agree_rate"}


@pytest.fixture(autouse=True)
def _no_judge_installed():
    judge_slot.set_current(None)
    yield
    judge_slot.set_current(None)


def _report(**over: Any) -> dict[str, Any]:
    row = {
        "subject_id": uuid.uuid4(),
        "status": "ok",
        "verdict": "supported",
        "confidence": 0.8,
        "labels": [],
        "reason": None,
        "tokens_in": 1000,
        "tokens_out": 50,
        "latency_ms": 900,
        "calls": 1,
        "judged_at": NOW,
        "finish_check": "clean",
        "audit": None,
    }
    return {**row, **over}


# ---- the rules -----------------------------------------------------------------------------------------------------


def test_the_report_numbers_of_seeded_rows():
    rows = [
        _report(audit="right"),  # agrees with both
        _report(verdict="not_supported", finish_check="flagged", audit="wrong", reason="The deploy failed."),
        _report(finish_check="flagged", judged_at=NOW + timedelta(seconds=1)),  # disagrees with the finish check
        _report(verdict="not_supported", finish_check="clean", audit="right", judged_at=NOW + timedelta(seconds=2)),
        _report(verdict="cannot_tell", finish_check="clean"),  # apart
        _report(finish_check="not_checked"),  # compared with nothing
        _report(finish_check=None),  # a task without a finish check
        _report(status="skipped", verdict="cannot_tell", labels=["evidence_missing"], calls=0, tokens_in=None),
        _report(status="error", verdict="cannot_tell", labels=["judge_error"], latency_ms=2000, tokens_in=None),
        _report(status="timeout", verdict="cannot_tell", labels=["judge_error"], latency_ms=60000, tokens_out=None),
    ]
    numbers = store.summarize_reports(rows)
    assert numbers["judged"] == 7
    assert numbers["by_verdict"] == {"supported": 4, "not_supported": 2, "cannot_tell": 1}
    assert numbers["skipped_by_label"] == {"evidence_missing": 1}
    assert numbers["errors"] == 2
    assert numbers["vs_finish_check"] == {
        "n": 6,
        "compared": 4,
        "agree": 2,
        "agree_rate": 0.5,
        "matrix": {
            "clean": {"supported": 1, "not_supported": 1, "cannot_tell": 1},
            "flagged": {"not_supported": 1, "supported": 1},
            "not_checked": {"supported": 1},
        },
    }
    assert numbers["vs_audit"] == {
        "n": 3,
        "compared": 3,
        "agree": 2,
        "agree_rate": round(2 / 3, 4),
        "matrix": {"right": {"supported": 1, "not_supported": 1}, "wrong": {"not_supported": 1}},
    }
    assert numbers["cost"] == {
        "calls": 9,
        "tokens_in": 8000,
        "tokens_out": 400,
        "tokens_per_verdict_avg": round(8400 / 9, 1),
        "latency_ms_p50": 900.0,
    }
    latest = numbers["not_supported"]
    assert [entry["reason"] for entry in latest] == [None, "The deploy failed."]
    assert set(latest[0]) == {"subject_id", "confidence", "labels", "reason", "finish_check", "audit", "judged_at"}


def test_a_finish_check_verdict_a_finish_check_does_not_give_is_read_as_none():
    rows = [_report(finish_check="made_up"), _report(finish_check=""), _report(verdict="not_supported", finish_check=7)]
    numbers = store.summarize_reports(rows)
    assert numbers["vs_finish_check"] == {"n": 0, "compared": 0, "agree": 0, "agree_rate": None, "matrix": {}}
    assert [entry["finish_check"] for entry in numbers["not_supported"]] == [None]


def test_no_report_rows_give_zeros_and_no_rates():
    numbers = store.summarize_reports([])
    assert numbers["judged"] == 0 and numbers["not_supported"] == []
    assert numbers["vs_finish_check"] == {"n": 0, "compared": 0, "agree": 0, "agree_rate": None, "matrix": {}}
    assert numbers["cost"]["tokens_per_verdict_avg"] is None and numbers["cost"]["latency_ms_p50"] is None


def test_at_most_twenty_not_supported_latest_first():
    rows = [_report(verdict="not_supported", judged_at=NOW + timedelta(seconds=i)) for i in range(30)]
    latest = store.summarize_reports(rows)["not_supported"]
    assert len(latest) == 20 and latest[0]["judged_at"] == (NOW + timedelta(seconds=29)).isoformat()


def _group(n: int, agree: int, harsher: int = 0, kinder: int = 0, cannot_tell: int = 0) -> dict[str, Any]:
    graded = n - cannot_tell
    return {
        "n": n,
        "agree": agree,
        "judge_harsher": harsher,
        "judge_kinder": kinder,
        "cannot_tell": cannot_tell,
        "agree_rate": round(agree / graded, 4) if graded else None,
    }


def test_the_headline_agreement_is_with_outcome_grades_and_the_review_route_and_stated_confidence_are_apart():
    by_grader = {
        "agent": _group(10, 2, harsher=8),
        "a2ui": _group(3, 2, kinder=1),
        "external": _group(1, 1),
        "auto": _group(4, 2, harsher=1, cannot_tell=1),
        store.STATED_CONFIDENCE_GRADER: _group(6, 1, kinder=5),
    }
    agreement = store.outcome_agreement(by_grader)
    assert agreement == {  # the owner's taps and the reviewer's outcome signals; not the agent, not the review route
        "outcome_grades": _group(7, 4, harsher=1, kinder=1, cannot_tell=1),
        "stated_confidence": _group(6, 1, kinder=5),
    }
    assert store.outcome_agreement({}) == {"outcome_grades": _group(0, 0), "stated_confidence": _group(0, 0)}


# ---- the route -----------------------------------------------------------------------------------------------------


class RefusingDatabase:
    def session(self) -> Any:
        raise AssertionError("a database session was opened")


def _app(tmp_path, database: Any, **over: Any):
    from nous.api.rest import create_app

    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        owner_route_secret_sha256=hashlib.sha256(OWNER_VALUE.encode()).hexdigest(),
        **over,
    )
    stand_in = SimpleNamespace()
    return create_app(stand_in, stand_in, SimpleNamespace(schedules=None), stand_in, database, settings)


async def _get(app, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://nous") as client:
        return await client.get(path, headers=OWNER)


async def test_with_reports_on_and_the_judge_off_the_route_answers_disabled_without_a_session(tmp_path):
    app = _app(tmp_path, RefusingDatabase(), outside_judge_reports_enabled=True)
    response = await _get(app, "/dashboard/outside-judge")
    assert (response.status_code, response.json()) == (200, {"enabled": False})


@pytest_asyncio.fixture
async def agent(db):
    name = f"test-f118-rroute-{uuid.uuid4().hex[:8]}"
    yield name
    async with db.session() as session:
        for table in ("nous_system.judge_verdicts", "nous_system.check_audits"):
            await session.execute(text(f"DELETE FROM {table} WHERE agent_id LIKE :a"), {"a": f"{name}%"})
        await session.commit()


async def _seed(db, agent: str, rows: list[dict[str, Any]]) -> None:
    async with db.session() as session:
        for row in rows:
            values = {
                "agent_id": agent,
                "subject_kind": "decision",
                "subject_id": uuid.uuid4(),
                "judge_name": "stub",
                "judge_version": "stub-9",
                "status": "ok",
                "verdict": "success",
                "confidence": 0.8,
                "labels": [],
                "reason": None,
                "against_grader": "agent",
                "against_grade": "success",
                "evidence": {"tool_calls": 1},
                "tokens_in": 1000,
                "tokens_out": 50,
                "latency_ms": 900,
                "calls": 1,
                "judged_at": datetime.now(UTC),
                **row,
            }
            await session.execute(
                text(
                    "INSERT INTO nous_system.judge_verdicts (agent_id, subject_kind, subject_id, judge_name, "
                    "judge_version, model, status, verdict, confidence, labels, reason, against_grader, against_grade, "
                    "evidence, tokens_in, tokens_out, latency_ms, calls, judged_at, claim_at) VALUES (:agent_id, "
                    ":subject_kind, :subject_id, :judge_name, :judge_version, 'claude-test', :status, :verdict, "
                    ":confidence, CAST(:labels AS jsonb), :reason, :against_grader, :against_grade, "
                    "CAST(:evidence AS jsonb), :tokens_in, :tokens_out, :latency_ms, :calls, :judged_at, :judged_at)"
                ),
                {**values, "labels": json.dumps(values["labels"]), "evidence": json.dumps(values["evidence"])},
            )
        await session.commit()


async def _audit(db, agent: str, task_id: uuid.UUID, verdict: str | None) -> None:
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO nous_system.check_audits (agent_id, task_id, node_id, check_kind, verdict) "
                "VALUES (:a, :t, :n, 'command', :v)"
            ),
            {"a": agent, "t": task_id, "n": uuid.uuid4(), "v": verdict},
        )
        await session.commit()


def _report_row(verdict: str, finish_check: str | None, **over: Any) -> dict[str, Any]:
    return {
        "subject_kind": "task_report",
        "judge_version": "stub-r9",
        "verdict": verdict,
        "against_grader": "agent",
        "against_grade": "completed",
        "evidence": {"tool_calls": 2, "finish_check": finish_check},
        **over,
    }


class _Stub:
    def __init__(self, version: str) -> None:
        self.name, self.version, self.model = "stub", version, "claude-stub"

    async def grade(self, record, *, timeout, max_calls=None):  # pragma: no cover - never called here
        raise AssertionError


# PR 1's answer for one decision row of the stub judge (copied from tests/test_outside_judge_route.py), with today's
# calls of the judge as given: the limit is per judge name, every kind of row.
def _main_answer(today_calls: int) -> dict[str, Any]:
    return {
        "enabled": True,
        "mode": "shadow",
        "judge": "stub",
        "version": "stub-9",
        "model": "claude-stub",
        "days": 14,
        "decisions": {
            "judged": 1,
            "by_verdict": {"success": 1},
            "skipped_by_label": {},
            "errors": 0,
            "by_grader": {
                "agent": {
                    "n": 1,
                    "agree": 1,
                    "judge_harsher": 0,
                    "judge_kinder": 0,
                    "cannot_tell": 0,
                    "agree_rate": 1.0,
                }
            },
        },
        "cost": {
            "calls": 1,
            "tokens_in": 1000,
            "tokens_out": 50,
            "tokens_per_verdict_avg": 1050.0,
            "latency_ms_p50": 900.0,
            "today": {"calls": today_calls, "cap": 7},
        },
        "disagreements": [],
        "runtime": {"queued": 0, "dropped": 0, "over_cap": 0, "worker_alive": False},
    }


@pytest.mark.postgres_only
@pytest.mark.parametrize("report_judge", [False, True])
async def test_with_reports_off_the_answer_is_mains_key_for_key_even_with_report_rows(
    tmp_path, db, agent, report_judge
):
    from nous.judge.service import OutsideJudge

    over = {"agent_id": agent, "outside_judge_mode": "shadow", "outside_judge_daily_calls": 7}
    service = OutsideJudge(
        db, Settings(_env_file=None, **over), _Stub("stub-9"), report_judge=_Stub("stub-r9") if report_judge else None
    )
    await _seed(db, agent, [{}, _report_row("not_supported", "flagged")])
    judge_slot.set_current(service)
    try:
        response = await _get(_app(tmp_path, db, **over), "/dashboard/outside-judge")
    finally:
        await service.close()
    assert response.json() == _main_answer(today_calls=2)


@pytest.mark.postgres_only
async def test_with_reports_on_the_answer_adds_the_report_and_sweep_numbers(tmp_path, db, agent):
    from nous.judge.service import OutsideJudge

    over = {
        "agent_id": agent,
        "outside_judge_mode": "shadow",
        "outside_judge_daily_calls": 7,
        "outside_judge_reports_enabled": True,
    }
    settings = Settings(_env_file=None, **over)
    service = OutsideJudge(db, settings, _Stub("stub-9"), report_judge=_Stub("stub-r9"))
    supported, not_supported = uuid.uuid4(), uuid.uuid4()
    await _seed(
        db,
        agent,
        [
            {},
            {"against_grader": "a2ui", "against_grade": "failure", "verdict": "failure"},
            {"against_grader": "auto", "against_grade": "success", "verdict": "failure"},
            {"against_grader": store.STATED_CONFIDENCE_GRADER, "against_grade": "failure", "verdict": "success"},
            _report_row("supported", "clean", subject_id=supported),
            _report_row("not_supported", "clean", subject_id=not_supported, reason="The deploy failed."),
            _report_row("cannot_tell", None, status="skipped", labels=["evidence_missing"], calls=0),
            _report_row("supported", "clean", judge_version="stub-r0"),  # another version: not read
            _report_row("supported", "clean", judged_at=datetime.now(UTC) - timedelta(days=20)),  # outside the window
        ],
    )
    await _seed(db, f"{agent}-other", [_report_row("supported", "clean")])
    await _audit(db, agent, supported, "right")
    await _audit(db, agent, not_supported, None)  # offered, not answered yet: not compared
    judge_slot.set_current(service)
    try:
        response = await _get(_app(tmp_path, db, **over), "/dashboard/outside-judge")
    finally:
        await service.close()
    body = response.json()
    assert response.status_code == 200, response.text
    assert set(body) == {*_main_answer(0), "reports", "sweep"}
    assert body["decisions"]["by_grader"]["a2ui"]["agree"] == 1
    reports = body["reports"]
    assert (reports["judge"], reports["version"]) == ("stub", "stub-r9")
    assert reports["judged"] == 2 and reports["by_verdict"] == {"supported": 1, "not_supported": 1}
    assert reports["skipped_by_label"] == {"evidence_missing": 1}
    assert reports["vs_finish_check"]["matrix"] == {"clean": {"supported": 1, "not_supported": 1}}
    assert (reports["vs_finish_check"]["compared"], reports["vs_finish_check"]["agree"]) == (2, 1)
    assert reports["vs_audit"] == {
        "n": 1,
        "compared": 1,
        "agree": 1,
        "agree_rate": 1.0,
        "matrix": {"right": {"supported": 1}},
    }
    assert reports["cost"]["calls"] == 2
    [entry] = reports["not_supported"]
    assert entry["subject_id"] == str(not_supported) and entry["reason"] == "The deploy failed."
    sweep = body["sweep"]
    assert sweep["agreement"]["outcome_grades"] == _group(2, 1, harsher=1)
    assert sweep["agreement"]["stated_confidence"] == _group(1, 0, kinder=1)
    assert sweep["runtime"] == {
        "passes": 0,
        "queued_decisions": {},
        "queued_reports": 0,
        "held_at_half_limit": 0,
        "failed": 0,
        "last_pass_at": None,
    }
    # Today's calls of the judge count both kinds at every version: four decisions and three reports of today.
    assert body["cost"]["today"] == {"calls": 7, "cap": 7}


class _Second(_Stub):
    def __init__(self) -> None:
        self.name, self.version, self.model = "jev", "jq-3:jev-1.14.0", "jev-1.14.0"


def _two_judges(db, settings: Settings, *, second_sweeps: bool) -> Any:
    from nous.judge.service import JudgeSet, OutsideJudge

    first = OutsideJudge(db, settings, _Stub("stub-9"), report_judge=_Stub("stub-r9"))
    # As main.py and the settings manager build the second judge's service: no report judge, its own limit.
    second = OutsideJudge(db, settings, _Second(), daily_calls=3, sweep=second_sweeps)
    return JudgeSet(first, second)


@pytest.mark.postgres_only
@pytest.mark.parametrize("second_sweeps", [True, False])
async def test_with_a_second_judge_the_sweep_block_adds_its_own_pass_only_when_it_runs_one(
    tmp_path, db, agent, second_sweeps
):
    over = {
        "agent_id": agent,
        "outside_judge_mode": "shadow",
        "outside_judge_daily_calls": 7,
        "outside_judge_reports_enabled": True,
    }
    both = _two_judges(db, Settings(_env_file=None, **over), second_sweeps=second_sweeps)
    if second_sweeps:
        assert await both.second.sweep_once() == 0  # one pass that found nothing
    judge_slot.set_current(both)
    try:
        response = await _get(_app(tmp_path, db, **over), "/dashboard/outside-judge")
    finally:
        await both.close()
    body = response.json()
    assert response.status_code == 200 and {"reports", "second", "between_judges"} <= set(body)
    assert set(body["second"]) == {"judge", "version", "model", "decisions", "cost", "disagreements"}
    sweep = body["sweep"]
    assert sweep["runtime"] == both.primary.sweep_numbers()  # the first judge's own numbers, unchanged
    if not second_sweeps:
        assert set(sweep) == {"agreement", "runtime"}
        return
    assert set(sweep) == {"agreement", "runtime", "second_runtime"}
    second_runtime = sweep["second_runtime"]
    keys = {"passes", "queued_decisions", "queued_reports", "held_at_half_limit", "failed", "last_pass_at"}
    assert set(second_runtime) == set(sweep["runtime"]) == keys
    assert (second_runtime["passes"], sweep["runtime"]["passes"]) == (1, 0)
    assert second_runtime["last_pass_at"] is not None and second_runtime["queued_reports"] == 0


@pytest.mark.postgres_only
async def test_when_the_second_judges_pass_numbers_cannot_be_read_only_they_are_left_out(
    tmp_path, db, agent, monkeypatch, caplog
):
    over = {"agent_id": agent, "outside_judge_mode": "shadow", "outside_judge_reports_enabled": True}
    both = _two_judges(db, Settings(_env_file=None, **over), second_sweeps=True)

    def broken() -> Any:
        raise RuntimeError("the second numbers broke")

    monkeypatch.setattr(both.second, "sweep_numbers", broken)
    judge_slot.set_current(both)
    try:
        with caplog.at_level(logging.WARNING):
            response = await _get(_app(tmp_path, db, **over), "/dashboard/outside-judge")
    finally:
        await both.close()
    body = response.json()
    assert response.status_code == 200 and "reports" in body and "second" in body
    assert set(body["sweep"]) == {"agreement", "runtime"} and body["sweep"]["runtime"] == both.primary.sweep_numbers()
    read = [record.getMessage() for record in caplog.records if "sweep numbers" in record.getMessage()]
    assert read == ["F118: a judge's sweep numbers were not read (RuntimeError)"]
    assert "the second numbers broke" not in caplog.text


@pytest.mark.postgres_only
async def test_with_reports_on_and_no_judge_running_the_report_blocks_name_the_built_in_judge(tmp_path, db, agent):
    from nous.judge.instructions import REPORT_INSTRUCTION_VERSION
    from nous.judge.model_judge import NAME

    over = {"agent_id": agent, "outside_judge_mode": "shadow", "outside_judge_reports_enabled": True}
    body = (await _get(_app(tmp_path, db, **over), "/dashboard/outside-judge")).json()
    assert (body["reports"]["judge"], body["reports"]["version"]) == (NAME, REPORT_INSTRUCTION_VERSION)
    assert body["reports"]["judged"] == 0 and body["sweep"]["runtime"] is None


@pytest.mark.postgres_only
async def test_when_the_report_numbers_cannot_be_read_the_rest_is_still_given(tmp_path, db, agent, monkeypatch, caplog):
    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the report numbers broke")

    monkeypatch.setattr(store, "report_numbers", broken)
    over = {"agent_id": agent, "outside_judge_mode": "shadow", "outside_judge_reports_enabled": True}
    await _seed(db, agent, [{"judge_name": "background_model", "judge_version": "dj-2"}])
    with caplog.at_level(logging.WARNING):
        response = await _get(_app(tmp_path, db, **over), "/dashboard/outside-judge")
    body = response.json()
    assert response.status_code == 200 and "reports" not in body and "sweep" not in body
    assert body["decisions"]["judged"] == 1
    assert "RuntimeError" in caplog.text
