"""F118: the second judge's own hourly pass of graded decisions (real Postgres, stub judges, and the real second judge
with a fake transport; no request leaves the process).

With ``NOUS_OUTSIDE_JUDGE_REPORTS_ENABLED`` the second judge's service is built with ``sweep=True`` and runs the same
pass as the first judge's, over decisions only, at its own name, version and daily limit: at most five a pass in the
sweep's order, nothing queued once its calls today reach half its limit, a failed one tried again after six hours and
at most three times in all. A decision graded by anyone but the agent is sent without that grade. The pass starts and
stops with its service through every start, swap and shutdown of the settings manager, and neither judge's pass
changes the other's.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from test_judge_second_manager import BOX, KEY, FixedKeyFile, _sealed
from test_outside_judge_sweep import FAST, NOW, StubJudge, _decision, _pass, _service, _settings, _task, _until

from nous.config import Settings
from nous.judge import evidence, jev_judge, second_settings, store
from nous.judge.jev_judge import JevJudge
from nous.judge.second_settings import SecondJudgeManager
from nous.judge.service import DECISION, SWEEP_BATCH, JudgeSet, OutsideJudge

pytestmark = pytest.mark.postgres_only

MODEL = "jev-1.14.0"
VERSION = f"{jev_judge.QUESTION_VERSION}:{MODEL}"
TEST_KEY = "ts-sweep-" + "W4" * 12
ANSWER = {
    "model": MODEL,
    "answers": {
        "verdict": {"type": "choice", "choice": "success", "confidence": 0.7},
        "sets_verdict": {"type": "noul", "noul": 0.1},
    },
    "usage": {"input_tokens": 1000, "output_tokens": 2},
}
NUMBERS_AT_START = {
    "passes": 0,
    "queued_decisions": {},
    "queued_reports": 0,
    "held_at_half_limit": 0,
    "failed": 0,
    "last_pass_at": None,
}


@pytest.fixture(autouse=True)
def _no_request_leaves_the_process(monkeypatch):
    async def refuse(self: Any, request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"a real request was attempted to {request.url.host}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)


@pytest_asyncio.fixture
async def agent(db):
    name = f"test-f118-ssw-{uuid.uuid4().hex[:8]}"
    yield name
    async with db.session() as session:
        for table in (
            "brain.decisions",
            "heart.subtasks",
            "nous_system.tool_call_log",
            "nous_system.judge_verdicts",
            "nous_system.judge_second_settings",
        ):
            await session.execute(text(f"DELETE FROM {table} WHERE agent_id LIKE :a"), {"a": f"{name}%"})
        await session.commit()


def _second(db, agent: str, judge: Any = None, *, daily_calls: int = 100) -> tuple[OutsideJudge, Any]:
    """The second judge's service as main.py and the settings manager build it with the setting on: no report judge,
    its own daily limit, and the pass."""
    judge = judge if judge is not None else StubJudge(name="jev", version=VERSION)
    return OutsideJudge(db, _settings(agent), judge, daily_calls=daily_calls, sweep=True, **FAST), judge


async def _verdict(
    db,
    agent: str,
    subject_id: uuid.UUID,
    *,
    name: str,
    version: str,
    status: str = "ok",
    attempts: int = 1,
    calls: int = 1,
    judged_at: datetime | None = None,
) -> None:
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO nous_system.judge_verdicts (agent_id, subject_kind, subject_id, judge_name, "
                "judge_version, status, verdict, claim_at, calls, attempts, judged_at) VALUES (:a, 'decision', :s, :n, "
                ":v, :st, :vd, now(), :c, :at, COALESCE(:j, now()))"
            ),
            {
                "a": agent,
                "s": subject_id,
                "n": name,
                "v": version,
                "st": status,
                "vd": "success" if status == "ok" else "cannot_tell",
                "c": calls,
                "at": attempts,
                "j": judged_at,
            },
        )
        await session.commit()


async def _calls_today(db, agent: str, name: str, calls: int) -> None:
    """Today's requests of the judge ``name``, on a subject of their own."""
    await _verdict(db, agent, uuid.uuid4(), name=name, version="earlier", calls=calls)


async def _rows(db, agent: str) -> list[dict]:
    async with db.session() as session:
        result = await session.execute(
            text(
                "SELECT judge_name, judge_version, subject_id, status, against_grader, against_grade, claim_at FROM "
                "nous_system.judge_verdicts WHERE agent_id = :a AND calls > 0 ORDER BY judged_at"
            ),
            {"a": agent},
        )
        return [dict(row._mapping) for row in result]


