"""F108 Phase 1 (recording), PR 2: the model-call recorder, its call context and the context estimate.

The row tests write ``nous_system.model_call_log`` and ``nous_system.model_call_attempt`` in real Postgres under their
own agent id and delete their rows afterwards (the attempts go with their call: ``ON DELETE CASCADE``). The rest need
no database.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from nous.api.anthropic_client import StreamEvent
from nous.api.models import ApiResponse
from nous.observability import context_logger, model_calls
from nous.observability.context_logger import ContextLogEntry
from nous.observability.model_calls import (
    MAIN_LOOP_PURPOSES,
    PURPOSES,
    QUIET_PURPOSES,
    CallContext,
    ContextStats,
    ModelCallRecorder,
    analyze_context,
    analyze_sections,
    context_kwargs,
    current_fallback,
    model_call_context,
    parse_system_sections,
)
from nous.observability.recording import BoundedWriter

_SETTINGS = SimpleNamespace()
_CUT_MARKER = "\n…[middle cut]…\n"
# A token the ledger's redaction masks (an sk- key needs 20+ characters after the prefix).
_SECRET = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv"
_PAYLOAD = {"model": "claude-test-model", "max_tokens": 10}
_USAGE = {"input_tokens": 2, "cache_read_input_tokens": 20000, "cache_creation_input_tokens": 500, "output_tokens": 40}


def _agent() -> str:
    return f"test-awv-mc-{uuid.uuid4().hex[:12]}"


async def _rows(db, agent: str) -> list[dict]:
    async with db.engine.connect() as conn:
        result = await conn.execute(
            text("SELECT * FROM nous_system.model_call_log WHERE agent_id = :a ORDER BY started_at, purpose, status"),
            {"a": agent},
        )
        return [dict(r) for r in result.mappings().all()]


async def _attempts(db, call_id: uuid.UUID) -> list[dict]:
    async with db.engine.connect() as conn:
        result = await conn.execute(
            text("SELECT * FROM nous_system.model_call_attempt WHERE call_id = :c ORDER BY attempt_no"), {"c": call_id}
        )
        return [dict(r) for r in result.mappings().all()]


async def _delete(db, agent: str) -> None:
    async with db.engine.begin() as conn:
        await conn.execute(text("DELETE FROM nous_system.model_call_log WHERE agent_id = :a"), {"a": agent})


def _json(value):
    """A jsonb value read through ``text()`` comes back as its JSON text."""
    return json.loads(value) if isinstance(value, str) else value


def _recorder(db, agent: str) -> tuple[BoundedWriter, ModelCallRecorder]:
    writer = BoundedWriter("test")
    return writer, ModelCallRecorder(writer, db, agent, _SETTINGS)


# ---------------------------------------------------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------------------------------------------------


def test_purpose_sets_are_nested():
    assert "unlabelled" in PURPOSES and {"embedding", "rerank", "chat", "critic"} <= PURPOSES
    assert QUIET_PURPOSES == {"heartbeat", "background_check"}
    assert QUIET_PURPOSES <= MAIN_LOOP_PURPOSES <= PURPOSES
    assert CallContext().purpose == "unlabelled"


# ---------------------------------------------------------------------------------------------------------------------
# Rows in Postgres
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_input_total_is_the_sum(db):
    agent = _agent()
    try:
        async with db.engine.begin() as conn:
            total = (
                await conn.execute(
                    text(
                        "INSERT INTO nous_system.model_call_log "
                        "(agent_id, purpose, label_source, call_kind, status, input_uncached, cache_read, cache_write) "
                        "VALUES (:a, 'chat', 'explicit', 'call', 'ok', 2, 20000, 500) RETURNING input_total"
                    ),
                    {"a": agent},
                )
            ).scalar_one()
            assert total == 20502
            # a missing part counts as zero
            partial = (
                await conn.execute(
                    text(
                        "INSERT INTO nous_system.model_call_log "
                        "(agent_id, purpose, label_source, call_kind, status, cache_read) "
                        "VALUES (:a, 'chat', 'explicit', 'call', 'ok', 7) RETURNING input_total"
                    ),
                    {"a": agent},
                )
            ).scalar_one()
            assert partial == 7
    finally:
        await _delete(db, agent)


@pytest.mark.postgres_only
async def test_scope_writes_one_row_with_usage_and_duration(db):
    agent = _agent()
    writer, recorder = _recorder(db, agent)
    turn_id, subtask_id, dag_id, group_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    stats = dataclasses.replace(
        analyze_context(SYSTEM, MESSAGES, TOOLS, 200_000),
        cache_break=True,
        cache_break_components=["tools"],
        cache_break_tokens_lost=1234,
    )
    ctx = CallContext(
        purpose="chat",
        session_id="s-1",
        parent_session_id="p-1",
        turn_id=turn_id,
        turn_number=4,
        frame_id="task",
        subtask_id=subtask_id,
        dag_id=dag_id,
        trace_id="trace-1",
        stats=stats,
        group_id=group_id,
        is_fallback=True,
    )
    try:
        with recorder.scope(_PAYLOAD, ctx, "call") as scope:
            time.sleep(0.03)  # not asyncio.sleep: on Windows a loop timer may fire a clock tick early
            scope.finish(ApiResponse(content=[], stop_reason="end_turn", usage=dict(_USAGE)))
        await writer.drain(5.0)
        rows = await _rows(db, agent)
        assert len(rows) == 1
        row = rows[0]
        assert (row["purpose"], row["label_source"], row["call_kind"], row["status"]) == (
            "chat",
            "explicit",
            "call",
            "ok",
        )
        assert row["model"] == "claude-test-model" and row["stop_reason"] == "end_turn"
        usage = (row["input_uncached"], row["cache_read"], row["cache_write"], row["output_tokens"])
        assert usage == (2, 20000, 500, 40)
        assert row["input_total"] == 20502
        assert row["duration_ms"] >= 30
        assert (row["attempts"], row["call_count"]) == (1, 1)
        assert row["http_status"] is None and row["error_text"] is None
        assert row["started_at"].tzinfo is not None
        # the call context
        assert (row["session_id"], row["parent_session_id"], row["turn_id"], row["turn_number"]) == (
            "s-1",
            "p-1",
            turn_id,
            4,
        )
        assert (row["frame_id"], row["subtask_id"], row["dag_id"], row["trace_id"]) == (
            "task",
            subtask_id,
            dag_id,
            "trace-1",
        )
        assert (row["group_id"], row["is_fallback"]) == (group_id, True)
        # the context estimate
        assert _json(row["section_tokens_est"]) == stats.section_tokens_est
        assert row["total_tokens_est"] == stats.total_tokens_est
        assert row["context_window_size"] == 200_000
        assert row["utilization_pct"] == pytest.approx(stats.utilization_pct)
        assert row["sections_present"] == stats.sections_present
        assert (row["tools_count"], row["tool_names"]) == (2, ["recall_deep", "bash"])
        assert (row["messages_count"], _json(row["message_roles"])) == (3, {"user": 2, "assistant": 1})
        assert (row["loaded_facts"], row["loaded_decisions"], row["loaded_procedures"]) == (3, 1, 3)
        assert (row["loaded_episodes"], row["recent_conversations"]) == (2, 1)
        assert (row["cache_break"], row["cache_break_components"], row["cache_break_tokens_lost"]) == (
            True,
            ["tools"],
            1234,
        )
        assert writer.stats.written == 1 and recorder.stats.written == 1
    finally:
        await _delete(db, agent)


@pytest.mark.postgres_only
async def test_stream_events_merge_usage_max_per_key(db):
    agent = _agent()
    writer, recorder = _recorder(db, agent)
    try:
        with recorder.scope(_PAYLOAD, CallContext(purpose="chat"), "stream") as scope:
            scope.observe_stream_event(
                StreamEvent(
                    type="message_start",
                    usage={
                        "input_tokens": 2,
                        "cache_read_input_tokens": 20000,
                        "cache_creation_input_tokens": 500,
                        "output_tokens": 1,
                    },
                )
            )
            scope.observe_stream_event(StreamEvent(type="text_delta", text="hi"))
            # a later event never lowers a count, and done gives the output only
            scope.observe_stream_event(
                StreamEvent(type="done", stop_reason="tool_use", usage={"output_tokens": 77, "input_tokens": 0})
            )
        await writer.drain(5.0)
        [row] = await _rows(db, agent)
        usage = (row["input_uncached"], row["cache_read"], row["cache_write"], row["output_tokens"])
        assert usage == (2, 20000, 500, 77)
        assert (row["call_kind"], row["status"], row["stop_reason"]) == ("stream", "ok", "tool_use")
    finally:
        await _delete(db, agent)


@pytest.mark.postgres_only
async def test_unknown_purpose_becomes_unlabelled_and_counted(db):
    agent = _agent()
    writer, recorder = _recorder(db, agent)
    try:
        with recorder.scope(_PAYLOAD, CallContext(purpose="not-a-purpose", session_id="s-x"), "call"):
            pass
        assert recorder.stats.unlabelled == 1
        assert current_fallback() is None
        with recorder.scope(_PAYLOAD, None, "call"):  # no context at all
            pass
        assert recorder.stats.unlabelled == 2
        with recorder.scope(_PAYLOAD, CallContext(purpose="critic"), "call"):
            pass
        assert recorder.stats.unlabelled == 2
        await writer.drain(5.0)
        rows = await _rows(db, agent)
        by_source = {(r["purpose"], r["label_source"]) for r in rows}
        assert by_source == {("unlabelled", "explicit"), ("unlabelled", "none"), ("critic", "explicit")}
        # an explicit context keeps its session even when its purpose is not one of ours
        assert (
            next(r for r in rows if r["label_source"] == "explicit" and r["purpose"] == "unlabelled")["session_id"]
            == "s-x"
        )
    finally:
        await _delete(db, agent)


@pytest.mark.postgres_only
async def test_explicit_context_beats_contextvar(db):
    agent = _agent()
    writer, recorder = _recorder(db, agent)
    try:
        with model_call_context(purpose="reflection", session_id="fallback-session", turn_number=9):
            assert current_fallback() == CallContext(purpose="reflection", session_id="fallback-session", turn_number=9)
            with recorder.scope(_PAYLOAD, CallContext(purpose="critic", session_id="explicit-session"), "call"):
                pass
            with recorder.scope(_PAYLOAD, None, "aggregated"):
                pass
        assert current_fallback() is None
        await writer.drain(5.0)
        rows = {r["label_source"]: r for r in await _rows(db, agent)}
        assert set(rows) == {"explicit", "context"}
        assert (rows["explicit"]["purpose"], rows["explicit"]["session_id"]) == ("critic", "explicit-session")
        assert rows["explicit"]["turn_number"] is None  # nothing is taken from the fallback
        assert (rows["context"]["purpose"], rows["context"]["session_id"], rows["context"]["turn_number"]) == (
            "reflection",
            "fallback-session",
            9,
        )
        assert rows["context"]["call_kind"] == "aggregated"
        assert recorder.stats.unlabelled == 0
    finally:
        await _delete(db, agent)


@pytest.mark.postgres_only
async def test_contextvar_read_once_at_scope_start(db):
    agent = _agent()
    writer, recorder = _recorder(db, agent)
    try:
        with model_call_context(purpose="critic", session_id="first"):
            scope = recorder.scope(_PAYLOAD, None, "call")
        with model_call_context(purpose="reflection", session_id="second"), scope:
            pass
        await writer.drain(5.0)
        [row] = await _rows(db, agent)
        assert (row["purpose"], row["session_id"], row["label_source"]) == ("critic", "first", "context")
    finally:
        await _delete(db, agent)


class _StatusError(Exception):
    """An HTTP error shaped like the SDK's (``status_code``) or httpx's (``response.status_code``)."""

    def __init__(self, message: str, status_code: int | None = None, response_status: int | None = None) -> None:
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code
        if response_status is not None:
            self.response = SimpleNamespace(status_code=response_status)


@pytest.mark.postgres_only
async def test_error_status_recorded_with_http_status_and_redacted_text(db):
    agent = _agent()
    writer, recorder = _recorder(db, agent)
    long_tail = "x" * 3000
    try:
        # fail() called by the client, which then raises on
        with pytest.raises(_StatusError):
            with recorder.scope(_PAYLOAD, CallContext(purpose="chat", session_id="a"), "call") as scope:
                exc = _StatusError(f"overloaded, key {_SECRET} {long_tail}", status_code=529)
                scope.fail(exc)
                raise exc
        # an exception that leaves the scope without fail() is recorded too
        with pytest.raises(_StatusError):
            with recorder.scope(_PAYLOAD, CallContext(purpose="chat", session_id="b"), "call"):
                raise _StatusError(f"bad request {_SECRET}", response_status=400)
        await writer.drain(5.0)
        rows = {r["session_id"]: r for r in await _rows(db, agent)}
        assert (rows["a"]["status"], rows["a"]["http_status"]) == ("error", 529)
        assert (rows["b"]["status"], rows["b"]["http_status"]) == ("error", 400)
        for row in rows.values():
            assert _SECRET not in row["error_text"]
            assert "_StatusError" in row["error_text"]
            assert row["output_tokens"] is None and row["input_total"] == 0
        assert len(rows["a"]["error_text"]) <= 1000 + len(_CUT_MARKER)
        assert _CUT_MARKER in rows["a"]["error_text"]
    finally:
        await _delete(db, agent)


@pytest.mark.postgres_only
async def test_cancelled_scope_records_cancelled(db):
    agent = _agent()
    writer, recorder = _recorder(db, agent)
    entered = asyncio.Event()

    async def call() -> None:
        with recorder.scope(_PAYLOAD, CallContext(purpose="chat", session_id="task"), "call"):
            entered.set()
            await asyncio.sleep(30)

    async def stream():
        with recorder.scope(_PAYLOAD, CallContext(purpose="chat", session_id="generator"), "stream"):
            yield 1
            yield 2

    try:
        task = asyncio.create_task(call())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        gen = stream()
        assert await gen.__anext__() == 1
        await gen.aclose()  # GeneratorExit at the yield
        await writer.drain(5.0)
        rows = {r["session_id"]: r for r in await _rows(db, agent)}
        assert rows["task"]["status"] == "cancelled" and rows["generator"]["status"] == "cancelled"
        assert rows["task"]["error_text"] is None
        assert model_calls._ACTIVE_SCOPE.get() is None
    finally:
        await _delete(db, agent)


@pytest.mark.postgres_only
async def test_attempts_rows_written_and_count_matches(db):
    agent = _agent()
    writer, recorder = _recorder(db, agent)
    try:
        assert model_calls._ACTIVE_SCOPE.get() is None
        with recorder.scope(_PAYLOAD, CallContext(purpose="chat"), "call") as scope:
            # a scope is the active one only while a client's request is bound to it
            assert model_calls._ACTIVE_SCOPE.get() is None
            with scope.bind_for_request():
                # what the SDK client's response hook does: append to the scope that is active
                active = model_calls.active_scope()
                assert active is scope
                active.attempt(529, "overloaded_error", f"Overloaded {_SECRET}", 812.4)
                active.attempt(529, "overloaded_error", "x" * 3000, 640)
                active.attempt(200, None, None, 1500)
            assert model_calls._ACTIVE_SCOPE.get() is None
            scope.finish({"stop_reason": "end_turn", "usage": dict(_USAGE)})  # a raw response dict works too
        assert model_calls._ACTIVE_SCOPE.get() is None
        await writer.drain(5.0)
        [row] = await _rows(db, agent)
        attempts = await _attempts(db, row["id"])
        assert row["attempts"] == len(attempts) == 3
        assert [a["attempt_no"] for a in attempts] == [1, 2, 3]
        assert [a["http_status"] for a in attempts] == [529, 529, 200]
        assert [a["duration_ms"] for a in attempts] == [812, 640, 1500]
        assert [a["error_type"] for a in attempts] == ["overloaded_error", "overloaded_error", None]
        assert all(a["agent_id"] == agent for a in attempts)
        assert _SECRET not in attempts[0]["error_text"] and "Overloaded" in attempts[0]["error_text"]
        assert len(attempts[1]["error_text"]) <= 1000 + len(_CUT_MARKER)
        assert attempts[2]["error_text"] is None
        # an attempt started its duration before it was reported (the hook runs when the response arrives)
        assert attempts[2]["started_at"] < attempts[0]["started_at"] < attempts[1]["started_at"]
        # the call's own status is the last attempt's when the client gave none
        assert (row["status"], row["http_status"], row["input_total"]) == ("ok", 200, 20502)
        # the attempts go with their call
        await _delete(db, agent)
        assert await _attempts(db, row["id"]) == []
    finally:
        await _delete(db, agent)


@pytest.mark.postgres_only
async def test_two_concurrent_scopes_do_not_cross(db):
    agent = _agent()
    writer, recorder = _recorder(db, agent)
    a_in, b_in, a_go = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def call(session: str, entered: asyncio.Event, wait_for: asyncio.Event, output: int, status: int) -> None:
        with model_call_context(purpose="critic" if session == "A" else "reflection", session_id=session):
            with recorder.scope(_PAYLOAD, None, "call") as scope, scope.bind_for_request():
                entered.set()
                await asyncio.wait_for(wait_for.wait(), 10)  # bounded: a broken scope fails the test, never hangs it
                model_calls._ACTIVE_SCOPE.get().attempt(status, None, None, 10)
                await asyncio.sleep(0)
                scope.finish(ApiResponse(content=[], stop_reason="end_turn", usage={"output_tokens": output}))

    try:
        # A enters first, B enters while A waits, A finishes while B waits, then B finishes
        task_a = asyncio.create_task(call("A", a_in, a_go, 111, 200))
        await asyncio.wait_for(a_in.wait(), 10)
        b_go = asyncio.Event()
        task_b = asyncio.create_task(call("B", b_in, b_go, 222, 201))
        await asyncio.wait_for(b_in.wait(), 10)
        a_go.set()
        await task_a
        b_go.set()
        await task_b
        await writer.drain(5.0)
        rows = {r["session_id"]: r for r in await _rows(db, agent)}
        assert (rows["A"]["purpose"], rows["A"]["output_tokens"], rows["A"]["http_status"]) == ("critic", 111, 200)
        assert (rows["B"]["purpose"], rows["B"]["output_tokens"], rows["B"]["http_status"]) == ("reflection", 222, 201)
        for row in rows.values():
            assert row["attempts"] == 1 and len(await _attempts(db, row["id"])) == 1
    finally:
        await _delete(db, agent)


@pytest.mark.postgres_only
async def test_bucket_aggregates_per_minute(db, monkeypatch):
    agent = _agent()
    writer, recorder = _recorder(db, agent)
    minute = datetime(2026, 11, 7, 12, 30, tzinfo=UTC)
    monkeypatch.setattr(model_calls, "_utcnow", lambda: minute + timedelta(seconds=5))
    try:
        recorder.record_bucket("embedding", "text-embedding-3-small", 100, 12.5, "ok")
        recorder.record_bucket("embedding", "text-embedding-3-small", 50, 7.5, "ok")
        recorder.record_bucket("embedding", "text-embedding-3-small", None, 30.0, "error")
        recorder.record_bucket("rerank", "ettin-17m", None, 4.0, "ok")
        monkeypatch.setattr(model_calls, "_utcnow", lambda: minute + timedelta(seconds=65))
        recorder.record_bucket("embedding", "text-embedding-3-small", 10, 1.0, "ok")
        await writer.drain(5.0)
        rows = await _rows(db, agent)
        got = {
            (r["started_at"], r["purpose"], r["call_kind"], r["model"], r["status"]): (
                r["call_count"],
                r["input_uncached"],
                r["duration_ms"],
            )
            for r in rows
        }
        later = minute + timedelta(minutes=1)
        assert got == {
            (minute, "embedding", "embedding", "text-embedding-3-small", "ok"): (2, 150, 20),
            (minute, "embedding", "embedding", "text-embedding-3-small", "error"): (1, None, 30),
            (minute, "rerank", "rerank", "ettin-17m", "ok"): (1, None, 4),
            (later, "embedding", "embedding", "text-embedding-3-small", "ok"): (1, 10, 1),
        }
        assert all(r["label_source"] == "explicit" and r["session_id"] is None for r in rows)
        assert all(r["attempts"] == 1 for r in rows)
        assert recorder.stats.written == 5
    finally:
        await _delete(db, agent)


# ---------------------------------------------------------------------------------------------------------------------
# No database
# ---------------------------------------------------------------------------------------------------------------------


def test_context_kwargs_skips_clients_without_support():
    ctx = CallContext(purpose="critic")

    class Supported:
        supports_call_context = True

    class Plain:
        async def call(self, payload):  # a test double that takes only the payload
            return payload

    assert context_kwargs(Supported(), ctx) == {"call_context": ctx}
    assert context_kwargs(Plain(), ctx) == {}
    assert context_kwargs(SimpleNamespace(supports_call_context=False), ctx) == {}


def test_scope_outside_a_loop_drops_the_row_counts_it_and_never_raises():
    class Boom:
        def session(self):
            raise AssertionError("never reached: there is no running loop")

    writer = BoundedWriter("test")
    recorder = ModelCallRecorder(writer, Boom(), "agent", _SETTINGS)
    # no running event loop: the writer drops the write and counts it, the scope still exits cleanly
    with recorder.scope(_PAYLOAD, CallContext(purpose="chat"), "call") as scope:
        scope.finish(object())  # a response with nothing in it
    assert recorder.stats.dropped_error == 1
    assert model_calls._ACTIVE_SCOPE.get() is None


# The old ContextLogEntry.from_payload gave these for this payload (captured before the estimate moved), except where
# Task 10 changed the estimate on purpose: the tool_result's content ("PR 1 shipped", 12 characters) now counts, so
# messages went 16 -> 19, total_tokens_est 149 -> 152 and utilization_pct 0.07 -> 0.08 (152 / 200,000). Every section
# heading here is in the marker table, so no section changed.
SYSTEM = (
    "You are Nous, a cognitive agent.\n\n"
    "## Current Date/Time\n2026-11-07 12:00 UTC\n"
    "## Relevant Facts\n- [user] likes tea\n- [user] lives in Toronto\n- the deploy host is pirate\n"
    "## Related Decisions\n- use pgvector\n"
    "## Known Procedures\n- **deploy**: build, push, restart\n  - with a nested bullet\n- **test**: run pytest\n"
    "## Procedure Catalog\n- release (ops)\n"
    "## Past Episodes\n- [2026-11-01] shipped PR 1\n- [2026-11-02] reviewed PR 2\n"
    "## Recent Conversations\n- [s-1] hello\n"
    "[Execution Ledger]\nnothing pending"
)
MESSAGES = [
    {"role": "user", "content": "what did we ship last week?"},
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Let me look."},
            {"type": "tool_use", "id": "t1", "name": "recall_deep", "input": {"query": "shipped last week"}},
        ],
    },
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "PR 1 shipped"}]},
]
TOOLS = [
    {
        "name": "recall_deep",
        "description": "Search memory.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
    },
    {"name": "bash", "description": "Run a command.", "input_schema": {"type": "object"}},
]
OLD_ENTRY = {
    "token_breakdown": {
        "datetime": 5,
        "relevant_facts": 18,
        "related_decisions": 3,
        "known_procedures": 20,
        "procedure_catalog": 3,
        "past_episodes": 14,
        "recent_conversations": 3,
        "execution_ledger": 3,
        "other": 8,
        "tools_definition": 56,
        "messages": 19,  # was 16: the tool_result's content counts (Task 10)
    },
    "total_tokens_est": 152,  # was 149
    "context_window_size": 200000,
    "utilization_pct": 0.08,  # was 0.07
    "sections_present": [
        "datetime",
        "relevant_facts",
        "related_decisions",
        "known_procedures",
        "procedure_catalog",
        "past_episodes",
        "recent_conversations",
        "execution_ledger",
        "other",
    ],
    "tools_count": 2,
    "tool_names": ["recall_deep", "bash"],
    "messages_count": 3,
    "message_roles": {"user": 2, "assistant": 1},
    "loaded_facts": 3,
    "loaded_decisions": 1,
    "loaded_procedures": 3,
    "loaded_episodes": 2,
    "recent_conversations": 1,
}


def test_analyze_context_matches_old_context_log_entry():
    stats = analyze_context(SYSTEM, MESSAGES, TOOLS, 200_000)
    assert isinstance(stats, ContextStats)
    entry = ContextLogEntry.from_payload(
        session_id="s",
        turn_number=1,
        call_type="chat",
        model="m",
        system_prompt=SYSTEM,
        messages=MESSAGES,
        tools=TOOLS,
        frame_id="f",
        context_window=200_000,
    )
    # the entry is what it was before the estimate moved (with Task 10's changes, above)
    for name, value in OLD_ENTRY.items():
        assert getattr(entry, name) == value, name
    # and the stats give the same fields (the section estimates under their new name)
    assert stats.section_tokens_est == entry.token_breakdown
    for name in OLD_ENTRY.keys() - {"token_breakdown"}:
        assert getattr(stats, name) == getattr(entry, name), name
    assert entry.sections_text["relevant_facts"].startswith("- [user] likes tea")
    # the cache-break fields are the runner's to set
    assert (stats.cache_break, stats.cache_break_components, stats.cache_break_tokens_lost) == (False, [], 0)
    # system blocks read as the runner's flat prompt (the blocks joined by a blank line)
    half = SYSTEM.index("## Past Episodes")
    blocks = [
        {"type": "text", "text": SYSTEM[:half].rstrip("\n")},
        {"type": "text", "text": SYSTEM[half:], "cache_control": {"type": "ephemeral"}},
    ]
    assert analyze_context(blocks, MESSAGES, TOOLS, 200_000) == analyze_context(
        "\n\n".join(b["text"] for b in blocks), MESSAGES, TOOLS, 200_000
    )
    assert analyze_context(None, [], None, 0).total_tokens_est == 0
    # the shared core is public: from_payload calls it with the sections it parsed itself
    assert analyze_sections(parse_system_sections(SYSTEM), MESSAGES, TOOLS, 200_000) == stats
    assert not hasattr(context_logger, "_analyze")
    # with the preamble (system block 0) the two still agree, field for field
    preamble = "You are Claude Code, Anthropic's official CLI for Claude."
    with_preamble = analyze_context(SYSTEM, MESSAGES, TOOLS, 200_000, preamble=preamble)
    entry = ContextLogEntry.from_payload(
        session_id="s",
        turn_number=1,
        call_type="chat",
        model="m",
        system_prompt=SYSTEM,
        messages=MESSAGES,
        tools=TOOLS,
        frame_id="f",
        context_window=200_000,
        preamble=preamble,
    )
    assert with_preamble.section_tokens_est == entry.token_breakdown == {"preamble": 14, **stats.section_tokens_est}
    for name in OLD_ENTRY.keys() - {"token_breakdown"}:
        assert getattr(with_preamble, name) == getattr(entry, name), name
    assert with_preamble.total_tokens_est == stats.total_tokens_est + 14


# ---------------------------------------------------------------------------------------------------------------------
# Fix round 1: every failure counted, a bad context never raises, a late attempt counted
# ---------------------------------------------------------------------------------------------------------------------

_LOGGER = "nous.observability.model_calls"


def _ok(output: int) -> ApiResponse:
    return ApiResponse(content=[], stop_reason="end_turn", usage={"output_tokens": output})


@pytest.mark.postgres_only
async def test_a_failed_attempt_is_logged_and_counted(db, monkeypatch, caplog):
    agent = _agent()
    writer, recorder = _recorder(db, agent)

    def broken_redaction(_text: str) -> str:
        raise RuntimeError("redaction broke")

    monkeypatch.setattr(model_calls, "_clip_error", broken_redaction)
    try:
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            with recorder.scope(_PAYLOAD, CallContext(purpose="chat"), "call") as scope:
                scope.attempt(529, "overloaded_error", "Overloaded", 10)  # the response hook's call: never raises
                scope.finish(_ok(3))
        assert recorder.stats.dropped_error == 1
        assert "model-call attempt not recorded (RuntimeError)" in caplog.text
        await writer.drain(5.0)
        [row] = await _rows(db, agent)
        # the call itself is still recorded, without the lost attempt
        assert (row["status"], row["output_tokens"], row["attempts"]) == ("ok", 3, 1)
        assert await _attempts(db, row["id"]) == []
        assert (recorder.stats.written, recorder.stats.dropped_error) == (1, 1)
    finally:
        await _delete(db, agent)


@pytest.mark.postgres_only
async def test_a_bad_call_context_never_raises_and_is_recorded_unlabelled(db, monkeypatch, caplog):
    agent = _agent()
    writer, recorder = _recorder(db, agent)
    bad_contexts = [
        {"purpose": "chat", "session_id": "not-a-call-context"},  # not a CallContext
        CallContext(purpose=["chat"], session_id="unhashable-purpose"),  # type: ignore[arg-type]
        CallContext(purpose=42, session_id="int-purpose"),  # type: ignore[arg-type]
    ]
    try:
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            for output, ctx in enumerate(bad_contexts, start=1):
                with recorder.scope(_PAYLOAD, ctx, "call") as scope:  # type: ignore[arg-type]
                    scope.finish(_ok(output))
            # a bad purpose through the fallback
            with model_call_context(purpose={"chat": 1}, session_id="bad-fallback"):
                with recorder.scope(_PAYLOAD, None, "call") as scope:
                    scope.finish(_ok(4))

            # any exception while naming the purpose
            def broken_label(_purpose: object) -> str:
                raise RuntimeError("label broke")

            monkeypatch.setattr(recorder, "_label", broken_label)
            with recorder.scope(_PAYLOAD, CallContext(purpose="chat", session_id="label-raises"), "call") as scope:
                scope.finish(_ok(5))
        assert recorder.stats.unlabelled == 5
        assert "bad model-call context (TypeError)" in caplog.text
        assert "bad model-call context (RuntimeError)" in caplog.text
        await writer.drain(5.0)
        rows = await _rows(db, agent)
        assert sorted(r["output_tokens"] for r in rows) == [1, 2, 3, 4, 5]
        assert {(r["purpose"], r["label_source"], r["session_id"], r["status"]) for r in rows} == {
            ("unlabelled", "none", None, "ok")
        }
        assert recorder.stats.written == 5 and recorder.stats.dropped_error == 0
    finally:
        await _delete(db, agent)


def test_a_late_attempt_is_counted_and_logged_once(caplog):
    writer = BoundedWriter("test")
    recorder = ModelCallRecorder(writer, None, "agent", _SETTINGS)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        with recorder.scope(_PAYLOAD, CallContext(purpose="chat"), "call") as first:
            first.attempt(200, None, None, 5)
        # reported after the row was written (no loop here, so the write itself is dropped and counted)
        first.attempt(529, "overloaded_error", "late", 7)
        first.attempt(529, "overloaded_error", "later", 9)
        with recorder.scope(_PAYLOAD, CallContext(purpose="chat"), "call") as second:
            pass
        second.attempt(200, None, None, 1)
    assert recorder.stats.dropped_late == 3
    assert caplog.text.count("reported after its call was written") == 1
    assert recorder.stats.as_dict()["dropped_late"] == 3
    assert writer.stats.dropped_late == 0
    assert recorder.stats.dropped_error == 2  # the two rows, dropped for want of a loop
