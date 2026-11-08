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
pass it, because a skipped write embed leaves a NULL vector. A write-path
sub-batch gets the larger _EMBED_BATCH_BUDGET_S, and its failure does not start
the window: a large healthy batch can be slow, and that says nothing about one
query. A read-path embed_batch(fail_fast=True) behaves like embed(): the window
applies, and each sub-batch gets the single-text budget and can open it.

F108: with a recorder set (``set_recorder``), each embeddings HTTP request (an
embed() miss or one embed_batch sub-batch, its retries included) is counted
once in its minute's ``embedding`` bucket: its final status, its
``usage.total_tokens`` and its time. A cache hit sends nothing and records
nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import time
from array import array
from collections import OrderedDict
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from nous.observability.model_calls import ModelCallRecorder

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 4
_RETRY_BACKOFF = (0.5, 1.0, 2.0)
# Wall-clock seconds for one embed(), all attempts and backoff included.
_EMBED_BUDGET_S = 10.0
# The same for one embed_batch sub-batch (up to 2048 inputs / 250 kB).
_EMBED_BATCH_BUDGET_S = 30.0
# After a failure, fail_fast calls raise at once for this many seconds.
_FAIL_FAST_WINDOW_S = 10.0
# Each input is cut to this many chars before it is sent (and cache-keyed).
# text-embedding-3 caps an input at 8191 tokens and longer input gets a 400;
# tiktoken is not a dependency, so this is a conservative CHAR bound: English
# prose and code run >= 2 chars/token. Dense CJK text can still exceed it.
_EMBED_CHAR_CAP = 16000
# Rows with a NULL vector re-embedded per type per sleep cycle (H-5 backfill).
_REEMBED_MAX_PER_CYCLE = 20
# A 4xx that rejects this request's input. Any other failure (401/403, 429,
# 5xx, transport) would fail every row alike, so it stops a backfill.
_REJECTED_INPUT_STATUSES = frozenset({400, 413, 422})
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


def _capped(text: str) -> str:
    """``text`` cut to _EMBED_CHAR_CAP chars, so an oversized input is embedded, not rejected."""
    if len(text) <= _EMBED_CHAR_CAP:
        return text
    logger.debug("Embedding input cut from %d to %d chars", len(text), _EMBED_CHAR_CAP)
    return text[:_EMBED_CHAR_CAP]


async def reembed_rows(embedder: Any, rows: Sequence[tuple[Any, str]], what: str) -> dict[Any, list[float]]:
    """Embed ``(id, text)`` rows one at a time for a sleep-cycle backfill.

    A row whose input the API rejects (400, 413, 422) is skipped, so it cannot
    block the rows after it. Any other failure (401/403, 429, 5xx, transport
    error, timeout) would fail every row, so it stops the batch; the next
    cycle retries. Empty texts are skipped. Returns the vectors by id.
    """
    vectors: dict[Any, list[float]] = {}
    for row_id, text in rows:
        if not text:
            continue
        try:
            vectors[row_id] = await embedder.embed(text)
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status in _REJECTED_INPUT_STATUSES:
                logger.warning("%s re-embed rejected for %s (HTTP %d); skipped", what, row_id, status)
                continue
            logger.warning(
                "%s re-embed failed for %s (HTTP %d); the rest wait for the next sleep cycle", what, row_id, status
            )
            break
        except Exception:
            logger.warning(
                "%s re-embed failed for %s; the rest wait for the next sleep cycle", what, row_id, exc_info=True
            )
            break
    return vectors


