"""F108 Phase 1 (recording), PR 2: both Anthropic clients record every model call and every HTTP attempt.

Every request goes to a stubbed Messages API (``httpx.MockTransport``, the pattern of
``tests/test_fix_x_preamble_first.py``) under the real backends, httpx and the SDK: nothing reaches the network. The
row tests write ``nous_system.model_call_log`` and ``nous_system.model_call_attempt`` in real Postgres under their own
agent id and delete their rows afterwards (the attempts go with their call). The rest need no database.
"""

from __future__ import annotations

import json
import time
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from nous.api import anthropic_client
from nous.config import Settings
from nous.observability import model_calls
from nous.observability.model_calls import CallContext, ModelCallRecorder, context_kwargs
from nous.observability.recording import BoundedWriter

BACKENDS = ["httpx", "sdk"]
_MODEL = "claude-sonnet-5-5"
_PAYLOAD: dict[str, Any] = {
    "model": _MODEL,
    "max_tokens": 64,
    "system": [{"type": "text", "text": "You are a test."}],
    "messages": [{"role": "user", "content": "hi"}],
}
_USAGE = {"input_tokens": 3, "cache_read_input_tokens": 2000, "cache_creation_input_tokens": 50, "output_tokens": 7}
_STREAM_TYPES = ["message_start", "text_block_start", "text_delta", "block_stop", "done", "message_stop"]


# ---------------------------------------------------------------------------------------------------------------------
# The stub API
# ---------------------------------------------------------------------------------------------------------------------


def _message() -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": _MODEL,
        "content": [{"type": "text", "text": "Hello"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": dict(_USAGE),
    }


def _ok_call() -> httpx.Response:
    return httpx.Response(200, json=_message())


def _error(status: int, kind: str, message: str, *, retry_soon: bool = False) -> httpx.Response:
    # retry-after-ms keeps a retry fast: both the httpx loop and the SDK honour it
    headers = {"retry-after-ms": "1"} if retry_soon else None
    return httpx.Response(status, headers=headers, json={"type": "error", "error": {"type": kind, "message": message}})


_MESSAGE_START = {
    "type": "message_start",
    "message": {**_message(), "content": [], "stop_reason": None, "usage": {**_USAGE, "output_tokens": 1}},
}
_SSE_OK = [
    _MESSAGE_START,
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}},
    {"type": "content_block_stop", "index": 0},
    {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": 7},
    },
    {"type": "message_stop"},
]
_SSE_ERROR = [_MESSAGE_START, {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}]


def _stream(events: list[dict[str, Any]] | None = None) -> httpx.Response:
    # the event: lines are for the SDK's decoder; the httpx client reads only the data: lines
    body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in (events or _SSE_OK))
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())


class _Api:
    """A stub Messages API: each request gets the next reply queued for its kind, ``call`` or ``stream``."""

    def __init__(
        self, call: list[httpx.Response] | None = None, stream: list[httpx.Response] | None = None, delay: float = 0
    ) -> None:
        self.replies = {"call": list(call or []), "stream": list(stream or [])}
        self.delay = delay
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        if self.delay:
            time.sleep(self.delay)  # not asyncio.sleep: on Windows a loop timer may fire a clock tick early
        return self.replies["stream" if body.get("stream") else "call"].pop(0)


def _settings(backend: str) -> Settings:
    return Settings(_env_file=None, api_backend=backend, ANTHROPIC_API_KEY="test-key")


async def _client(backend: str, api: _Api, monkeypatch, recorder: ModelCallRecorder | None = None) -> Any:
    """A started client of ``backend`` talking to ``api``; the recorder is set after ``start()``, as main.py will."""
    monkeypatch.setattr(
        anthropic_client, "_build_transport_with_env_proxies", lambda **_: (httpx.MockTransport(api), {})
    )
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    client = anthropic_client.create_client(_settings(backend))
    await client.start()
    if recorder is not None:
        client.set_recorder(recorder)
    return client


# ---------------------------------------------------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------------------------------------------------


def _agent() -> str:
    return f"test-awv-cl-{uuid.uuid4().hex[:12]}"


def _recorder(db: Any, agent: str) -> tuple[BoundedWriter, ModelCallRecorder]:
    writer = BoundedWriter("test")
    return writer, ModelCallRecorder(writer, db, agent, SimpleNamespace())


