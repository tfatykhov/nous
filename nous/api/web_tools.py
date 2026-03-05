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
) -> dict[str, Any]:
    """Search via Brave Search API."""
    try:
        # Check API key
        if not _settings.brave_search_api_key:
            # Try Exa as fallback
            exa_result = await _exa_search(query, count, _settings=_settings, _http=_http)
            if exa_result:
                return exa_result
            return _mcp_response("Error: No search provider configured. Set BRAVE_SEARCH_API_KEY or EXA_API_KEY.")

        # Check rate limit
        rate_error = _check_rate_limit(_settings)
        if rate_error:
            # Try Exa as fallback
            logger.info("Brave rate limit hit, trying Exa fallback")
            exa_result = await _exa_search(query, count, _settings=_settings, _http=_http)
            if exa_result:
                return exa_result
            return _mcp_response(f"Rate limit: {rate_error} (Exa fallback also unavailable)")

        count = min(count, 10)

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
            logger.warning("Brave search failed (HTTP %d), trying Exa fallback", response.status_code)
            exa_result = await _exa_search(query, count, _settings=_settings, _http=_http)
            if exa_result:
                return exa_result
            return _mcp_response(f"Search failed (HTTP {response.status_code}). Check BRAVE_SEARCH_API_KEY if 401.")

        data = response.json()
        results = []
        for item in data.get("web", {}).get("results", [])[:count]:
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("description", ""),
            })

        if not results:
            return _mcp_response(f"No results found for: {query}")

        # Format as readable text for LLM
        lines = [f"Search results for: {query}\n"]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r['title']}")
            lines.append(f"   URL: {r['url']}")
            lines.append(f"   {r['snippet']}\n")

        return _mcp_response("\n".join(lines))

    except httpx.TimeoutException:
        logger.warning("Brave search timed out, trying Exa fallback")
        exa_result = await _exa_search(query, count, _settings=_settings, _http=_http)
        if exa_result:
            return exa_result
        return _mcp_response("Web search timed out on all providers. Try again.")
    except httpx.ConnectError as e:
        logger.warning("Brave connect error, trying Exa fallback: %s", e)
        exa_result = await _exa_search(query, count, _settings=_settings, _http=_http)
        if exa_result:
            return exa_result
        return _mcp_response(f"Could not connect to any search service: {e}")
    except Exception as e:
        logger.exception("web_search error")
        exa_result = await _exa_search(query, count, _settings=_settings, _http=_http)
        if exa_result:
            return exa_result
        return _mcp_response(f"Search error: {e}")