def _pending(service: OutsideJudge) -> set[uuid.UUID]:
    return {subject_id for _kind, subject_id in service._pending}


# ---- what the second judge's pass takes -----------------------------------------------------------------------------


async def test_the_second_judges_pass_takes_the_sweeps_order_at_most_five_a_pass(db, agent):
    owner_old = await _decision(db, agent, "a2ui", reviewed_at=NOW - timedelta(days=20))
    owner_new = await _decision(db, agent, "a2ui", reviewed_at=NOW - timedelta(hours=2))
    lost = await _decision(db, agent, "agent", reviewed_at=NOW - timedelta(days=1))
    external = await _decision(db, agent, "external", reviewed_at=NOW - timedelta(minutes=30))
    auto = [await _decision(db, agent, "auto", reviewed_at=NOW - timedelta(minutes=m)) for m in (1, 2)]
    second, judge = _second(db, agent)
    second.start()
    try:
        assert await _pass(second) == SWEEP_BATCH
        assert [record.subject_id for record in judge.records] == [owner_new, owner_old, lost, external, auto[0]]
        assert await _pass(second) == 1
        assert judge.records[-1].subject_id == auto[1]
        assert await _pass(second) == 0
    finally:
        await second.close()
    numbers = second.sweep_numbers()
    assert numbers["passes"] == 3 and numbers["queued_reports"] == 0
    assert numbers["queued_decisions"] == {"a2ui": 2, "agent": 1, "external": 1, "auto": 2}
    assert {(row["judge_name"], row["judge_version"]) for row in await _rows(db, agent)} == {("jev", VERSION)}


async def test_the_second_judge_takes_what_it_has_not_judged_at_its_own_name_and_version(db, agent):
    first_only, both, old_version, second_only = [await _decision(db, agent, "a2ui") for _ in range(4)]
    first, first_judge, _reports = _service(db, agent)
    await _verdict(db, agent, first_only, name=first_judge.name, version=first_judge.version)
    for name, version in ((first_judge.name, first_judge.version), ("jev", VERSION)):
        await _verdict(db, agent, both, name=name, version=version)
    await _verdict(db, agent, old_version, name="jev", version=f"jq-1:{MODEL}")
    await _verdict(db, agent, second_only, name="jev", version=VERSION)
    second, _judge = _second(db, agent)
    try:
        assert await second.sweep_once() == 2 and await first.sweep_once() == 2
        assert _pending(second) == {first_only, old_version}
        assert _pending(first) == {old_version, second_only}
    finally:
        await first.close()
        await second.close()


async def test_the_second_judges_pass_never_takes_a_task_report(db, agent):
    decision_id = await _decision(db, agent, "a2ui")
    await _task(db, agent)
    second, judge = _second(db, agent)
    assert second.report_judge is None
    second.start()
    try:
        assert await _pass(second) == 1
    finally:
        await second.close()
    assert [(record.kind, record.subject_id) for record in judge.records] == [(DECISION, decision_id)]
    assert second.sweep_numbers()["queued_reports"] == 0
    assert [row["subject_id"] for row in await _rows(db, agent)] == [decision_id]


# ---- its own limit --------------------------------------------------------------------------------------------------


async def test_the_second_judges_pass_holds_at_half_of_its_own_limit_counted_from_its_own_calls(db, agent):
    await _decision(db, agent, "a2ui")
    first, first_judge, _r = _service(db, agent)  # the server's limit: 100
    await _calls_today(db, agent, first_judge.name, 99)
    second, _j = _second(db, agent, daily_calls=100)
    try:
        assert await first.sweep_once() == 0 and first.sweep_numbers()["held_at_half_limit"] == 1
        assert await second.sweep_once() == 1 and second.sweep_numbers()["held_at_half_limit"] == 0
    finally:
        await first.close()
        await second.close()
    await _calls_today(db, agent, "jev", 50)
    held, _j = _second(db, agent, daily_calls=100)
    assert await held.sweep_once() == 0 and held.sweep_numbers()["held_at_half_limit"] == 1 and held.idle
    await held.close()
    small = f"{agent}-small"
    await _decision(db, small, "a2ui")
    await _calls_today(db, small, "jev", 5)
    at_half, _j = _second(db, small, daily_calls=10)
    below_half, _j = _second(db, small, daily_calls=11)
    try:
        assert await at_half.sweep_once() == 0 and at_half.sweep_numbers()["held_at_half_limit"] == 1
        assert await below_half.sweep_once() == 1
    finally:
        await at_half.close()
        await below_half.close()