async def _rows(db: Any, agent: str) -> list[dict[str, Any]]:
    async with db.engine.connect() as conn:
        result = await conn.execute(
            text("SELECT * FROM nous_system.model_call_log WHERE agent_id = :a ORDER BY started_at"), {"a": agent}
        )
        return [dict(r) for r in result.mappings().all()]


async def _attempts(db: Any, call_id: uuid.UUID) -> list[dict[str, Any]]:
    async with db.engine.connect() as conn:
        result = await conn.execute(
            text("SELECT * FROM nous_system.model_call_attempt WHERE call_id = :c ORDER BY attempt_no"), {"c": call_id}
        )
        return [dict(r) for r in result.mappings().all()]


async def _delete(db: Any, agent: str) -> None:
    async with db.engine.begin() as conn:
        await conn.execute(text("DELETE FROM nous_system.model_call_log WHERE agent_id = :a"), {"a": agent})


def _usage(row: dict[str, Any]) -> tuple[Any, ...]:
    return row["input_uncached"], row["cache_read"], row["cache_write"], row["output_tokens"]


# ---------------------------------------------------------------------------------------------------------------------
# call
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.postgres_only
@pytest.mark.parametrize("backend", BACKENDS)
async def test_call_ok_records_usage_and_duration(backend, db, monkeypatch):
    agent = _agent()
    writer, recorder = _recorder(db, agent)
    client = await _client(backend, _Api(call=[_ok_call()], delay=0.03), monkeypatch, recorder)
    try:
        response = await client.call(dict(_PAYLOAD), call_context=CallContext(purpose="critic", session_id="s-1"))
        assert response.content == [{"type": "text", "text": "Hello"}] and response.stop_reason == "end_turn"
        await writer.drain(5.0)
        [row] = await _rows(db, agent)
        assert (row["purpose"], row["label_source"], row["session_id"]) == ("critic", "explicit", "s-1")
        assert (row["call_kind"], row["status"], row["model"], row["stop_reason"]) == ("call", "ok", _MODEL, "end_turn")
        assert _usage(row) == (3, 2000, 50, 7)
        assert row["duration_ms"] >= 30
        [attempt] = await _attempts(db, row["id"])
        assert (row["attempts"], row["http_status"]) == (1, 200)
        assert (attempt["http_status"], attempt["error_type"], attempt["error_text"]) == (200, None, None)
        assert attempt["duration_ms"] >= 30
    finally:
        await client.close()
        await _delete(db, agent)


@pytest.mark.postgres_only
@pytest.mark.parametrize("backend", BACKENDS)
async def test_retry_429_then_200_is_two_attempts_one_row(backend, db, monkeypatch):
    agent = _agent()
    writer, recorder = _recorder(db, agent)
    api = _Api(call=[_error(429, "rate_limit_error", "slow down", retry_soon=True), _ok_call()])
    client = await _client(backend, api, monkeypatch, recorder)
    try:
        response = await client.call(dict(_PAYLOAD), call_context=CallContext(purpose="chat"))
        assert response.stop_reason == "end_turn"
        assert len(api.bodies) == 2  # the retry is the client's own, unchanged
        await writer.drain(5.0)
        [row] = await _rows(db, agent)
        attempts = await _attempts(db, row["id"])
        assert (row["status"], row["attempts"], row["http_status"]) == ("ok", 2, 200)
        assert [a["http_status"] for a in attempts] == [429, 200]
        assert [a["attempt_no"] for a in attempts] == [1, 2]
        # the httpx client parses the error body; the SDK's response hook sees the status only
        if backend == "httpx":
            assert (attempts[0]["error_type"], attempts[0]["error_text"]) == ("rate_limit_error", "slow down")
        else:
            assert (attempts[0]["error_type"], attempts[0]["error_text"]) == (None, None)
        assert attempts[1]["error_type"] is None
        assert _usage(row) == (3, 2000, 50, 7)
    finally:
        await client.close()
        await _delete(db, agent)


