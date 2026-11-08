"""F108 Phase 1 (recording), PR 2: the model calls that bypass the Anthropic clients.

The browser task loop posts to the Messages API through its own httpx client (``nous/browser/loop.py``): each call is
one ``browser_step`` row, purpose ``browser_task``, and each HTTP try one of its attempts. Embeddings
(``nous/brain/embeddings.py``) and the cross-encoder (``nous/heart/reranker.py``) are counted per minute in a bucket
row (``ModelCallRecorder.record_bucket``): one bucket call per embeddings HTTP request, its retries included, with its
``usage.total_tokens``; one per rerank, with the number of candidates scored. Recording never changes what is sent,
returned or raised, and a recorder fault never raises.

The row tests write ``nous_system.model_call_log`` and ``nous_system.model_call_attempt`` in real Postgres under their
own agent id and delete their rows afterwards. Every request goes to an ``httpx.MockTransport`` stub; nothing reaches
the network. Every wait is bounded.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from nous.api.preamble import CLAUDE_CODE_PREAMBLE
from nous.brain import embeddings as embeddings_mod
from nous.brain.embeddings import EmbeddingProvider, EmbeddingUnavailableError
from nous.browser import loop
from nous.browser.loop import BrowserApiError, make_api_caller
from nous.heart import reranker as reranker_mod
from nous.heart.reranker import cross_encoder_rerank
from nous.observability import model_calls
from nous.observability.model_calls import ModelCallRecorder
from nous.observability.recording import BoundedWriter

_GUARD_S = 10.0
_BROWSER_MODEL = "claude-sonnet-5-5"
_EMBED_MODEL = "text-embedding-3-small"
_RERANK_MODEL = "test-cross-encoder"
_MESSAGE_USAGE = {
    "input_tokens": 4,
    "cache_read_input_tokens": 6000,
    "cache_creation_input_tokens": 70,
    "output_tokens": 9,
}
_MINUTE = datetime(2026, 11, 8, 9, 30, tzinfo=UTC)


def _agent() -> str:
    return f"test-awv-bp-{uuid.uuid4().hex[:12]}"


def _recorder(db: Any, agent: str) -> tuple[BoundedWriter, ModelCallRecorder]:
    writer = BoundedWriter("test")
    return writer, ModelCallRecorder(writer, db, agent, SimpleNamespace())


async def _rows(db: Any, agent: str) -> list[dict]:
    async with db.engine.connect() as conn:
        result = await conn.execute(
            text("SELECT * FROM nous_system.model_call_log WHERE agent_id = :a ORDER BY started_at, purpose, status"),
            {"a": agent},
        )
        return [dict(r) for r in result.mappings().all()]


async def _attempts(db: Any, call_id: uuid.UUID) -> list[dict]:
    async with db.engine.connect() as conn:
        result = await conn.execute(
            text("SELECT * FROM nous_system.model_call_attempt WHERE call_id = :c ORDER BY attempt_no"), {"c": call_id}
        )
        return [dict(r) for r in result.mappings().all()]


async def _delete(db: Any, agent: str) -> None:
    async with db.engine.begin() as conn:
        await conn.execute(text("DELETE FROM nous_system.model_call_log WHERE agent_id = :a"), {"a": agent})


class _Buckets:
    """A recorder double that keeps every ``record_bucket`` call (the counting rules, without a database)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, int | None, float, str]] = []

    def record_bucket(
        self, purpose: str, model: str | None, tokens: int | None, duration_ms: float, status: str
    ) -> None:
        self.calls.append((purpose, model, tokens, duration_ms, status))


class _BrokenDatabase:
    """A database whose every session fails: each bucket write fails in the writer."""

    def session(self) -> Any:
        raise RuntimeError("database down")


# ---------------------------------------------------------------------------------------------------------------------
# The browser loop
# ---------------------------------------------------------------------------------------------------------------------


def _browser_settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        browser_task_enabled=True,
        browser_task_model=_BROWSER_MODEL,
        browser_task_max_steps=30,
        browser_task_timeout_seconds=60,
        tool_timeout=120,
        workspace_dir=str(tmp_path),
        anthropic_api_key="sk-ant-api-test",
        anthropic_auth_token="",
        api_base_url="https://api.anthropic.invalid",
    )


