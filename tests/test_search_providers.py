"""Unit tests for search providers -- Tavily, Exa, Brave.

All HTTP calls are mocked via unittest.mock.AsyncMock.
"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest


def _mock_http(response: MagicMock) -> AsyncMock:
    """Create a mock httpx.AsyncClient whose .get()/.post() returns the given response."""
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.return_value = response
    client.post.return_value = response
    return client


def _mock_response(status_code: int = 200, json_data: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    return resp


# ---------------------------------------------------------------------------
# SearchResult
# ---------------------------------------------------------------------------


class TestSearchResult:
    def test_dataclass_fields(self):
        from nous.api.search_providers import SearchResult

        r = SearchResult(title="T", url="https://x.com", snippet="S")
        assert r.title == "T"
        assert r.url == "https://x.com"
        assert r.snippet == "S"
        assert r.score is None
        assert r.provider == ""

    def test_with_optional_fields(self):
        from nous.api.search_providers import SearchResult

        r = SearchResult(title="T", url="https://x.com", snippet="S", score=0.95, provider="tavily")
        assert r.score == 0.95
        assert r.provider == "tavily"


# ---------------------------------------------------------------------------
# BraveProvider
# ---------------------------------------------------------------------------


class TestBraveProvider:
    def _make_provider(self, api_key: str = "brave-key"):
        from nous.api.search_providers import BraveProvider

        return BraveProvider(api_key=api_key)

    @pytest.mark.asyncio
    async def test_successful_search(self):
        provider = self._make_provider()
        response = _mock_response(
            200,
            {
                "web": {
                    "results": [
                        {"title": "Result 1", "url": "https://example.com", "description": "Desc 1"},
                        {"title": "Result 2", "url": "https://example2.com", "description": "Desc 2"},
                    ]
                }
            },
        )
        http = _mock_http(response)

        results = await provider.search("test query", count=5, http=http)

        assert len(results) == 2
        assert results[0].title == "Result 1"
        assert results[0].provider == "brave"
        http.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_api_key_raises(self):
        provider = self._make_provider(api_key="")
        http = _mock_http(_mock_response())

        with pytest.raises(RuntimeError, match="not configured"):
            await provider.search("test", count=5, http=http)

    @pytest.mark.asyncio
    async def test_http_error_raises(self):
        provider = self._make_provider()
        http = _mock_http(_mock_response(status_code=401))

        with pytest.raises(RuntimeError, match="401"):
            await provider.search("test", count=5, http=http)

    @pytest.mark.asyncio
    async def test_empty_results(self):
        provider = self._make_provider()
        http = _mock_http(_mock_response(200, {"web": {"results": []}}))

        results = await provider.search("obscure", count=5, http=http)
        assert results == []

    def test_name_property(self):
        provider = self._make_provider()
        assert provider.name == "brave"

    def test_is_available_with_key(self):
        assert self._make_provider("key").is_available is True

    def test_is_available_without_key(self):
        assert self._make_provider("").is_available is False

    @pytest.mark.asyncio
    async def test_freshness_maps_to_brave_param(self):
        """freshness='week' maps to Brave freshness param 'pw'."""
        provider = self._make_provider()
        http = _mock_http(_mock_response(200, {"web": {"results": []}}))

        await provider.search("test", count=5, http=http, freshness="week")

        call_kwargs = http.get.call_args
        params = call_kwargs.kwargs.get("params") or call_kwargs[1].get("params", {})
        assert params.get("freshness") == "pw"


# ---------------------------------------------------------------------------
# TavilyProvider
# ---------------------------------------------------------------------------


class TestTavilyProvider:
    def _make_provider(self, api_key: str = "tvly-test"):
        from nous.api.search_providers import TavilyProvider

        return TavilyProvider(api_key=api_key)

    @pytest.mark.asyncio
    async def test_successful_search(self):
        provider = self._make_provider()
        response = _mock_response(
            200,
            {
                "results": [
                    {
                        "title": "Tavily Result",
                        "url": "https://tavily.com",
                        "content": "Full content snippet from Tavily",
                        "score": 0.95,
                    },
                ]
            },
        )
        http = _mock_http(response)

        results = await provider.search("test query", count=5, http=http)

        assert len(results) == 1
        assert results[0].title == "Tavily Result"
        assert results[0].snippet == "Full content snippet from Tavily"
        assert results[0].score == 0.95
        assert results[0].provider == "tavily"
        http.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_api_key_raises(self):
        provider = self._make_provider(api_key="")
        http = _mock_http(_mock_response())

        with pytest.raises(RuntimeError, match="not configured"):
            await provider.search("test", count=5, http=http)

    @pytest.mark.asyncio
    async def test_http_error_raises(self):
        provider = self._make_provider()
        http = _mock_http(_mock_response(status_code=500))

        with pytest.raises(RuntimeError, match="500"):
            await provider.search("test", count=5, http=http)

    @pytest.mark.asyncio
    async def test_empty_results(self):
        provider = self._make_provider()
        http = _mock_http(_mock_response(200, {"results": []}))

        results = await provider.search("obscure", count=5, http=http)
        assert results == []

    def test_name_property(self):
        assert self._make_provider().name == "tavily"

    def test_is_available(self):
        assert self._make_provider("key").is_available is True
        assert self._make_provider("").is_available is False

    @pytest.mark.asyncio
    async def test_sends_correct_payload(self):
        """Verify Tavily POST body includes required fields."""
        provider = self._make_provider(api_key="tvly-abc")
        response = _mock_response(200, {"results": []})
        http = _mock_http(response)

        await provider.search("climate change", count=3, http=http)

        call_kwargs = http.post.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json", {})
        assert payload["query"] == "climate change"
        assert payload["max_results"] == 3
        assert payload["api_key"] == "tvly-abc"

    @pytest.mark.asyncio
    async def test_freshness_maps_to_days(self):
        """freshness='day' maps to Tavily days=1 param."""
        provider = self._make_provider()
        http = _mock_http(_mock_response(200, {"results": []}))

        await provider.search("test", count=5, http=http, freshness="day")

        call_kwargs = http.post.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json", {})
        assert payload["days"] == 1


# ---------------------------------------------------------------------------
# ExaProvider
# ---------------------------------------------------------------------------


class TestExaProvider:
    def _make_provider(self, api_key: str = "exa-test"):
        from nous.api.search_providers import ExaProvider

        return ExaProvider(api_key=api_key)

    @pytest.mark.asyncio
    async def test_successful_search(self):
        provider = self._make_provider()
        response = _mock_response(
            200,
            {
                "results": [
                    {
                        "title": "Deep Research Paper",
                        "url": "https://arxiv.org/paper",
                        "text": "Neural embedding search finds conceptually related content",
                        "score": 0.88,
                    },
                ]
            },
        )
        http = _mock_http(response)

        results = await provider.search("neural search embeddings", count=5, http=http)

        assert len(results) == 1
        assert results[0].title == "Deep Research Paper"
        assert results[0].snippet == "Neural embedding search finds conceptually related content"
        assert results[0].score == 0.88
        assert results[0].provider == "exa"
        http.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_api_key_raises(self):
        provider = self._make_provider(api_key="")
        http = _mock_http(_mock_response())

        with pytest.raises(RuntimeError, match="not configured"):
            await provider.search("test", count=5, http=http)

    @pytest.mark.asyncio
    async def test_http_error_raises(self):
        provider = self._make_provider()
        http = _mock_http(_mock_response(status_code=403))

        with pytest.raises(RuntimeError, match="403"):
            await provider.search("test", count=5, http=http)

    @pytest.mark.asyncio
    async def test_empty_results(self):
        provider = self._make_provider()
        http = _mock_http(_mock_response(200, {"results": []}))

        results = await provider.search("obscure", count=5, http=http)
        assert results == []

    def test_name_property(self):
        assert self._make_provider().name == "exa"

    def test_is_available(self):
        assert self._make_provider("key").is_available is True
        assert self._make_provider("").is_available is False

    @pytest.mark.asyncio
    async def test_sends_auth_header(self):
        """Verify Exa uses Authorization header, not body API key."""
        provider = self._make_provider(api_key="exa-secret")
        response = _mock_response(200, {"results": []})
        http = _mock_http(response)

        await provider.search("query", count=3, http=http)

        call_kwargs = http.post.call_args
        headers = call_kwargs.kwargs.get("headers") or {}
        assert headers.get("Authorization") == "Bearer exa-secret"

    @pytest.mark.asyncio
    async def test_freshness_maps_to_start_published_date(self):
        """freshness='month' adds start_published_date to Exa payload."""
        provider = self._make_provider()
        http = _mock_http(_mock_response(200, {"results": []}))

        await provider.search("test", count=5, http=http, freshness="month")

        call_kwargs = http.post.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json", {})
        assert "start_published_date" in payload
        # Should be a date string roughly 30 days ago
        assert "T" in payload["start_published_date"]
