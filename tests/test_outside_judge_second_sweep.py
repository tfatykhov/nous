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
from nous.judge import service as service_module
from nous.judge.jev_judge import JevJudge
from nous.judge.second_settings import SecondJudgeManager
from nous.judge.service import DECISION, SWEEP_BATCH, JudgeSet, OutsideJudge, call_bound

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


async def _many_calls(db, agent: str, decision_id: uuid.UUID, reviewed_at: datetime, count: int = 40) -> None:
    """``count`` more tool calls with long inputs and results in the session of a decision made by ``_decision``."""
    async with db.session() as session:
        for index in range(1, count + 1):
            await session.execute(
                text(
                    "INSERT INTO nous_system.tool_call_log (agent_id, session_id, call_index, work_kind, tool_name, "
                    "tool_name_called, outcome, input_text, result_preview, started_at) VALUES (:a, :s, :i, "
                    "'conversation', 'bash', 'bash', 'ok', :input, :result, :t)"
                ),
                {
                    "a": agent,
                    "s": f"sess-{decision_id.hex[:8]}",
                    "i": index,
                    "input": json.dumps({"command": f"copy part {index} of the backup " * 20}),
                    "result": f"copied part {index} of the nightly backup to the new disk. " * 30,
                    "t": reviewed_at - timedelta(minutes=4) + timedelta(seconds=index),
                },
            )
        await session.commit()


async def _calls_counted(db, agent: str) -> int:
    """The requests the table counts for this agent (every judge, every day)."""
    async with db.session() as session:
        found = await session.scalar(
            text("SELECT COALESCE(SUM(calls), 0) FROM nous_system.judge_verdicts WHERE agent_id = :a"), {"a": agent}
        )
    return int(found)


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
    try:
        assert await held.sweep_once() == 0 and held.sweep_numbers()["held_at_half_limit"] == 1 and held.idle
    finally:
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

    reviewed = NOW - timedelta(hours=1)
    decision_id = await _decision(
        db, agent, "a2ui", outcome="failure", reviewed_at=reviewed, note="the backup did not move"
    )
    await _many_calls(db, agent, decision_id, reviewed)  # far more evidence than the record's bound
    jev = JevJudge(TEST_KEY, MODEL, transport=httpx.MockTransport(answer))
    second, _j = _second(db, agent, jev)
    second.start()
    try:
        assert await _pass(second) == 1
    finally:
        await second.close()
    [request] = sent
    body = json.loads(request.content)
    state = body["state"]
    assert state["written_by_author"]["claim"] is None
    assert "the backup did not move" not in request.content.decode("utf-8")
    assert body["questions"] == jev_judge.questions()
    # The swept record is cut to the same bound as a live one: items left out, the record within EVIDENCE_MAX_CHARS.
    assert state["evidence_truncated"] is True and state["evidence_dropped"] > 0
    assert len(json.dumps(state, ensure_ascii=False)) <= evidence.EVIDENCE_MAX_CHARS
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
    try:
        await second.close()
        assert await second.sweep_once() == 0 and second.idle
        assert both.submit_decision(decision_id, grade="success", note=None, grader="agent", resolved_at=NOW) is True
        assert _pending(first) == {decision_id} and second.runtime()["dropped"] == 1
    finally:
        await both.close()


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
        # The provider answers while this is set; a test clears it to hold the requests that arrive.
        self.answering = asyncio.Event()
        self.answering.set()


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

        async def answer(request: httpx.Request) -> httpx.Response:
            env.sent.append(request)
            await env.answering.wait()
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
        env.answering.set()
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


# ---- closing with work in flight ------------------------------------------------------------------------------------


async def test_a_close_while_the_pass_is_inside_a_query_stops_it_at_once_and_queues_nothing(
    db, agent, monkeypatch, caplog
):
    monkeypatch.setattr(service_module, "SWEEP_FIRST_DELAY_SECONDS", 0.0)
    decision_id = await _decision(db, agent, "a2ui")
    reading = asyncio.Event()
    real = store.sweep_decisions

    async def slow_for_the_second(session, agent_id, *, judge_name, **kwargs):
        if judge_name == "jev":
            reading.set()
            await session.execute(text("SELECT pg_sleep(30)"))
        return await real(session, agent_id, judge_name=judge_name, **kwargs)

    monkeypatch.setattr(store, "sweep_decisions", slow_for_the_second)
    first, first_judge, _r = _service(db, agent)
    second, judge = _second(db, agent)
    second.start()
    try:
        await asyncio.wait_for(reading.wait(), 5)
        started = asyncio.get_running_loop().time()
        with caplog.at_level(logging.WARNING, logger="nous.judge.service"):
            await second.close()
        assert asyncio.get_running_loop().time() - started < 5.0
        assert second._sweeper.done() and second.idle and judge.records == []
        assert second.runtime()["dropped"] == 0 and second.runtime()["queued"] == 0
        # The cancellation can come back from the session's cleanup as a database error: then the cut pass is counted
        # as failed, with one WARNING naming the class only.
        warnings = [
            r.getMessage() for r in caplog.records if r.name == "nous.judge.service" and r.levelno >= logging.WARNING
        ]
        assert len(warnings) == second.sweep_numbers()["failed"] <= 1
        assert all(message.startswith("F118: a sweep pass failed (") for message in warnings)
        # The sessions still serve, and the first judge's pass is untouched (run before its own loop starts).
        assert await first.sweep_once() == 1
        first.start()
        await _until(lambda: first.idle)
        assert [record.subject_id for record in first_judge.records] == [decision_id]
    finally:
        await first.close()
        await second.close()