@pytest.mark.postgres_only
@pytest.mark.parametrize("backend", BACKENDS)
async def test_non_retryable_400_records_error_row_with_status(backend, db, monkeypatch):
    agent = _agent()
    writer, recorder = _recorder(db, agent)
    api = _Api(call=[_error(400, "invalid_request_error", "max_tokens: too large")])
    client = await _client(backend, api, monkeypatch, recorder)
    try:
        with pytest.raises(RuntimeError, match="max_tokens: too large"):
            await client.call(dict(_PAYLOAD), call_context=CallContext(purpose="chat"))
        assert len(api.bodies) == 1
        await writer.drain(5.0)
        [row] = await _rows(db, agent)
        assert (row["status"], row["http_status"], row["attempts"]) == ("error", 400, 1)
        assert "max_tokens: too large" in row["error_text"]
        assert row["output_tokens"] is None
        [attempt] = await _attempts(db, row["id"])
        assert attempt["http_status"] == 400
    finally:
        await client.close()
        await _delete(db, agent)


# ---------------------------------------------------------------------------------------------------------------------
# stream
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.postgres_only
@pytest.mark.parametrize("backend", BACKENDS)
async def test_stream_records_usage_from_message_start_and_done(backend, db, monkeypatch):
    agent = _agent()
    writer, recorder = _recorder(db, agent)
    client = await _client(backend, _Api(stream=[_stream()]), monkeypatch, recorder)
    try:
        events = [e async for e in client.stream(dict(_PAYLOAD), call_context=CallContext(purpose="chat"))]
        assert [e.type for e in events] == _STREAM_TYPES
        await writer.drain(5.0)
        [row] = await _rows(db, agent)
        assert (row["call_kind"], row["status"], row["stop_reason"], row["purpose"]) == (
            "stream",
            "ok",
            "end_turn",
            "chat",
        )
        assert _usage(row) == (3, 2000, 50, 7)
        assert [a["http_status"] for a in await _attempts(db, row["id"])] == [200]
    finally:
        await client.close()
        await _delete(db, agent)


@pytest.mark.postgres_only
@pytest.mark.parametrize("where", ["mid-stream", "at-open"])
@pytest.mark.parametrize("backend", BACKENDS)
async def test_stream_error_event_records_error(backend, where, db, monkeypatch):
    agent = _agent()
    writer, recorder = _recorder(db, agent)
    reply = _stream(_SSE_ERROR) if where == "mid-stream" else _error(400, "invalid_request_error", "bad tools")
    client = await _client(backend, _Api(stream=[reply]), monkeypatch, recorder)
    try:
        events = [e async for e in client.stream(dict(_PAYLOAD), call_context=CallContext(purpose="chat"))]
        assert events[-1].type == "error"
        await writer.drain(5.0)
        [row] = await _rows(db, agent)
        assert row["status"] == "error"
        if where == "mid-stream":
            assert "Overloaded" in row["error_text"]
            assert _usage(row) == (3, 2000, 50, None)  # the input counts came before the error
            assert row["http_status"] == 200
        else:
            assert "bad tools" in row["error_text"]
            assert row["http_status"] == 400
        assert row["attempts"] == 1
    finally:
        await client.close()
        await _delete(db, agent)


@pytest.mark.postgres_only
async def test_httpx_stream_non_200_attempt_carries_the_api_error(db, monkeypatch):
    """The httpx stream's non-200 attempt carries the API's error type and message, as ``call``'s does; a body that
    is not JSON gives ``http_error`` and the status with the body's text."""
    agent = _agent()
    writer, recorder = _recorder(db, agent)
    not_json = httpx.Response(400, content=b"<html>bad request</html>")
    api = _Api(stream=[_error(429, "rate_limit_error", "slow down", retry_soon=True), _stream(), not_json])
    client = await _client("httpx", api, monkeypatch, recorder)
    try:
        for _ in range(2):
            [e async for e in client.stream(dict(_PAYLOAD), call_context=CallContext(purpose="chat"))]
        await writer.drain(5.0)
        retried, refused = await _rows(db, agent)
        assert [(a["http_status"], a["error_type"], a["error_text"]) for a in await _attempts(db, retried["id"])] == [
            (429, "rate_limit_error", "slow down"),
            (200, None, None),
        ]
        [attempt] = await _attempts(db, refused["id"])
        assert (attempt["http_status"], attempt["error_type"], attempt["error_text"]) == (
            400,
            "http_error",
            "HTTP 400: <html>bad request</html>",
        )
    finally:
        await client.close()
        await _delete(db, agent)


