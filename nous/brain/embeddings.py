"""Generate embeddings via OpenAI API.

Uses httpx.AsyncClient for async HTTP with connection pooling.
Gracefully handles missing API key by returning None.

Audit D2/S7 (2026-06-09): a bounded in-process LRU cache fronts the API.
The same query was embedded 4-7x per recall (one per per-type search leg)
and the same fact content up to ~10x per learn (dedup probes, _learn,
graph-linker templates); the densifier re-embeds the same orphan/candidate
templates every sleep cycle. All of those are exact-text repeats, so a
content-keyed LRU eliminates them without threading vectors through every
call signature. Cache size via NOUS_EMBEDDING_CACHE_SIZE (entries; 0
disables). Vectors are stored PACKED (array('f'), 4 bytes/dim — codex P2:
a list of boxed Python floats is ~50 MB at 1024x1536, not the naive 13);
packed float32 matches pgvector's float4 storage, so no downstream
precision is lost. 1024 entries x 1536 dims ~ 6 MB packed.

F-1/F-5 (2026-11-03): one wall-clock budget bounds each embed() and each
embed_batch sub-batch across all attempts (it was a 30 s timeout per attempt,
three attempts, and pre-turn legs embed one after another). Transport errors,
429 and 5xx are retried inside the budget. Read paths pass ``fail_fast=True``:
after a failed embed() those calls raise EmbeddingUnavailableError at once for
_FAIL_FAST_WINDOW_S, then one call goes through as a probe. Write paths do not
pass it, because a skipped write embed leaves a NULL vector. A sub-batch gets
the larger _EMBED_BATCH_BUDGET_S, and its failure does not start the window:
a large healthy batch can be slow, and that says nothing about one query.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from array import array
from collections import OrderedDict

import httpx

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 4
_RETRY_BACKOFF = (0.5, 1.0, 2.0)
# Wall-clock seconds for one embed(), all attempts and backoff included.
_EMBED_BUDGET_S = 10.0
# The same for one embed_batch sub-batch (up to 2048 inputs / 250 kB).
_EMBED_BATCH_BUDGET_S = 30.0
# After a failure, fail_fast calls raise at once for this many seconds.
_FAIL_FAST_WINDOW_S = 10.0
_DEFAULT_CACHE_SIZE = 1024

# OpenAI embeddings per-request hard limits (API facts, not tuning knobs).
# 2048 = max inputs per request. The byte budget bounds the ~300k-token
# per-request cap without a tokenizer dependency: byte-level BPE maps
# every token to a non-empty UTF-8 byte sequence, so tokens <= utf8
# bytes — a hard invariant, unlike char counts (one emoji char can be
# 3+ tokens; codex P2 on PR #554). Typical English text (~4 bytes/token)
# sits far below. A large transcript (~2000 chunks x ~600 chars ~ 300k
# tokens) previously shipped as ONE request → HTTP 400 → callers like
# the F067 chunk path aborted and stored nothing.
_MAX_BATCH_ITEMS = 2048
_MAX_BATCH_BYTES = 250_000


class EmbeddingUnavailableError(RuntimeError):
    """A fail_fast embed skipped the API because a recent call failed."""


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """The response's Retry-After in seconds, or None when absent or not a number."""
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