async def test_a_swap_while_the_old_judges_pass_is_reading_closes_it_cleanly(db, agent, managed, monkeypatch):
    monkeypatch.setattr(service_module, "SWEEP_FIRST_DELAY_SECONDS", 0.0)
    reading, release = asyncio.Event(), asyncio.Event()
    real = store.sweep_decisions

    async def held_once_for_the_second(session, agent_id, *, judge_name, **kwargs):
        if judge_name == "jev" and not reading.is_set():
            reading.set()
            await release.wait()
        return await real(session, agent_id, judge_name=judge_name, **kwargs)

    monkeypatch.setattr(store, "sweep_decisions", held_once_for_the_second)
    await _decision(db, agent, "a2ui")
    env = await managed(row={"enabled": True}, settle=(60.0, 60.0))
    [old] = env.built
    await asyncio.wait_for(reading.wait(), 5)
    try:
        answer = await env.manager.patch({"model": "jev-1.15.0"}, confirmed=None, actor="owner")
    finally:
        release.set()
    assert answer.status == 200
    [_old, new] = env.built
    assert old._closed and old._sweeper.done() and old.idle and old.runtime()["dropped"] == 0
    assert not new._sweeper.done() and not env.primary._sweeper.done()
    assert env.sent == []


async def test_after_swaps_and_a_shutdown_no_judge_task_of_this_test_is_left(managed):
    before = {task for task in asyncio.all_tasks() if task.get_name().startswith("outside-judge")}
    env = await managed(row={"enabled": True})
    for model in ("jev-1.15.0", "jev-1.16.0", "jev-1.17.0"):
        assert (await env.manager.patch({"model": model}, confirmed=None, actor="owner")).status == 200
    await env.manager.close()
    await env.judges.close()
    await asyncio.sleep(0)
    left = {
        task for task in asyncio.all_tasks() if task.get_name().startswith("outside-judge") and not task.done()
    } - before
    assert left == set()


async def test_a_request_in_flight_when_the_judge_is_replaced_ends_with_its_row_and_is_not_sent_again(
    db, agent, managed, monkeypatch
):
    """The old judge's close waits out a request it has sent (the judge's own timeout and grace), so the request
    ends with its row: it is counted in the day's calls and attempts, and the new judge's pass does not send the
    decision again. The times are shortened, the close's general wait kept below the judge's timeout as 5 s is below
    Jev's 10 s."""
    monkeypatch.setattr(service_module, "CLOSE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(jev_judge, "TIMEOUT_SECONDS", 1.0)
    decision_id = await _decision(db, agent, "a2ui")
    env = await managed(row={"enabled": True})
    [old] = env.built
    assert old._close_timeout == call_bound(1.0)
    env.answering.clear()  # the provider has the request and does not answer
    assert await old.sweep_once() == 1
    await _until(lambda: len(env.sent) == 1)
    # A lower limit keeps the name and version: the new judge looks for the same row.
    lowered = await env.manager.patch({"daily_calls": 50}, confirmed=None, actor="owner")
    assert lowered.status == 200
    [_old, new] = env.built
    assert await new.sweep_once() == 0  # the failed attempt waits SWEEP_RETRY_AFTER before it is tried again
    async with db.session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT status, calls, attempts FROM nous_system.judge_verdicts "
                    "WHERE agent_id = :a AND subject_id = :s"
                ),
                {"a": agent, "s": decision_id},
            )
        ).all()
    assert [tuple(row) for row in rows] == [("timeout", 1, 1)]
    assert len(env.sent) == 1 == await _calls_counted(db, agent)


async def test_at_shutdown_a_request_the_second_judge_has_sent_ends_with_its_row(db, agent, monkeypatch):
    monkeypatch.setattr(service_module, "CLOSE_TIMEOUT_SECONDS", 0.05)
    await _decision(db, agent, "a2ui")
    sent: list[httpx.Request] = []
    answering = asyncio.Event()

    async def held(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        await answering.wait()
        return httpx.Response(200, json=ANSWER)

    jev = JevJudge(TEST_KEY, MODEL, transport=httpx.MockTransport(held))
    first, _d, _r = _service(db, agent)
    # As main.py builds it: the judge's own timeout (shortened here) and close waiting out a request in flight.
    second = OutsideJudge(
        db,
        _settings(agent),
        jev,
        settle_seconds=0.0,
        timeout_seconds=1.0,
        daily_calls=100,
        sweep=True,
        close_timeout=call_bound(1.0),
    )
    both = JudgeSet(first, second)
    first.start()
    second.start()
    try:
        assert await second.sweep_once() == 1
        await _until(lambda: len(sent) == 1)
        await both.close()  # as main.py shuts down: each judge waits its own time
        assert first._close_timeout == 0.05  # the first judge's wait is not changed
    finally:
        answering.set()
        await both.close()
    assert [(row["judge_name"], row["status"]) for row in await _rows(db, agent)] == [("jev", "timeout")]
    assert len(sent) == 1 == await _calls_counted(db, agent)


async def test_the_judge_sets_close_lets_each_judge_wait_its_own_time_unless_told():
    closed: list[tuple[str, float | None]] = []

    class Recording:
        def __init__(self, name: str) -> None:
            self.name = name

        async def close(self, timeout: float | None = None) -> None:
            closed.append((self.name, timeout))

    await JudgeSet(Recording("first"), Recording("second")).close()
    await JudgeSet(Recording("first"), Recording("second")).close(0)
    assert closed == [("first", None), ("second", None), ("first", 0), ("second", 0)]
