"""Unit tests for nous/api/web_tools.py -- web_search, web_fetch, SSRF, rate limiting.

All tests are pure async (no database required). HTTP calls are mocked via
unittest.mock.AsyncMock. DNS resolution is mocked via socket.getaddrinfo patch.
Imports from nous.api.web_tools are done inline to tolerate the file being
written in parallel.
"""

import importlib
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from nous.api.tools import ToolDispatcher


def _extract_text(result: dict) -> str:
    """Extract text from MCP-format response."""
    return result["content"][0]["text"]


def _make_settings(**overrides) -> MagicMock:
    """Create a mock Settings with web-tool defaults.

    Uses MagicMock instead of real Settings to avoid pydantic-settings
    env file/alias complications in unit tests.
    """
    defaults = {
        "brave_search_api_key": "test-brave-key",
        "web_search_daily_limit": 100,
        "web_fetch_max_chars": 10000,
    }
    defaults.update(overrides)
    mock = MagicMock()
    for key, value in defaults.items():
        setattr(mock, key, value)
    return mock


def _mock_response(
    status_code: int = 200,
    json_data: dict | None = None,
    text: str = "",
    headers: dict | None = None,
) -> MagicMock:
    """Create a mock httpx.Response with common attributes."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.text = text
    resp.headers = headers or {}
    return resp


def _mock_http_client(response: MagicMock | None = None) -> AsyncMock:
    """Create a mock httpx.AsyncClient whose .get() returns the given response."""
    client = AsyncMock(spec=httpx.AsyncClient)
    if response is not None:
        client.get.return_value = response
    return client


def _brave_search_response(results: list[dict] | None = None) -> dict:
    """Build a Brave Search API response payload."""
    if results is None:
        results = [
            {
                "title": "Example Result",
                "url": "https://example.com",
                "description": "An example search result.",
            }
        ]
    return {"web": {"results": results}}


# ---------------------------------------------------------------------------
# Helpers to import web_tools module (inline, tolerant of parallel writing)
# ---------------------------------------------------------------------------


def _import_web_tools():
    """Import nous.api.web_tools, reloading to pick up latest code."""
    import nous.api.web_tools as wt

    importlib.reload(wt)
    return wt


# ---------------------------------------------------------------------------
# SSRF Protection tests
# ---------------------------------------------------------------------------


class TestIsUrlSafe:
    """Tests for _is_url_safe() SSRF protection."""

    def _is_url_safe(self, url: str) -> tuple[bool, str]:
        wt = _import_web_tools()
        return wt._is_url_safe(url)

    @patch("socket.getaddrinfo")
    def test_blocks_localhost(self, mock_dns):
        """localhost hostname is blocked before DNS resolution."""
        safe, error = self._is_url_safe("http://localhost/admin")
        assert safe is False
        assert "Blocked hostname" in error or "localhost" in error.lower()

    @patch("socket.getaddrinfo")
    def test_blocks_127_0_0_1(self, mock_dns):
        """127.0.0.1 resolves to loopback and is blocked."""
        mock_dns.return_value = [(2, 1, 6, "", ("127.0.0.1", 0))]
        safe, error = self._is_url_safe("http://127.0.0.1/secret")
        assert safe is False
        assert "blocked" in error.lower() or "127" in error

    @patch("socket.getaddrinfo")
    def test_blocks_metadata_ip(self, mock_dns):
        """169.254.169.254 (cloud metadata) is blocked."""
        mock_dns.return_value = [(2, 1, 6, "", ("169.254.169.254", 0))]
        safe, error = self._is_url_safe("http://169.254.169.254/latest/meta-data/")
        assert safe is False
        assert "blocked" in error.lower()

    @patch("socket.getaddrinfo")
    def test_blocks_ipv6_loopback(self, mock_dns):
        """[::1] IPv6 loopback is blocked."""
        mock_dns.return_value = [(10, 1, 6, "", ("::1", 0, 0, 0))]
        safe, error = self._is_url_safe("http://[::1]/")
        assert safe is False

    @patch("socket.getaddrinfo")
    def test_blocks_docker_hostname_postgres(self, mock_dns):
        """Docker internal hostname 'postgres' is blocked."""
        safe, error = self._is_url_safe("http://postgres:5432/")
        assert safe is False
        assert "Blocked hostname" in error or "postgres" in error.lower()

    @patch("socket.getaddrinfo")
    def test_allows_public_url(self, mock_dns):
        """Public URLs like example.com are allowed."""
        mock_dns.return_value = [(2, 1, 6, "", ("93.184.216.34", 0))]
        safe, error = self._is_url_safe("https://example.com/page")
        assert safe is True
        assert error == ""

    @patch("socket.getaddrinfo")
    def test_allows_github(self, mock_dns):
        """github.com is allowed."""
        mock_dns.return_value = [(2, 1, 6, "", ("140.82.121.4", 0))]
        safe, error = self._is_url_safe("https://github.com/anthropics")
        assert safe is True
        assert error == ""

    @patch("socket.getaddrinfo", side_effect=Exception("DNS failure"))
    def test_dns_resolution_failure(self, mock_dns):
        """DNS resolution failure returns safe=False with error message."""
        safe, error = self._is_url_safe("http://nonexistent.invalid/")
        assert safe is False
        assert "error" in error.lower() or "resolve" in error.lower() or "Could not" in error

    def test_no_hostname(self):
        """URL with no hostname returns safe=False."""
        safe, error = self._is_url_safe("http://")
        assert safe is False
        assert "hostname" in error.lower() or "parse" in error.lower()

    @patch("socket.getaddrinfo")
    def test_blocks_private_10_range(self, mock_dns):
        """10.x.x.x private range is blocked."""
        mock_dns.return_value = [(2, 1, 6, "", ("10.0.0.1", 0))]
        safe, error = self._is_url_safe("http://internal.corp/")
        assert safe is False

    @patch("socket.getaddrinfo")
    def test_blocks_private_192_168_range(self, mock_dns):
        """192.168.x.x private range is blocked."""
        mock_dns.return_value = [(2, 1, 6, "", ("192.168.1.1", 0))]
        safe, error = self._is_url_safe("http://router.local/")
        assert safe is False


# ---------------------------------------------------------------------------
# Rate Limiting tests
# ---------------------------------------------------------------------------


class TestCheckRateLimit:
    """Tests for _check_rate_limit() daily rate limiting."""

    @pytest.fixture(autouse=True)
    def reset_rate_limit(self):
        """Reset module-level _rate_limit state before each test."""
        wt = _import_web_tools()
        wt._rate_limit["date"] = ""
        wt._rate_limit["count"] = 0
        yield

    def test_increments_counter(self):
        """Each call increments the counter, returns None when under limit."""
        wt = _import_web_tools()
        settings = _make_settings(web_search_daily_limit=10)
        result = wt._check_rate_limit(settings)
        assert result is None
        assert wt._rate_limit["count"] == 1

    def test_returns_error_at_limit(self):
        """Returns error message when limit is reached."""
        wt = _import_web_tools()
        settings = _make_settings(web_search_daily_limit=5)
        # Exhaust the limit
        for _ in range(5):
            wt._check_rate_limit(settings)
        # Next call should fail
        result = wt._check_rate_limit(settings)
        assert result is not None
        assert "limit" in result.lower()
        assert "5" in result

    def test_resets_on_new_day(self):
        """Counter resets when date changes (new day)."""
        wt = _import_web_tools()
        settings = _make_settings(web_search_daily_limit=5)

        # Day 1: exhaust limit
        wt._rate_limit["date"] = "2026-02-24"
        wt._rate_limit["count"] = 5
        assert wt._check_rate_limit(settings) is not None  # Blocked

        # Day 2: simulate date change by setting a different date
        # _check_rate_limit reads time.strftime internally, so we manipulate _rate_limit directly
        # to simulate what happens when the date changes
        wt._rate_limit["date"] = "2026-02-24"
        wt._rate_limit["count"] = 5

        # Patch time.strftime at the module level to return a new day
        with patch.object(wt.time, "strftime", return_value="2026-02-25"):
            result = wt._check_rate_limit(settings)
        assert result is None  # Allowed again
        assert wt._rate_limit["count"] == 1

    def test_warns_at_80_percent(self, caplog):
        """Logs a warning when usage reaches 80% of limit."""
        wt = _import_web_tools()
        settings = _make_settings(web_search_daily_limit=10)

        with caplog.at_level(logging.WARNING, logger="nous.api.web_tools"):
            # Use 8 calls (80%) -- 80% threshold is current >= int(10 * 0.8) = 8
            for _ in range(8):
                wt._check_rate_limit(settings)

            caplog.clear()
            # 9th call: current=8 >= int(10*0.8)=8, should trigger warning
            wt._check_rate_limit(settings)
            assert any("rate limit" in r.message.lower() or "9/" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# web_search tests
# ---------------------------------------------------------------------------


class TestWebSearch:
    """Tests for the _web_search handler."""

    @pytest.fixture(autouse=True)
    def reset_rate_limit(self):
        """Reset rate limit state before each test."""
        wt = _import_web_tools()
        wt._rate_limit["date"] = ""
        wt._rate_limit["count"] = 0
        yield

    @pytest.mark.asyncio
    async def test_successful_search(self):
        """Successful search returns formatted results in MCP format."""
        wt = _import_web_tools()
        settings = _make_settings()
        response = _mock_response(
            status_code=200,
            json_data=_brave_search_response(
                [
                    {
                        "title": "Python Docs",
                        "url": "https://docs.python.org",
                        "description": "Official Python documentation",
                    },
                    {"title": "PyPI", "url": "https://pypi.org", "description": "Python Package Index"},
                ]
            ),
        )
        client = _mock_http_client(response)

        result = await wt._web_search("python", count=5, freshness=None, _settings=settings, _http=client)

        text = _extract_text(result)
        assert "Python Docs" in text
        assert "https://docs.python.org" in text
        assert "PyPI" in text
        assert "Search results for: python" in text

    @pytest.mark.asyncio
    async def test_empty_api_key_returns_error(self):
        """Missing BRAVE_SEARCH_API_KEY returns clear error message."""
        wt = _import_web_tools()
        settings = _make_settings(brave_search_api_key="")
        client = _mock_http_client()

        result = await wt._web_search("test", _settings=settings, _http=client)

        text = _extract_text(result)
        assert "BRAVE_SEARCH_API_KEY" in text
        assert "not configured" in text.lower() or "error" in text.lower()
        # Should NOT have called the HTTP client
        client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_rate_limit_exceeded(self):
        """Returns error when daily rate limit is exceeded."""
        wt = _import_web_tools()
        settings = _make_settings(web_search_daily_limit=1)
        client = _mock_http_client(_mock_response(status_code=200, json_data=_brave_search_response()))

        # First call succeeds
        await wt._web_search("first", _settings=settings, _http=client)

        # Second call should be rate limited
        result = await wt._web_search("second", _settings=settings, _http=client)
        text = _extract_text(result)
        assert "rate limit" in text.lower() or "limit" in text.lower()

    @pytest.mark.asyncio
    async def test_brave_api_401_error(self):
        """Brave API 401 returns helpful error mentioning API key."""
        wt = _import_web_tools()
        settings = _make_settings()
        response = _mock_response(status_code=401)
        client = _mock_http_client(response)

        result = await wt._web_search("test", _settings=settings, _http=client)

        text = _extract_text(result)
        assert "401" in text
        assert "BRAVE_SEARCH_API_KEY" in text or "failed" in text.lower()

    @pytest.mark.asyncio
    async def test_timeout_returns_friendly_message(self):
        """httpx.TimeoutException returns friendly message."""
        wt = _import_web_tools()
        settings = _make_settings()
        client = _mock_http_client()
        client.get.side_effect = httpx.TimeoutException("Connection timed out")

        result = await wt._web_search("test", _settings=settings, _http=client)

        text = _extract_text(result)
        assert "timed out" in text.lower()

    @pytest.mark.asyncio
    async def test_empty_results(self):
        """Empty search results returns 'No results found' message."""
        wt = _import_web_tools()
        settings = _make_settings()
        response = _mock_response(status_code=200, json_data={"web": {"results": []}})
        client = _mock_http_client(response)

        result = await wt._web_search("obscure query xyz", _settings=settings, _http=client)

        text = _extract_text(result)
        assert "No results found" in text

    @pytest.mark.asyncio
    async def test_mcp_response_format(self):
        """Response is in MCP format: {content: [{type: 'text', text: '...'}]}."""
        wt = _import_web_tools()
        settings = _make_settings()
        response = _mock_response(status_code=200, json_data=_brave_search_response())
        client = _mock_http_client(response)

        result = await wt._web_search("test", _settings=settings, _http=client)

        assert "content" in result
        assert isinstance(result["content"], list)
        assert len(result["content"]) >= 1
        assert result["content"][0]["type"] == "text"
        assert isinstance(result["content"][0]["text"], str)

    @pytest.mark.asyncio
    async def test_search_passes_api_key_header(self):
        """Brave API key is passed in X-Subscription-Token header."""
        wt = _import_web_tools()
        settings = _make_settings(brave_search_api_key="my-secret-key")
        response = _mock_response(status_code=200, json_data=_brave_search_response())
        client = _mock_http_client(response)

        await wt._web_search("test", _settings=settings, _http=client)

        # Verify the HTTP call was made with the API key header
        client.get.assert_called_once()
        call_kwargs = client.get.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers", {})
        assert headers.get("X-Subscription-Token") == "my-secret-key"

    @pytest.mark.asyncio
    async def test_connect_error(self):
        """httpx.ConnectError returns friendly message."""
        wt = _import_web_tools()
        settings = _make_settings()
        client = _mock_http_client()
        client.get.side_effect = httpx.ConnectError("Connection refused")

        result = await wt._web_search("test", _settings=settings, _http=client)

        text = _extract_text(result)
        assert "connect" in text.lower() or "error" in text.lower()


# ---------------------------------------------------------------------------
# web_fetch tests
# ---------------------------------------------------------------------------


class TestWebFetch:
    """Tests for the _web_fetch handler."""

    @pytest.mark.asyncio
    async def test_html_page_returns_extracted_text(self):
        """HTML page returns extracted readable text."""
        wt = _import_web_tools()
        settings = _make_settings()
        html = "<html><body><h1>Hello World</h1><p>This is content.</p></body></html>"
        response = _mock_response(
            status_code=200,
            text=html,
            headers={"content-type": "text/html; charset=utf-8"},
        )
        client = _mock_http_client(response)

        with patch.object(wt, "_is_url_safe", return_value=(True, "")):
            result = await wt._web_fetch("https://example.com", _settings=settings, _http=client)

        text = _extract_text(result)
        assert "Hello World" in text
        assert "This is content" in text
        assert "Content from" in text

    @pytest.mark.asyncio
    async def test_binary_content_rejected(self):
        """Binary content type (image/png) is rejected with error."""
        wt = _import_web_tools()
        settings = _make_settings()
        response = _mock_response(
            status_code=200,
            headers={"content-type": "image/png"},
        )
        client = _mock_http_client(response)

        with patch.object(wt, "_is_url_safe", return_value=(True, "")):
            result = await wt._web_fetch("https://example.com/image.png", _settings=settings, _http=client)

        text = _extract_text(result)
        assert "binary" in text.lower() or "Cannot extract" in text
        assert "image/png" in text

    @pytest.mark.asyncio
    async def test_ssrf_url_blocked(self):
        """SSRF URL (localhost) is blocked before any HTTP request."""
        wt = _import_web_tools()
        settings = _make_settings()
        client = _mock_http_client()

        with patch.object(wt, "_is_url_safe", return_value=(False, "Blocked hostname: localhost")):
            result = await wt._web_fetch("http://localhost/admin", _settings=settings, _http=client)

        text = _extract_text(result)
        assert "Blocked" in text
        # Should NOT have made any HTTP request
        client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_content_truncated_at_max_chars(self):
        """Content exceeding max_chars is truncated with marker."""
        wt = _import_web_tools()
        settings = _make_settings(web_fetch_max_chars=100)
        long_text = "A" * 500
        response = _mock_response(
            status_code=200,
            text=long_text,
            headers={"content-type": "text/plain"},
        )
        client = _mock_http_client(response)

        with patch.object(wt, "_is_url_safe", return_value=(True, "")):
            result = await wt._web_fetch("https://example.com/long", max_chars=100, _settings=settings, _http=client)

        text = _extract_text(result)
        assert "truncated" in text.lower()

    @pytest.mark.asyncio
    async def test_timeout_returns_friendly_message(self):
        """httpx.TimeoutException returns friendly message."""
        wt = _import_web_tools()
        settings = _make_settings()
        client = _mock_http_client()
        client.get.side_effect = httpx.TimeoutException("Read timed out")

        with patch.object(wt, "_is_url_safe", return_value=(True, "")):
            result = await wt._web_fetch("https://slow.example.com", _settings=settings, _http=client)

        text = _extract_text(result)
        assert "timed out" in text.lower()

    @pytest.mark.asyncio
    async def test_invalid_url_scheme_rejected(self):
        """Non-http/https URL scheme is rejected."""
        wt = _import_web_tools()
        settings = _make_settings()
        client = _mock_http_client()

        result = await wt._web_fetch("ftp://example.com/file", _settings=settings, _http=client)

        text = _extract_text(result)
        assert "http" in text.lower()
        # Should NOT have made any HTTP request
        client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_json_content_type_allowed(self):
        """application/json content type is treated as text."""
        wt = _import_web_tools()
        settings = _make_settings()
        json_text = '{"key": "value", "items": [1, 2, 3]}'
        response = _mock_response(
            status_code=200,
            text=json_text,
            headers={"content-type": "application/json"},
        )
        client = _mock_http_client(response)

        with patch.object(wt, "_is_url_safe", return_value=(True, "")):
            result = await wt._web_fetch("https://api.example.com/data", _settings=settings, _http=client)

        text = _extract_text(result)
        assert '"key"' in text or "key" in text
        assert "Content from" in text

    @pytest.mark.asyncio
    async def test_mcp_response_format(self):
        """Response is in MCP format."""
        wt = _import_web_tools()
        settings = _make_settings()
        response = _mock_response(
            status_code=200,
            text="Hello",
            headers={"content-type": "text/plain"},
        )
        client = _mock_http_client(response)

        with patch.object(wt, "_is_url_safe", return_value=(True, "")):
            result = await wt._web_fetch("https://example.com", _settings=settings, _http=client)

        assert "content" in result
        assert isinstance(result["content"], list)
        assert result["content"][0]["type"] == "text"

    @pytest.mark.asyncio
    async def test_connect_error(self):
        """httpx.ConnectError returns friendly message."""
        wt = _import_web_tools()
        settings = _make_settings()
        client = _mock_http_client()
        client.get.side_effect = httpx.ConnectError("Connection refused")

        with patch.object(wt, "_is_url_safe", return_value=(True, "")):
            result = await wt._web_fetch("https://down.example.com", _settings=settings, _http=client)

        text = _extract_text(result)
        assert "connect" in text.lower() or "Could not" in text

    @pytest.mark.asyncio
    async def test_max_chars_capped_at_50000(self):
        """max_chars parameter is capped at 50000 even if higher value passed."""
        wt = _import_web_tools()
        settings = _make_settings(web_fetch_max_chars=100000)
        # Content just over 50000 chars
        long_text = "B" * 60000
        response = _mock_response(
            status_code=200,
            text=long_text,
            headers={"content-type": "text/plain"},
        )
        client = _mock_http_client(response)

        with patch.object(wt, "_is_url_safe", return_value=(True, "")):
            result = await wt._web_fetch("https://example.com/huge", max_chars=100000, _settings=settings, _http=client)

        text = _extract_text(result)
        assert "truncated" in text.lower()


# ---------------------------------------------------------------------------
# HTML Extraction tests
# ---------------------------------------------------------------------------


class TestExtractReadable:
    """Tests for _extract_readable() HTML text extraction."""

    def _extract(self, html: str) -> str:
        wt = _import_web_tools()
        return wt._extract_readable(html)

    def test_strips_script_tags(self):
        """Script tags and their content are removed."""
        html = "<html><body><script>alert('xss')</script><p>Safe content</p></body></html>"
        result = self._extract(html)
        assert "alert" not in result
        assert "script" not in result.lower()
        assert "Safe content" in result

    def test_strips_style_tags(self):
        """Style tags and their content are removed."""
        html = "<html><head><style>.red { color: red; }</style></head><body><p>Visible</p></body></html>"
        result = self._extract(html)
        assert "color: red" not in result
        assert "Visible" in result

    def test_strips_html_comments(self):
        """HTML comments are removed."""
        html = "<html><body><!-- This is a comment --><p>Real content</p></body></html>"
        result = self._extract(html)
        assert "comment" not in result.lower()
        assert "Real content" in result

    def test_strips_nav_header_footer(self):
        """nav, header, and footer tags are removed."""
        html = (
            "<html><body>"
            "<header><a href='/'>Home</a></header>"
            "<nav><ul><li>Menu</li></ul></nav>"
            "<main><p>Article body</p></main>"
            "<footer>Copyright 2026</footer>"
            "</body></html>"
        )
        result = self._extract(html)
        assert "Article body" in result
        assert "Menu" not in result
        assert "Copyright" not in result

    def test_decodes_html_entities(self):
        """HTML entities are decoded: &amp; -> &, &lt; -> <."""
        html = "<p>Tom &amp; Jerry &lt;3 cheese</p>"
        result = self._extract(html)
        assert "Tom & Jerry" in result
        assert "<3" in result
        assert "&amp;" not in result
        assert "&lt;" not in result

    def test_normalizes_whitespace(self):
        """Multiple whitespace characters are collapsed to single space."""
        html = "<p>Hello    World</p>  <p>  Next   paragraph  </p>"
        result = self._extract(html)
        assert "  " not in result  # No double spaces
        assert "Hello World" in result
        assert "Next paragraph" in result

    def test_empty_html(self):
        """Empty HTML returns empty string."""
        result = self._extract("")
        assert result == ""

    def test_plain_text_passthrough(self):
        """HTML with no tags returns the text content."""
        result = self._extract("Just plain text here")
        assert "Just plain text here" in result


# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------


class TestRegisterWebTools:
    """Tests for register_web_tools() dispatcher integration."""

    def test_both_tools_registered(self):
        """register_web_tools registers both web_search and web_fetch."""
        wt = _import_web_tools()
        dispatcher = ToolDispatcher()
        settings = _make_settings()
        client = AsyncMock(spec=httpx.AsyncClient)

        wt.register_web_tools(dispatcher, settings, client)

        definitions = dispatcher.tool_definitions()
        names = {d["name"] for d in definitions}
        assert "web_search" in names
        assert "web_fetch" in names
        assert len(names) == 2

    @pytest.mark.asyncio
    async def test_closures_capture_settings_and_client(self):
        """Registered closures use the settings and httpx client from registration."""
        wt = _import_web_tools()
        dispatcher = ToolDispatcher()
        settings = _make_settings(brave_search_api_key="closure-test-key")
        response = _mock_response(status_code=200, json_data=_brave_search_response())
        client = _mock_http_client(response)

        wt.register_web_tools(dispatcher, settings, client)

        # Dispatch web_search -- should use the captured settings and client
        result_text, is_error = await dispatcher.dispatch("web_search", {"query": "test"})
        assert is_error is False
        # The client.get should have been called (proves closure captured it)
        client.get.assert_called_once()
        # Verify API key from settings was used
        call_kwargs = client.get.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers", {})
        assert headers.get("X-Subscription-Token") == "closure-test-key"

    def test_tool_schemas_have_required_fields(self):
        """Tool schemas include required fields and descriptions."""
        wt = _import_web_tools()
        dispatcher = ToolDispatcher()
        settings = _make_settings()
        client = AsyncMock(spec=httpx.AsyncClient)

        wt.register_web_tools(dispatcher, settings, client)

        definitions = dispatcher.tool_definitions()
        for defn in definitions:
            assert "name" in defn
            assert "description" in defn
            assert "input_schema" in defn
            schema = defn["input_schema"]
            assert schema["type"] == "object"
            assert "properties" in schema
            assert "required" in schema

    @pytest.mark.asyncio
    async def test_web_fetch_closure_dispatch(self):
        """web_fetch closure dispatches correctly through ToolDispatcher."""
        wt = _import_web_tools()
        dispatcher = ToolDispatcher()
        settings = _make_settings()
        response = _mock_response(
            status_code=200,
            text="Page content here",
            headers={"content-type": "text/plain"},
        )
        client = _mock_http_client(response)

        wt.register_web_tools(dispatcher, settings, client)

        with patch.object(wt, "_is_url_safe", return_value=(True, "")):
            result_text, is_error = await dispatcher.dispatch("web_fetch", {"url": "https://example.com"})

        assert is_error is False
        assert "Page content here" in result_text


# ---------------------------------------------------------------------------
# Router integration tests (F033)
# ---------------------------------------------------------------------------


class TestWebSearchWithRouter:
    """Tests for web_search backed by SearchRouter."""

    @pytest.fixture(autouse=True)
    def reset_rate_limit(self):
        wt = _import_web_tools()
        wt._rate_limit["date"] = ""
        wt._rate_limit["count"] = 0
        yield

    @pytest.mark.asyncio
    async def test_search_uses_router(self):
        """web_search delegates to SearchRouter and includes provider tag."""
        wt = _import_web_tools()
        from nous.api.search_providers import SearchResult

        mock_router = MagicMock()
        mock_router.search = AsyncMock(
            return_value=(
                [SearchResult(title="T1", url="https://t.com", snippet="S1", provider="tavily")],
                "tavily",
            )
        )

        settings = _make_settings()
        result = await wt._web_search(
            "test query",
            count=5,
            freshness=None,
            _settings=settings,
            _http=AsyncMock(),
            _router=mock_router,
        )

        text = _extract_text(result)
        assert "T1" in text
        assert "https://t.com" in text
        assert "tavily" in text.lower()
        mock_router.search.assert_called_once()

    @pytest.mark.asyncio
    async def test_router_failure_returns_error(self):
        """When all providers fail, returns error message."""
        wt = _import_web_tools()

        mock_router = MagicMock()
        mock_router.search = AsyncMock(side_effect=RuntimeError("All search providers failed"))

        settings = _make_settings()
        result = await wt._web_search(
            "test",
            count=5,
            freshness=None,
            _settings=settings,
            _http=AsyncMock(),
            _router=mock_router,
        )

        text = _extract_text(result)
        assert "failed" in text.lower() or "error" in text.lower()

    @pytest.mark.asyncio
    async def test_fallback_to_brave_when_no_router(self):
        """When _router is None, falls back to direct Brave (backward compat)."""
        wt = _import_web_tools()
        settings = _make_settings()
        response = _mock_response(
            status_code=200,
            json_data=_brave_search_response(
                [
                    {"title": "Brave Result", "url": "https://b.com", "description": "From Brave"},
                ]
            ),
        )
        client = _mock_http_client(response)

        result = await wt._web_search(
            "test",
            count=5,
            freshness=None,
            _settings=settings,
            _http=client,
            _router=None,
        )

        text = _extract_text(result)
        assert "Brave Result" in text