@pytest.mark.postgres_only
@pytest.mark.parametrize("backend", BACKENDS)
async def test_stream_consumer_stops_early_records_cancelled(backend, db, monkeypatch):
    agent = _agent()
    writer, recorder = _recorder(db, agent)
    client = await _client(backend, _Api(stream=[_stream()]), monkeypatch, recorder)
    try:
        gen = client.stream(dict(_PAYLOAD), call_context=CallContext(purpose="chat"))
        first = await gen.__anext__()
        assert first.type == "message_start"
        await gen.aclose()  # GeneratorExit at the yield, before done
        await writer.drain(5.0)
        [row] = await _rows(db, agent)
        assert (row["status"], row["error_text"], row["stop_reason"]) == ("cancelled", None, None)
        assert _usage(row) == (3, 2000, 50, None)
        assert [a["http_status"] for a in await _attempts(db, row["id"])] == [200]
    finally:
        await client.close()
        await _delete(db, agent)


@pytest.mark.postgres_only
@pytest.mark.parametrize("backend", BACKENDS)
async def test_error_then_generator_exit_stays_error(backend, db, monkeypatch):
    agent = _agent()
    writer, recorder = _recorder(db, agent)
    client = await _client(backend, _Api(stream=[_stream(_SSE_ERROR)]), monkeypatch, recorder)
    try:
        gen = client.stream(dict(_PAYLOAD), call_context=CallContext(purpose="chat"))
        async for event in gen:
            if event.type == "error":
                break  # as the runner does: it stops at the error event
        await gen.aclose()  # GeneratorExit at the error event's yield
        await writer.drain(5.0)
        [row] = await _rows(db, agent)
        assert row["status"] == "error"
        assert "Overloaded" in row["error_text"]
    finally:
        await client.close()
        await _delete(db, agent)


@pytest.mark.postgres_only
@pytest.mark.parametrize("backend", BACKENDS)
async def test_stream_truncated_records_error(backend, db, monkeypatch):
    """A stream whose body ends with no done, message_stop or error event was cut off: the caller gets the events
    that came, as before, and the row says ``error``."""
    agent = _agent()
    writer, recorder = _recorder(db, agent)
    client = await _client(backend, _Api(stream=[_stream(_SSE_OK[:3])]), monkeypatch, recorder)
    try:
        events = [e async for e in client.stream(dict(_PAYLOAD), call_context=CallContext(purpose="chat"))]
        assert [e.type for e in events] == _STREAM_TYPES[:3]
        await writer.drain(5.0)
        [row] = await _rows(db, agent)
        assert (row["status"], row["error_text"], row["stop_reason"]) == (
            "error",
            "stream ended without a terminal event",
            None,
        )
        assert _usage(row) == (3, 2000, 50, None)
        assert [a["http_status"] for a in await _attempts(db, row["id"])] == [200]
    finally:
        await client.close()
        await _delete(db, agent)


@pytest.mark.parametrize("backend", BACKENDS)
async def test_the_scope_gets_the_payload_that_is_sent(backend, monkeypatch):
    """Both backends open the scope on the payload after ``with_default_thinking``, the one the API receives."""
    payload = {**_PAYLOAD, "model": "claude-haiku-5-5"}  # a model whose thinking is disabled by default
    recorder, _rows_kept = _memory_recorder(monkeypatch)
    seen: list[tuple[str, dict[str, Any]]] = []
    real_scope = recorder.scope

    def spy(scope_payload: dict[str, Any], ctx: Any, call_kind: str) -> Any:
        seen.append((call_kind, scope_payload))
        return real_scope(scope_payload, ctx, call_kind)

    monkeypatch.setattr(recorder, "scope", spy)
    api = _Api(call=[_ok_call()], stream=[_stream(), _stream()])
    client = await _client(backend, api, monkeypatch, recorder)
    try:
        ctx = CallContext(purpose="chat")
        await client.call(dict(payload), call_context=ctx)
        [e async for e in client.stream(dict(payload), call_context=ctx)]
        await client.call_streaming_aggregated(dict(payload), call_context=ctx)
    finally:
        await client.close()
    assert [kind for kind, _ in seen] == ["call", "stream", "aggregated"]
    assert all(p["thinking"] == {"type": "disabled"} for _, p in seen)
    assert all(body["thinking"] == {"type": "disabled"} for body in api.bodies)  # what is sent is unchanged


