"""Multi-tier search router for web_search tool (F033).

Classifies queries and routes to the best provider with automatic fallback:
- Factual/current events -> Tavily (primary) -> Brave (fallback)
- Deep research/semantic -> Exa (primary) -> Tavily -> Brave (fallback)
- Forced mode -> specified provider -> others as fallback
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

from nous.api.search_providers import SearchProvider, SearchResult

logger = logging.getLogger(__name__)

# Keywords that signal a research/deep query (case-insensitive)
_RESEARCH_KEYWORDS = re.compile(
    r"\b(research|academic|paper|thesis|study|analyze|analysis|"
    r"compare|comparison|versus|vs\.?|deep dive|in.depth|"
    r"comprehensive|systematic|literature|survey|theoretical|"
    r"mechanism|how does .+ work)\b",
    re.IGNORECASE,
)


class SearchRouter:
    """Routes search queries to the best available provider."""

    def __init__(
        self,
        *,
        tavily: SearchProvider,
        exa: SearchProvider,
        brave: SearchProvider,
        mode: str = "auto",
    ) -> None:
        self._providers = {"tavily": tavily, "exa": exa, "brave": brave}
        self._mode = mode

    @staticmethod
    def _classify_query(query: str) -> str:
        """Classify query as 'factual' or 'research'."""
        if _RESEARCH_KEYWORDS.search(query):
            return "research"
        return "factual"

    def _provider_order(self, query_type: str) -> list[SearchProvider]:
        """Return providers in priority order, filtering unavailable ones."""
        if self._mode != "auto" and self._mode in self._providers:
            forced = self._providers[self._mode]
            others = [p for k, p in self._providers.items() if k != self._mode]
            candidates = [forced] + others
        elif query_type == "research":
            candidates = [
                self._providers["exa"],
                self._providers["tavily"],
                self._providers["brave"],
            ]
        else:
            # factual: Tavily primary, Brave fallback (skip Exa — not suited)
            candidates = [
                self._providers["tavily"],
                self._providers["brave"],
            ]
        return [p for p in candidates if p.is_available]

    async def search(
        self,
        query: str,
        count: int = 5,
        *,
        http: httpx.AsyncClient,
        freshness: str | None = None,
    ) -> tuple[list[SearchResult], str]:
        """Search using the best provider with fallback.

        Returns (results, provider_name) tuple.
        Raises RuntimeError if all providers fail.
        """
        query_type = self._classify_query(query)
        providers = self._provider_order(query_type)

        if not providers:
            raise RuntimeError(
                "No search providers available. Configure at least one of: "
                "TAVILY_API_KEY, EXA_API_KEY, BRAVE_SEARCH_API_KEY"
            )

        errors: list[str] = []
        for provider in providers:
            try:
                results = await provider.search(
                    query, count=count, http=http, freshness=freshness,
                )
                if results:
                    logger.info(
                        "Search '%s' via %s (%s) — %d results",
                        query[:50], provider.name, query_type, len(results),
                    )
                    return results, provider.name
                logger.debug(
                    "Search '%s' via %s returned empty, trying next",
                    query[:50], provider.name,
                )
            except Exception as e:
                errors.append(f"{provider.name}: {e}")
                logger.warning(
                    "Search provider %s failed for '%s': %s",
                    provider.name, query[:50], e,
                )

        raise RuntimeError(
            f"All search providers failed for '{query[:80]}'. "
            f"Errors: {'; '.join(errors)}"
        )