async def test_a_second_pass_that_cannot_count_its_calls_queues_nothing_and_the_first_pass_runs_on(
    db, agent, monkeypatch, caplog
):
    decision_id = await _decision(db, agent, "a2ui")
    real = store.calls_today

    async def the_second_judges_count_fails(session, agent_id, now, judge_name=None, subject_kind=None):
        if judge_name == "jev":
            raise RuntimeError("the count of the second judge failed")
        return await real(session, agent_id, now, judge_name=judge_name, subject_kind=subject_kind)

    monkeypatch.setattr(store, "calls_today", the_second_judges_count_fails)
    first, _d, _r = _service(db, agent)
    second, _j = _second(db, agent)
    try:
        with caplog.at_level(logging.WARNING, logger="nous.judge.service"):
            assert await second.sweep_once() == 0
            assert await first.sweep_once() == 1
    finally:
        await first.close()
        await second.close()
    assert second.sweep_numbers()["failed"] == 1 and second.idle
    assert first.sweep_numbers()["failed"] == 0 and decision_id not in _pending(second)
    warnings = [
        r.getMessage() for r in caplog.records if r.name == "nous.judge.service" and r.levelno >= logging.WARNING
    ]
    assert warnings == ["F118: a sweep pass failed (RuntimeError)"]
    assert "the count of the second judge failed" not in caplog.text


# ---- retries, per judge ---------------------------------------------------------------------------------------------


async def test_the_retry_wait_and_the_attempts_are_counted_per_judge(db, agent):
    held_for_second, held_for_first, spent = [await _decision(db, agent, "a2ui") for _ in range(3)]
    first, first_judge, _r = _service(db, agent)
    second, _j = _second(db, agent)
    failed_at = NOW - timedelta(hours=1)
    await _verdict(db, agent, held_for_second, name="jev", version=VERSION, status="error", judged_at=failed_at)
    first_name, first_version = first_judge.name, first_judge.version
    await _verdict(
        db, agent, held_for_first, name=first_name, version=first_version, status="timeout", judged_at=failed_at
    )
    spent_at = NOW - timedelta(days=1)
    await _verdict(
        db, agent, spent, name="jev", version=VERSION, status="error", attempts=store.MAX_ATTEMPTS, judged_at=spent_at
    )
    try:
        # Within the wait each judge holds only the decision its own attempt failed on.
        assert await second.sweep_once(now=NOW) == 1 and _pending(second) == {held_for_first}
        assert await first.sweep_once(now=NOW) == 2 and _pending(first) == {held_for_second, spent}
        # After the wait the second judge tries its failed one again; one at its last attempt is never taken.
        after_the_wait = failed_at + store.SWEEP_RETRY_AFTER + timedelta(minutes=1)
        assert await second.sweep_once(now=after_the_wait) == 1
        assert _pending(second) == {held_for_first, held_for_second} and spent not in _pending(second)
    finally:
        await first.close()
        await second.close()


# ---- what it sends --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("reviewer", ["a2ui", "external", "auto"])
async def test_the_second_judge_reads_a_decision_graded_by_another_without_that_grade(db, agent, reviewer):
    reviewed = NOW - timedelta(hours=3)
    await _decision(db, agent, reviewer, outcome="partial", reviewed_at=reviewed, note="only half of it moved")
    second, judge = _second(db, agent)
    second.start()
    try:
        await _pass(second)
    finally:
        await second.close()
    [record] = judge.records
    assert record.claim is None
    rendered = evidence.render_record(record)
    assert json.loads(rendered)["written_by_author"]["claim"] is None and "only half of it moved" not in rendered
    [row] = await _rows(db, agent)
    assert (row["judge_name"], row["against_grader"], row["against_grade"], row["claim_at"]) == (
        "jev",
        reviewer,
        "partial",
        reviewed,
    )


async def test_the_second_judge_reads_the_agents_own_grade_as_it_stands_at_the_pass(db, agent):
    decision_id = await _decision(db, agent, "agent", reviewed_at=NOW - timedelta(hours=5), note="it ran")
    regraded = NOW - timedelta(hours=2)
    async with db.session() as session:
        await session.execute(
            text(
                "UPDATE brain.decisions SET outcome = 'partial', outcome_result = 'half of it', reviewed_at = :r "
                "WHERE id = :id"
            ),
            {"r": regraded, "id": decision_id},
        )
        await session.commit()
    second, judge = _second(db, agent)
    second.start()
    try:
        await _pass(second)
    finally:
        await second.close()
    [record] = judge.records
    assert (record.claim.grader, record.claim.grade, record.claim.note) == ("agent", "partial", "half of it")
    [row] = await _rows(db, agent)
    assert (row["against_grader"], row["against_grade"], row["claim_at"]) == ("agent", "partial", regraded)


