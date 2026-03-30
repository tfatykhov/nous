"""Web tools for the Nous agent: web_search and web_fetch.

Gives the agent web access capabilities, gated by cognitive frames.
Uses a separate httpx client (NOT AgentRunner's — that has API credentials).
"""

from __future__ import annotations

import html as html_module
import ipaddress
import logging
import re
import socket
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from nous.api.tools import ToolDispatcher
from nous.config import Settings

logger = logging.getLogger(__name__)

# Rate limit state (in-memory, resets on restart)
_rate_limit: dict[str, Any] = {"date": "", "count": 0}

# Blocked IP ranges for SSRF protection
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),       # Loopback
    ipaddress.ip_network("10.0.0.0/8"),         # RFC1918
    ipaddress.ip_network("172.16.0.0/12"),      # RFC1918
    ipaddress.ip_network("192.168.0.0/16"),     # RFC1918
    ipaddress.ip_network("169.254.0.0/16"),     # Link-local
    ipaddress.ip_network("::1/128"),            # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),           # IPv6 unique local
    ipaddress.ip_network("fe80::/10"),          # IPv6 link-local
]

# Blocked hostnames (Docker internal services, etc.)
_BLOCKED_HOSTNAMES = {"localhost", "postgres", "nous", "redis", "0.0.0.0"}


def _mcp_response(text: str) -> dict[str, Any]:
    """Build MCP-format response."""
    return {"content": [{"type": "text", "text": text}]}


def _is_url_safe(url: str) -> tuple[bool, str]:
    """Check if URL is safe from SSRF attacks.

    Resolves hostname to IP and checks against blocked ranges.
    Returns (is_safe, error_message).
    """
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname

        if not hostname:
            return False, "Could not parse hostname from URL"

        # Check blocked hostnames
        if hostname.lower() in _BLOCKED_HOSTNAMES:
            return False, f"Blocked hostname: {hostname}"

        # Resolve to IP and check ranges
        try:
            addr_infos = socket.getaddrinfo(hostname, None)
        except socket.gaierror:
            return False, f"Could not resolve hostname: {hostname}"

        for addr_info in addr_infos:
            ip = ipaddress.ip_address(addr_info[4][0])
            for network in _BLOCKED_NETWORKS:
                if ip in network:
                    return False, f"URL resolves to blocked IP range ({network})"

        return True, ""
    except Exception as e:
        return False, f"URL validation error: {e}"


def _check_rate_limit(settings: Settings) -> str | None:
    """Check and increment daily rate limit.

    Returns error message if limit exceeded, None if OK.
    """
    today = time.strftime("%Y-%m-%d")

    if _rate_limit["date"] != today:
        _rate_limit["date"] = today
        _rate_limit["count"] = 0

    limit = settings.web_search_daily_limit
    current = _rate_limit["count"]

    if current >= limit:
        return f"Daily web search limit reached ({limit}). Resets tomorrow."

    _rate_limit["count"] = current + 1

    if current >= int(limit * 0.8):
        logger.warning("Web search rate limit at %d/%d (%.0f%%)", current + 1, limit, (current + 1) / limit * 100)

    return None


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


async def _web_search(
    query: str,
    count: int = 5,
    freshness: str | None = None,
    *,
    _settings: Settings,
    _http: httpx.AsyncClient,
    _router: Any | None = None,
) -> dict[str, Any]:
    """Search via multi-tier search router (F033).

    If a SearchRouter is provided, delegates to it with automatic
    provider selection and fallback. Otherwise, falls back to direct
    Brave Search API call (backward compatibility).
    """
    try:
        # Check rate limit (applies regardless of provider)
        rate_error = _check_rate_limit(_settings)
        if rate_error:
            return _mcp_response(f"Rate limit: {rate_error}")

        count = min(count, 10)

        # Route through multi-tier search if router available
        if _router is not None:
            from nous.api.search_router import SearchRouter
            router: SearchRouter = _router
            try:
                results, provider_name = await router.search(
                    query, count=count, http=_http, freshness=freshness,
                )
            except RuntimeError as e:
                return _mcp_response(f"Search error: {e}")

            lines = [f"Search results for: {query} [via {provider_name}]\n"]
            for i, r in enumerate(results, 1):
                lines.append(f"{i}. {r.title}")
                lines.append(f"   URL: {r.url}")
                lines.append(f"   {r.snippet}\n")

            return _mcp_response("\n".join(lines))

        # Fallback: direct Brave (no router configured)
        if not _settings.brave_search_api_key:
            return _mcp_response(
                "Error: No search provider configured. Set TAVILY_API_KEY, "
                "EXA_API_KEY, or BRAVE_SEARCH_API_KEY."
            )

        params: dict[str, Any] = {"q": query, "count": count}
        if freshness:
            freshness_map = {"day": "pd", "week": "pw", "month": "pm"}
            mapped = freshness_map.get(freshness)
            if mapped:
                params["freshness"] = mapped

        response = await _http.get(
            "https://api.search.brave.com/res/v1/web/search",
            params=params,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": _settings.brave_search_api_key,
            },
            timeout=10,
        )

        if response.status_code != 200:
            return _mcp_response(
                f"Search failed (HTTP {response.status_code}). "
                "Check BRAVE_SEARCH_API_KEY if 401."
            )

        data = response.json()
        results_list = []
        for item in data.get("web", {}).get("results", [])[:count]:
            results_list.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("description", ""),
            })

        if not results_list:
            return _mcp_response(f"No results found for: {query}")

        lines = [f"Search results for: {query}\n"]
        for i, r in enumerate(results_list, 1):
            lines.append(f"{i}. {r['title']}")
            lines.append(f"   URL: {r['url']}")
            lines.append(f"   {r['snippet']}\n")

        return _mcp_response("\n".join(lines))

    except httpx.TimeoutException:
        return _mcp_response("Web search timed out. Try again.")
    except httpx.ConnectError as e:
        return _mcp_response(f"Could not connect to search service: {e}")
    except Exception as e:
        logger.exception("web_search error")
        return _mcp_response(f"Search error: {e}")


