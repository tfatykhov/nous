"""Search provider implementations for multi-tier search routing (F033).

Defines the SearchProvider protocol and concrete providers:
- TavilyProvider: primary, structured output for factual/current queries
- ExaProvider: secondary, neural search for deep research queries
- BraveProvider: tertiary fallback

All providers use httpx REST calls — no external SDKs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    """Normalized search result from any provider."""
    title: str
    url: str
    snippet: str
    score: float | None = None
    provider: str = ""


class SearchProvider(Protocol):
    """Protocol for search providers."""

    @property
    def name(self) -> str: ...

    @property
    def is_available(self) -> bool: ...

    async def search(
        self,
        query: str,
        count: int = 5,
        *,
        http: httpx.AsyncClient,
        freshness: str | None = None,
    ) -> list[SearchResult]: ...


# ---------------------------------------------------------------------------
# Brave
# ---------------------------------------------------------------------------


class BraveProvider:
    """Brave Search API provider (existing, now behind protocol)."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    @property
    def name(self) -> str:
        return "brave"

    @property
    def is_available(self) -> bool:
        return bool(self._api_key)

    async def search(
        self,
        query: str,
        count: int = 5,
        *,
        http: httpx.AsyncClient,
        freshness: str | None = None,
    ) -> list[SearchResult]:
        if not self._api_key:
            raise RuntimeError("BRAVE_SEARCH_API_KEY not configured")

        params: dict[str, Any] = {"q": query, "count": min(count, 10)}
        if freshness:
            freshness_map = {"day": "pd", "week": "pw", "month": "pm"}
            mapped = freshness_map.get(freshness)
            if mapped:
                params["freshness"] = mapped

        response = await http.get(
            "https://api.search.brave.com/res/v1/web/search",
            params=params,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": self._api_key,
            },
            timeout=10,
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"Brave search failed (HTTP {response.status_code})"
            )

        data = response.json()
        results = []
        for item in data.get("web", {}).get("results", [])[:count]:
            results.append(SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=item.get("description", ""),
                provider="brave",
            ))
        return results


# ---------------------------------------------------------------------------
# Tavily
# ---------------------------------------------------------------------------


class TavilyProvider:
    """Tavily Search API provider -- primary for factual/current queries.

    REST API: POST https://api.tavily.com/search
    Returns structured JSON with title, url, content, score.
    """

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    @property
    def name(self) -> str:
        return "tavily"

    @property
    def is_available(self) -> bool:
        return bool(self._api_key)

    async def search(
        self,
        query: str,
        count: int = 5,
        *,
        http: httpx.AsyncClient,
        freshness: str | None = None,
    ) -> list[SearchResult]:
        if not self._api_key:
            raise RuntimeError("TAVILY_API_KEY not configured")

        payload: dict[str, Any] = {
            "api_key": self._api_key,
            "query": query,
            "max_results": min(count, 10),
            "search_depth": "basic",
            "include_answer": False,
        }
        # Tavily supports days parameter for recency filtering
        if freshness:
            days_map = {"day": 1, "week": 7, "month": 30}
            days = days_map.get(freshness)
            if days:
                payload["days"] = days

        response = await http.post(
            "https://api.tavily.com/search",
            json=payload,
            timeout=10,
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"Tavily search failed (HTTP {response.status_code})"
            )

        data = response.json()
        results = []
        for item in data.get("results", [])[:count]:
            results.append(SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=item.get("content", ""),
                score=item.get("score"),
                provider="tavily",
            ))
        return results


# ---------------------------------------------------------------------------
# Exa
# ---------------------------------------------------------------------------


class ExaProvider:
    """Exa Search API provider -- secondary for deep research/semantic queries.

    REST API: POST https://api.exa.ai/search
    Uses neural/embedding-based search for conceptual matching.
    """

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    @property
    def name(self) -> str:
        return "exa"

    @property
    def is_available(self) -> bool:
        return bool(self._api_key)

    async def search(
        self,
        query: str,
        count: int = 5,
        *,
        http: httpx.AsyncClient,
        freshness: str | None = None,
    ) -> list[SearchResult]:
        if not self._api_key:
            raise RuntimeError("EXA_API_KEY not configured")

        payload: dict[str, Any] = {
            "query": query,
            "num_results": min(count, 10),
            "type": "neural",
            "contents": {
                "text": {"max_characters": 500},
            },
        }
        # Exa supports start_published_date for recency filtering
        if freshness:
            days_map = {"day": 1, "week": 7, "month": 30}
            days = days_map.get(freshness)
            if days:
                cutoff = datetime.now(timezone.utc) - timedelta(days=days)
                payload["start_published_date"] = cutoff.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        response = await http.post(
            "https://api.exa.ai/search",
            json=payload,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            timeout=15,
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"Exa search failed (HTTP {response.status_code})"
            )

        data = response.json()
        results = []
        for item in data.get("results", [])[:count]:
            results.append(SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=item.get("text", ""),
                score=item.get("score"),
                provider="exa",
            ))
        return results