async def test_the_real_second_judge_sends_an_owner_graded_decision_with_a_null_claim(db, agent):
    sent: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=ANSWER)

    await _decision(db, agent, "a2ui", outcome="failure", note="the backup did not move")
    jev = JevJudge(TEST_KEY, MODEL, transport=httpx.MockTransport(answer))
    second, _j = _second(db, agent, jev)
    second.start()
    try:
        assert await _pass(second) == 1
    finally:
        await second.close()
    [request] = sent
    body = json.loads(request.content)
    assert body["state"]["written_by_author"]["claim"] is None
    assert "the backup did not move" not in request.content.decode("utf-8")
    assert body["questions"] == jev_judge.questions()
    # The record is the bounded one: the body is at most the record's bound, the questions and the model's name.
    questions = json.dumps(body["questions"], separators=(",", ":"))
    assert len(request.content) <= evidence.EVIDENCE_MAX_CHARS + len(questions) + len(MODEL) + 64
    [row] = await _rows(db, agent)
    assert (row["judge_version"], row["status"], row["against_grader"]) == (VERSION, "ok", "a2ui")


async def test_a_decision_both_passes_took_is_a_pair_and_one_the_first_judge_had_live_is_not(db, agent):
    """``between_judges`` pairs verdicts on the same claim time. Both passes give an owner-graded decision its
    ``reviewed_at``; a decision the first judge had at the resolve (the resolve's time) and the second judge takes by
    its pass (``reviewed_at``) is counted as ``other_claim``."""
    await _decision(db, agent, "a2ui", reviewed_at=NOW - timedelta(hours=2))
    live = await _decision(db, agent, "agent", reviewed_at=NOW - timedelta(hours=1))
    first, first_judge, _r = _service(db, agent)
    second, _j = _second(db, agent)
    resolved_at = NOW - timedelta(minutes=59)
    assert first.submit_decision(live, grade="success", note="it ran", grader="agent", resolved_at=resolved_at)
    first.start()
    second.start()
    try:
        assert await first.sweep_once() == 1  # the owner's; the agent's is already in its line
        assert await second.sweep_once() == 2
        await _until(lambda: first.idle and second.idle)
    finally:
        await first.close()
        await second.close()
    async with db.session() as session:
        numbers = await store.pair_numbers(
            session, agent, days=14, first=(first_judge.name, first_judge.version), second=("jev", VERSION)
        )
    assert (numbers["pairs"], numbers["other_claim"]) == (1, 1)


# ---- isolation ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("failing", ["jev", "stub"])
async def test_a_failing_pass_of_one_judge_leaves_the_others_pass_untouched(db, agent, monkeypatch, failing):
    decision_id = await _decision(db, agent, "a2ui")
    real = store.sweep_decisions

    async def fails_for_one(session, agent_id, *, judge_name, **kwargs):
        if judge_name == failing:
            raise RuntimeError("the read failed")
        return await real(session, agent_id, judge_name=judge_name, **kwargs)

    monkeypatch.setattr(store, "sweep_decisions", fails_for_one)
    first, first_judge, _r = _service(db, agent)
    second, second_judge = _second(db, agent)
    first.start()
    second.start()
    try:
        queued = {first_judge.name: await first.sweep_once(), second_judge.name: await second.sweep_once()}
        await _until(lambda: first.idle and second.idle)
        services = {first_judge.name: (first, first_judge), second_judge.name: (second, second_judge)}
        working = next(name for name in services if name != failing)
        assert queued == {failing: 0, working: 1}
        assert services[failing][0].sweep_numbers()["failed"] == 1 and services[failing][1].records == []
        assert services[working][0].sweep_numbers()["failed"] == 0
        assert [record.subject_id for record in services[working][1].records] == [decision_id]
        assert not first._sweeper.done() and not second._sweeper.done()  # both loops run on
    finally:
        await first.close()
        await second.close()


