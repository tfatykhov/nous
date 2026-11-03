"""EmbeddingProvider wall-clock budget and read-path fail-fast (F-1/F-5).

A hung or failing embeddings API used to stall a turn: a 30 s client timeout,
three attempts and no retry on 429 put the worst case near 90 s per embed(),
paid once per pre-turn search leg. Now every embed() (and every embed_batch
sub-batch) has one wall-clock budget across attempts, transport errors, 429 and
5xx are retried inside it, and read paths opt into ``fail_fast``: after a
failure they raise at once for a short window, then one call probes.

The budget, window and backoff are module constants, shrunk here so the suite
runs in well under a second per test. Every call is wrapped in an outer
``asyncio.wait_for`` so a regression fails instead of hanging.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from nous.brain import embeddings as embeddings_mod
from nous.brain.embeddings import EmbeddingProvider

_OUTER_GUARD_S = 5.0


def _ok(request: httpx.Request) -> httpx.Response:
    inputs = json.loads(request.content)["input"]
    count = len(inputs) if isinstance(inputs, list) else 1
    return httpx.Response(200, json={"data": [{"index": i, "embedding": [0.1, 0.2]} for i in range(count)]})


def _is_batch(request: httpx.Request) -> bool:
    return isinstance(json.loads(request.content)["input"], list)


@pytest.fixture
def fast_limits(monkeypatch):
    """Small budgets, window and backoff so retries and windows fit in a test."""
    monkeypatch.setattr(embeddings_mod, "_EMBED_BUDGET_S", 0.3, raising=False)
    monkeypatch.setattr(embeddings_mod, "_EMBED_BATCH_BUDGET_S", 0.6, raising=False)
    monkeypatch.setattr(embeddings_mod, "_FAIL_FAST_WINDOW_S", 10.0, raising=False)
    monkeypatch.setattr(embeddings_mod, "_RETRY_BACKOFF", (0.01, 0.01, 0.01))


async def _provider(handler) -> EmbeddingProvider:
    provider = EmbeddingProvider(api_key="test", cache_size=0)
    await provider._client.aclose()
    provider._client = httpx.AsyncClient(
        base_url="https://api.openai.com/v1",
        transport=httpx.MockTransport(handler),
    )
    return provider


async def _guarded(coro):
    return await asyncio.wait_for(coro, timeout=_OUTER_GUARD_S)


async def test_hung_provider_is_bounded_by_the_budget(fast_limits):
    async def hang(request):
        await asyncio.sleep(60)
        return _ok(request)

    provider = await _provider(hang)
    start = time.monotonic()
    with pytest.raises(Exception) as excinfo:
        await _guarded(provider.embed("x"))
    elapsed = time.monotonic() - start
    await provider.close()
    assert elapsed < 1.5, f"embed() took {elapsed:.2f}s against a 0.3s budget"
    assert not isinstance(excinfo.value, asyncio.CancelledError)


async def test_hung_batch_is_bounded_by_the_budget(fast_limits):
    async def hang(request):
        await asyncio.sleep(60)
        return _ok(request)

    provider = await _provider(hang)
    start = time.monotonic()
    with pytest.raises(Exception):
        await _guarded(provider.embed_batch(["a", "b"]))
    elapsed = time.monotonic() - start
    await provider.close()
    assert elapsed < 1.5, f"embed_batch() took {elapsed:.2f}s against a 0.6s budget"


async def test_a_sub_batch_gets_the_larger_batch_budget(fast_limits, monkeypatch):
    # Wide margin on the batch side so a stalled CI event loop cannot flip it.
    monkeypatch.setattr(embeddings_mod, "_EMBED_BATCH_BUDGET_S", 2.0)

    async def slow_but_healthy(request):
        await asyncio.sleep(0.4)  # over the single-text budget, under the batch one
        return _ok(request)

    provider = await _provider(slow_but_healthy)
    assert await _guarded(provider.embed_batch(["a", "b"])) == [[0.1, 0.2], [0.1, 0.2]]
    with pytest.raises(TimeoutError):
        await _guarded(provider.embed("c"))
    await provider.close()


async def test_a_failed_batch_does_not_open_the_fail_fast_window(fast_limits):
    calls = 0

    def batches_fail(request):
        nonlocal calls
        calls += 1
        return httpx.Response(503) if _is_batch(request) else _ok(request)

    provider = await _provider(batches_fail)
    with pytest.raises(httpx.HTTPStatusError):
        await _guarded(provider.embed_batch(["a", "b"]))
    before = calls
    assert await _guarded(provider.embed("query", fail_fast=True)) == [0.1, 0.2]
    assert calls == before + 1, "a failed batch made a fail_fast read skip the API"
    await provider.close()


async def test_fail_fast_call_in_window_does_not_reach_transport(fast_limits):
    calls = 0

    def unavailable(request):
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    provider = await _provider(unavailable)
    with pytest.raises(httpx.HTTPStatusError):
        await _guarded(provider.embed("first", fail_fast=True))
    after_first = calls
    assert after_first >= 1

    with pytest.raises(embeddings_mod.EmbeddingUnavailableError):
        await _guarded(provider.embed("second", fail_fast=True))
    assert calls == after_first, "a fail_fast call inside the window reached the transport"
    await provider.close()


async def test_window_end_lets_exactly_one_probe_through(fast_limits, monkeypatch):
    monkeypatch.setattr(embeddings_mod, "_FAIL_FAST_WINDOW_S", 0.05, raising=False)
    calls = 0
    healthy = False

    async def flaky(request):
        nonlocal calls
        calls += 1
        if not healthy:
            return httpx.Response(503)
        await asyncio.sleep(0.1)  # keep the probe in flight while the second call arrives
        return _ok(request)

    provider = await _provider(flaky)
    with pytest.raises(httpx.HTTPStatusError):
        await _guarded(provider.embed("down", fail_fast=True))
    await asyncio.sleep(0.06)
    healthy = True
    before = calls

    results = await _guarded(
        asyncio.gather(
            provider.embed("probe-1", fail_fast=True),
            provider.embed("probe-2", fail_fast=True),
            return_exceptions=True,
        )
    )
    assert calls - before == 1, "more than one call went through as the half-open probe"
    assert sum(isinstance(r, list) for r in results) == 1
    assert sum(isinstance(r, embeddings_mod.EmbeddingUnavailableError) for r in results) == 1

    # The probe succeeded, so the window is closed and fail_fast calls go through.
    assert await _guarded(provider.embed("after", fail_fast=True)) == [0.1, 0.2]
    await provider.close()


async def test_non_fail_fast_call_still_retries_inside_the_window(fast_limits):
    calls = 0
    fail_next = 10**6

    def handler(request):
        nonlocal calls, fail_next
        calls += 1
        if fail_next > 0:
            fail_next -= 1
            return httpx.Response(503)
        return _ok(request)

    provider = await _provider(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await _guarded(provider.embed("open the window", fail_fast=True))

    # The window is open; a write-path call ignores it and retries a 503.
    fail_next = 1
    before = calls
    assert await _guarded(provider.embed("write path")) == [0.1, 0.2]
    assert calls - before == 2
    await provider.close()


async def test_429_is_retried(fast_limits):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return _ok(request)

    provider = await _provider(handler)
    assert await _guarded(provider.embed("x")) == [0.1, 0.2]
    assert calls == 2
    await provider.close()


async def test_retry_after_beyond_the_budget_gives_up_at_once(fast_limits):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "60"})

    provider = await _provider(handler)
    start = time.monotonic()
    with pytest.raises(httpx.HTTPStatusError):
        await _guarded(provider.embed("x"))
    assert calls == 1
    assert time.monotonic() - start < 0.2
    await provider.close()


@pytest.mark.parametrize("exc_type", [httpx.ConnectTimeout, httpx.RemoteProtocolError])
async def test_transport_errors_are_retried(fast_limits, exc_type):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise exc_type("boom", request=request)
        return _ok(request)

    provider = await _provider(handler)
    assert await _guarded(provider.embed("x")) == [0.1, 0.2]
    assert calls == 2
    await provider.close()


async def test_a_400_is_not_retried_and_does_not_open_the_window(fast_limits):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(400, json={"error": "input too long"})
        return _ok(request)

    provider = await _provider(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await _guarded(provider.embed("too long", fail_fast=True))
    assert calls == 1
    # A per-request rejection says nothing about the provider's health.
    assert await _guarded(provider.embed("fine", fail_fast=True)) == [0.1, 0.2]
    assert calls == 2
    await provider.close()