async def _web_fetch(
    url: str,
    max_chars: int | None = None,
    *,
    _settings: Settings,
    _http: httpx.AsyncClient,
) -> dict[str, Any]:
    """Fetch URL and extract readable content."""
    try:
        # Validate URL scheme
        if not url.startswith(("http://", "https://")):
            return _mcp_response("URL must start with http:// or https://")

        # SSRF protection
        is_safe, error = _is_url_safe(url)
        if not is_safe:
            return _mcp_response(f"Blocked: {error}")

        effective_max = min(max_chars or _settings.web_fetch_max_chars, 50000)

        # Manual redirect following with SSRF check on each hop (P1-1 fix)
        max_redirects = 5
        current_url = url
        response = None
        for _ in range(max_redirects + 1):
            response = await _http.get(
                current_url,
                headers={"User-Agent": "Nous/0.1 (cognitive agent)"},
                follow_redirects=False,
                timeout=15,
            )
            if response.status_code not in (301, 302, 303, 307, 308):
                break
            redirect_url = response.headers.get("location", "")
            if not redirect_url:
                break
            # Resolve relative redirects
            if redirect_url.startswith("/"):
                parsed_current = urlparse(current_url)
                redirect_url = f"{parsed_current.scheme}://{parsed_current.netloc}{redirect_url}"
            # SSRF check on redirect target
            redirect_safe, redirect_error = _is_url_safe(redirect_url)
            if not redirect_safe:
                return _mcp_response(f"Blocked redirect to unsafe URL: {redirect_error}")
            current_url = redirect_url
        else:
            return _mcp_response(f"Too many redirects (max {max_redirects})")

        if response is None:
            return _mcp_response("No response received")

        content_type = response.headers.get("content-type", "")

        # Reject binary content
        is_text = any(t in content_type for t in ["text/", "application/json", "application/xml", "application/xhtml"])
        if content_type and not is_text:
            return _mcp_response(f"Cannot extract text from binary content (content-type: {content_type})")

        if "html" in content_type or "xhtml" in content_type:
            text = _extract_readable(response.text)
        else:
            text = response.text

        if len(text) > effective_max:
            text = text[:effective_max] + "\n\n[... truncated]"

        return _mcp_response(f"Content from {url} ({len(text)} chars):\n\n{text}")

    except httpx.TimeoutException:
        return _mcp_response(f"Fetch timed out for: {url}")
    except httpx.ConnectError as e:
        return _mcp_response(f"Could not connect to {url}: {e}")
    except Exception as e:
        logger.exception("web_fetch error")
        return _mcp_response(f"Fetch error: {e}")


# ---------------------------------------------------------------------------
# HTML extraction
# ---------------------------------------------------------------------------


def _extract_readable(html: str) -> str:
    """Extract readable text from HTML using stdlib."""
    # Remove script, style, noscript, nav, header, footer tags
    text = re.sub(
        r'<(script|style|noscript|nav|header|footer)[^>]*>.*?</\1>',
        '', html, flags=re.DOTALL | re.IGNORECASE
    )
    # Remove HTML comments
    text = re.sub(r'<!--.*?-->', '', text, flags=re.DOTALL)
    # Remove HTML tags
    text = re.sub(r'<[^>]+>', ' ', text)
    # Decode entities
    text = html_module.unescape(text)
    # Clean whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------


_WEB_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Search the web for current information. Returns titles, URLs, and snippets.",
    "properties": {
        "query": {"type": "string", "description": "Search query string"},
        "count": {
            "type": "integer",
            "description": "Number of results (1-10, default 5)",
            "minimum": 1,
            "maximum": 10,
            "default": 5,
        },
        "freshness": {
            "type": "string",
            "description": "Filter by recency: 'day', 'week', 'month', or omit for all time",
            "enum": ["day", "week", "month"],
        },
    },
    "required": ["query"],
}

_WEB_FETCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Fetch and extract readable content from a URL. Returns clean text.",
    "properties": {
        "url": {"type": "string", "description": "URL to fetch (must be http or https)"},
        "max_chars": {
            "type": "integer",
            "description": "Maximum characters to return (default from config, max 50000)",
            "maximum": 50000,
        },
    },
    "required": ["url"],
}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_web_tools(
    dispatcher: ToolDispatcher,
    settings: Settings,
    http_client: httpx.AsyncClient,
    router: Any | None = None,
) -> None:
    """Register web tools (web_search, web_fetch) with the dispatcher.

    Creates closure wrappers that inject settings, httpx client, and
    optional SearchRouter for multi-tier search (F033).
    """
    async def _search(query: str, count: int = 5, freshness: str | None = None) -> dict[str, Any]:
        return await _web_search(
            query, count, freshness,
            _settings=settings, _http=http_client, _router=router,
        )

    async def _fetch(url: str, max_chars: int | None = None) -> dict[str, Any]:
        return await _web_fetch(url, max_chars, _settings=settings, _http=http_client)

    dispatcher.register("web_search", _search, _WEB_SEARCH_SCHEMA)
    dispatcher.register("web_fetch", _fetch, _WEB_FETCH_SCHEMA)
