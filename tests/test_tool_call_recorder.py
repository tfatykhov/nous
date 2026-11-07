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
from nous.cognitive.ledger_store import detail_text
from nous.observability.recording import BoundedWriter, RecorderStats
from nous.observability.tool_calls import WORK_KINDS, ToolCallRecord, ToolCallRecorder, work_kind_for

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
    stats = RecorderStats(written=1, dropped_timeout=2, dropped_error=3, dropped_backpressure=4, unlabelled=5)
    assert stats.as_dict() == {
        "written": 1,
        "dropped_timeout": 2,
        "dropped_error": 3,
        "dropped_backpressure": 4,
        "unlabelled": 5,
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