class EmbeddingProvider:
    """Async embedding generation using the OpenAI embeddings API."""

    def __init__(
        self,
        api_key: str,
        model: str = "text-embedding-3-small",
        dimensions: int = 1536,
        cache_size: int | None = None,
    ) -> None:
        self.model = model
        self.dimensions = dimensions
        if cache_size is None:
            try:
                cache_size = int(
                    os.environ.get("NOUS_EMBEDDING_CACHE_SIZE", str(_DEFAULT_CACHE_SIZE))
                )
            except ValueError:
                cache_size = _DEFAULT_CACHE_SIZE
        self._cache_size = max(0, cache_size)
        self._cache: OrderedDict[str, array] = OrderedDict()
        self.cache_hits = 0
        self.cache_misses = 0
        # fail_fast state: monotonic time until which fail_fast calls raise
        # (None = the last call did not fail), and whether the one probe
        # allowed after that time is still running.
        self._unavailable_until: float | None = None
        self._probe_in_flight = False
        self._client = httpx.AsyncClient(
            base_url="https://api.openai.com/v1",
            headers={"Authorization": f"Bearer {api_key}"},
            # The per-call budgets (asyncio.timeout) are the real bound. This
            # per-phase timeout must not cut a slow sub-batch short of its own.
            timeout=_EMBED_BATCH_BUDGET_S,
        )

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------

    def _cache_key(self, text: str) -> str:
        # Model + dimensions in the key so a provider reconfiguration can
        # never serve vectors from a different embedding space.
        return hashlib.sha256(
            f"{self.model}:{self.dimensions}:{text}".encode("utf-8", "replace")
        ).hexdigest()

    def _cache_get(self, key: str) -> list[float] | None:
        if self._cache_size == 0:
            return None
        vec = self._cache.get(key)
        if vec is None:
            self.cache_misses += 1
            return None
        self._cache.move_to_end(key)
        self.cache_hits += 1
        # Materializing a fresh list from the packed array is also the
        # defensive copy — a caller mutating the result can't poison the
        # cached entry.
        return vec.tolist()

    def _cache_put(self, key: str, vec: list[float]) -> None:
        if self._cache_size == 0 or not vec:
            return
        # Packed float32 (codex P2): ~4 bytes/dim instead of ~48 for boxed
        # Python floats. pgvector stores float4, so f32 loses nothing the
        # database would have kept anyway.
        self._cache[key] = array("f", vec)
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    @property
    def cache_stats(self) -> dict[str, int]:
        """Hit/miss/size counters for observability and eval validation."""
        return {
            "hits": self.cache_hits,
            "misses": self.cache_misses,
            "entries": len(self._cache),
            "capacity": self._cache_size,
        }

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    async def _post_with_retry(self, payload: dict) -> httpx.Response:
        """POST to the embeddings endpoint within one wall-clock budget.

        Transport errors, timeouts, 429 and 5xx are retried with backoff (or
        the response's Retry-After when it fits) until the budget runs out.
        Any other 4xx raises at once and leaves the fail_fast window as it
        was: it is about this request, not the provider. An exhausted
        retryable failure of a single text opens the window; a sub-batch's
        does not. Any success closes it.
        """
        # embed_batch sends a list: a sub-batch gets the batch budget, and its
        # failure (possibly a large, slow but healthy request) opens no window.
        batch = isinstance(payload.get("input"), list)
        budget = _EMBED_BATCH_BUDGET_S if batch else _EMBED_BUDGET_S
        deadline = time.monotonic() + budget
        last_exc: Exception = TimeoutError(f"embeddings budget of {budget}s spent")
        for attempt in range(_MAX_ATTEMPTS):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            retry_after: float | None = None
            try:
                # The hard bound: httpx timeouts are per phase, not per request.
                async with asyncio.timeout(remaining):
                    response = await self._client.post("/embeddings", json=payload)
            except (httpx.TransportError, TimeoutError) as e:
                logger.warning(
                    "OpenAI embeddings network error (attempt %d/%d): %r",
                    attempt + 1,
                    _MAX_ATTEMPTS,
                    e,
                )
                last_exc = e
            else:
                if response.status_code != 429 and response.status_code < 500:
                    response.raise_for_status()
                    self._unavailable_until = None
                    return response
                logger.warning(
                    "OpenAI embeddings API returned %d (attempt %d/%d)",
                    response.status_code,
                    attempt + 1,
                    _MAX_ATTEMPTS,
                )
                last_exc = httpx.HTTPStatusError(
                    f"Server error {response.status_code}",
                    request=response.request,
                    response=response,
                )
                retry_after = _retry_after_seconds(response)

            if attempt == _MAX_ATTEMPTS - 1:
                break
            delay = retry_after if retry_after is not None else _RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)]
            if time.monotonic() + delay >= deadline:
                break
            await asyncio.sleep(delay)

        if not batch:
            self._unavailable_until = time.monotonic() + _FAIL_FAST_WINDOW_S
        raise last_exc

    async def embed(self, text: str, *, fail_fast: bool = False) -> list[float]:
        """Generate embedding for a single text (LRU-cached).

        ``fail_fast`` is for read paths: within _FAIL_FAST_WINDOW_S of a
        failed call it raises EmbeddingUnavailableError without calling the
        API. After the window one fail_fast call goes through as a probe, and
        the others still raise until it returns. Write paths leave it False,
        so they always try.
        """
        key = self._cache_key(text)
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        probe = False
        if fail_fast and self._unavailable_until is not None:
            if time.monotonic() < self._unavailable_until or self._probe_in_flight:
                raise EmbeddingUnavailableError("embeddings API failed recently; skipped on a read path")
            self._probe_in_flight = probe = True
        try:
            response = await self._post_with_retry({
                "model": self.model,
                "input": text,
                "dimensions": self.dimensions,
            })
        finally:
            if probe:
                self._probe_in_flight = False
        vec = response.json()["data"][0]["embedding"]
        self._cache_put(key, vec)
        return vec

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts.

        Cache-aware: only texts missing from the LRU are sent to the API
        (duplicates collapsed); results are reassembled in input order.
        Misses are split into sub-batches respecting the OpenAI
        per-request limits (_MAX_BATCH_ITEMS inputs, _MAX_BATCH_BYTES
        UTF-8 bytes) and sent sequentially; each sub-batch is cached as it
        succeeds, so a mid-run failure doesn't re-pay completed requests
        when the caller retries.
        """
        if not texts:
            return []
        keys = [self._cache_key(t) for t in texts]
        # Per-call memo so duplicate texts within one batch count a single
        # hit/miss (review P2: cache_misses must track API-item count).
        lookup_memo: dict[str, list[float] | None] = {}
        out: list[list[float] | None] = []
        for k in keys:
            if k not in lookup_memo:
                lookup_memo[k] = self._cache_get(k)
            out.append(lookup_memo[k])

        # Unique missing texts, preserving first-seen order.
        miss_keys: list[str] = []
        miss_texts: list[str] = []
        seen: set[str] = set()
        for k, t, v in zip(keys, texts, out):
            if v is None and k not in seen:
                seen.add(k)
                miss_keys.append(k)
                miss_texts.append(t)

        if miss_texts:
            # UTF-8 byte lengths, not char counts: tokens <= utf8 bytes is
            # the invariant that makes the budget a hard token bound.
            byte_lens = [len(t.encode("utf-8", "replace")) for t in miss_texts]
            fetched: dict[str, list[float]] = {}
            start = 0
            n_batches = 0
            while start < len(miss_texts):
                # Greedy sub-batch: extend while both per-request limits
                # hold. A single text over the byte budget still ships as
                # a batch of one (end starts past it) rather than looping.
                end = start + 1
                batch_bytes = byte_lens[start]
                while (
                    end < len(miss_texts)
                    and end - start < _MAX_BATCH_ITEMS
                    and batch_bytes + byte_lens[end] <= _MAX_BATCH_BYTES
                ):
                    batch_bytes += byte_lens[end]
                    end += 1
                sub_texts = miss_texts[start:end]
                n_batches += 1

                response = await self._post_with_retry({
                    "model": self.model,
                    "input": sub_texts,
                    "dimensions": self.dimensions,
                })
                data = response.json()["data"]
                vectors = [
                    item["embedding"] for item in sorted(data, key=lambda x: x["index"])
                ]
                # Guard BEFORE caching: a short/skipped-index response would
                # misalign the zip and poison the LRU with wrong vectors that
                # then silently serve dedup/recall until eviction (review P2).
                if len(vectors) != len(sub_texts):
                    raise RuntimeError(
                        f"embeddings API returned {len(vectors)} vectors "
                        f"for {len(sub_texts)} inputs"
                    )
                for k, v in zip(miss_keys[start:end], vectors):
                    fetched[k] = v
                    self._cache_put(k, v)
                start = end

            if n_batches > 1:
                logger.info(
                    "embed_batch: %d texts split into %d sub-batches",
                    len(miss_texts), n_batches,
                )
            out = [v if v is not None else fetched[k] for k, v in zip(keys, out)]

        return out  # type: ignore[return-value]

    async def close(self) -> None:
        """Close the underlying httpx client."""
        await self._client.aclose()