@pytest.mark.postgres_only
@pytest.mark.parametrize("backend", BACKENDS)
async def test_done_then_generator_exit_stays_ok(backend, db, monkeypatch):
    """The response is complete at ``done``: a consumer that stops there (before ``message_stop``) leaves it ``ok``."""
    agent = _agent()
    writer, recorder = _recorder(db, agent)
    client = await _client(backend, _Api(stream=[_stream()]), monkeypatch, recorder)
    try:
        gen = client.stream(dict(_PAYLOAD), call_context=CallContext(purpose="chat"))
        async for event in gen:
            if event.type == "done":
                break
        await gen.aclose()  # GeneratorExit at the done event's yield
        await writer.drain(5.0)
        [row] = await _rows(db, agent)
        assert (row["status"], row["stop_reason"], _usage(row)) == ("ok", "end_turn", (3, 2000, 50, 7))
    finally:
        await client.close()
        await _delete(db, agent)


@pytest.mark.postgres_only
@pytest.mark.parametrize("backend", BACKENDS)
async def test_request_during_suspended_stream_does_not_join_its_scope(backend, db, monkeypatch):
    """The scope a stream binds for its HTTP request is unbound before the first event is yielded, so a request
    made while the stream is suspended (here a recorded call and an unrecorded one on a second client, whose SDK
    response hook is installed too) adds nothing to the stream's row."""
    agent = _agent()
    writer, recorder = _recorder(db, agent)
    api = _Api(
        call=[_error(429, "rate_limit_error", "slow down", retry_soon=True), _ok_call(), _ok_call()],
        stream=[_stream()],
    )
    recorded = await _client(backend, api, monkeypatch, recorder)
    plain = await _client(backend, api, monkeypatch)
    try:
        gen = recorded.stream(dict(_PAYLOAD), call_context=CallContext(purpose="chat", session_id="stream"))
        first = await gen.__anext__()
        assert first.type == "message_start"
        assert model_calls._ACTIVE_SCOPE.get() is None  # nothing is bound while the stream is suspended
        await recorded.call(dict(_PAYLOAD), call_context=CallContext(purpose="critic", session_id="call"))
        await plain.call(dict(_PAYLOAD))
        rest = [e async for e in gen]
        assert [first.type] + [e.type for e in rest] == _STREAM_TYPES
        await writer.drain(5.0)
        rows = {r["session_id"]: r for r in await _rows(db, agent)}
        assert set(rows) == {"stream", "call"}
        assert [a["http_status"] for a in await _attempts(db, rows["stream"]["id"])] == [200]
        assert [a["http_status"] for a in await _attempts(db, rows["call"]["id"])] == [429, 200]
        assert (rows["stream"]["attempts"], rows["call"]["attempts"]) == (1, 2)
        assert (rows["stream"]["status"], rows["call"]["status"]) == ("ok", "ok")
    finally:
        await recorded.close()
        await plain.close()
        await _delete(db, agent)


# ---------------------------------------------------------------------------------------------------------------------
# call_streaming_aggregated
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.postgres_only
@pytest.mark.parametrize("backend", BACKENDS)
async def test_aggregated_call_records(backend, db, monkeypatch):
    agent = _agent()
    writer, recorder = _recorder(db, agent)
    api = _Api(stream=[_error(529, "overloaded_error", "Overloaded", retry_soon=True), _stream()])
    client = await _client(backend, api, monkeypatch, recorder)
    try:
        response = await client.call_streaming_aggregated(
            dict(_PAYLOAD), call_context=CallContext(purpose="subtask", session_id="sub-1")
        )
        assert response.content == [{"type": "text", "text": "Hello"}] and response.stop_reason == "end_turn"
        await writer.drain(5.0)
        # one row for the call: the httpx aggregator streams under the hood, and that stream is not a second row
        [row] = await _rows(db, agent)
        assert (row["call_kind"], row["status"], row["purpose"], row["session_id"]) == (
            "aggregated",
            "ok",
            "subtask",
            "sub-1",
        )
        assert (row["stop_reason"], _usage(row)) == ("end_turn", (3, 2000, 50, 7))
        assert row["attempts"] == 2
        assert [a["http_status"] for a in await _attempts(db, row["id"])] == [529, 200]
    finally:
        await client.close()
        await _delete(db, agent)


