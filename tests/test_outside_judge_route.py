"""F118: GET /dashboard/outside-judge, the outside judge's numbers (owner-guarded; the guard's own tests are in
tests/test_owner_guard_routes.py).

Agreement counts only ``ok`` rows of the running judge's name and version, grouped by the grader of the claim as it
was stored; ``cannot_tell`` is counted apart and left out of the agreement rate; with ``failure < partial < success``
a verdict below the claim is harsher, above it kinder. The answer carries ids, labels and the judge's one sentence,
never evidence text or the agent's note. With the judge off: 200 ``{"enabled": false}`` and no database session.
"""

from __future__ import annotations

import hashlib
import json
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
from nous.judge import jev_judge, store

OWNER_VALUE = "owner-only-test-value"
OWNER = {"X-Nous-Owner-Secret": OWNER_VALUE}
NOW = datetime(2026, 11, 22, 15, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _no_request_leaves_the_process(monkeypatch):
    """No judge here is given a decision; a request through a real transport fails the test."""

    async def refuse(self: Any, request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"a real request was attempted to {request.url.host}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)


def _row(**over: Any) -> dict[str, Any]:
    row = {
        "subject_id": uuid.uuid4(),
        "status": "ok",
        "verdict": "success",
        "confidence": 0.8,
        "labels": [],
        "reason": None,
        "against_grader": "agent",
        "against_grade": "success",
        "tokens_in": 1000,
        "tokens_out": 50,
        "latency_ms": 900,
        "calls": 1,
        "judged_at": NOW,
    }
    return {**row, **over}


# ---- the rules -----------------------------------------------------------------------------------------------------


def test_the_numbers_of_seeded_rows():
    rows = [
        _row(),  # agree
        _row(verdict="partial"),  # harsher than success
        _row(verdict="failure", against_grade="partial", reason="It did not run.", labels=["evidence_contradicts"]),
        _row(verdict="success", against_grade="failure"),  # kinder
        _row(verdict="cannot_tell", against_grade="success"),
        _row(verdict="failure", against_grade="failure", against_grader="a2ui"),
        _row(
            status="skipped",
            verdict="cannot_tell",
            labels=["evidence_missing"],
            tokens_in=None,
            tokens_out=None,
            latency_ms=None,
            calls=0,
        ),
        _row(
            status="skipped",
            verdict="cannot_tell",
            labels=["evidence_missing"],
            tokens_in=None,
            tokens_out=None,
            latency_ms=None,
            calls=0,
        ),
        _row(
            status="error",
            verdict="cannot_tell",
            labels=["judge_error"],
            tokens_in=None,
            tokens_out=None,
            latency_ms=100,
        ),
        _row(
            status="timeout",
            verdict="cannot_tell",
            labels=["judge_error"],
            tokens_in=20,
            tokens_out=None,
            latency_ms=60000,
            calls=2,  # two attempts
        ),
    ]
    numbers = store.summarize(rows)
    assert numbers["decisions"] == {
        "judged": 6,
        "by_verdict": {"success": 2, "partial": 1, "failure": 2, "cannot_tell": 1},
        "skipped_by_label": {"evidence_missing": 2},
        "errors": 2,
        "by_grader": {
            "agent": {
                "n": 5,
                "agree": 1,
                "judge_harsher": 2,
                "judge_kinder": 1,
                "cannot_tell": 1,
                "agree_rate": 0.25,
            },
            "a2ui": {"n": 1, "agree": 1, "judge_harsher": 0, "judge_kinder": 0, "cannot_tell": 0, "agree_rate": 1.0},
        },
    }
    # Calls: the model requests each row records (6 ok, 1 error, 2 for the timeout); a skipped row made none.
    assert numbers["cost"] == {
        "calls": 9,
        "tokens_in": 6020,
        "tokens_out": 300,
        "tokens_per_verdict_avg": round((6020 + 300) / 8, 1),
        "latency_ms_p50": 900,
    }
    # Disagreements: ok rows whose verdict differs from the claim, cannot_tell left out, latest first.
    assert [(d["against_grade"], d["verdict"]) for d in numbers["disagreements"]] == [
        ("success", "partial"),
        ("partial", "failure"),
        ("failure", "success"),
    ]
    detail = numbers["disagreements"][1]
    assert set(detail) == {
        "subject_id",
        "against_grader",
        "against_grade",
        "verdict",
        "confidence",
        "labels",
        "reason",
        "judged_at",
    }
    assert (detail["reason"], detail["labels"], detail["judged_at"]) == (
        "It did not run.",
        ["evidence_contradicts"],
        NOW.isoformat(),
    )


def test_no_rows_give_zeros_and_no_rates():
    numbers = store.summarize([])
    assert numbers["decisions"] == {"judged": 0, "by_verdict": {}, "skipped_by_label": {}, "errors": 0, "by_grader": {}}
    assert numbers["cost"] == {
        "calls": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "tokens_per_verdict_avg": None,
        "latency_ms_p50": None,
    }
    assert numbers["disagreements"] == []


def test_a_grader_with_only_cannot_tell_has_no_rate_and_an_unknown_grade_is_neither_harsher_nor_kinder():
    numbers = store.summarize([_row(verdict="cannot_tell"), _row(against_grader="auto", against_grade="weird")])
    assert numbers["decisions"]["by_grader"]["agent"]["agree_rate"] is None
    auto = numbers["decisions"]["by_grader"]["auto"]
    assert (auto["n"], auto["agree"], auto["judge_harsher"], auto["judge_kinder"], auto["agree_rate"]) == (
        1,
        0,
        0,
        0,
        0.0,
    )


def test_at_most_fifty_disagreements_latest_first():
    rows = [_row(verdict="failure", judged_at=NOW - timedelta(minutes=i)) for i in range(60)]
    listed = store.summarize(rows)["disagreements"]
    assert len(listed) == 50 and listed[0]["judged_at"] == NOW.isoformat()


def _pair(first: str, second: str, **over: Any) -> dict[str, Any]:
    """One joined row of ``_PAIR_ROWS``: both judges' ``ok`` rows on one subject."""
    row = {
        "subject_id": uuid.uuid4(),
        "same_claim": True,
        "same_evidence": True,
        "claim_at": NOW,
        "against_grader": "agent",
        "against_grade": "success",
        "first_verdict": first,
        "second_verdict": second,
        "first_confidence": 0.8,
        "second_confidence": 0.6,
        "first_labels": [],
        "second_labels": [],
    }
    return {**row, **over}


def test_the_pair_numbers_of_joined_rows():
    rows = [
        _pair("success", "success"),
        _pair("failure", "failure", same_evidence=False),
        _pair("success", "failure", first_labels=["evidence_confirms"], second_labels=["asks_for_verdict"]),
        _pair("partial", "cannot_tell"),
        _pair("cannot_tell", "cannot_tell"),
        _pair("success", "partial", same_claim=False),  # another claim of the subject: not a pair
    ]
    numbers = store.summarize_pairs(rows)
    assert {k: v for k, v in numbers.items() if k != "disagreements"} == {
        "pairs": 5,
        "agree": 2,
        "either_cannot_tell": 2,
        "agree_rate": round(2 / 3, 4),
        "matrix": {
            "success": {"success": 1, "failure": 1},
            "failure": {"failure": 1},
            "partial": {"cannot_tell": 1},
            "cannot_tell": {"cannot_tell": 1},
        },
        "evidence_differs": 1,
        "other_claim": 1,
    }
    [disagreement] = numbers["disagreements"]
    assert disagreement == {
        "subject_id": str(rows[2]["subject_id"]),
        "claim_at": NOW.isoformat(),
        "against_grader": "agent",
        "against_grade": "success",
        "first_verdict": "success",
        "second_verdict": "failure",
        "first_confidence": 0.8,
        "second_confidence": 0.6,
        "first_labels": ["evidence_confirms"],
        "second_labels": ["asks_for_verdict"],
        "evidence_differs": False,
    }


def test_no_pairs_give_zeros_and_no_rate():
    assert store.summarize_pairs([]) == {
        "pairs": 0,
        "agree": 0,
        "either_cannot_tell": 0,
        "agree_rate": None,
        "matrix": {},
        "evidence_differs": 0,
        "other_claim": 0,
        "disagreements": [],
    }


def test_at_most_twenty_pair_disagreements_latest_claim_first():
    rows = [_pair("success", "failure", claim_at=NOW - timedelta(minutes=i)) for i in range(25)]
    listed = store.summarize_pairs(rows)["disagreements"]
    assert len(listed) == 20 and listed[0]["claim_at"] == NOW.isoformat()
    assert listed[-1]["claim_at"] == (NOW - timedelta(minutes=19)).isoformat()


@pytest.mark.parametrize(("raw", "days"), [(None, 14), ("", 14), ("1", 1), ("90", 90)])
def test_days_parse(raw, days):
    assert store.parse_days(raw) == days


@pytest.mark.parametrize("raw", ["0", "91", "abc", "-3", "1.5", "٣"])
def test_days_out_of_range_are_refused(raw):
    with pytest.raises(ValueError):
        store.parse_days(raw)


# ---- the route -----------------------------------------------------------------------------------------------------


class RefusingDatabase:
    """A database that fails the test if a session is opened."""

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


async def _get(app, path: str, headers: dict[str, str] | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://nous") as client:
        return await client.get(path, headers=headers or {})


async def test_with_the_judge_off_the_route_answers_disabled_without_a_session(tmp_path):
    response = await _get(_app(tmp_path, RefusingDatabase()), "/dashboard/outside-judge?days=500", OWNER)
    assert (response.status_code, response.json()) == (200, {"enabled": False})


async def test_the_route_needs_the_owners_secret(tmp_path):
    response = await _get(_app(tmp_path, RefusingDatabase(), outside_judge_mode="shadow"), "/dashboard/outside-judge")
    assert (response.status_code, response.json()) == (403, {"error": "forbidden"})


@pytest.mark.parametrize("days", ["0", "91", "x"])
async def test_days_out_of_range_are_a_400_without_a_session(tmp_path, days):
    app = _app(tmp_path, RefusingDatabase(), outside_judge_mode="shadow")
    response = await _get(app, f"/dashboard/outside-judge?days={days}", OWNER)
    assert response.status_code == 400 and "days" in response.json()["error"]


@pytest_asyncio.fixture
async def agent(db):
    name = f"test-f118-route-{uuid.uuid4().hex[:8]}"
    yield name
    async with db.session() as session:
        await session.execute(text("DELETE FROM nous_system.judge_verdicts WHERE agent_id = :a"), {"a": name})
        await session.commit()


async def _seed(db, agent: str, rows: list[dict[str, Any]]) -> None:
    async with db.session() as session:
        for row in rows:
            await session.execute(
                text(
                    "INSERT INTO nous_system.judge_verdicts (agent_id, subject_kind, subject_id, judge_name, "
                    "judge_version, model, status, verdict, confidence, labels, reason, against_grader, against_grade, "
                    "evidence, tokens_in, tokens_out, latency_ms, calls, judged_at, claim_at) VALUES (:agent_id, "
                    "'decision', :subject_id, :judge_name, :judge_version, 'claude-test', :status, :verdict, "
                    ":confidence, CAST(:labels AS jsonb), :reason, :against_grader, :against_grade, "
                    "CAST(:evidence AS jsonb), :tokens_in, :tokens_out, :latency_ms, :calls, :judged_at, :judged_at)"
                ),
                {
                    "agent_id": agent,
                    "judge_name": "background_model",
                    "judge_version": "dj-2",
                    "evidence": json.dumps({"link": "session_id", "note": "never shown"}),
                    **row,
                    "labels": json.dumps(row.get("labels", [])),
                },
            )
        await session.commit()


@pytest.mark.postgres_only
async def test_the_route_reads_the_numbers_of_this_agent_this_judge_and_this_window(tmp_path, db, agent):
    now = datetime.now(UTC)
    today = store.start_of_utc_day(now)  # rows of today, whatever the hour the test runs at
    await _seed(
        db,
        agent,
        [
            _row(judged_at=today + timedelta(seconds=3)),
            _row(verdict="failure", reason="Nothing ran.", judged_at=today + timedelta(seconds=2)),
            _row(
                status="skipped",
                verdict="cannot_tell",
                labels=["evidence_missing"],
                tokens_in=None,
                tokens_out=None,
                latency_ms=None,
                calls=0,
                judged_at=today + timedelta(seconds=1),
            ),
            _row(verdict="failure", judged_at=now - timedelta(days=20)),  # outside the window of 14 days
            {**_row(verdict="failure", judged_at=today), "judge_version": "dj-0"},  # another version
            {**_row(calls=4, judged_at=today), "judge_name": "another_judge"},  # another judge
        ],
    )
    await _seed(db, f"{agent}-other", [_row(verdict="partial", judged_at=today)])
    try:
        judge_slot.set_current(None)
        response = await _get(
            _app(tmp_path, db, agent_id=agent, outside_judge_mode="shadow"), "/dashboard/outside-judge", OWNER
        )
    finally:
        async with db.session() as session:
            await session.execute(
                text("DELETE FROM nous_system.judge_verdicts WHERE agent_id = :a"), {"a": f"{agent}-other"}
            )
            await session.commit()
    assert response.status_code == 200, response.text
    body = response.json()
    assert {k: body[k] for k in ("enabled", "mode", "judge", "version", "model", "days")} == {
        "enabled": True,
        "mode": "shadow",
        "judge": "background_model",
        "version": "dj-2",
        "model": None,  # no judge runs in this process
        "days": 14,
    }
    assert body["decisions"]["judged"] == 2 and body["decisions"]["skipped_by_label"] == {"evidence_missing": 1}
    assert body["decisions"]["by_grader"]["agent"]["agree"] == 1
    assert body["decisions"]["by_grader"]["agent"]["judge_harsher"] == 1
    # Today's calls are this judge's at any version (the limit is per judge): not another judge's four.
    assert body["cost"]["calls"] == 2 and body["cost"]["today"] == {"calls": 3, "cap": 100}
    assert body["runtime"] == {"queued": 0, "dropped": 0, "over_cap": 0, "worker_alive": False}
    [disagreement] = body["disagreements"]
    assert disagreement["reason"] == "Nothing ran." and "evidence" not in disagreement
    assert "never shown" not in response.text

    wide = await _get(
        _app(tmp_path, db, agent_id=agent, outside_judge_mode="shadow"), "/dashboard/outside-judge?days=30", OWNER
    )
    assert wide.json()["decisions"]["judged"] == 3 and wide.json()["days"] == 30


@pytest.mark.postgres_only
async def test_the_route_names_the_running_judge_and_its_queue(tmp_path, db, agent):
    from nous.judge.service import OutsideJudge

    class Stub:
        name, version, model = "stub", "stub-9", "claude-stub"

        async def grade(self, record, *, timeout, max_calls=None):  # pragma: no cover - never called here
            raise AssertionError

    settings = Settings(_env_file=None, agent_id=agent, outside_judge_mode="shadow", outside_judge_daily_calls=7)
    service = OutsideJudge(db, settings, Stub())
    service.submit_decision(uuid.uuid4(), grade="success", note=None, grader="agent", resolved_at=datetime.now(UTC))
    await _seed(db, agent, [{**_row(judged_at=datetime.now(UTC)), "judge_name": "stub", "judge_version": "stub-9"}])
    judge_slot.set_current(service)
    try:
        app = _app(tmp_path, db, agent_id=agent, outside_judge_mode="shadow", outside_judge_daily_calls=7)
        body = (await _get(app, "/dashboard/outside-judge", OWNER)).json()
    finally:
        await service.close()
    assert (body["judge"], body["version"], body["model"]) == ("stub", "stub-9", "claude-stub")
    assert body["runtime"] == {"queued": 1, "dropped": 0, "over_cap": 0, "worker_alive": False}
    assert body["decisions"]["judged"] == 1 and body["cost"]["today"] == {"calls": 1, "cap": 7}


# ---- two judges -----------------------------------------------------------------------------------------------------

FIRST = ("background_model", "dj-2")
SECOND = ("jev", "jq-2:jev-1.14.0")


async def _seed_verdicts(db, agent: str, rows: list[dict[str, Any]]) -> None:
    """Verdict rows with their own judge, ``claim_at`` and ``evidence`` (``_seed`` sets the claim's time to the
    verdict's)."""
    async with db.session() as session:
        for row in rows:
            values = {
                "agent_id": agent,
                "status": "ok",
                "confidence": 0.7,
                "labels": [],
                "against_grader": "agent",
                "against_grade": "success",
                "evidence": {"tool_calls": 1},
                "tokens_in": None,
                "calls": 1,
                "judged_at": datetime.now(UTC),
                **row,
            }
            await session.execute(
                text(
                    "INSERT INTO nous_system.judge_verdicts (agent_id, subject_kind, subject_id, judge_name, "
                    "judge_version, status, verdict, confidence, labels, against_grader, against_grade, claim_at, "
                    "evidence, tokens_in, calls, judged_at) VALUES (:agent_id, 'decision', :subject_id, :judge_name, "
                    ":judge_version, :status, :verdict, :confidence, CAST(:labels AS jsonb), :against_grader, "
                    ":against_grade, :claim_at, CAST(:evidence AS jsonb), :tokens_in, :calls, :judged_at)"
                ),
                {**values, "labels": json.dumps(values["labels"]), "evidence": json.dumps(values["evidence"])},
            )
        await session.commit()


def _both(first: str, second: str, claim_at: datetime, **over: Any) -> list[dict[str, Any]]:
    """The two judges' rows on one new subject, on the same claim unless ``over`` says otherwise."""
    subject_id = uuid.uuid4()
    first_row = {"subject_id": subject_id, "judge_name": FIRST[0], "judge_version": FIRST[1], "verdict": first}
    second_row = {"subject_id": subject_id, "judge_name": SECOND[0], "judge_version": SECOND[1], "verdict": second}
    return [{**first_row, "claim_at": claim_at}, {**second_row, "claim_at": claim_at, **over}]


@pytest.mark.postgres_only
async def test_pair_numbers_pair_ok_rows_of_both_judges_on_the_same_claim(db, agent):
    now = datetime.now(UTC)
    at = now - timedelta(hours=1)
    rows = [
        *_both("success", "success", at),  # agree
        *_both("failure", "success", at - timedelta(minutes=1)),  # disagree
        *_both("partial", "cannot_tell", at),  # one cannot_tell
        *_both("success", "cannot_tell", at, status="error", labels=["judge_error"]),  # an error row: not paired
        *_both("success", "failure", at, judge_version="jq-0:jev-1.13.0"),  # another version: not paired
        *_both("success", "failure", at + timedelta(seconds=5)),  # different claim times below
        *_both("failure", "failure", at, evidence={"tool_calls": 2}),  # the judges read different records
        *_both("failure", "success", at, judged_at=now - timedelta(days=20)),  # outside the window
    ]
    rows[11]["claim_at"] = at + timedelta(seconds=9)  # the second judge's row of the sixth subject: another claim
    await _seed_verdicts(db, agent, rows)
    await _seed_verdicts(db, f"{agent}-other", _both("success", "failure", at))  # another agent: not read
    try:
        async with db.session() as session:
            numbers = await store.pair_numbers(session, agent, days=14, first=FIRST, second=SECOND, now=now)
    finally:
        async with db.session() as session:
            await session.execute(
                text("DELETE FROM nous_system.judge_verdicts WHERE agent_id = :a"), {"a": f"{agent}-other"}
            )
            await session.commit()
    assert {k: v for k, v in numbers.items() if k != "disagreements"} == {
        "pairs": 4,
        "agree": 2,
        "either_cannot_tell": 1,
        "agree_rate": round(2 / 3, 4),
        "matrix": {"success": {"success": 1}, "failure": {"success": 1, "failure": 1}, "partial": {"cannot_tell": 1}},
        "evidence_differs": 1,
        "other_claim": 1,
    }
    [disagreement] = numbers["disagreements"]
    assert (disagreement["first_verdict"], disagreement["second_verdict"]) == ("failure", "success")
    assert disagreement["subject_id"] == str(rows[2]["subject_id"])
    assert disagreement["claim_at"] == (at - timedelta(minutes=1)).isoformat()


class _StubJudge:
    name, version, model = "stub", "stub-9", "claude-stub"

    async def grade(self, record, *, timeout, max_calls=None):  # pragma: no cover - never called here
        raise AssertionError


JEV_KEY = "ts-route-" + "R5" * 16


def _refusing_jev() -> Any:
    from nous.judge.jev_judge import JevJudge

    def refuse(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never called here
        raise AssertionError("the route sends no request")

    return JevJudge(JEV_KEY, "jev-1.14.0", transport=httpx.MockTransport(refuse))


# The answer of PR 1 for one stub row: every key and value (the route adds nothing with one judge).
def _one_judge_answer(runtime: dict[str, Any]) -> dict[str, Any]:
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
            "today": {"calls": 1, "cap": 7},
        },
        "disagreements": [],
        "runtime": runtime,
    }


@pytest.mark.postgres_only
@pytest.mark.parametrize("installed", ["the first judge", "a set without a second judge"])
@pytest.mark.parametrize("second", ["", "jev"])
async def test_with_one_judge_running_the_answer_is_pr_1s_key_for_key(tmp_path, db, agent, second, installed):
    """With no second judge running (the switch empty, or set but the second judge did not start, or a set whose
    second judge was taken away), the answer is exactly PR 1's."""
    from nous.judge.service import JudgeSet, OutsideJudge

    over = {"agent_id": agent, "outside_judge_mode": "shadow", "outside_judge_daily_calls": 7}
    settings = Settings(_env_file=None, outside_judge_second=second, **over)
    first = OutsideJudge(db, settings, _StubJudge())
    service = first if installed == "the first judge" else JudgeSet(first, None)
    await _seed(db, agent, [{**_row(judged_at=datetime.now(UTC)), "judge_name": "stub", "judge_version": "stub-9"}])
    judge_slot.set_current(service)
    try:
        response = await _get(
            _app(tmp_path, db, outside_judge_second=second, **over), "/dashboard/outside-judge", OWNER
        )
    finally:
        judge_slot.set_current(None)
        await service.close()
    runtime = {"queued": 0, "dropped": 0, "over_cap": 0, "worker_alive": False}
    assert response.json() == _one_judge_answer(runtime)


@pytest.mark.postgres_only
async def test_with_two_judges_the_answer_adds_the_seconds_numbers_and_how_the_two_compare(tmp_path, db, agent):
    from nous.judge.service import JudgeSet, OutsideJudge

    over = {"agent_id": agent, "outside_judge_mode": "shadow", "outside_judge_daily_calls": 7}
    settings = Settings(_env_file=None, outside_judge_second="jev", TYPESAFE_API_KEY=JEV_KEY, **over)
    # The second judge's own daily limit (3) is the one its block shows; the first judge's is the setting (7).
    both = JudgeSet(
        OutsideJudge(db, settings, _StubJudge()), OutsideJudge(db, settings, _refusing_jev(), daily_calls=3)
    )
    both.second.submit_decision(uuid.uuid4(), grade="success", note=None, grader="agent", resolved_at=datetime.now(UTC))
    claim_at = datetime.now(UTC) - timedelta(minutes=5)
    agree, differ = uuid.uuid4(), uuid.uuid4()
    first = {"judge_name": "stub", "judge_version": "stub-9", "claim_at": claim_at, "tokens_in": 1000}
    second = {"judge_name": "jev", "judge_version": both.second.judge.version, "claim_at": claim_at, "tokens_in": 4100}
    await _seed_verdicts(
        db,
        agent,
        [
            {**first, "subject_id": agree, "verdict": "success"},
            {**second, "subject_id": agree, "verdict": "success", "labels": ["asks_for_verdict"]},
            {**first, "subject_id": differ, "verdict": "success"},
            {**second, "subject_id": differ, "verdict": "failure", "confidence": 0.9},
        ],
    )
    judge_slot.set_current(both)
    try:
        app = _app(tmp_path, db, outside_judge_second="jev", TYPESAFE_API_KEY=JEV_KEY, **over)
        response = await _get(app, "/dashboard/outside-judge", OWNER)
    finally:
        judge_slot.set_current(None)
        await both.close()
    body = response.json()
    assert set(body) == set(_one_judge_answer({})) | {"second", "between_judges"}
    assert (body["judge"], body["version"], body["model"]) == ("stub", "stub-9", "claude-stub")
    assert body["decisions"]["judged"] == 2 and body["cost"]["today"] == {"calls": 2, "cap": 7}
    assert body["runtime"] == {
        "queued": 0,
        "dropped": 0,
        "over_cap": 0,
        "worker_alive": False,
        "second": {"queued": 1, "dropped": 0, "over_cap": 0, "worker_alive": False},
    }
    second_block = body["second"]
    assert set(second_block) == {"judge", "version", "model", "decisions", "cost", "disagreements"}
    assert (second_block["judge"], second_block["version"], second_block["model"]) == (
        "jev",
        f"{jev_judge.QUESTION_VERSION}:jev-1.14.0",
        "jev-1.14.0",
    )
    assert second_block["decisions"]["by_verdict"] == {"success": 1, "failure": 1}
    assert second_block["cost"]["tokens_in"] == 8200 and second_block["cost"]["today"] == {"calls": 2, "cap": 3}
    [disagreement] = second_block["disagreements"]
    assert (disagreement["subject_id"], disagreement["verdict"], disagreement["reason"]) == (
        str(differ),
        "failure",
        None,
    )
    between = body["between_judges"]
    assert {k: v for k, v in between.items() if k != "disagreements"} == {
        "pairs": 2,
        "agree": 1,
        "either_cannot_tell": 0,
        "agree_rate": 0.5,
        "matrix": {"success": {"success": 1, "failure": 1}},
        "evidence_differs": 0,
        "other_claim": 0,
    }
    [pair] = between["disagreements"]
    assert (pair["subject_id"], pair["first_verdict"], pair["second_verdict"]) == (str(differ), "success", "failure")
    assert JEV_KEY not in response.text


@pytest.mark.postgres_only
@pytest.mark.parametrize("failing", ["the second judge's numbers", "the comparison"])
async def test_when_the_second_judges_numbers_cannot_be_read_the_first_judges_are_still_returned(
    tmp_path, db, agent, monkeypatch, caplog, failing
):
    import logging

    from nous.judge.service import JudgeSet, OutsideJudge

    over = {"agent_id": agent, "outside_judge_mode": "shadow", "outside_judge_daily_calls": 7}
    settings = Settings(_env_file=None, **over)
    both = JudgeSet(OutsideJudge(db, settings, _StubJudge()), OutsideJudge(db, settings, _refusing_jev()))
    await _seed(db, agent, [{**_row(judged_at=datetime.now(UTC)), "judge_name": "stub", "judge_version": "stub-9"}])
    real_numbers = store.get_numbers

    async def second_numbers_fail(session: Any, agent_id: str, **kwargs: Any) -> dict[str, Any]:
        if kwargs["judge_name"] == "jev":
            raise ConnectionError("the read failed")
        return await real_numbers(session, agent_id, **kwargs)

    async def comparison_fails(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise ConnectionError("the read failed")

    if failing == "the comparison":
        monkeypatch.setattr(store, "pair_numbers", comparison_fails)
    else:
        monkeypatch.setattr(store, "get_numbers", second_numbers_fail)
    judge_slot.set_current(both)
    try:
        with caplog.at_level(logging.WARNING, logger="nous.api.rest"):
            response = await _get(_app(tmp_path, db, **over), "/dashboard/outside-judge", OWNER)
    finally:
        judge_slot.set_current(None)
        await both.close()
    assert response.status_code == 200
    idle = {"queued": 0, "dropped": 0, "over_cap": 0, "worker_alive": False}
    assert response.json() == _one_judge_answer({**idle, "second": idle})
    [line] = [r.getMessage() for r in caplog.records if r.name == "nous.api.rest" and r.levelno >= logging.WARNING]
    assert "ConnectionError" in line and "the read failed" not in line


@pytest.mark.postgres_only
async def test_with_two_judges_the_route_still_needs_the_owners_secret(tmp_path, db, agent):
    from nous.judge.service import JudgeSet, OutsideJudge

    settings = Settings(_env_file=None, agent_id=agent, outside_judge_mode="shadow")
    both = JudgeSet(OutsideJudge(db, settings, _StubJudge()), OutsideJudge(db, settings, _refusing_jev()))
    judge_slot.set_current(both)
    try:
        response = await _get(
            _app(tmp_path, RefusingDatabase(), outside_judge_mode="shadow"), "/dashboard/outside-judge"
        )
    finally:
        judge_slot.set_current(None)
        await both.close()
    assert (response.status_code, response.json()) == (403, {"error": "forbidden"})


@pytest.mark.postgres_only
async def test_pair_numbers_list_the_latest_twenty_disagreements(db, agent):
    at = datetime.now(UTC) - timedelta(hours=1)
    rows = [row for i in range(25) for row in _both("success", "failure", at - timedelta(minutes=i))]
    await _seed_verdicts(db, agent, rows)
    async with db.session() as session:
        numbers = await store.pair_numbers(session, agent, days=14, first=FIRST, second=SECOND)
    assert numbers["pairs"] == 25 and len(numbers["disagreements"]) == 20
    assert numbers["disagreements"][0]["claim_at"] == at.isoformat()
