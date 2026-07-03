"""embed_batch sub-batching (2026-07-03 MAB large-transcript bug).

``embed_batch`` sent ALL cache misses in ONE OpenAI embeddings request.
A large transcript (~2000 chunks x ~600 chars ~ 300k tokens) blows the
API's per-request token cap and returns HTTP 400 — the summarizer's F067
chunk path catches the error and aborts, storing 0 chunks. Note the
observed 400 fired BELOW the 2048 input-count cap, so the binding limit
is the ~300k-token/request cap: the fix needs a size budget, not just a
count cap.

Fix: split misses into sub-batches capped at ``_MAX_BATCH_ITEMS`` (2048,
the OpenAI input-count hard limit) AND ``_MAX_BATCH_CHARS`` (250k chars,
a dependency-free proxy that stays under 300k tokens even at the
1-token-per-char worst case), sequential requests, reassembled in input
order. Each sub-batch is cached as it succeeds so a mid-run failure
doesn't re-pay earlier requests on retry.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nous.brain import embeddings as embeddings_mod
from nous.brain.embeddings import EmbeddingProvider


def _content_keyed_provider(api_calls: list, cache_size: int = 4096) -> EmbeddingProvider:
    """Provider whose fake vectors depend ONLY on text content.

    The pre-existing test fake encodes the index-within-request, which
    would mask cross-sub-batch reassembly bugs (index resets per request).
    """
    provider = EmbeddingProvider(api_key="test", cache_size=cache_size)

    async def fake_post(payload):
        api_calls.append(payload)
        inp = payload["input"]
        texts = [inp] if isinstance(inp, str) else inp
        data = [
            {"index": i, "embedding": [float(len(t)), float(ord(t[0]))]}
            for i, t in enumerate(texts)
        ]
        response = MagicMock()
        response.json = MagicMock(return_value={"data": data})
        return response

    provider._post_with_retry = fake_post
    return provider


def _expected(text: str) -> list[float]:
    return [float(len(text)), float(ord(text[0]))]


class TestDefaults:
    def test_caps_match_openai_limits(self):
        assert embeddings_mod._MAX_BATCH_ITEMS == 2048
        # 300k tokens/request at the 1-token-per-char worst case
        assert 0 < embeddings_mod._MAX_BATCH_CHARS <= 300_000


class TestCountCap:
    @pytest.mark.asyncio
    async def test_items_over_cap_split_into_multiple_requests(self, monkeypatch):
        monkeypatch.setattr(embeddings_mod, "_MAX_BATCH_ITEMS", 2)
        calls: list = []
        p = _content_keyed_provider(calls)
        texts = ["aa", "bbb", "cccc", "ddddd", "e"]
        result = await p.embed_batch(texts)
        assert [len(c["input"]) for c in calls] == [2, 2, 1]
        assert calls[0]["input"] == ["aa", "bbb"]
        assert calls[2]["input"] == ["e"]
        assert result == [_expected(t) for t in texts]

    @pytest.mark.asyncio
    async def test_default_cap_splits_2049_inputs(self):
        """The real 2048 OpenAI cap: 2049 unique tiny texts → 2 requests."""
        calls: list = []
        p = _content_keyed_provider(calls, cache_size=0)
        texts = [f"t{i}" for i in range(2049)]
        result = await p.embed_batch(texts)
        assert [len(c["input"]) for c in calls] == [2048, 1]
        assert result[0] == _expected("t0")
        assert result[-1] == _expected("t2048")


class TestCharBudget:
    @pytest.mark.asyncio
    async def test_char_budget_splits_requests(self, monkeypatch):
        """The MAB failure shape: input count under the cap, total size over
        the token budget — MUST still split."""
        monkeypatch.setattr(embeddings_mod, "_MAX_BATCH_CHARS", 10)
        calls: list = []
        p = _content_keyed_provider(calls)
        result = await p.embed_batch(["aaaaa", "bbbbb", "cc"])
        assert [c["input"] for c in calls] == [["aaaaa", "bbbbb"], ["cc"]]
        assert result == [_expected("aaaaa"), _expected("bbbbb"), _expected("cc")]

    @pytest.mark.asyncio
    async def test_oversized_single_text_sent_alone(self, monkeypatch):
        """A single text larger than the budget still ships as a batch of
        one — never dropped, never an infinite loop."""
        monkeypatch.setattr(embeddings_mod, "_MAX_BATCH_CHARS", 10)
        calls: list = []
        p = _content_keyed_provider(calls)
        big = "x" * 50
        result = await p.embed_batch(["aa", big, "bb"])
        assert [c["input"] for c in calls] == [["aa"], [big], ["bb"]]
        assert result == [_expected("aa"), _expected(big), _expected("bb")]


class TestCrossBatchSemantics:
    @pytest.mark.asyncio
    async def test_reassembly_preserves_input_order_across_sub_batches(self, monkeypatch):
        monkeypatch.setattr(embeddings_mod, "_MAX_BATCH_ITEMS", 2)
        calls: list = []
        p = _content_keyed_provider(calls)
        # duplicates + a warm cache hit interleaved with cold misses
        await p.embed("warm")
        texts = ["c1", "warm", "c2", "c1", "c3", "c4", "c5"]
        result = await p.embed_batch(texts)
        # unique misses c1..c5 → sub-batches of 2, 2, 1; no re-embed of warm
        assert [len(c["input"]) for c in calls[1:]] == [2, 2, 1]
        assert result == [_expected(t) for t in texts]

    @pytest.mark.asyncio
    async def test_mid_run_failure_keeps_earlier_sub_batch_cached(self, monkeypatch):
        monkeypatch.setattr(embeddings_mod, "_MAX_BATCH_ITEMS", 2)
        calls: list = []
        p = _content_keyed_provider(calls)
        real_post = p._post_with_retry

        async def failing_second(payload):
            if calls:  # first request succeeds, second fails
                raise RuntimeError("api down")
            return await real_post(payload)

        p._post_with_retry = failing_second
        with pytest.raises(RuntimeError, match="api down"):
            await p.embed_batch(["k1", "k2", "k3"])
        # first sub-batch landed in the LRU: retry serves k1/k2 without API
        p._post_with_retry = real_post
        await p.embed_batch(["k1", "k2", "k3"])
        assert [c["input"] for c in calls[1:]] == [["k3"]]

    @pytest.mark.asyncio
    async def test_short_response_in_later_sub_batch_raises(self, monkeypatch):
        """The review-P2 misalignment guard must run PER sub-batch."""
        monkeypatch.setattr(embeddings_mod, "_MAX_BATCH_ITEMS", 2)
        p = _content_keyed_provider([])
        real_post = p._post_with_retry
        request_n = 0

        async def short_second(payload):
            nonlocal request_n
            request_n += 1
            if request_n < 2:
                return await real_post(payload)
            response = MagicMock()
            response.json = MagicMock(return_value={"data": [
                {"index": 0, "embedding": [1.0, 0.0]},
            ]})
            return response

        p._post_with_retry = short_second
        with pytest.raises(RuntimeError, match="returned 1 vectors for 2"):
            await p.embed_batch(["m1", "m2", "m3", "m4"])
