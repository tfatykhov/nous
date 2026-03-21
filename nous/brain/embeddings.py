"""Generate embeddings via OpenAI API.

Uses httpx.AsyncClient for async HTTP with connection pooling.
Gracefully handles missing API key by returning None.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_RETRY_BACKOFF = (0.5, 1.0, 2.0)


class EmbeddingProvider:
    """Async embedding generation using OpenAI text-embedding-3-small."""

    def __init__(
        self,
        api_key: str,
        model: str = "text-embedding-3-small",
        dimensions: int = 1536,
    ) -> None:
        self.model = model
        self.dimensions = dimensions
        self._client = httpx.AsyncClient(
            base_url="https://api.openai.com/v1",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
        )

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
        """Generate embedding for a single text."""
        response = await self._post_with_retry({
            "model": self.model,
            "input": text,
            "dimensions": self.dimensions,
        })
        return response.json()["data"][0]["embedding"]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts (single API call)."""
        response = await self._post_with_retry({
            "model": self.model,
            "input": texts,
            "dimensions": self.dimensions,
        })
        data = response.json()["data"]
        return [item["embedding"] for item in sorted(data, key=lambda x: x["index"])]

    async def close(self) -> None:
        """Close the underlying httpx client."""
        await self._client.aclose()
