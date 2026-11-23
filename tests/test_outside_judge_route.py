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
from nous.judge import store

OWNER_VALUE = "owner-only-test-value"
OWNER = {"X-Nous-Owner-Secret": OWNER_VALUE}
NOW = datetime(2026, 11, 22, 15, 0, tzinfo=UTC)


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
                    "evidence, tokens_in, tokens_out, latency_ms, calls, judged_at) VALUES (:agent_id, 'decision', "
                    ":subject_id, :judge_name, :judge_version, 'claude-test', :status, :verdict, :confidence, "
                    "CAST(:labels AS jsonb), :reason, :against_grader, :against_grade, CAST(:evidence AS jsonb), "
                    ":tokens_in, :tokens_out, :latency_ms, :calls, :judged_at)"
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
    # Today's calls count every row of today at any version (the cap is per agent, not per judge).
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

        async def grade(self, record, *, timeout):  # pragma: no cover - never called here
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