@pytest.mark.postgres_only
async def test_aggregated_truncated_stream_records_error(db, monkeypatch):
    """The httpx aggregator raises when its stream ends with no terminal event; the call's scope spans that check, so
    the row says ``error``. (The SDK's ``get_final_message`` does not raise on a truncated stream: unchanged here.)"""
    agent = _agent()
    writer, recorder = _recorder(db, agent)
    client = await _client("httpx", _Api(stream=[_stream(_SSE_OK[:3])]), monkeypatch, recorder)
    try:
        with pytest.raises(RuntimeError):
            await client.call_streaming_aggregated(dict(_PAYLOAD), call_context=CallContext(purpose="subtask"))
        await writer.drain(5.0)
        [row] = await _rows(db, agent)
        assert (row["call_kind"], row["status"]) == ("aggregated", "error")
    finally:
        await client.close()
        await _delete(db, agent)


# ---------------------------------------------------------------------------------------------------------------------
# No recorder, a failing recorder, the factory (no database)
# ---------------------------------------------------------------------------------------------------------------------


def _script_api() -> _Api:
    """Replies for ``_script``: a call, a retried call, a refused call, a stream, a refused stream, an aggregated call
    and an aggregated call whose stream errors."""
    return _Api(
        call=[
            _ok_call(),
            _error(429, "rate_limit_error", "slow down", retry_soon=True),
            _ok_call(),
            _error(400, "invalid_request_error", "max_tokens: too large"),
        ],
        stream=[
            _stream(),
            _error(400, "invalid_request_error", "bad tools"),
            _stream(),
            _stream(_SSE_ERROR),
        ],
    )


async def _script(client: Any, ctx: CallContext | None) -> list[Any]:
    """What a caller receives from each kind of call: results, raised exceptions (type and text) and events."""
    kwargs = {} if ctx is None else {"call_context": ctx}

    async def outcome(make: Any) -> Any:
        try:
            return await make()
        except Exception as exc:
            return (type(exc).__name__, str(exc))

    async def events() -> list[Any]:
        return [(e.type, e.text, e.stop_reason, e.usage) async for e in client.stream(dict(_PAYLOAD), **kwargs)]

    return [
        await outcome(lambda: client.call(dict(_PAYLOAD), **kwargs)),
        await outcome(lambda: client.call(dict(_PAYLOAD), **kwargs)),
        await outcome(lambda: client.call(dict(_PAYLOAD), **kwargs)),
        await outcome(events),
        await outcome(events),
        await outcome(lambda: client.call_streaming_aggregated(dict(_PAYLOAD), **kwargs)),
        await outcome(lambda: client.call_streaming_aggregated(dict(_PAYLOAD), **kwargs)),
    ]


def _memory_recorder(monkeypatch) -> tuple[ModelCallRecorder, list[tuple[dict[str, Any], list[dict[str, Any]]]]]:
    """A recorder whose rows are kept in a list instead of written."""
    recorder = ModelCallRecorder(BoundedWriter("test"), None, "agent", SimpleNamespace())
    rows: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    monkeypatch.setattr(recorder, "_submit_call", lambda params, attempts: rows.append((params, attempts)))
    return recorder, rows