async def _exa_search(
    query: str,
    count: int = 5,
    *,
    search_type: str = "auto",
    content_mode: str = "text",
    max_text_chars: int = 3000,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
    output_schema: dict[str, Any] | None = None,
    category: str | None = None,
    max_age_hours: int | None = None,
    _settings: Settings,
    _http: httpx.AsyncClient,
) -> dict[str, Any] | None:
    """Search via Exa API (fallback + research provider).

    Supports all Exa search types, content modes, and category indexes:
    - search_type: "auto" (balanced), "fast" (quick), "deep" (research, 4-12s),
                   "deep-reasoning" (complex multi-step)
    - content_mode: "text" (full content) or "highlights" (key excerpts)
    - output_schema: structured output schema for deep/deep-reasoning types
    - include_domains / exclude_domains: domain filtering
      (can be combined: include broad domain, exclude specific subdomain)
    - category: search dedicated indexes — "people", "company", "news",
                "research paper", "tweet". Omit for general web search.
    - max_age_hours: content freshness control for livecrawl.
        24 = livecrawl if cache > 24h old
        1 = near real-time
        0 = always livecrawl (ignore cache)
        -1 = never livecrawl (cache only, fastest)
        None = default (livecrawl as fallback if no cache)

    Returns formatted results dict, or None if Exa is not configured or fails.
    """
    if not _settings.exa_api_key:
        return None

    try:
        # Build payload
        payload: dict[str, Any] = {
            "query": query,
            "type": search_type,
            "numResults": min(count, 10),
        }

        # Content configuration — text or highlights, not both
        if content_mode == "highlights":
            payload["contents"] = {
                "highlights": {"maxCharacters": min(max_text_chars, 4000)}
            }
        else:
            payload["contents"] = {
                "text": {"maxCharacters": min(max_text_chars, 20000)}
            }

        # Domain filtering (can combine: include broad domain, exclude subdomain)
        if include_domains:
            payload["includeDomains"] = include_domains
        if exclude_domains:
            payload["excludeDomains"] = exclude_domains

        # Category filter — searches dedicated indexes
        # Valid: "people", "company", "news", "research paper", "tweet"
        if category:
            payload["category"] = category

        # Content freshness / livecrawl control
        if max_age_hours is not None:
            payload["maxAgeHours"] = max_age_hours

        # Structured output (deep and deep-reasoning only)
        if output_schema and search_type in ("deep", "deep-reasoning"):
            payload["outputSchema"] = output_schema

        # Adjust timeout for deep searches (they take 4-12s)
        timeout = 30 if search_type in ("deep", "deep-reasoning") else 15

        response = await _http.post(
            "https://api.exa.ai/search",
            json=payload,
            headers={
                "x-api-key": _settings.exa_api_key,
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )

        if response.status_code != 200:
            logger.warning("Exa search failed (HTTP %d): %s", response.status_code, response.text[:200])
            return None

        data = response.json()

        # Handle structured output response (deep search with schema)
        if output_schema and "output" in data:
            output = data["output"]
            lines = [f"Structured results for: {query}  [via Exa {search_type}]\n"]
            import json as json_mod
            lines.append(json_mod.dumps(output.get("content", {}), indent=2))
            grounding = output.get("grounding", [])
            if grounding:
                lines.append("\nGrounding/Citations:")
                for g in grounding[:10]:
                    field = g.get("field", "")
                    confidence = g.get("confidence", "")
                    citations = g.get("citations", [])
                    cite_urls = ", ".join(c.get("url", "") for c in citations[:3])
                    lines.append(f"  {field} ({confidence}): {cite_urls}")
            return _mcp_response("\n".join(lines))

        # Standard results
        results = data.get("results", [])

        if not results:
            return _mcp_response(f"No results found for: {query}")

        lines = [f"Search results for: {query}  [via Exa {search_type}]\n"]
        for i, r in enumerate(results[:count], 1):
            title = r.get("title", "No title")
            url = r.get("url", "")

            # Extract content based on mode
            if content_mode == "highlights":
                highlights = r.get("highlights", [])
                snippet = " ... ".join(highlights)[:500].strip() if highlights else ""
            else:
                snippet = r.get("text", "")[:500].strip()

            lines.append(f"{i}. {title}")
            lines.append(f"   URL: {url}")
            if snippet:
                lines.append(f"   {snippet}\n")
            else:
                lines.append("")

        return _mcp_response("\n".join(lines))

    except httpx.TimeoutException:
        logger.warning("Exa search timed out for query: %s (type=%s)", query, search_type)
        return None
    except Exception as e:
        logger.warning("Exa search error: %s", e)
        return None



async def _exa_get_contents(
    urls: list[str],
    *,
    content_mode: str = "text",
    max_text_chars: int = 3000,
    _settings: Settings,
    _http: httpx.AsyncClient,
) -> dict[str, Any] | None:
    """Get contents for known URLs via Exa /contents endpoint.

    Useful when you already have URLs and want to extract clean content
    without searching. Supports text or highlights mode.

    Returns formatted results dict, or None if Exa is not configured or fails.
    """
    if not _settings.exa_api_key:
        return None

    try:
        payload: dict[str, Any] = {"urls": urls}

        if content_mode == "highlights":
            payload["highlights"] = {"maxCharacters": min(max_text_chars, 4000)}
        else:
            payload["text"] = {"maxCharacters": min(max_text_chars, 20000)}

        response = await _http.post(
            "https://api.exa.ai/contents",
            json=payload,
            headers={
                "x-api-key": _settings.exa_api_key,
                "Content-Type": "application/json",
            },
            timeout=15,
        )

        if response.status_code != 200:
            logger.warning("Exa contents failed (HTTP %d): %s", response.status_code, response.text[:200])
            return None

        data = response.json()
        results = data.get("results", [])

        if not results:
            return _mcp_response("No content extracted from provided URLs")

        lines = ["Content extracted via Exa:\n"]
        for i, r in enumerate(results, 1):
            title = r.get("title", "No title")
            url = r.get("url", "")
            if content_mode == "highlights":
                highlights = r.get("highlights", [])
                content_text = " ... ".join(highlights)[:500].strip() if highlights else ""
            else:
                content_text = r.get("text", "")[:500].strip()

            lines.append(f"{i}. {title}")
            lines.append(f"   URL: {url}")
            if content_text:
                lines.append(f"   {content_text}\n")

        return _mcp_response("\n".join(lines))

    except Exception as e:
        logger.warning("Exa contents error: %s", e)
        return None


async def _exa_answer(
    query: str,
    *,
    _settings: Settings,
    _http: httpx.AsyncClient,
) -> dict[str, Any] | None:
    """Q&A with citations from web search via Exa /answer endpoint.

    Returns a direct answer to the query with source citations.
    Useful for factual questions where you want a synthesized answer
    rather than a list of search results.

    Returns formatted answer dict, or None if Exa is not configured or fails.
    """
    if not _settings.exa_api_key:
        return None

    try:
        payload: dict[str, Any] = {"query": query}

        response = await _http.post(
            "https://api.exa.ai/answer",
            json=payload,
            headers={
                "x-api-key": _settings.exa_api_key,
                "Content-Type": "application/json",
            },
            timeout=30,
        )

        if response.status_code != 200:
            logger.warning("Exa answer failed (HTTP %d): %s", response.status_code, response.text[:200])
            return None

        data = response.json()
        answer = data.get("answer", "")
        citations = data.get("citations", [])

        lines = [f"Answer for: {query}  [via Exa Answer]\n"]
        lines.append(answer)

        if citations:
            lines.append("\nSources:")
            for c in citations[:10]:
                title = c.get("title", "")
                url = c.get("url", "")
                lines.append(f"  • {title}: {url}")

        return _mcp_response("\n".join(lines))

    except Exception as e:
        logger.warning("Exa answer error: %s", e)
        return None


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
) -> None:
    """Register web tools (web_search, web_fetch) with the dispatcher.

    Creates closure wrappers that inject settings and httpx client.
    Uses a SEPARATE httpx client from AgentRunner (no auth headers).
    """
    async def _search(query: str, count: int = 5, freshness: str | None = None) -> dict[str, Any]:
        return await _web_search(query, count, freshness, _settings=settings, _http=http_client)

    async def _fetch(url: str, max_chars: int | None = None) -> dict[str, Any]:
        return await _web_fetch(url, max_chars, _settings=settings, _http=http_client)

    dispatcher.register("web_search", _search, _WEB_SEARCH_SCHEMA)
    dispatcher.register("web_fetch", _fetch, _WEB_FETCH_SCHEMA)
