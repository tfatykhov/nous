"""F118: the outside judge's queue, worker, daily cap and verdict rows (real Postgres, stub judges, no model).

``submit_decision`` is synchronous, never raises and never waits; the claim is fixed when it is called. The worker
judges one subject at a time, after a settle delay, at most once per subject, judge name and version, within the daily
cap read from the table. A judge that raises or hangs gives an ``error`` or ``timeout`` row; ``close`` cancels a call
in flight and writes nothing. The decision row is never changed.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text

from nous.config import Settings
from nous.judge import service as service_module
from nous.judge import store
from nous.judge.service import OutsideJudge
from nous.judge.slot import JudgeInput, JudgeOutput

pytestmark = pytest.mark.postgres_only

FAST = {"settle_seconds": 0.0, "timeout_seconds": 2.0}


@pytest.fixture(autouse=True)
def _no_request_leaves_the_process(monkeypatch):
    """The second judge's tests use a fake transport: a request through a real one fails the test."""
    import httpx

    async def refuse(self, request):
        raise AssertionError(f"a real request was attempted to {request.url.host}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)


class StubJudge:
    """A judge that answers, raises or hangs, and records what it was given."""

    name = "stub"

    def __init__(
        self, behaviour: str = "ok", version: str = "stub-1", verdict: str = "failure", calls: int | None = None
    ) -> None:
        self.behaviour = behaviour
        self.version = version
        self.verdict = verdict
        self.calls = calls
        self.tokens: tuple[int | None, int | None] = (1200, 35)
        self.records: list[JudgeInput] = []
        self.called_at: list[float] = []
        self.max_calls: list[int | None] = []

    async def grade(self, record: JudgeInput, *, timeout: float, max_calls: int | None = None) -> JudgeOutput:
        self.records.append(record)
        self.called_at.append(asyncio.get_running_loop().time())
        self.max_calls.append(max_calls)
        if self.behaviour == "raise":
            raise RuntimeError("the stub judge broke")
        if self.behaviour == "hang":
            await asyncio.sleep(3600)
        if self.behaviour == "error":
            return JudgeOutput(
                status="error",
                verdict="cannot_tell",
                tokens_in=self.tokens[0],
                tokens_out=self.tokens[1],
                calls=self.calls,
            )
        return JudgeOutput(
            status="ok",
            verdict=self.verdict,
            confidence=0.6,
            labels=("evidence_contradicts",),
            reason="The job did not run after the change.",
            model="stub-model",
            tokens_in=self.tokens[0],
            tokens_out=self.tokens[1],
            calls=self.calls,
        )


@pytest_asyncio.fixture
async def agent(db):
    name = f"test-f118-svc-{uuid.uuid4().hex[:8]}"
    yield name
    async with db.session() as session:
        for table in ("brain.decisions", "nous_system.tool_call_log", "nous_system.judge_verdicts"):
            await session.execute(text(f"DELETE FROM {table} WHERE agent_id = :a"), {"a": name})
        await session.commit()


def _settings(agent: str, **over) -> Settings:
    return Settings(_env_file=None, agent_id=agent, outside_judge_mode="shadow", **over)


async def _decision(
    db,
    agent: str,
    *,
    with_call: bool = True,
    description: str = "Restart the nightly job",
    result: str = "job restarted",
    args: str = "",
) -> uuid.UUID:
    decision_id = uuid.uuid4()
    session_id = f"sess-{decision_id.hex[:8]}"
    now = datetime.now(UTC)
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO brain.decisions (id, agent_id, description, confidence, category, stakes, session_id, "
                "created_at) VALUES (:id, :a, :d, 0.7, 'tooling', 'low', :s, :t)"
            ),
            {"id": decision_id, "a": agent, "d": description, "s": session_id, "t": now - timedelta(minutes=5)},
        )
        if with_call:
            await session.execute(
                text(
                    "INSERT INTO nous_system.tool_call_log (agent_id, session_id, call_index, work_kind, tool_name, "
                    "tool_name_called, outcome, input_text, result_preview, started_at) VALUES (:a, :s, 0, "
                    "'conversation', 'bash', 'bash', 'ok', :i, :r, :t)"
                ),
                {"a": agent, "s": session_id, "i": args, "r": result, "t": now - timedelta(minutes=4)},
            )
        await session.commit()
    return decision_id


async def _rows(db, agent: str) -> list[dict]:
    async with db.session() as session:
        result = await session.execute(
            text(
                "SELECT subject_kind, subject_id, judge_name, judge_version, model, status, verdict, confidence, "
                "labels, reason, against_grader, against_grade, evidence, tokens_in, tokens_out, latency_ms, calls, "
                "attempts, claim_at FROM nous_system.judge_verdicts WHERE agent_id = :a ORDER BY judged_at"
            ),
            {"a": agent},
        )
        return [dict(row._mapping) for row in result]


async def _decision_row(db, decision_id: uuid.UUID) -> tuple:
    async with db.session() as session:
        return tuple(
            (
                await session.execute(
                    text("SELECT * FROM brain.decisions WHERE id = :id"),
                    {"id": decision_id},
                )
            ).one()
        )


def _submit(
    judge: OutsideJudge,
    decision_id: uuid.UUID,
    grade: str = "success",
    note: str | None = "it ran",
    resolved_at: datetime | None = None,
) -> bool:
    at = resolved_at or datetime.now(UTC)
    return judge.submit_decision(decision_id, grade=grade, note=note, grader="agent", resolved_at=at)


