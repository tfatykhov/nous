"""Unit tests for SearchRouter — query classification and fallback chain."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest


def _make_provider(name: str, available: bool = True, results: list | None = None, fail: bool = False):
    """Create a mock SearchProvider."""
    provider = MagicMock()
    provider.name = name
    provider.is_available = available
    if fail:
        provider.search = AsyncMock(side_effect=RuntimeError(f"{name} failed"))
    else:
        provider.search = AsyncMock(return_value=results or [])
    return provider


def _mock_http() -> AsyncMock:
    return AsyncMock(spec=httpx.AsyncClient)


# ---------------------------------------------------------------------------
# Query classification
# ---------------------------------------------------------------------------


class TestClassifyQuery:
    def _classify(self, query: str) -> str:
        from nous.api.search_router import SearchRouter

        return SearchRouter._classify_query(query)

    def test_factual_query(self):
        assert self._classify("weather in Tokyo today") == "factual"

    def test_research_query_with_keyword(self):
        assert self._classify("deep analysis of transformer architecture") == "research"

    def test_research_query_compare(self):
        assert self._classify("compare Redis vs Memcached for caching") == "research"

    def test_research_query_academic(self):
        assert self._classify("academic papers on reinforcement learning") == "research"

    def test_simple_factual_default(self):
        assert self._classify("python list comprehension syntax") == "factual"

    def test_research_how_does_work(self):
        assert self._classify("how does neural network backpropagation work in detail") == "research"


# ---------------------------------------------------------------------------
# Provider ordering
# ---------------------------------------------------------------------------


class TestProviderOrdering:
    def test_auto_factual_tavily_first(self):
        from nous.api.search_router import SearchRouter

        tavily = _make_provider("tavily")
        exa = _make_provider("exa")
        brave = _make_provider("brave")
        router = SearchRouter(tavily=tavily, exa=exa, brave=brave, mode="auto")

        order = router._provider_order("factual")
        names = [p.name for p in order]
        assert names == ["tavily", "brave"]

    def test_auto_research_exa_first(self):
        from nous.api.search_router import SearchRouter

        tavily = _make_provider("tavily")
        exa = _make_provider("exa")
        brave = _make_provider("brave")
        router = SearchRouter(tavily=tavily, exa=exa, brave=brave, mode="auto")

        order = router._provider_order("research")
        names = [p.name for p in order]
        assert names == ["exa", "tavily", "brave"]

    def test_forced_provider(self):
        from nous.api.search_router import SearchRouter

        tavily = _make_provider("tavily")
        exa = _make_provider("exa")
        brave = _make_provider("brave")
        router = SearchRouter(tavily=tavily, exa=exa, brave=brave, mode="brave")

        order = router._provider_order("factual")
        names = [p.name for p in order]
        assert names[0] == "brave"

    def test_unavailable_provider_skipped(self):
        from nous.api.search_router import SearchRouter

        tavily = _make_provider("tavily", available=False)
        exa = _make_provider("exa")
        brave = _make_provider("brave")
        router = SearchRouter(tavily=tavily, exa=exa, brave=brave, mode="auto")

        order = router._provider_order("factual")
        names = [p.name for p in order]
        assert "tavily" not in names


# ---------------------------------------------------------------------------
# Search with fallback
# ---------------------------------------------------------------------------


class TestSearchWithFallback:
    @pytest.mark.asyncio
    async def test_primary_succeeds(self):
        from nous.api.search_providers import SearchResult
        from nous.api.search_router import SearchRouter

        results = [SearchResult(title="R1", url="https://x.com", snippet="S1", provider="tavily")]
        tavily = _make_provider("tavily", results=results)
        brave = _make_provider("brave")
        router = SearchRouter(tavily=tavily, exa=_make_provider("exa", available=False), brave=brave, mode="auto")

        got, provider_name = await router.search("test query", count=5, http=_mock_http())

        assert len(got) == 1
        assert provider_name == "tavily"
        brave.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_fallback_on_primary_failure(self):
        from nous.api.search_providers import SearchResult
        from nous.api.search_router import SearchRouter

        results = [SearchResult(title="Brave R1", url="https://b.com", snippet="S", provider="brave")]
        tavily = _make_provider("tavily", fail=True)
        brave = _make_provider("brave", results=results)
        router = SearchRouter(tavily=tavily, exa=_make_provider("exa", available=False), brave=brave, mode="auto")

        got, provider_name = await router.search("test query", count=5, http=_mock_http())

        assert len(got) == 1
        assert provider_name == "brave"

    @pytest.mark.asyncio
    async def test_all_providers_fail(self):
        from nous.api.search_router import SearchRouter

        tavily = _make_provider("tavily", fail=True)
        exa = _make_provider("exa", available=False)
        brave = _make_provider("brave", fail=True)
        router = SearchRouter(tavily=tavily, exa=exa, brave=brave, mode="auto")

        with pytest.raises(RuntimeError, match="All search providers failed"):
            await router.search("test", count=5, http=_mock_http())

    @pytest.mark.asyncio
    async def test_fallback_on_empty_results(self):
        """If primary returns empty results, fall through to next provider."""
        from nous.api.search_providers import SearchResult
        from nous.api.search_router import SearchRouter

        tavily = _make_provider("tavily", results=[])  # empty
        results = [SearchResult(title="B", url="https://b.com", snippet="S", provider="brave")]
        brave = _make_provider("brave", results=results)
        router = SearchRouter(tavily=tavily, exa=_make_provider("exa", available=False), brave=brave, mode="auto")

        got, provider_name = await router.search("test", count=5, http=_mock_http())

        assert provider_name == "brave"

    @pytest.mark.asyncio
    async def test_no_providers_available(self):
        from nous.api.search_router import SearchRouter

        tavily = _make_provider("tavily", available=False)
        exa = _make_provider("exa", available=False)
        brave = _make_provider("brave", available=False)
        router = SearchRouter(tavily=tavily, exa=exa, brave=brave, mode="auto")

        with pytest.raises(RuntimeError, match="No search providers available"):
            await router.search("test", count=5, http=_mock_http())