def _message(stop_reason: str = "end_turn") -> dict[str, Any]:
    return {
        "type": "message",
        "role": "assistant",
        "model": _BROWSER_MODEL,
        "content": [{"type": "text", "text": "done"}],
        "stop_reason": stop_reason,
        "usage": dict(_MESSAGE_USAGE),
    }


async def _browser_call(tmp_path: Path, handler: Any, recorder: Any = None) -> tuple[list[httpx.Request], Any]:
    """One browser step's API call against ``handler(request, n)``: the requests sent and the result (or error)."""
    seen: list[httpx.Request] = []

    def stub(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request, len(seen))

    async with httpx.AsyncClient(transport=httpx.MockTransport(stub)) as http:
        call = make_api_caller(_browser_settings(tmp_path), http, recorder=recorder)
        try:
            result = await asyncio.wait_for(call(loop._payload(_BROWSER_MODEL, [])), _GUARD_S)
        except Exception as e:  # noqa: BLE001 -- returned for the assertions
            result = e
    return seen, result


@pytest.mark.postgres_only
async def test_browser_step_records_usage_and_attempts(db, tmp_path, monkeypatch):
    monkeypatch.setattr(loop, "_retry_delay", lambda *a, **k: 0)
    agent = _agent()
    writer, recorder = _recorder(db, agent)
    try:
        seen, result = await _browser_call(
            tmp_path,
            lambda r, n: (
                httpx.Response(429, json={"error": {"type": "rate_limit_error", "message": "slow down"}})
                if n == 1
                else httpx.Response(200, json=_message())
            ),
            recorder,
        )
        assert len(seen) == 2 and result == _message()
        await writer.drain(5.0)
        [row] = await _rows(db, agent)
        assert (row["purpose"], row["label_source"], row["call_kind"], row["model"]) == (
            "browser_task",
            "explicit",
            "browser_step",
            _BROWSER_MODEL,
        )
        assert (row["status"], row["http_status"], row["attempts"], row["stop_reason"]) == ("ok", 200, 2, "end_turn")
        assert (row["input_uncached"], row["cache_read"], row["cache_write"], row["output_tokens"]) == (4, 6000, 70, 9)
        assert row["input_total"] == 6074 and row["call_count"] == 1
        assert [a["http_status"] for a in await _attempts(db, row["id"])] == [429, 200]
    finally:
        await _delete(db, agent)


@pytest.mark.postgres_only
async def test_browser_step_failures_are_error_rows(db, tmp_path):
    agent = _agent()
    writer, recorder = _recorder(db, agent)
    try:
        _, refused = await _browser_call(
            tmp_path,
            lambda r, n: httpx.Response(400, json={"error": {"type": "invalid_request_error", "message": "bad"}}),
            recorder,
        )
        assert isinstance(refused, BrowserApiError)

        def unreachable(request: httpx.Request, n: int) -> httpx.Response:
            raise httpx.ConnectError("no route", request=request)

        _, broken = await _browser_call(tmp_path, unreachable, recorder)
        assert isinstance(broken, BrowserApiError)
        await writer.drain(5.0)
        rows = await _rows(db, agent)
        assert len(rows) == 2 and all(r["status"] == "error" and r["attempts"] == 1 for r in rows)
        by_status = {r["http_status"]: r for r in rows}
        assert set(by_status) == {400, None}
        assert "Anthropic API error (400): invalid_request_error - bad" in by_status[400]["error_text"]
        assert "Anthropic API request failed: ConnectError" in by_status[None]["error_text"]
        [attempt] = await _attempts(db, by_status[None]["id"])
        assert (attempt["http_status"], attempt["error_type"]) == (None, "ConnectError")
    finally:
        await _delete(db, agent)


async def test_browser_loop_still_opens_with_the_preamble(tmp_path):
    """Recording reads the call; the request is the same byte for byte, with the preamble at system block 0."""
    writer = BoundedWriter("test")
    recorder = ModelCallRecorder(writer, _BrokenDatabase(), _agent(), SimpleNamespace())
    sent = {}
    for name, rec in (("plain", None), ("recorded", recorder)):
        seen, result = await _browser_call(tmp_path, lambda r, n: httpx.Response(200, json=_message()), rec)
        assert result == _message()
        [request] = seen
        sent[name] = (request.content, dict(request.headers), str(request.url))
    await writer.drain(5.0)
    assert sent["plain"] == sent["recorded"]
    body = json.loads(sent["recorded"][0])
    assert body == loop._payload(_BROWSER_MODEL, [])
    assert body["system"][0] == {"type": "text", "text": CLAUDE_CODE_PREAMBLE}