@pytest.mark.parametrize("backend", BACKENDS)
async def test_no_recorder_means_no_overhead_and_same_result(backend, monkeypatch):
    built: list[Any] = []
    original_init = model_calls.ModelCallScope.__init__

    def counting_init(self: Any, *args: Any, **kwargs: Any) -> None:
        built.append(self)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(model_calls.ModelCallScope, "__init__", counting_init)

    plain_api, context_api = _script_api(), _script_api()
    plain = await _client(backend, plain_api, monkeypatch)
    with_context = await _client(backend, context_api, monkeypatch)
    try:
        unrecorded = await _script(plain, None)
        # a context passed to a client with no recorder changes nothing either
        assert await _script(with_context, CallContext(purpose="chat")) == unrecorded
    finally:
        await plain.close()
        await with_context.close()
    assert built == []  # no scope was ever built
    assert model_calls._ACTIVE_SCOPE.get() is None
    assert context_api.bodies == plain_api.bodies
    # the script reached every path: a result, a retry, a refusal, a stream, a refused stream, aggregated twice
    assert unrecorded[0].stop_reason == unrecorded[1].stop_reason == "end_turn"
    assert unrecorded[2][0] == "RuntimeError" and "max_tokens: too large" in unrecorded[2][1]
    assert [e[0] for e in unrecorded[3]] == _STREAM_TYPES and unrecorded[4][-1][0] == "error"
    assert unrecorded[5].content == [{"type": "text", "text": "Hello"}] and unrecorded[6][0] == "RuntimeError"

    recorder, rows = _memory_recorder(monkeypatch)
    recorded_api = _script_api()
    client = await _client(backend, recorded_api, monkeypatch, recorder)
    try:
        assert await _script(client, CallContext(purpose="chat")) == unrecorded
    finally:
        await client.close()
    # the same requests went out, and each call made one row
    assert recorded_api.bodies == plain_api.bodies
    assert len(rows) == 7 and len(built) == 7


@pytest.mark.parametrize("backend", BACKENDS)
async def test_recorder_exception_never_breaks_the_call(backend, monkeypatch):
    baseline_api = _script_api()
    plain = await _client(backend, baseline_api, monkeypatch)
    try:
        baseline = await _script(plain, None)
    finally:
        await plain.close()

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("recorder broke")

    # every part of recording that could fail, failing: the redaction, the field reads, the HTTP-status read, the
    # SDK response hook's lookup and the row's submission
    with monkeypatch.context() as patch:
        for name in ("_clip_error", "_get", "_http_status_of"):
            patch.setattr(model_calls, name, broken)
        patch.setattr(anthropic_client, "active_scope", broken)
        recorder = ModelCallRecorder(BoundedWriter("test"), None, "agent", SimpleNamespace())
        patch.setattr(recorder, "_submit_call", broken)
        api = _script_api()
        client = await _client(backend, api, patch, recorder)
        try:
            assert await _script(client, CallContext(purpose="chat")) == baseline
        finally:
            await client.close()
        assert api.bodies == baseline_api.bodies  # no extra retry: a failing hook is not a failed request
        assert recorder.stats.dropped_error >= 7

    # a recorder that cannot even open a scope
    recorder = ModelCallRecorder(BoundedWriter("test"), None, "agent", SimpleNamespace())
    monkeypatch.setattr(recorder, "scope", broken)
    api = _script_api()
    client = await _client(backend, api, monkeypatch, recorder)
    try:
        assert await _script(client, CallContext(purpose="chat")) == baseline
    finally:
        await client.close()
    assert api.bodies == baseline_api.bodies


@pytest.mark.parametrize("backend", BACKENDS)
async def test_create_client_wires_recorder_only_when_given(backend, monkeypatch):
    settings = _settings(backend)
    plain = anthropic_client.create_client(settings)
    assert plain.supports_call_context is True
    assert plain.records_calls is False  # the runner computes no context estimate for it
    assert context_kwargs(plain, CallContext(purpose="chat")) == {"call_context": CallContext(purpose="chat")}

    def fresh_api(**_: Any) -> tuple[httpx.MockTransport, dict]:
        return httpx.MockTransport(_Api([_ok_call()])), {}

    monkeypatch.setattr(anthropic_client, "_build_transport_with_env_proxies", fresh_api)
    recorder, rows = _memory_recorder(monkeypatch)
    wired = anthropic_client.create_client(settings, recorder=recorder)
    assert wired.records_calls is True
    for client in (plain, wired):
        await client.start()
        try:
            await client.call(dict(_PAYLOAD), call_context=CallContext(purpose="chat"))
        finally:
            await client.close()
    assert len(rows) == 1  # the wired client's call only
    assert rows[0][0]["purpose"] == "chat" and rows[0][0]["call_kind"] == "call"

    wired.set_recorder(None)
    await wired.start()
    try:
        await wired.call(dict(_PAYLOAD))
    finally:
        await wired.close()
    assert len(rows) == 1  # unset: nothing more is recorded
