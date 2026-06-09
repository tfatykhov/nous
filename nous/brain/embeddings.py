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
disables). 1024 entries x 1536 floats ~ 13 MB ceiling.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from collections import OrderedDict

import httpx

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_RETRY_BACKOFF = (0.5, 1.0, 2.0)
_DEFAULT_CACHE_SIZE = 1024


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
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self.cache_hits = 0
        self.cache_misses = 0
        self._client = httpx.AsyncClient(
            base_url="https://api.openai.com/v1",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
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
        # Defensive copy — vectors are list-mutated nowhere today, but a
        # poisoned cache entry would silently corrupt every later consumer.
        return list(vec)

    def _cache_put(self, key: str, vec: list[float]) -> None:
        if self._cache_size == 0 or not vec:
            return
        self._cache[key] = list(vec)
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
        """POST to embeddings endpoint with retry on 5xx/network errors."""
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                response = await self._client.post("/embeddings", json=payload)
                if response.status_code < 500:
                    response.raise_for_status()
                    return response
                # 5xx — retry
                logger.warning(
                    "OpenAI embeddings API returned %d (attempt %d/%d)",
                    response.status_code, attempt + 1, _MAX_RETRIES,
                )
                last_exc = httpx.HTTPStatusError(
                    f"Server error {response.status_code}",
                    request=response.request,
                    response=response,
                )
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout) as e:
                logger.warning(
                    "OpenAI embeddings network error (attempt %d/%d): %s",
                    attempt + 1, _MAX_RETRIES, e,
                )
                last_exc = e

            if attempt < _MAX_RETRIES - 1:
                await asyncio.sleep(_RETRY_BACKOFF[attempt])

        raise last_exc  # type: ignore[misc]

    async def embed(self, text: str) -> list[float]:
        """Generate embedding for a single text (LRU-cached)."""
        key = self._cache_key(text)
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        response = await self._post_with_retry({
            "model": self.model,
            "input": text,
            "dimensions": self.dimensions,
        })
        vec = response.json()["data"][0]["embedding"]
        self._cache_put(key, vec)
        return vec

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts.

        Cache-aware: only texts missing from the LRU are sent to the API
        (one call for all misses, duplicates collapsed); results are
        reassembled in input order.
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
            response = await self._post_with_retry({
                "model": self.model,
                "input": miss_texts,
                "dimensions": self.dimensions,
            })
            data = response.json()["data"]
            vectors = [item["embedding"] for item in sorted(data, key=lambda x: x["index"])]
            # Guard BEFORE caching: a short/skipped-index response would
            # misalign the zip and poison the LRU with wrong vectors that
            # then silently serve dedup/recall until eviction (review P2).
            if len(vectors) != len(miss_texts):
                raise RuntimeError(
                    f"embeddings API returned {len(vectors)} vectors "
                    f"for {len(miss_texts)} inputs"
                )
            fetched = dict(zip(miss_keys, vectors))
            for k, v in fetched.items():
                self._cache_put(k, v)
            out = [v if v is not None else fetched[k] for k, v in zip(keys, out)]

        return out  # type: ignore[return-value]

    async def close(self) -> None:
        """Close the underlying httpx client."""
        await self._client.aclose()
