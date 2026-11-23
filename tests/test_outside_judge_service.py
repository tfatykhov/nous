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