def _total_tokens(body: Any) -> int | None:
    """The response body's ``usage.total_tokens``, or None when it has none."""
    usage = body.get("usage") if isinstance(body, dict) else None
    tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
    return tokens if isinstance(tokens, int) and not isinstance(tokens, bool) else None


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

    # F108: the recorder each request reports to (set_recorder); None records nothing.
    _recorder: ModelCallRecorder | None = None

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

    def set_recorder(self, recorder: ModelCallRecorder | None) -> None:
        """Count each embeddings request from now on in ``recorder``'s per-minute ``embedding`` bucket; None stops."""
        self._recorder = recorder

    async def _request(self, payload: dict, *, batch: bool = False) -> Any:
        """``_post_with_retry`` and the response's parsed body. With a recorder set, the request is counted once,
        with its final status, its ``usage.total_tokens`` and its time, retries included."""
        recorder = self._recorder
        if recorder is None:
            return (await self._post_with_retry(payload, batch=batch)).json()
        started = time.perf_counter()
        status, tokens = "error", None
        try:
            body = (await self._post_with_retry(payload, batch=batch)).json()
            status, tokens = "ok", _total_tokens(body)
            return body
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            recorder.record_bucket(
                purpose="embedding",
                model=self.model,
                tokens=tokens,
                duration_ms=(time.perf_counter() - started) * 1000,
                status=status,
            )

    async def _post_with_retry(self, payload: dict, *, batch: bool = False) -> httpx.Response:
        """POST to the embeddings endpoint within one wall-clock budget.

        Transport errors, timeouts, 429 and 5xx are retried with backoff (or
        the response's Retry-After when it fits) until the budget runs out.
        Any other 4xx raises at once and leaves the fail_fast window as it
        was: it is about this request, not the provider. Any success closes
        the window. ``batch`` marks a write-path sub-batch: it gets the batch
        budget, and its exhausted failure (possibly a large, slow but healthy
        request) opens no window. Otherwise the failure opens it.
        """
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

    @contextlib.contextmanager
    def _fail_fast_gate(self, fail_fast: bool) -> Iterator[None]:
        """For a fail_fast call: raise inside the window; after it, let one probe through."""
        probe = False
        if fail_fast and self._unavailable_until is not None:
            if time.monotonic() < self._unavailable_until or self._probe_in_flight:
                raise EmbeddingUnavailableError("embeddings API failed recently; skipped on a read path")
            self._probe_in_flight = probe = True
        try:
            yield
        finally:
            if probe:
                self._probe_in_flight = False

    async def embed(self, text: str, *, fail_fast: bool = False) -> list[float]:
        """Generate embedding for a single text (LRU-cached).

        ``fail_fast`` is for read paths: within _FAIL_FAST_WINDOW_S of a
        failed call it raises EmbeddingUnavailableError without calling the
        API. After the window one fail_fast call goes through as a probe, and
        the others still raise until it returns. Write paths leave it False,
        so they always try. Text over _EMBED_CHAR_CAP chars is cut first; the
        cache keys on what is sent.
        """
        text = _capped(text)
        key = self._cache_key(text)
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        with self._fail_fast_gate(fail_fast):
            body = await self._request({
                "model": self.model,
                "input": text,
                "dimensions": self.dimensions,
            })
        vec = body["data"][0]["embedding"]
        self._cache_put(key, vec)
        return vec

    async def embed_batch(self, texts: list[str], *, fail_fast: bool = False) -> list[list[float]]:
        """Generate embeddings for multiple texts.

        Cache-aware: only texts missing from the LRU are sent to the API
        (duplicates collapsed); results are reassembled in input order.
        Misses are split into sub-batches respecting the OpenAI
        per-request limits (_MAX_BATCH_ITEMS inputs, _MAX_BATCH_BYTES
        UTF-8 bytes) and sent sequentially; each sub-batch is cached as it
        succeeds, so a mid-run failure doesn't re-pay completed requests
        when the caller retries. Each text is cut to _EMBED_CHAR_CAP chars
        first, as in embed(). ``fail_fast`` is for read paths and works as in
        embed(): inside the window a batch with any cache miss raises at once,
        and each sub-batch gets the single-text budget.
        """
        if not texts:
            return []
        texts = [_capped(t) for t in texts]
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
            with self._fail_fast_gate(fail_fast):
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

                    body = await self._request(
                        {"model": self.model, "input": sub_texts, "dimensions": self.dimensions},
                        batch=not fail_fast,
                    )
                    data = body["data"]
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
