"""F108 Phase 1 (recording): the shared BoundedWriter and the tool-call recorder.

The writer tests need no database. The two row tests write ``nous_system.tool_call_log`` in real Postgres under their
own agent id and delete their rows afterwards.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from nous.api.execution_context import CONTEXT_KINDS
from nous.cognitive import ledger_store
from nous.cognitive.ledger_store import detail_text
from nous.observability.recording import BoundedWriter, RecorderStats
from nous.observability.tool_calls import (
    RESULT_TEXT_MAX_CHARS,
    WORK_KINDS,
    ToolCallRecord,
    ToolCallRecorder,
    work_kind_for,
)

_SETTINGS = SimpleNamespace(tool_input_max_chars=2000, tool_result_preview_chars=500, tool_reason_max_chars=500)
_CUT_MARKER = "\n…[middle cut]…\n"
# A token the ledger's redaction masks (an sk- key needs 20+ characters after the prefix).
_SECRET = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv"


def _agent() -> str:
    return f"test-awv-rec-{uuid.uuid4().hex[:12]}"


async def _rows(db, agent: str) -> list[dict]:
    async with db.engine.connect() as conn:
        result = await conn.execute(
            text("SELECT * FROM nous_system.tool_call_log WHERE agent_id = :a ORDER BY call_index"), {"a": agent}
        )
        return [dict(r) for r in result.mappings().all()]


async def _delete(db, agent: str) -> None:
    async with db.engine.begin() as conn:
        await conn.execute(text("DELETE FROM nous_system.tool_call_log WHERE agent_id = :a"), {"a": agent})


def _record(**overrides) -> ToolCallRecord:
    values = {
        "call_index": 0,
        "work_kind": "conversation",
        "tool_name": "bash",
        "tool_name_called": "bash",
        "outcome": "ran",
    }
    values.update(overrides)
    return ToolCallRecord(**values)


# ---------------------------------------------------------------------------
# Rows in Postgres
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_record_clips_input_to_2000_and_result_to_500(db):
    agent = _agent()
    writer = BoundedWriter("test")
    recorder = ToolCallRecorder(writer, db, agent, _SETTINGS)
    big_input = "i" * 1500 + "MIDDLE-OF-INPUT" + "j" * 1500
    big_result = "r" * 400 + "MIDDLE-OF-RESULT" + "s" * 400
    big_reason = "because " * 200
    big_error = "e" * 3000
    try:
        recorder.record(
            _record(
                input_text=big_input,
                result_preview=big_result,
                reason=big_reason,
                error_text=big_error,
                is_error=True,
                outcome="error",
                turn_id=uuid.uuid4(),
                turn_number=3,
                context_kind="interactive",
                session_id="s-1",
                duration_ms=12,
            )
        )
        await writer.drain(5.0)
        rows = await _rows(db, agent)
        assert len(rows) == 1
        row = rows[0]
        assert row["input_text"] == detail_text(big_input, 2000)
        assert len(row["input_text"]) <= 2000 + len(_CUT_MARKER)
        assert "MIDDLE-OF-INPUT" not in row["input_text"]
        assert row["result_preview"] == detail_text(big_result, 500)
        assert len(row["result_preview"]) <= 500 + len(_CUT_MARKER)
        assert "MIDDLE-OF-RESULT" not in row["result_preview"]
        assert len(row["reason"]) <= 500 + len(_CUT_MARKER)
        assert len(row["error_text"]) <= 1000 + len(_CUT_MARKER)
        assert row["is_error"] is True and row["outcome"] == "error"
        assert row["turn_number"] == 3 and row["context_kind"] == "interactive" and row["duration_ms"] == 12
        assert row["started_at"] is not None
        assert writer.stats.written == 1
    finally:
        await _delete(db, agent)


@pytest.mark.postgres_only
async def test_a_mid_length_result_keeps_its_preview_at_the_prod_scan_pad(db, monkeypatch):
    """Prod's scan pad is 3148 (the longest own secret). A 1500-character result was stored as the cut marker alone:
    its scan windows covered the whole text and the pad was still dropped from each side."""
    monkeypatch.setattr(ledger_store, "redaction_scan_pad", lambda: 3148)
    agent = _agent()
    writer = BoundedWriter("test")
    recorder = ToolCallRecorder(writer, db, agent, _SETTINGS)
    result = ("RESULT-START " + "the command printed this line\n" * 60)[:1490] + " RESULT-END"
    try:
        recorder.record(_record(result_preview=result))
        await writer.drain(5.0)
        rows = await _rows(db, agent)
        assert len(rows) == 1
        preview = rows[0]["result_preview"]
        assert preview != _CUT_MARKER
        assert preview.startswith("RESULT-START") and preview.endswith(" RESULT-END")
        assert len(preview) == 500 + len(_CUT_MARKER)
    finally:
        await _delete(db, agent)


@pytest.mark.postgres_only
async def test_secret_in_input_is_masked_before_store(db):
    agent = _agent()
    writer = BoundedWriter("test")
    recorder = ToolCallRecorder(writer, db, agent, _SETTINGS)
    started = datetime(2026, 11, 7, 12, 30, 15, 123000, tzinfo=UTC)
    try:
        recorder.record(
            _record(
                started_at=started,
                input_text=f'{{"command": "curl -H x-api-key:{_SECRET} https://example"}}',
                result_preview=f"echoed {_SECRET}",
                reason=f"uses {_SECRET}",
                error_text=f"failed with {_SECRET}",
            )
        )
        await writer.drain(5.0)
        rows = await _rows(db, agent)
        assert len(rows) == 1
        stored = " ".join(str(rows[0][k]) for k in ("input_text", "result_preview", "reason", "error_text"))
        assert _SECRET not in stored
        assert "AbCdEfGhIjKlMnOpQrStUv" not in stored
        assert rows[0]["started_at"] == started  # a given start is kept, not replaced by now()
    finally:
        await _delete(db, agent)


# ---------------------------------------------------------------------------
# Failure handling, bounds and counters (no database)
# ---------------------------------------------------------------------------


class _BrokenDatabase:
    def session(self):
        raise RuntimeError("database is down")


async def test_record_never_raises_when_database_fails(caplog):
    writer = BoundedWriter("test")
    recorder = ToolCallRecorder(writer, _BrokenDatabase(), "test-awv-broken", _SETTINGS)
    with caplog.at_level(logging.DEBUG, logger="nous.observability"):
        assert recorder.record(_record()) is None  # returns, does not raise
        await writer.drain(1.0)
    assert writer.stats.dropped_error == 1
    assert recorder.stats.dropped_error == 1
    assert writer.stats.written == 0
    failures = [r for r in caplog.records if "database is down" in r.getMessage()]
    assert failures and all(r.levelno == logging.WARNING for r in failures)


async def test_write_timeout_is_counted(caplog):
    writer = BoundedWriter("test", timeout_s=0.05)

    async def slow() -> None:
        await asyncio.sleep(1.0)

    with caplog.at_level(logging.DEBUG, logger="nous.observability"):
        assert writer.submit(slow) is True
        await writer.drain(2.0)
    assert writer.stats.dropped_timeout == 1
    assert writer.stats.written == 0
    assert any(r.levelno == logging.WARNING and "timed out" in r.getMessage() for r in caplog.records)


async def test_pending_cap_drops_and_counts():
    writer = BoundedWriter("test", max_pending=3)
    release = asyncio.Event()

    async def slow() -> None:
        await release.wait()

    accepted = [writer.submit(slow) for _ in range(10)]
    assert accepted == [True] * 3 + [False] * 7
    assert writer.stats.dropped_backpressure == 7
    release.set()
    await writer.drain(2.0)
    assert writer.stats.written == 3


async def test_concurrency_is_bounded():
    writer = BoundedWriter("test")
    running = 0
    peak = 0

    async def write() -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1

    for _ in range(12):
        assert writer.submit(write) is True
    await writer.drain(5.0)
    assert peak == 4
    assert writer.stats.written == 12


async def test_two_callers_sharing_a_writer_keep_separate_counts():
    writer = BoundedWriter("test", timeout_s=0.05, max_pending=4)
    mine, theirs = RecorderStats(), RecorderStats()

    async def ok() -> None:
        return None

    async def broken() -> None:
        raise RuntimeError("write failed")

    async def slow() -> None:
        await asyncio.sleep(1.0)

    assert writer.submit(ok, stats=mine) is True
    assert writer.submit(ok, stats=mine) is True
    assert writer.submit(broken, stats=mine) is True
    assert writer.submit(slow, stats=theirs) is True
    assert writer.submit(ok, stats=theirs) is False  # the cap of 4 is shared
    await writer.drain(2.0)
    assert mine.as_dict() == {
        "written": 2,
        "dropped_timeout": 0,
        "dropped_error": 1,
        "dropped_backpressure": 0,
        "unlabelled": 0,
        "dropped_late": 0,
    }
    assert theirs.as_dict() == {
        "written": 0,
        "dropped_timeout": 1,
        "dropped_error": 0,
        "dropped_backpressure": 1,
        "unlabelled": 0,
        "dropped_late": 0,
    }
    assert writer.stats.as_dict() == {
        "written": 2,
        "dropped_timeout": 1,
        "dropped_error": 1,
        "dropped_backpressure": 1,
        "unlabelled": 0,
        "dropped_late": 0,
    }


async def test_recorders_sharing_a_writer_count_their_own_writes():
    writer = BoundedWriter("test")
    first = ToolCallRecorder(writer, _BrokenDatabase(), "test-awv-broken-1", _SETTINGS)
    second = ToolCallRecorder(writer, _BrokenDatabase(), "test-awv-broken-2", _SETTINGS)
    first.record(_record())
    first.record(_record(call_index=1))
    second.record(_record())
    await writer.drain(1.0)
    assert first.stats is not writer.stats
    assert first.stats.dropped_error == 2
    assert second.stats.dropped_error == 1
    assert writer.stats.dropped_error == 3


async def test_drain_with_nothing_pending_returns():
    writer = BoundedWriter("test")
    await writer.drain(0.1)
    assert writer.stats.as_dict() == RecorderStats().as_dict()


def test_submit_without_a_loop_is_counted_not_raised():
    writer = BoundedWriter("test")
    called = []

    async def write() -> None:
        called.append(1)

    assert writer.submit(lambda: write()) is False
    assert called == []
    assert writer.stats.dropped_error == 1


def test_stats_as_dict():
    stats = RecorderStats(
        written=1, dropped_timeout=2, dropped_error=3, dropped_backpressure=4, unlabelled=5, dropped_late=6
    )
    assert stats.as_dict() == {
        "written": 1,
        "dropped_timeout": 2,
        "dropped_error": 3,
        "dropped_backpressure": 4,
        "unlabelled": 5,
        "dropped_late": 6,
    }


# ---------------------------------------------------------------------------
# Work kinds
# ---------------------------------------------------------------------------


def test_work_kind_mapping():
    assert set(WORK_KINDS) == {
        "conversation",
        "subtask",
        "workflow_step",
        "intention",
        "heartbeat",
        "background_check",
    }
    for kind in CONTEXT_KINDS:
        assert work_kind_for(kind) in WORK_KINDS, kind
    assert work_kind_for("dag_node") == "workflow_step"
    # Not in the plan's paragraph; ruled by the lead: an owner-approved proposal is intention work.
    assert work_kind_for("approved_action") == "intention"
    expected = {
        "interactive": "conversation",
        "mcp": "conversation",
        "agent_action": "conversation",
        "subtask": "subtask",
        "scheduled": "subtask",
        "dag_node": "workflow_step",
        "continuation": "intention",
        "approved_action": "intention",
        "heartbeat_triage": "heartbeat",
        "heartbeat_check": "background_check",
        "heartbeat_callback": "background_check",
        "background": "background_check",
        "dag_summary": "background_check",
    }
    assert set(expected) == set(CONTEXT_KINDS)
    for kind, work_kind in expected.items():
        assert work_kind_for(kind) == work_kind, kind
    # An unknown kind still gets a value: work_kind is NOT NULL and recording never fails.
    assert work_kind_for("no-such-kind") == "background_check"


# ---------------------------------------------------------------------------------------------------------------------
# result_text: the redacted full result of a call whose preview was cut
# ---------------------------------------------------------------------------------------------------------------------

_BEARER = "Bearer abcdEFGH1234ijklMNOP5678qrstUVWX9012"


async def _record_one(db, **overrides) -> dict:
    agent = _agent()
    writer = BoundedWriter("test")
    recorder = ToolCallRecorder(writer, db, agent, _SETTINGS)
    try:
        recorder.record(_record(**overrides))
        await writer.drain(5.0)
        rows = await _rows(db, agent)
        assert len(rows) == 1
        return rows[0]
    finally:
        await _delete(db, agent)


@pytest.mark.postgres_only
async def test_a_cut_preview_keeps_the_full_result_redacted_up_to_the_cap(db):
    result = "head-" + "plain words here. " * 300 + "-tail"
    row = await _record_one(db, result_preview=result)
    assert row["result_preview"] == detail_text(result, 500)
    assert row["result_text"] == detail_text(result, RESULT_TEXT_MAX_CHARS) == result  # under the cap: whole


@pytest.mark.postgres_only
async def test_a_huge_result_is_cut_at_20000_with_the_marker(db):
    assert RESULT_TEXT_MAX_CHARS == 20_000
    result = "HEAD" + "lorem ipsum. " * 8000 + "TAIL"
    row = await _record_one(db, result_preview=result)
    assert len(row["result_text"]) <= RESULT_TEXT_MAX_CHARS + len(_CUT_MARKER)
    assert _CUT_MARKER in row["result_text"]
    assert row["result_text"].startswith("HEAD") and row["result_text"].endswith("TAIL")


@pytest.mark.postgres_only
@pytest.mark.parametrize("result", ["", "short", "ok. " * 125], ids=["empty", "short", "exactly the cap"])
async def test_a_result_the_preview_holds_whole_stores_no_full_copy(db, result):
    row = await _record_one(db, result_preview=result)
    assert row["result_text"] is None
    assert row["result_preview"] == result


@pytest.mark.postgres_only
async def test_an_error_stores_no_full_copy(db):
    row = await _record_one(db, result_preview="boom " * 400, is_error=True, outcome="error", error_text="boom")
    assert row["result_text"] is None


@pytest.mark.postgres_only
@pytest.mark.parametrize(
    "where",
    ["start", "middle", "end", "straddling the preview cut", "straddling the head cut", "straddling the tail cut"],
)
async def test_a_bearer_token_is_never_stored_in_the_full_result(db, where):
    filler = "z " * 3000
    if where == "start":
        result = f"{_BEARER} {filler}"
    elif where == "middle":
        result = f"{filler} {_BEARER} {filler}"
    elif where == "end":
        result = f"{filler} {_BEARER}"
    elif where == "straddling the preview cut":
        result = "a" * 230 + f" {_BEARER} " + filler  # the token runs over the preview's 250-character head
    elif where == "straddling the head cut":
        # the token starts at offset 9,981 and runs past 10,000, where the 20,000-character cap keeps its head
        result = "b " * 4990 + f" {_BEARER} " + "c " * 8000
    else:
        # the last 10,000 characters are kept as the tail: it starts inside the token
        result = "b " * 8000 + f" {_BEARER} " + "c " * 4990
    row = await _record_one(db, result_preview=result)
    token = _BEARER.split(" ", 1)[1]
    assert token not in row["result_text"] and token not in row["result_preview"]
    assert row["result_text"] is not None