async def _until(predicate, seconds: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out waiting")
        await asyncio.sleep(0.02)


async def _drain(service: OutsideJudge, seconds: float = 10.0) -> None:
    await _until(lambda: service.idle, seconds)


# ---- submit ---------------------------------------------------------------------------------------------------------


def test_submit_is_synchronous():
    assert not inspect.iscoroutinefunction(OutsideJudge.submit_decision)


async def test_submit_drops_when_full_closed_or_already_queued_and_never_raises(db, agent):
    service = OutsideJudge(db, _settings(agent), StubJudge(), queue_max=2, **FAST)  # not started: nothing is taken
    first, second, third = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    assert _submit(service, first) is True
    assert _submit(service, first) is False  # already queued
    assert _submit(service, second) is True
    assert _submit(service, third) is False  # full
    assert service.submit_decision("not-a-uuid", grade=None, note=object(), grader="agent", resolved_at=None) is False
    assert service.runtime() == {"queued": 2, "dropped": 3, "over_cap": 0, "worker_alive": False}
    await service.close()
    assert _submit(service, uuid.uuid4()) is False  # closed
    assert service.runtime()["dropped"] == 4


async def test_the_claim_is_fixed_when_it_is_submitted(db, agent):
    stub = StubJudge()
    service = OutsideJudge(db, _settings(agent), stub, **FAST)
    decision_id = await _decision(db, agent)
    note = ["it ran"]
    resolved_at = datetime.now(UTC)
    service.submit_decision(decision_id, grade="success", note=note[0], grader="agent", resolved_at=resolved_at)
    # The decision row is graded again before the worker runs: the judge still compares with the first claim.
    async with db.session() as session:
        await session.execute(
            text("UPDATE brain.decisions SET outcome = 'failure', outcome_result = 'changed' WHERE id = :id"),
            {"id": decision_id},
        )
        await session.commit()
    service.start()
    await _drain(service)
    await service.close()
    [record] = stub.records
    assert (record.claim.grader, record.claim.grade, record.claim.note) == ("agent", "success", "it ran")
    [row] = await _rows(db, agent)
    assert row["claim_at"] == resolved_at  # the time the claim was fixed, not the time of the verdict
    assert (row["against_grader"], row["against_grade"]) == ("agent", "success")


# ---- the worker -----------------------------------------------------------------------------------------------------


async def test_a_verdict_is_written_after_the_settle_delay_and_the_decision_row_is_unchanged(db, agent):
    stub = StubJudge()
    service = OutsideJudge(db, _settings(agent), stub, settle_seconds=0.4, timeout_seconds=2.0)
    decision_id = await _decision(db, agent)
    before = await _decision_row(db, decision_id)
    service.start()
    submitted = asyncio.get_running_loop().time()
    _submit(service, decision_id)
    await asyncio.sleep(0.2)
    assert stub.records == []  # still settling
    await _drain(service)
    await service.close()
    assert stub.called_at[0] - submitted >= 0.4
    [row] = await _rows(db, agent)
    assert {k: row[k] for k in ("subject_kind", "subject_id", "judge_name", "judge_version", "status", "verdict")} == {
        "subject_kind": "decision",
        "subject_id": decision_id,
        "judge_name": "stub",
        "judge_version": "stub-1",
        "status": "ok",
        "verdict": "failure",
    }
    assert row["labels"] == ["evidence_contradicts"] and row["reason"] == "The job did not run after the change."
    # A judge that does not say how many calls it made is counted as one.
    assert (row["model"], row["tokens_in"], row["tokens_out"], row["calls"], row["attempts"]) == (
        "stub-model",
        1200,
        35,
        1,
        1,
    )
    assert abs(row["confidence"] - 0.6) < 1e-6 and row["latency_ms"] >= 0
    assert row["evidence"]["link"] == "session_id" and row["evidence"]["tool_calls"] == 1
    assert await _decision_row(db, decision_id) == before


async def test_one_verdict_per_subject_and_version(db, agent):
    stub = StubJudge()
    service = OutsideJudge(db, _settings(agent), stub, **FAST)
    service.start()
    decision_id = await _decision(db, agent)
    _submit(service, decision_id)
    await _drain(service)
    _submit(service, decision_id, grade="partial")  # judged already at this version: no second call
    await _drain(service)
    await service.close()
    assert len(stub.records) == 1 and len(await _rows(db, agent)) == 1

    newer = StubJudge(version="stub-2")
    service = OutsideJudge(db, _settings(agent), newer, **FAST)
    service.start()
    _submit(service, decision_id, grade="partial")
    await _drain(service)
    await service.close()
    assert len(newer.records) == 1
    assert [(r["judge_version"], r["against_grade"]) for r in await _rows(db, agent)] == [
        ("stub-1", "success"),
        ("stub-2", "partial"),
    ]


async def test_a_decision_without_evidence_is_skipped_without_a_call(db, agent):
    stub = StubJudge()
    service = OutsideJudge(db, _settings(agent), stub, **FAST)
    service.start()
    decision_id = await _decision(db, agent, with_call=False)
    _submit(service, decision_id)
    await _drain(service)
    await service.close()
    assert stub.records == []
    [row] = await _rows(db, agent)
    assert (row["status"], row["verdict"], row["labels"], row["model"]) == (
        "skipped",
        "cannot_tell",
        ["evidence_missing"],
        None,
    )
    assert row["evidence"]["link"] == "session_id" and row["evidence"]["tool_calls"] == 0


async def test_a_decision_that_is_gone_writes_nothing(db, agent, caplog):
    stub = StubJudge()
    service = OutsideJudge(db, _settings(agent), stub, **FAST)
    service.start()
    with caplog.at_level(logging.DEBUG, logger="nous.judge.service"):
        _submit(service, uuid.uuid4())
        await _drain(service)
    await service.close()
    assert stub.records == [] and await _rows(db, agent) == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_a_judge_that_raises_gives_an_error_row(db, agent, caplog):
    service = OutsideJudge(db, _settings(agent), StubJudge("raise"), **FAST)
    service.start()
    decision_id = await _decision(db, agent)
    with caplog.at_level(logging.WARNING, logger="nous.judge.service"):
        _submit(service, decision_id)
        await _drain(service)
    await service.close()
    [row] = await _rows(db, agent)
    assert (row["status"], row["verdict"], row["labels"]) == ("error", "cannot_tell", ["judge_error"])
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "RuntimeError" in warnings[0] and "broke" not in warnings[0]


async def test_a_judge_that_hangs_past_the_timeout_gives_a_timeout_row(db, agent):
    service = OutsideJudge(db, _settings(agent), StubJudge("hang"), settle_seconds=0.0, timeout_seconds=0.2)
    service.start()
    decision_id = await _decision(db, agent)
    _submit(service, decision_id)
    await _drain(service)
    await service.close()
    [row] = await _rows(db, agent)
    assert (row["status"], row["verdict"], row["labels"]) == ("timeout", "cannot_tell", ["judge_error"])


async def test_the_built_in_judges_own_timeout_keeps_its_model_on_the_row(db, agent):
    from types import SimpleNamespace

    from nous.judge.model_judge import BackgroundModelJudge

    class HangingClient:
        supports_call_context = True

        async def call(self, payload, call_context=None):
            await asyncio.sleep(3600)
            return SimpleNamespace(content=[], stop_reason="end_turn")

    judge = BackgroundModelJudge(HangingClient(), "claude-test")
    service = OutsideJudge(db, _settings(agent), judge, settle_seconds=0.0, timeout_seconds=0.2)
    service.start()
    _submit(service, await _decision(db, agent))
    await _drain(service)
    await service.close()
    [row] = await _rows(db, agent)
    assert (row["status"], row["verdict"], row["model"], row["judge_name"]) == (
        "timeout",
        "cannot_tell",
        "claude-test",
        "background_model",
    )
    assert row["latency_ms"] < 400  # its own bound, not the outer one


async def test_a_verdict_a_judge_may_not_give_is_stored_as_an_error(db, agent):
    service = OutsideJudge(db, _settings(agent), StubJudge(verdict="supported"), **FAST)  # a report's verdict
    service.start()
    _submit(service, await _decision(db, agent))
    await _drain(service)
    await service.close()
    [row] = await _rows(db, agent)
    assert (row["status"], row["verdict"]) == ("error", "cannot_tell")


async def test_a_record_that_cannot_be_rendered_is_skipped_without_a_call(db, agent, monkeypatch):
    from nous.judge import evidence

    def broken(*args, **kwargs):
        raise evidence.RenderError("not JSON")

    monkeypatch.setattr(evidence, "assemble", broken)
    stub = StubJudge()
    service = OutsideJudge(db, _settings(agent, outside_judge_daily_calls=1), stub, **FAST)
    service.start()
    _submit(service, await _decision(db, agent))
    await _drain(service)
    assert stub.records == []
    [row] = await _rows(db, agent)
    assert (row["status"], row["verdict"], row["labels"], row["calls"]) == (
        "skipped",
        "cannot_tell",
        ["record_not_rendered"],
        0,
    )
    # It made no call, so the day's one call is still there for the next decision.
    monkeypatch.undo()
    _submit(service, await _decision(db, agent))
    await _drain(service)
    await service.close()
    assert len(stub.records) == 1


# A string that ends in a credential label, in each place a string of the record comes from.
TAILS = ["password=", "password: ", "api_key=", "Authorization: Bearer ", 'password="']
PLACES = ["result", "note", "description", "args"]


@pytest.mark.parametrize("place", PLACES)
@pytest.mark.parametrize("tail", TAILS)
async def test_a_string_ending_in_a_credential_label_is_judged_with_its_item(db, agent, place, tail):
    with_tail = f"the deploy step said {tail}"
    stub = StubJudge()
    service = OutsideJudge(db, _settings(agent), stub, **FAST)
    service.start()
    decision_id = await _decision(
        db,
        agent,
        description=with_tail if place == "description" else "Restart the nightly job",
        result=with_tail if place == "result" else "job restarted",
        args=with_tail if place == "args" else '{"command": "systemctl restart nightly"}',
    )
    _submit(service, decision_id, note=with_tail if place == "note" else "it ran")
    await _drain(service)
    await service.close()
    [record] = stub.records
    [item] = record.evidence
    assert (item["source"], item["tool"]) == ("tool_call", "bash")
    [row] = await _rows(db, agent)
    assert (row["status"], row["verdict"], row["calls"]) == ("ok", "failure", 1)
    assert row["evidence"]["dropped"] == 0


# ---- a verdict that failed is tried again -------------------------------------------------------------------------


async def test_an_error_row_is_replaced_by_the_next_attempt(db, agent):
    decision_id = await _decision(db, agent)
    failing = StubJudge("raise", calls=1)
    service = OutsideJudge(db, _settings(agent), failing, **FAST)
    service.start()
    # Both after the session's call (four minutes ago), so each attempt has evidence.
    first_claim = datetime.now(UTC) - timedelta(minutes=2)
    _submit(service, decision_id, grade="success", resolved_at=first_claim)
    await _drain(service)
    await service.close()
    [row] = await _rows(db, agent)
    # It raised: whether it sent a request is not known, so one is counted.
    assert (row["status"], row["attempts"], row["calls"], row["claim_at"]) == ("error", 1, 1, first_claim)

    working = StubJudge(calls=1)
    service = OutsideJudge(db, _settings(agent), working, **FAST)
    service.start()
    second_claim = first_claim + timedelta(minutes=1)
    _submit(service, decision_id, grade="partial", resolved_at=second_claim)
    await _drain(service)
    await service.close()
    [row] = await _rows(db, agent)
    assert (row["status"], row["verdict"], row["attempts"], row["calls"]) == ("ok", "failure", 2, 2)
    # The claim the verdict was given on, and when that claim was fixed.
    assert (row["against_grade"], row["claim_at"]) == ("partial", second_claim)


async def test_a_timeout_row_is_tried_again_at_most_three_times_in_all(db, agent):
    decision_id = await _decision(db, agent)
    hanging = StubJudge("hang")
    service = OutsideJudge(db, _settings(agent), hanging, settle_seconds=0.0, timeout_seconds=0.1)
    service.start()
    for _ in range(5):
        _submit(service, decision_id)
        await _drain(service)
    await service.close()
    assert len(hanging.records) == 3
    [row] = await _rows(db, agent)
    assert (row["status"], row["attempts"], row["calls"]) == ("timeout", 3, 3)


async def test_an_ok_or_skipped_row_is_final(db, agent):
    judged, skipped = await _decision(db, agent), await _decision(db, agent, with_call=False)
    stub = StubJudge()
    service = OutsideJudge(db, _settings(agent), stub, **FAST)
    service.start()
    for decision_id in (judged, skipped, judged, skipped):
        _submit(service, decision_id)
        await _drain(service)
    await service.close()
    assert len(stub.records) == 1
    assert sorted((r["status"], r["attempts"]) for r in await _rows(db, agent)) == [("ok", 1), ("skipped", 1)]


# ---- the worker stays up ------------------------------------------------------------------------------------------


async def test_the_runtime_says_whether_the_worker_runs(db, agent):
    service = OutsideJudge(db, _settings(agent), StubJudge(), **FAST)
    assert service.runtime()["worker_alive"] is False
    service.start()
    assert service.runtime()["worker_alive"] is True
    await service.close()
    assert service.runtime()["worker_alive"] is False


async def test_a_judging_cancelled_from_outside_does_not_end_the_worker(db, agent):
    stub = StubJudge("hang")
    service = OutsideJudge(db, _settings(agent), stub, settle_seconds=0.0, timeout_seconds=30.0)
    service.start()
    _submit(service, await _decision(db, agent))
    await _until(lambda: service.busy)
    service._inflight.cancel()  # not close(): something else cancelled the judging
    await _drain(service)
    assert service.runtime()["worker_alive"] is True
    stub.behaviour = "ok"
    _submit(service, await _decision(db, agent))
    await _drain(service)
    await service.close()
    assert len(stub.records) == 2
    assert [r["status"] for r in await _rows(db, agent)] == ["ok"]


async def test_a_worker_that_ended_is_started_again_by_the_next_submit(db, agent, caplog):
    stub = StubJudge()
    service = OutsideJudge(db, _settings(agent), stub, **FAST)
    service.start()
    service._worker.cancel()  # not close()
    await asyncio.gather(service._worker, return_exceptions=True)
    assert service.runtime()["worker_alive"] is False
    with caplog.at_level(logging.WARNING, logger="nous.judge.service"):
        assert _submit(service, await _decision(db, agent)) is True
    assert service.runtime()["worker_alive"] is True
    await _drain(service)
    await service.close()
    assert len(stub.records) == 1
    assert len([r for r in caplog.records if "worker" in r.getMessage()]) == 1


async def test_close_cancels_a_call_in_flight_and_writes_nothing(db, agent):
    stub = StubJudge("hang")
    service = OutsideJudge(db, _settings(agent), stub, settle_seconds=0.0, timeout_seconds=30.0)
    service.start()
    _submit(service, await _decision(db, agent))
    await _until(lambda: len(stub.records) == 1)
    started = asyncio.get_running_loop().time()
    await service.close(timeout=0.2)
    assert asyncio.get_running_loop().time() - started < 2.0
    assert await _rows(db, agent) == []
    assert _submit(service, uuid.uuid4()) is False


async def test_close_waits_for_a_call_that_ends_within_its_time(db, agent):
    class Slow(StubJudge):
        async def grade(self, record: JudgeInput, *, timeout: float, max_calls: int | None = None) -> JudgeOutput:
            await asyncio.sleep(0.2)
            return await super().grade(record, timeout=timeout, max_calls=max_calls)

    stub = Slow()
    service = OutsideJudge(db, _settings(agent), stub, **FAST)
    service.start()
    _submit(service, await _decision(db, agent))
    await _until(lambda: service.busy)
    await service.close(timeout=5.0)
    assert len(await _rows(db, agent)) == 1


# ---- the daily cap -------------------------------------------------------------------------------------------------


async def test_the_cap_stops_calls_writes_no_row_and_survives_a_new_service(db, agent, caplog):
    stub = StubJudge()
    settings = _settings(agent, outside_judge_daily_calls=1)
    service = OutsideJudge(db, settings, stub, **FAST)
    service.start()
    first, second, third = [await _decision(db, agent) for _ in range(3)]
    _submit(service, first)
    await _drain(service)
    with caplog.at_level(logging.INFO, logger="nous.judge.service"):
        _submit(service, second)
        await _drain(service)
        _submit(service, third)
        await _drain(service)
    await service.close()
    assert len(stub.records) == 1 and len(await _rows(db, agent)) == 1
    assert service.runtime()["over_cap"] == 2
    assert len([r for r in caplog.records if "daily limit" in r.getMessage()]) == 1  # one line a day

    # A new service (a restart) reads today's count from the table.
    again = StubJudge()
    service = OutsideJudge(db, settings, again, **FAST)
    service.start()
    _submit(service, second)
    await _drain(service)
    await service.close()
    assert again.records == [] and len(await _rows(db, agent)) == 1


async def test_a_skipped_row_does_not_count_against_the_cap(db, agent):
    stub = StubJudge()
    service = OutsideJudge(db, _settings(agent, outside_judge_daily_calls=1), stub, **FAST)
    service.start()
    _submit(service, await _decision(db, agent, with_call=False))
    await _drain(service)
    _submit(service, await _decision(db, agent))
    await _drain(service)
    await service.close()
    assert len(stub.records) == 1
    assert sorted(r["status"] for r in await _rows(db, agent)) == ["ok", "skipped"]


async def test_a_cap_of_zero_makes_no_call(db, agent):
    stub = StubJudge()
    service = OutsideJudge(db, _settings(agent, outside_judge_daily_calls=0), stub, **FAST)
    service.start()
    _submit(service, await _decision(db, agent))
    await _drain(service)
    await service.close()
    assert stub.records == [] and await _rows(db, agent) == []


async def test_the_cap_fails_closed_when_the_count_cannot_be_read(db, agent, monkeypatch):
    async def broken(*args, **kwargs):
        raise ConnectionError("count failed")

    monkeypatch.setattr(store, "calls_today", broken)
    stub = StubJudge()
    service = OutsideJudge(db, _settings(agent), stub, **FAST)
    service.start()
    _submit(service, await _decision(db, agent))
    await _drain(service)
    await service.close()
    assert stub.records == [] and await _rows(db, agent) == []
    assert service.runtime()["over_cap"] == 1


async def _verdict_rows(db, agent: str, rows: list[tuple[str, str, int, datetime]]) -> None:
    """Rows of (judge name, status, calls, judged at), each for a subject of its own."""
    async with db.session() as session:
        for judge_name, status, calls, judged_at in rows:
            await session.execute(
                text(
                    "INSERT INTO nous_system.judge_verdicts (agent_id, subject_kind, subject_id, judge_name, "
                    "judge_version, status, verdict, calls, judged_at, claim_at) VALUES (:a, 'decision', :s, :j, "
                    "'v-1', :st, 'cannot_tell', :n, :t, :t)"
                ),
                {"a": agent, "s": uuid.uuid4(), "j": judge_name, "st": status, "n": calls, "t": judged_at},
            )
        await session.commit()


async def test_calls_today_counts_the_model_calls_sent_today(db, agent):
    now = datetime.now(UTC)
    await _verdict_rows(
        db,
        agent,
        [
            ("stub", "ok", 1, now),
            ("stub", "ok", 2, now),  # a model that refused the forced tool choice: two requests
            ("stub", "error", 0, now),  # failed before any request
            ("stub", "timeout", 3, now),  # three attempts
            ("stub", "skipped", 0, now),
            ("stub", "ok", 1, now - timedelta(days=1, hours=1)),
        ],
    )
    async with db.session() as session:
        # With one judge, its own count and the count of every judge are the same.
        assert await store.calls_today(session, agent, now) == 6
        assert await store.calls_today(session, agent, now, judge_name="stub") == 6
        assert await store.calls_today(session, f"{agent}-other", now) == 0


async def test_calls_today_counts_one_judge_when_it_is_named(db, agent):
    now = datetime.now(UTC)
    await _verdict_rows(db, agent, [("stub", "ok", 1, now), ("stub", "ok", 2, now), ("other_judge", "ok", 5, now)])
    async with db.session() as session:
        assert await store.calls_today(session, agent, now, judge_name="stub") == 3
        assert await store.calls_today(session, agent, now, judge_name="other_judge") == 5
        assert await store.calls_today(session, agent, now) == 8


async def test_another_judges_calls_do_not_count_against_this_judges_limit(db, agent):
    await _verdict_rows(db, agent, [("other_judge", "ok", 5, datetime.now(UTC))])
    stub = StubJudge()
    service = OutsideJudge(db, _settings(agent, outside_judge_daily_calls=1), stub, **FAST)
    service.start()
    _submit(service, await _decision(db, agent))
    await _drain(service)
    _submit(service, await _decision(db, agent))  # its own one call of the day is spent
    await _drain(service)
    await service.close()
    assert len(stub.records) == 1 and service.runtime()["over_cap"] == 1


async def test_a_judge_that_sends_two_requests_counts_two_against_the_cap(db, agent):
    stub = StubJudge(calls=2)
    service = OutsideJudge(db, _settings(agent, outside_judge_daily_calls=2), stub, **FAST)
    service.start()
    for _ in range(2):
        _submit(service, await _decision(db, agent))
        await _drain(service)
    await service.close()
    assert len(stub.records) == 1 and service.runtime()["over_cap"] == 1
    [row] = await _rows(db, agent)
    assert row["calls"] == 2


def test_the_module_constants():
    assert (service_module.SETTLE_SECONDS, service_module.JUDGE_TIMEOUT_SECONDS, service_module.QUEUE_MAX) == (
        30.0,
        60.0,
        50,
    )


# ---- tokens across attempts ---------------------------------------------------------------------------------------


async def _two_attempts(db, agent: str, first: StubJudge, second: StubJudge) -> dict:
    decision_id = await _decision(db, agent)
    for judge in (first, second):
        service = OutsideJudge(db, _settings(agent), judge, **FAST)
        service.start()
        _submit(service, decision_id)
        await _drain(service)
        await service.close()
    [row] = await _rows(db, agent)
    return row


async def test_a_retried_row_adds_the_tokens_of_every_attempt(db, agent):
    failed = StubJudge("error", calls=1)
    failed.tokens = (900, 12)
    row = await _two_attempts(db, agent, failed, StubJudge(calls=1))
    assert (row["status"], row["attempts"], row["calls"]) == ("ok", 2, 2)
    assert (row["tokens_in"], row["tokens_out"]) == (900 + 1200, 12 + 35)


async def test_an_attempt_whose_tokens_are_unknown_adds_nothing_to_the_known_ones(db, agent):
    failed = StubJudge("error", calls=1)
    failed.tokens = (None, None)
    row = await _two_attempts(db, agent, failed, StubJudge(calls=1))
    assert (row["tokens_in"], row["tokens_out"]) == (1200, 35)


async def test_tokens_stay_unknown_while_no_attempt_knows_them(db, agent):
    first, second = StubJudge("error", calls=1), StubJudge("error", calls=1)
    first.tokens = second.tokens = (None, None)
    row = await _two_attempts(db, agent, first, second)
    assert (row["status"], row["attempts"]) == ("error", 2)
    assert (row["tokens_in"], row["tokens_out"]) == (None, None)


# ---- the requests left in the day's limit --------------------------------------------------------------------------


async def test_the_judge_is_given_the_requests_left_today(db, agent):
    await _verdict_rows(db, agent, [("stub", "ok", 1, datetime.now(UTC))])
    stub = StubJudge()
    service = OutsideJudge(db, _settings(agent, outside_judge_daily_calls=3), stub, **FAST)
    service.start()
    _submit(service, await _decision(db, agent))
    await _drain(service)
    await service.close()
    assert stub.max_calls == [2]


async def test_with_one_request_left_a_model_that_rejects_forcing_gets_exactly_one(db, agent, monkeypatch):
    from types import SimpleNamespace

    import nous.handlers
    from nous.judge.model_judge import BackgroundModelJudge

    monkeypatch.setattr(nous.handlers, "_REJECTS_FORCED_TOOL_CHOICE", set())

    class RejectsForcing:
        supports_call_context = True

        def __init__(self) -> None:
            self.payloads: list[dict] = []

        async def call(self, payload, call_context=None):
            self.payloads.append(payload)
            if "tool_choice" in payload:
                raise RuntimeError(
                    'Anthropic API error (400): invalid_request_error - tool_choice: type "tool" and "any" are not '
                    "supported for this model."
                )
            block = {
                "type": "tool_use",
                "id": "t",
                "name": payload["tools"][0]["name"],
                "input": {"verdict": "success"},
            }
            return SimpleNamespace(content=[block], stop_reason="tool_use")

    client = RejectsForcing()
    service = OutsideJudge(
        db, _settings(agent, outside_judge_daily_calls=1), BackgroundModelJudge(client, "claude-rejects"), **FAST
    )
    service.start()
    _submit(service, await _decision(db, agent))
    await _drain(service)
    _submit(service, await _decision(db, agent))  # the day's one request is spent
    await _drain(service)
    await service.close()
    assert len(client.payloads) == 1  # the forced request only: no fallback past the limit
    [row] = await _rows(db, agent)
    assert (row["status"], row["calls"], row["attempts"]) == ("error", 1, 1)  # tried again on a later resolve
    assert service.runtime()["over_cap"] == 1


# ---- two judges side by side (JudgeSet) ----------------------------------------------------------------------------


class SecondStub(StubJudge):
    name = "second"


def _pair(db, agent: str, first: StubJudge, second: StubJudge, **over) -> service_module.JudgeSet:
    settings = _settings(agent, **over)
    return service_module.JudgeSet(
        OutsideJudge(db, settings, first, **FAST), OutsideJudge(db, settings, second, **FAST)
    )


async def _drain_both(both: service_module.JudgeSet, seconds: float = 10.0) -> None:
    await _until(lambda: both.primary.idle and both.second.idle, seconds)


def _start(both: service_module.JudgeSet) -> None:
    both.primary.start()
    both.second.start()


def test_a_judge_set_submits_synchronously():
    assert not inspect.iscoroutinefunction(service_module.JudgeSet.submit_decision)


async def test_a_judge_set_reads_as_the_first_judge_and_holds_the_second(db, agent):
    first, second = StubJudge(), SecondStub()
    both = _pair(db, agent, first, second)
    assert both.judge is first and both.second.judge is second and both.primary.judge is first


async def test_a_judge_set_queues_on_both_and_returns_the_first_judges_answer(db, agent):
    settings = _settings(agent)
    # Not started: nothing is taken. The first judge's queue holds one subject.
    both = service_module.JudgeSet(
        OutsideJudge(db, settings, StubJudge(), queue_max=1, **FAST), OutsideJudge(db, settings, SecondStub(), **FAST)
    )
    assert _submit(both, uuid.uuid4()) is True
    assert (both.primary.runtime()["queued"], both.second.runtime()["queued"]) == (1, 1)
    # The first judge's queue is full: its answer is returned, and the second still takes the subject.
    assert _submit(both, uuid.uuid4()) is False
    assert (both.primary.runtime()["queued"], both.second.runtime()["queued"]) == (1, 2)
    await both.close()


class RaisingSubmit:
    def __init__(self) -> None:
        self.calls = 0

    def submit_decision(self, *args, **kwargs) -> bool:
        self.calls += 1
        raise RuntimeError("submit broke")

    def runtime(self) -> dict:
        return {}

    async def close(self, timeout: float = 0.0) -> None:
        raise RuntimeError("close broke")


async def test_a_second_judge_whose_submit_raises_does_not_stop_the_first(db, agent, caplog):
    primary = OutsideJudge(db, _settings(agent), StubJudge(), **FAST)
    broken = RaisingSubmit()
    both = service_module.JudgeSet(primary, broken)
    with caplog.at_level(logging.WARNING, logger="nous.judge.service"):
        assert _submit(both, uuid.uuid4()) is True
    assert primary.runtime()["queued"] == 1 and broken.calls == 1
    [line] = [r.getMessage() for r in caplog.records if r.name == "nous.judge.service"]
    assert "RuntimeError" in line and "broke" not in line
    await primary.close()


async def test_a_first_judge_whose_submit_raises_still_hands_the_claim_to_the_second(db, agent):
    second = OutsideJudge(db, _settings(agent), SecondStub(), **FAST)
    both = service_module.JudgeSet(RaisingSubmit(), second)
    assert _submit(both, uuid.uuid4()) is False
    assert second.runtime()["queued"] == 1
    await second.close()


async def test_two_judges_write_two_rows_on_the_same_claim(db, agent):
    first, second = StubJudge(), SecondStub(verdict="success")
    both = _pair(db, agent, first, second)
    _start(both)
    decision_id = await _decision(db, agent)
    resolved_at = datetime.now(UTC)
    assert _submit(both, decision_id, resolved_at=resolved_at) is True
    await _drain_both(both)
    await both.close()
    assert len(first.records) == len(second.records) == 1
    assert first.records[0] == second.records[0]  # each built the same record
    rows = sorted(await _rows(db, agent), key=lambda r: r["judge_name"])
    assert [(r["judge_name"], r["verdict"], r["claim_at"], r["against_grade"]) for r in rows] == [
        ("second", "success", resolved_at, "success"),
        ("stub", "failure", resolved_at, "success"),
    ]
    assert rows[0]["evidence"] == rows[1]["evidence"]


async def test_a_second_judge_that_hangs_leaves_the_first_judges_row_as_it_would_be_alone(db, agent):
    decision_id = await _decision(db, agent)
    alone_agent = f"{agent}-alone"
    alone = OutsideJudge(db, _settings(alone_agent), StubJudge(), **FAST)
    hanging = SecondStub("hang")
    settings = _settings(agent)
    both = service_module.JudgeSet(
        OutsideJudge(db, settings, StubJudge(), **FAST),
        # It ignores its timeout: the service stops it at 2 s plus the grace, long after the first judge's row.
        OutsideJudge(db, settings, hanging, settle_seconds=0.0, timeout_seconds=2.0),
    )
    try:
        _start(both)
        _submit(both, decision_id)
        await _until(lambda: both.primary.idle and both.second.busy)
        [first_row] = await _rows(db, agent)  # written while the second judge still waits
        assert first_row["judge_name"] == "stub"
        await _drain_both(both)
        await both.close()
        # The same decision judged by the first judge alone (another agent's copy) gives the same row.
        copy_id = await _decision(db, alone_agent)
        alone.start()
        _submit(alone, copy_id)
        await _drain(alone)
        await alone.close()
        [alone_row] = await _rows(db, alone_agent)
    finally:
        async with db.session() as session:
            for table in ("brain.decisions", "nous_system.tool_call_log", "nous_system.judge_verdicts"):
                await session.execute(text(f"DELETE FROM {table} WHERE agent_id = :a"), {"a": alone_agent})
            await session.commit()
    rows = {r["judge_name"]: r for r in await _rows(db, agent)}
    assert rows["second"]["status"] == "timeout" and rows["second"]["labels"] == ["judge_error"]
    same = ("status", "verdict", "confidence", "labels", "reason", "model", "tokens_in", "tokens_out", "calls")
    assert {k: rows["stub"][k] for k in same} == {k: alone_row[k] for k in same}
    assert rows["stub"] == first_row


async def test_a_second_judge_that_raises_gives_its_own_error_row_and_the_first_is_ok(db, agent):
    both = _pair(db, agent, StubJudge(), SecondStub("raise"))
    _start(both)
    _submit(both, await _decision(db, agent))
    await _drain_both(both)
    await both.close()
    rows = {r["judge_name"]: (r["status"], r["verdict"]) for r in await _rows(db, agent)}
    assert rows == {"stub": ("ok", "failure"), "second": ("error", "cannot_tell")}


@pytest.mark.parametrize("spent", ["first", "second"])
async def test_each_judge_has_its_own_daily_limit(db, agent, spent):
    first, second = StubJudge(), SecondStub()
    name = first.name if spent == "first" else second.name
    await _verdict_rows(db, agent, [(name, "ok", 1, datetime.now(UTC))])  # that judge's one call of the day is spent
    both = _pair(db, agent, first, second, outside_judge_daily_calls=1)
    _start(both)
    _submit(both, await _decision(db, agent))
    await _drain_both(both)
    await both.close()
    spent_judge, other_judge = (first, second) if spent == "first" else (second, first)
    spent_service, other_service = (both.primary, both.second) if spent == "first" else (both.second, both.primary)
    assert spent_judge.records == [] and spent_service.runtime()["over_cap"] == 1
    assert len(other_judge.records) == 1 and other_service.runtime()["over_cap"] == 0


async def test_the_runtime_of_a_judge_set_is_the_first_judges_with_the_seconds_under_its_own_key(db, agent):
    both = _pair(db, agent, StubJudge(), SecondStub())
    both.primary.start()
    _submit(both, uuid.uuid4())
    assert both.runtime() == {
        "queued": both.primary.runtime()["queued"],
        "dropped": 0,
        "over_cap": 0,
        "worker_alive": True,
        "second": {"queued": 1, "dropped": 0, "over_cap": 0, "worker_alive": False},
    }
    await both.close()


async def test_close_closes_both_even_when_one_raises(db, agent, caplog):
    second = OutsideJudge(db, _settings(agent), SecondStub(), **FAST)
    second.start()
    both = service_module.JudgeSet(RaisingSubmit(), second)
    with caplog.at_level(logging.WARNING, logger="nous.judge.service"):
        await both.close()
    assert second.runtime()["worker_alive"] is False and _submit(second, uuid.uuid4()) is False
    assert [r for r in caplog.records if "RuntimeError" in r.getMessage() and "broke" not in r.getMessage()]


async def test_a_judge_set_without_a_second_judge_is_the_first_judge_alone(db, agent):
    primary = OutsideJudge(db, _settings(agent), StubJudge(), **FAST)
    alone = service_module.JudgeSet(primary, None)
    assert alone.second is None and alone.judge is primary.judge
    assert _submit(alone, uuid.uuid4()) is True and primary.runtime()["queued"] == 1
    assert alone.runtime() == primary.runtime() == {"queued": 1, "dropped": 0, "over_cap": 0, "worker_alive": False}
    primary.start()
    await alone.close()
    assert primary.runtime()["worker_alive"] is False and _submit(alone, uuid.uuid4()) is False


def test_replacing_the_second_judge_is_synchronous():
    assert not inspect.iscoroutinefunction(service_module.JudgeSet.replace_second)


async def test_replacing_the_second_judge_starts_and_closes_nothing_and_returns_the_old_one(db, agent):
    settings = _settings(agent)
    primary = OutsideJudge(db, settings, StubJudge(), **FAST)
    old, new = OutsideJudge(db, settings, SecondStub(), **FAST), OutsideJudge(db, settings, SecondStub(), **FAST)
    old.start()
    both = service_module.JudgeSet(primary, old)
    assert both.replace_second(new) is old
    assert both.second is new and both.primary is primary
    assert old.runtime()["worker_alive"] is True and new.runtime()["worker_alive"] is False  # the caller's to do
    _submit(both, uuid.uuid4())
    assert (old.runtime()["queued"], new.runtime()["queued"]) == (0, 1)
    assert both.replace_second(None) is new and both.second is None
    assert set(both.runtime()) == {"queued", "dropped", "over_cap", "worker_alive"}
    for service in (primary, old, new):
        await service.close()


async def test_a_submit_reads_the_second_judge_once(db, agent):
    settings = _settings(agent)
    second = OutsideJudge(db, settings, SecondStub(), **FAST)

    class ReplacesWhileSubmitting:
        def submit_decision(self, *args, **kwargs) -> bool:
            both.replace_second(None)  # the second judge is taken away while this submit runs
            return True

    both = service_module.JudgeSet(ReplacesWhileSubmitting(), second)
    assert _submit(both, uuid.uuid4()) is True
    assert second.runtime()["queued"] == 1 and both.second is None
    await second.close()


def test_the_daily_limit_is_the_setting_unless_one_is_given():
    settings = Settings(_env_file=None, agent_id="test-f118-limit", outside_judge_daily_calls=7)
    assert OutsideJudge(None, settings, StubJudge()).daily_calls == 7
    given = OutsideJudge(None, settings, StubJudge(), daily_calls=2)
    assert given.daily_calls == 2
    with pytest.raises(AttributeError):
        given.daily_calls = 5


async def test_a_given_daily_limit_is_the_one_a_service_keeps(db, agent):
    stub = StubJudge()
    service = OutsideJudge(db, _settings(agent, outside_judge_daily_calls=100), stub, daily_calls=1, **FAST)
    service.start()
    for _ in range(2):
        _submit(service, await _decision(db, agent))
        await _drain(service)
    await service.close()
    assert len(stub.records) == 1 and service.runtime()["over_cap"] == 1
    assert stub.max_calls == [1]


# ---- the second judge through the service -------------------------------------------------------------------------


JEV_KEY = "ts-svc-" + "Q9" * 16


def _jev(handler) -> object:
    import httpx

    from nous.judge.jev_judge import JevJudge

    return JevJudge(JEV_KEY, "jev-1.14.0", transport=httpx.MockTransport(handler))


async def test_the_second_judges_answer_is_stored_under_its_name_and_version(db, agent):
    import httpx

    answer = {
        "model": "jev-1.14.0",
        "answers": {
            "verdict": {"type": "choice", "choice": "partial", "confidence": 0.64},
            "sets_verdict": {"type": "noul", "noul": 0.91},
        },
        "usage": {"input_tokens": 3900, "output_tokens": 2},
    }
    service = OutsideJudge(db, _settings(agent), _jev(lambda request: httpx.Response(200, json=answer)), **FAST)
    service.start()
    _submit(service, await _decision(db, agent))
    await _drain(service)
    await service.close()
    [row] = await _rows(db, agent)
    assert {k: row[k] for k in ("judge_name", "judge_version", "model", "status", "verdict", "labels", "reason")} == {
        "judge_name": "jev",
        "judge_version": "jq-2:jev-1.14.0",
        "model": "jev-1.14.0",
        "status": "ok",
        "verdict": "partial",
        "labels": ["asks_for_verdict"],
        "reason": None,
    }
    assert (row["tokens_in"], row["tokens_out"], row["calls"]) == (3900, 2, 1)
    assert abs(row["confidence"] - 0.64) < 1e-6


async def test_the_key_is_in_no_row_and_no_log_record_when_the_request_fails_with_it(db, agent, caplog):
    def fails(request):
        raise RuntimeError(f"refused {request.headers['authorization']}")

    service = OutsideJudge(db, _settings(agent), _jev(fails), **FAST)
    service.start()
    with caplog.at_level(logging.DEBUG):
        _submit(service, await _decision(db, agent))
        await _drain(service)
    await service.close()
    whole_rows = text("SELECT to_jsonb(v)::text FROM nous_system.judge_verdicts v WHERE agent_id = :a")
    async with db.session() as session:
        rows = (await session.execute(whole_rows, {"a": agent})).scalars().all()
    assert len(rows) == 1 and '"status": "error"' in rows[0]
    assert JEV_KEY not in rows[0]
    logged = "\n".join(f"{r.getMessage()} {r.args!r} {r.exc_text or ''}" for r in caplog.records)
    assert JEV_KEY not in logged and "RuntimeError" in logged