async def test_browser_recorder_fault_never_breaks_the_call(tmp_path, monkeypatch):
    monkeypatch.setattr(loop, "_retry_delay", lambda *a, **k: 0)

    class _NoScope:
        stats = SimpleNamespace(dropped_error=0)

        def scope(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("recorder broken")

    broken = _NoScope()
    script = lambda r, n: httpx.Response(529, json={}) if n == 1 else httpx.Response(200, json=_message())  # noqa: E731
    seen, result = await _browser_call(tmp_path, script, broken)
    assert (len(seen), result) == (2, _message()) and broken.stats.dropped_error == 1

    writer = BoundedWriter("test")
    failing = ModelCallRecorder(writer, _BrokenDatabase(), _agent(), SimpleNamespace())
    seen, result = await _browser_call(tmp_path, script, failing)
    assert (len(seen), result) == (2, _message())
    await writer.drain(5.0)
    assert (failing.stats.written, failing.stats.dropped_error) == (0, 1)


async def test_run_browser_task_hands_the_set_recorder_to_its_caller(tmp_path, monkeypatch):
    """main.py wires the loop with ``loop.set_recorder``; ``run_browser_task`` passes it to the caller it builds."""
    handed: list[Any] = []

    def caller(settings: Any, http: Any, deadline: float | None = None, recorder: Any = None) -> Any:
        handed.append(recorder)

        async def call(payload: dict[str, Any]) -> dict[str, Any]:
            return _message()

        return call

    class _Executor:
        blocked_requests = 0

        async def execute(self, name: str, input_: dict) -> list[dict]:
            return []

        async def final_screenshot(self, path: Path) -> str | None:
            return None

        def current_url(self) -> str:
            return ""

        async def checked_final_url(self) -> str | None:
            return None

    @asynccontextmanager
    async def factory(allowlist: Any, artifacts_dir: Path):
        yield _Executor()

    async def resolved(hosts: Any) -> None:
        return None

    monkeypatch.setattr(loop, "make_api_caller", caller)
    monkeypatch.setattr(loop, "check_resolution", resolved)
    marker = object()
    assert loop._recorder is None  # nothing records until main.py sets a recorder
    for recorder in (marker, None):
        loop.set_recorder(recorder)
        try:
            result = await asyncio.wait_for(
                loop.run_browser_task(
                    "goal", ["example.com"], settings=_browser_settings(tmp_path), browser_factory=factory
                ),
                _GUARD_S,
            )
        finally:
            loop.set_recorder(None)
        assert result.status == "completed"
    assert handed == [marker, None]


# ---------------------------------------------------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------------------------------------------------


def _embed_ok(request: httpx.Request, tokens_per_input: int = 3) -> httpx.Response:
    inputs = json.loads(request.content)["input"]
    texts = inputs if isinstance(inputs, list) else [inputs]
    tokens = tokens_per_input * len(texts)
    return httpx.Response(
        200,
        json={
            "object": "list",
            "data": [{"index": i, "embedding": [float(len(t)), 0.5]} for i, t in enumerate(texts)],
            "model": _EMBED_MODEL,
            "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
        },
    )


async def _provider(handler: Any, *, cache_size: int = 0, recorder: Any = None) -> EmbeddingProvider:
    provider = EmbeddingProvider(api_key="test", model=_EMBED_MODEL, cache_size=cache_size)
    await provider._client.aclose()
    provider._client = httpx.AsyncClient(base_url="https://api.openai.com/v1", transport=httpx.MockTransport(handler))
    if recorder is not None:
        provider.set_recorder(recorder)
    return provider


@pytest.fixture
def fast_retries(monkeypatch):
    """Short backoff, so a retried embeddings request finishes at once."""
    monkeypatch.setattr(embeddings_mod, "_RETRY_BACKOFF", (0.01, 0.01, 0.01))


@pytest.mark.postgres_only
async def test_embedding_batch_records_tokens_in_one_bucket_row(db, monkeypatch):
    agent = _agent()
    writer, recorder = _recorder(db, agent)
    monkeypatch.setattr(model_calls, "_utcnow", lambda: _MINUTE + timedelta(seconds=20))
    provider = await _provider(_embed_ok, recorder=recorder)
    try:
        # Three requests in one minute: two batches and a single text.
        assert await asyncio.wait_for(provider.embed_batch(["aa", "bbb"]), _GUARD_S) == [[2.0, 0.5], [3.0, 0.5]]
        assert await asyncio.wait_for(provider.embed_batch(["c", "dd", "eee"]), _GUARD_S) == [
            [1.0, 0.5],
            [2.0, 0.5],
            [3.0, 0.5],
        ]
        assert await asyncio.wait_for(provider.embed("ffff"), _GUARD_S) == [4.0, 0.5]
        await writer.drain(5.0)
        [row] = await _rows(db, agent)
        assert (row["started_at"], row["purpose"], row["call_kind"], row["model"], row["status"]) == (
            _MINUTE,
            "embedding",
            "embedding",
            _EMBED_MODEL,
            "ok",
        )
        assert (row["call_count"], row["input_uncached"], row["input_total"]) == (3, 18, 18)
        assert row["duration_ms"] is not None and row["duration_ms"] >= 0
        assert recorder.stats.written == 3
    finally:
        await provider.close()
        await _delete(db, agent)


async def test_each_embeddings_request_is_one_bucket_call(monkeypatch, fast_retries):
    """A sub-batch is one request; a retried request is counted once, with its final status."""
    monkeypatch.setattr(embeddings_mod, "_MAX_BATCH_ITEMS", 2)
    buckets = _Buckets()
    provider = await _provider(_embed_ok, recorder=buckets)
    assert len(await asyncio.wait_for(provider.embed_batch(["a", "b", "c", "d", "e"]), _GUARD_S)) == 5
    assert [(c[0], c[1], c[2], c[4]) for c in buckets.calls] == [
        ("embedding", _EMBED_MODEL, n, "ok") for n in (6, 6, 3)
    ]
    await provider.close()

    def overloaded_once(request: httpx.Request, seen: list = []) -> httpx.Response:  # noqa: B006 -- a call count
        seen.append(request)
        return httpx.Response(429, json={}) if len(seen) == 1 else _embed_ok(request)

    buckets = _Buckets()
    provider = await _provider(overloaded_once, recorder=buckets)
    assert await asyncio.wait_for(provider.embed("x"), _GUARD_S) == [1.0, 0.5]
    assert [(c[2], c[4]) for c in buckets.calls] == [(3, "ok")]
    await provider.close()

    buckets = _Buckets()
    provider = await _provider(lambda r: httpx.Response(400, json={"error": {"message": "bad"}}), recorder=buckets)
    with pytest.raises(httpx.HTTPStatusError):
        await asyncio.wait_for(provider.embed("x"), _GUARD_S)
    with pytest.raises(httpx.HTTPStatusError):
        await asyncio.wait_for(provider.embed_batch(["x", "y"]), _GUARD_S)
    assert [(c[2], c[4]) for c in buckets.calls] == [(None, "error"), (None, "error")]
    await provider.close()

    monkeypatch.setattr(embeddings_mod, "_EMBED_BUDGET_S", 0.3)
    buckets = _Buckets()
    provider = await _provider(lambda r: httpx.Response(503, json={}), recorder=buckets)
    with pytest.raises(httpx.HTTPStatusError):
        await asyncio.wait_for(provider.embed("x"), _GUARD_S)
    assert [(c[2], c[4]) for c in buckets.calls] == [(None, "error")]  # four tries, one bucket call
    await provider.close()


async def test_embedding_cache_hit_records_nothing(monkeypatch):
    buckets = _Buckets()
    provider = await _provider(_embed_ok, cache_size=16, recorder=buckets)
    first = await asyncio.wait_for(provider.embed("same text"), _GUARD_S)
    assert len(buckets.calls) == 1
    assert await asyncio.wait_for(provider.embed("same text"), _GUARD_S) == first
    assert await asyncio.wait_for(provider.embed_batch(["same text", "same text"]), _GUARD_S) == [first, first]
    assert len(buckets.calls) == 1 and provider.cache_stats["hits"] == 2
    # A read path inside the fail-fast window sends nothing, so it records nothing either.
    provider._unavailable_until = time.monotonic() + 60
    with pytest.raises(EmbeddingUnavailableError):
        await provider.embed("another", fail_fast=True)
    assert len(buckets.calls) == 1
    await provider.close()


async def test_embedding_results_are_the_same_with_and_without_a_recorder(fast_retries):
    def script(request: httpx.Request, seen: list = []) -> httpx.Response:  # noqa: B006 -- a call count
        seen.append(request)
        return httpx.Response(500, json={}) if len(seen) % 3 == 1 else _embed_ok(request)

    results = {}
    for name, rec in (("plain", None), ("recorded", _Buckets())):
        provider = await _provider(script, recorder=rec)
        results[name] = (
            await asyncio.wait_for(provider.embed("one"), _GUARD_S),
            await asyncio.wait_for(provider.embed_batch(["two", "three"]), _GUARD_S),
        )
        await provider.close()
    assert results["plain"] == results["recorded"]


# ---------------------------------------------------------------------------------------------------------------------
# The cross-encoder
# ---------------------------------------------------------------------------------------------------------------------


class _Candidate:
    def __init__(self, name: str, text_: str) -> None:
        self.name = name
        self.text = text_
        self.score = 0.0


class _FakeCrossEncoder:
    """Scores a pair by its text length; sleeps a little, so a rerank takes measurable time."""

    def __init__(self, raises: Exception | None = None) -> None:
        self.raises = raises

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        time.sleep(0.01)
        if self.raises is not None:
            raise self.raises
        return [float(len(d)) for _, d in pairs]


def _candidates() -> list[_Candidate]:
    return [_Candidate("a", "x"), _Candidate("b", "xxx"), _Candidate("empty", ""), _Candidate("c", "xx")]


async def _rerank(candidates: list[_Candidate]) -> list[_Candidate]:
    return await asyncio.wait_for(
        cross_encoder_rerank(
            "query", candidates, lambda c: c.text, model_name=_RERANK_MODEL, max_candidates=3, text_limit=100
        ),
        _GUARD_S,
    )


@pytest.fixture
def fake_cross_encoder(monkeypatch):
    model = _FakeCrossEncoder()
    monkeypatch.setattr(reranker_mod, "CROSS_ENCODER_AVAILABLE", True)
    monkeypatch.setattr(reranker_mod, "_load_cross_encoder", lambda name: model)
    yield model
    reranker_mod.set_recorder(None)


@pytest.mark.postgres_only
async def test_rerank_records_duration_and_candidate_count_in_bucket(db, monkeypatch, fake_cross_encoder):
    agent = _agent()
    writer, recorder = _recorder(db, agent)
    monkeypatch.setattr(model_calls, "_utcnow", lambda: _MINUTE + timedelta(seconds=40))
    plain = [(c.name, c.score) for c in await _rerank(_candidates())]
    reranker_mod.set_recorder(recorder)
    try:
        # The head is 3 candidates, one with no text: 2 pairs are scored. The order and scores are unchanged.
        assert [(c.name, c.score) for c in await _rerank(_candidates())] == plain
        assert [c.name for c in await _rerank(_candidates())] == ["b", "a", "empty", "c"]
        await writer.drain(5.0)
        [row] = await _rows(db, agent)
        assert (row["started_at"], row["purpose"], row["call_kind"], row["model"], row["status"]) == (
            _MINUTE,
            "rerank",
            "rerank",
            _RERANK_MODEL,
            "ok",
        )
        assert (row["call_count"], row["input_uncached"]) == (2, 4)
        assert row["duration_ms"] >= 20  # two reranks of at least 10 ms each
    finally:
        await _delete(db, agent)


async def test_rerank_records_failures_and_nothing_when_no_model_runs(monkeypatch, fake_cross_encoder):
    buckets = _Buckets()
    reranker_mod.set_recorder(buckets)
    # No model call: nothing recorded.
    assert await _rerank([]) == []
    empty = [_Candidate("empty", "")]
    assert await _rerank(empty) is empty
    monkeypatch.setattr(reranker_mod, "CROSS_ENCODER_AVAILABLE", False)
    unavailable = _candidates()
    assert await _rerank(unavailable) is unavailable
    assert buckets.calls == []
    monkeypatch.setattr(reranker_mod, "CROSS_ENCODER_AVAILABLE", True)
    # The model fails: the candidates come back unranked, and the failure is an error bucket call.
    fake_cross_encoder.raises = RuntimeError("predict failed")
    failed = _candidates()
    assert await _rerank(failed) is failed

    def no_model(name: str) -> Any:
        raise OSError("no such model")

    monkeypatch.setattr(reranker_mod, "_load_cross_encoder", no_model)
    unloaded = _candidates()
    assert await _rerank(unloaded) is unloaded
    assert [(c[0], c[1], c[2], c[4]) for c in buckets.calls] == [
        ("rerank", _RERANK_MODEL, 2, "error"),
        ("rerank", _RERANK_MODEL, 2, "error"),
    ]
    assert buckets.calls[0][3] >= 10


# ---------------------------------------------------------------------------------------------------------------------
# A recorder fault never raises
# ---------------------------------------------------------------------------------------------------------------------


async def test_bucket_failure_does_not_raise(monkeypatch, fake_cross_encoder):
    """A fault inside ``record_bucket`` and a failed bucket write are counted; the embedding and the rerank go on."""
    plain_provider = await _provider(_embed_ok)
    plain_vectors = await asyncio.wait_for(plain_provider.embed_batch(["one", "two"]), _GUARD_S)
    await plain_provider.close()
    plain_order = [(c.name, c.score) for c in await _rerank(_candidates())]

    # 1. A fault while the bucket is built (inside record_bucket).
    writer = BoundedWriter("test")
    recorder = ModelCallRecorder(writer, _BrokenDatabase(), _agent(), SimpleNamespace())

    def clock_broken() -> datetime:
        raise RuntimeError("clock broken")

    with monkeypatch.context() as patch:
        patch.setattr(model_calls, "_utcnow", clock_broken)
        provider = await _provider(_embed_ok, recorder=recorder)
        assert await asyncio.wait_for(provider.embed_batch(["one", "two"]), _GUARD_S) == plain_vectors
        await provider.close()
        reranker_mod.set_recorder(recorder)
        assert [(c.name, c.score) for c in await _rerank(_candidates())] == plain_order
    assert recorder.stats.dropped_error == 2 and writer.stats.written == 0

    # 2. The bucket write fails in the writer (the database is down).
    writer = BoundedWriter("test")
    recorder = ModelCallRecorder(writer, _BrokenDatabase(), _agent(), SimpleNamespace())
    provider = await _provider(_embed_ok, recorder=recorder)
    assert await asyncio.wait_for(provider.embed_batch(["one", "two"]), _GUARD_S) == plain_vectors
    await provider.close()
    reranker_mod.set_recorder(recorder)
    assert [(c.name, c.score) for c in await _rerank(_candidates())] == plain_order
    await writer.drain(5.0)
    assert (recorder.stats.written, recorder.stats.dropped_error) == (0, 2)


async def test_a_body_without_usage_records_no_tokens():
    """The existing fakes answer ``{"data": [...]}``: the bucket call has no tokens, and nothing raises."""

    def no_usage(request: httpx.Request) -> httpx.Response:
        body = json.loads(_embed_ok(request).content)
        del body["usage"]
        return httpx.Response(200, json=body)

    buckets = _Buckets()
    provider = await _provider(no_usage, recorder=buckets)
    assert await asyncio.wait_for(provider.embed("abc"), _GUARD_S) == [3.0, 0.5]
    assert [(c[2], c[4]) for c in buckets.calls] == [(None, "ok")]
    await provider.close()


async def test_a_cancelled_embedding_or_rerank_is_a_cancelled_bucket_call(monkeypatch, fake_cross_encoder):
    """Nothing records until main.py sets a recorder; a request or rerank cut off by its caller is ``cancelled``."""
    assert EmbeddingProvider._recorder is None and reranker_mod._recorder is None

    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(60)
        return _embed_ok(request)

    buckets = _Buckets()
    provider = await _provider(hang, recorder=buckets)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(provider.embed("x"), 0.1)
    await provider.close()

    slow = _FakeCrossEncoder()
    slow.predict = lambda pairs: time.sleep(0.3) or [1.0] * len(pairs)  # type: ignore[method-assign]
    monkeypatch.setattr(reranker_mod, "_load_cross_encoder", lambda name: slow)
    reranker_mod.set_recorder(buckets)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            cross_encoder_rerank(
                "query", _candidates(), lambda c: c.text, model_name=_RERANK_MODEL, max_candidates=3, text_limit=100
            ),
            0.1,
        )
    assert [(c[0], c[2], c[4]) for c in buckets.calls] == [
        ("embedding", None, "cancelled"),
        ("rerank", 2, "cancelled"),
    ]