async def test_a_closed_second_judge_takes_nothing_and_the_first_still_takes_decisions(db, agent):
    decision_id = await _decision(db, agent, "a2ui")
    first, _d, _r = _service(db, agent)
    second, _j = _second(db, agent)
    both = JudgeSet(first, second)
    second.start()
    await second.close()
    assert await second.sweep_once() == 0 and second.idle
    assert both.submit_decision(decision_id, grade="success", note=None, grader="agent", resolved_at=NOW) is True
    assert _pending(first) == {decision_id} and second.runtime()["dropped"] == 1
    await first.close()


async def test_a_provider_failure_writes_an_error_row_for_the_second_judge_only(db, agent):
    decision_id = await _decision(db, agent, "a2ui")
    jev = JevJudge(TEST_KEY, MODEL, transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    first, _d, _r = _service(db, agent)
    second, _j = _second(db, agent, jev)
    first.start()
    second.start()
    try:
        assert await first.sweep_once() == 1 and await second.sweep_once() == 1
        await _until(lambda: first.idle and second.idle)
    finally:
        await first.close()
        await second.close()
    rows = {(row["judge_name"], row["status"], row["subject_id"]) for row in await _rows(db, agent)}
    assert rows == {("stub", "ok", decision_id), ("jev", "error", decision_id)}
    assert first.sweep_numbers()["failed"] == 0 and second.sweep_numbers()["failed"] == 0


# ---- the pass through the settings manager --------------------------------------------------------------------------


class Managed:
    """One test's settings manager, its judge set, the real first judge and every second judge's service it built."""

    def __init__(self, manager: SecondJudgeManager, judges: JudgeSet | None, primary: OutsideJudge | None) -> None:
        self.manager, self.judges, self.primary = manager, judges, primary
        self.built: list[OutsideJudge] = []
        self.sent: list[httpx.Request] = []


@pytest_asyncio.fixture
async def managed(db, agent, monkeypatch):
    made: list[Managed] = []

    async def build(
        *,
        reports: bool = True,
        mode: str = "shadow",
        row: dict[str, Any] | None = None,
        key: str | None = KEY,
        settle: tuple[float, ...] = (),
    ) -> Managed:
        """The manager over a real first judge, loaded from ``row`` (with ``key`` sealed). Each second judge's service
        it builds is recorded; the n-th waits ``settle[n]`` before judging (0 when not given)."""
        settings = Settings(
            _env_file=None,
            agent_id=agent,
            outside_judge_mode=mode,
            outside_judge_reports_enabled=reports,
            outside_judge_settings_enabled=True,
        )
        primary = judges = None
        if mode == "shadow":
            report_judge = StubJudge(name="background_model", version="rj-1", verdict="supported") if reports else None
            primary = OutsideJudge(
                db, settings, StubJudge(name="background_model", version="dj-2"), report_judge=report_judge, **FAST
            )
            primary.start()
            judges = JudgeSet(primary, None)
        values = {**(row or {}), **(_sealed(agent, key) if key else {})}
        if values:
            async with db.session() as session:
                await second_settings.upsert_row(session, agent, values, "owner")
                await session.commit()
        env: Managed

        def answer(request: httpx.Request) -> httpx.Response:
            env.sent.append(request)
            return httpx.Response(200, json=ANSWER)

        manager = SecondJudgeManager(
            db, settings, judges, transport=httpx.MockTransport(answer), key_file=FixedKeyFile(BOX)
        )
        env = Managed(manager, judges, primary)
        real = manager._make_service

        def recording(config: Any) -> OutsideJudge:
            service = real(config)
            service._settle = settle[len(env.built)] if len(env.built) < len(settle) else 0.0
            env.built.append(service)
            return service

        monkeypatch.setattr(manager, "_make_service", recording)
        made.append(env)
        await manager.load()
        return env

    yield build
    for env in made:
        await env.manager.close()
        if env.judges is not None:
            await env.judges.close()
        for service in env.built:  # a replaced one is closed already; closing again does nothing
            await service.close()


async def test_with_the_setting_on_the_second_judge_the_settings_start_runs_its_own_pass(managed):
    env = await managed(row={"enabled": True})
    [second] = env.built
    assert env.judges.second is second and second.report_judge is None
    assert second._sweeper is not None and not second._sweeper.done()
    assert env.judges.second_sweep_numbers() == NUMBERS_AT_START


@pytest.mark.parametrize(
    ("mode", "row", "key"),
    [
        ("shadow", {"enabled": False}, KEY),
        ("shadow", {"enabled": True}, None),
        ("shadow", {"enabled": True, "daily_calls": 0}, KEY),
        ("off", {"enabled": True}, KEY),
    ],
    ids=["switched off", "no key", "a limit of 0", "the mode off"],
)
async def test_while_the_second_judge_does_not_send_no_service_and_no_pass_is_built(managed, mode, row, key):
    env = await managed(mode=mode, row=row, key=key)
    assert env.built == []
    assert env.judges is None or (env.judges.second is None and env.judges.second_sweep_numbers() is None)


async def test_removing_the_key_stops_the_second_judge_and_its_pass(managed):
    env = await managed(row={"enabled": True})
    [service] = env.built
    answer = await env.manager.remove_key(confirmed=None, actor="owner")
    assert answer.status == 200 and env.judges.second is None and env.judges.second_sweep_numbers() is None
    assert service._closed and service._sweeper.done() and len(env.built) == 1


async def test_with_the_setting_off_the_second_judge_runs_without_a_pass(managed):
    env = await managed(reports=False, row={"enabled": True})
    [second] = env.built
    assert env.judges.second is second
    assert second._sweeper is None and second.sweep_numbers() is None and env.judges.second_sweep_numbers() is None


async def test_a_swap_stops_the_old_judges_pass_and_starts_the_new_ones_and_the_first_judges_runs_on(managed):
    env = await managed(row={"enabled": True})
    [old] = env.built
    first_sweeper = env.primary._sweeper
    answer = await env.manager.patch({"model": "jev-1.15.0"}, confirmed=None, actor="owner")
    assert answer.status == 200
    [_old, new] = env.built
    assert env.judges.second is new and new.judge.model == "jev-1.15.0"
    assert old._closed and old._sweeper.done()
    assert new._sweeper is not None and not new._sweeper.done() and new._sweeper is not old._sweeper
    assert env.primary._sweeper is first_sweeper and not first_sweeper.done()


async def test_two_quick_swaps_leave_exactly_one_pass_running(managed):
    env = await managed(row={"enabled": True})
    answers = await asyncio.gather(
        env.manager.patch({"model": "jev-1.15.0"}, confirmed=None, actor="owner"),
        env.manager.patch({"model": "jev-1.16.0"}, confirmed=None, actor="owner"),
    )
    assert [answer.status for answer in answers] == [200, 200] and len(env.built) == 3
    current = env.judges.second
    unchanged = await env.manager.patch({"model": current.judge.model}, confirmed=None, actor="owner")
    assert unchanged.status == 200 and unchanged.body["changed"] is False and len(env.built) == 3
    assert [service for service in env.built if not service._sweeper.done()] == [current]
    assert all(service._closed and service._sweeper.done() for service in env.built if service is not current)


async def test_at_shutdown_every_pass_of_the_second_judge_has_stopped(managed):
    env = await managed(row={"enabled": True})
    assert (await env.manager.patch({"model": "jev-1.15.0"}, confirmed=None, actor="owner")).status == 200
    await env.manager.close()  # as main.py shuts down: the manager first, then the judges
    await env.judges.close()
    assert len(env.built) == 2 and all(service._closed and service._sweeper.done() for service in env.built)
    assert env.primary._sweeper.done()


async def test_a_decision_swept_across_a_swap_is_sent_once_and_ends_as_one_row(db, agent, managed):
    """The old judge's pass queued the decision just before a swap, and the old judge is closed with it still waiting
    in its line; the new judge's pass takes it. It reaches the provider once and ends as exactly one final row at the
    second judge's name and version."""
    decision_id = await _decision(db, agent, "a2ui")
    env = await managed(row={"enabled": True}, settle=(60.0,))
    [old] = env.built
    assert await old.sweep_once() == 1 and not old.idle
    # A lower limit keeps the model and the key: the new judge has the same name and version, and asks no yes.
    lowered = await env.manager.patch({"daily_calls": 50}, confirmed=None, actor="owner")
    assert lowered.status == 200
    [_old, new] = env.built
    assert old._closed and (new.judge.name, new.judge.version) == (old.judge.name, old.judge.version)
    assert await new.sweep_once() == 1
    await _until(lambda: new.idle)
    async with db.session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT judge_name, judge_version, status FROM nous_system.judge_verdicts "
                    "WHERE agent_id = :a AND subject_id = :s"
                ),
                {"a": agent, "s": decision_id},
            )
        ).all()
    assert [tuple(row) for row in rows] == [("jev", VERSION, "ok")]
    assert len(env.sent) == 1
