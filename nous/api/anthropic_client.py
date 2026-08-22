"""Anthropic API client abstraction — strategy pattern for httpx vs SDK backends.

Provides AnthropicClient protocol with two implementations:
- HttpxAnthropicClient: direct httpx calls (extracted from runner.py)
- SdkAnthropicClient: wraps official anthropic.AsyncAnthropic SDK

Runner builds payloads via _build_api_payload(); clients consume them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import AsyncGenerator
from email.utils import parsedate_to_datetime
from typing import Any, Protocol, runtime_checkable

import httpx

from nous.api.models import ApiResponse
from nous.config import Settings

logger = logging.getLogger(__name__)

# Anthropic API version header
_API_VERSION = "2023-06-01"

# Retry constants (shared by httpx path)
_MAX_RETRIES = 5
_BACKOFF_DELAYS = (1.0, 2.0, 4.0, 8.0, 16.0)
_BACKOFF_CAP = 30.0
_HEADER_DELAY_MAX = 60.0


# ---------------------------------------------------------------------------
# StreamEvent (moved from runner.py — used by both clients)
# ---------------------------------------------------------------------------

from dataclasses import dataclass, field


@dataclass
class StreamEvent:
    """A single event from the streaming API response."""

    type: str  # text_delta, tool_start, tool_input_delta, tool_end, block_stop, done, error, message_stop
    text: str = ""
    tool_name: str = ""
    tool_id: str = ""
    tool_input: dict = field(default_factory=dict)
    stop_reason: str = ""
    block_index: int = 0
    usage: dict[str, int] | None = None


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class AnthropicClient(Protocol):
    """Abstract interface for Anthropic API backends."""

    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def call(self, payload: dict[str, Any]) -> ApiResponse: ...
    async def stream(self, payload: dict[str, Any]) -> AsyncGenerator[StreamEvent, None]: ...
    async def call_streaming_aggregated(self, payload: dict[str, Any]) -> ApiResponse: ...


# ---------------------------------------------------------------------------
# Socket-options helper (F048 — TCP keep-alive on httpx AsyncHTTPTransport)
# ---------------------------------------------------------------------------


def _build_socket_options(settings: Settings) -> list[tuple[int, int, int]] | None:
    """Build socket options for TCP keep-alive on httpx.AsyncHTTPTransport.

    Returns None when keep-alive is disabled. Otherwise returns a list of
    (level, optname, value) tuples suitable for `socket_options=` on the
    transport. Platform differences:
      - Linux: TCP_KEEPIDLE + TCP_KEEPINTVL + TCP_KEEPCNT
      - macOS: TCP_KEEPALIVE (same semantic as TCP_KEEPIDLE)
      - Windows: only SO_KEEPALIVE; idle/interval/count fall back to OS defaults
    """
    if not settings.api_socket_keepalive_enabled:
        return None

    import socket
    import sys

    opts: list[tuple[int, int, int]] = [
        (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
    ]

    keepidle_added = False
    if hasattr(socket, "TCP_KEEPIDLE"):
        opts.append((socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, settings.api_socket_keepalive_idle))
        keepidle_added = True
    elif hasattr(socket, "TCP_KEEPALIVE"):  # macOS
        opts.append((socket.IPPROTO_TCP, socket.TCP_KEEPALIVE, settings.api_socket_keepalive_idle))
        keepidle_added = True

    if hasattr(socket, "TCP_KEEPINTVL"):
        opts.append((socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, settings.api_socket_keepalive_interval))
    if hasattr(socket, "TCP_KEEPCNT"):
        opts.append((socket.IPPROTO_TCP, socket.TCP_KEEPCNT, settings.api_socket_keepalive_count))

    if not keepidle_added:
        logger.warning(
            "TCP keep-alive tunables not available on this platform (os=%s) -- "
            "only SO_KEEPALIVE is set; idle probe timing relies on OS defaults",
            sys.platform,
        )

    return opts


def _build_transport_with_env_proxies(
    *,
    http2: bool,
    limits: httpx.Limits,
    socket_options: list[tuple[int, int, int]] | None,
    trust_env: bool = True,
) -> tuple[httpx.AsyncHTTPTransport, dict[str, httpx.AsyncHTTPTransport]]:
    """Build an AsyncHTTPTransport with keep-alive options plus env-proxy mounts.

    F048 codex P1: When a caller passes ``transport=`` to ``httpx.AsyncClient``,
    httpx's auto-loading of ``HTTP_PROXY`` / ``HTTPS_PROXY`` / ``ALL_PROXY``
    environment variables is silently disabled
    (``allow_env_proxies = trust_env and transport is None``). We need the
    custom transport so we can set ``socket_options`` for TCP keep-alive, so
    we replicate httpx's env-proxy logic manually by returning a ``mounts``
    dict that the caller passes alongside ``transport=``.

    Returns ``(default_transport, mounts)``. ``mounts`` is empty when no
    proxies are set or ``trust_env=False``.
    """
    transport_kwargs: dict[str, Any] = {
        "http2": http2,
        "limits": limits,
    }
    if socket_options is not None:
        transport_kwargs["socket_options"] = socket_options

    default_transport = httpx.AsyncHTTPTransport(**transport_kwargs)

    mounts: dict[str, httpx.AsyncHTTPTransport] = {}
    if not trust_env:
        return default_transport, mounts

    import os

    # Match httpx's auto-load matrix: HTTPS_PROXY binds https://, HTTP_PROXY
    # binds http://, ALL_PROXY binds all:// (lowest precedence).
    for env_keys, scheme in (
        (("HTTPS_PROXY", "https_proxy"), "https://"),
        (("HTTP_PROXY", "http_proxy"), "http://"),
        (("ALL_PROXY", "all_proxy"), "all://"),
    ):
        proxy_url = next(
            (os.environ[k] for k in env_keys if os.environ.get(k)),
            None,
        )
        if not proxy_url:
            continue
        mounts[scheme] = httpx.AsyncHTTPTransport(
            **transport_kwargs, proxy=proxy_url,
        )

    if mounts:
        logger.info(
            "F048: honouring env proxies for Anthropic transport: %s",
            sorted(mounts.keys()),
        )
    return default_transport, mounts


# ---------------------------------------------------------------------------
# Httpx helpers (extracted from runner.py)
# ---------------------------------------------------------------------------


def _should_retry(status_code: int, headers: httpx.Headers) -> bool:
    """Always retry on retryable status codes. Log x-should-retry header if present."""
    should = headers.get("x-should-retry")
    is_retryable = status_code in (408, 409, 429) or status_code >= 500

    if should is not None:
        if should.lower() == "true":
            logger.info("x-should-retry: true (status %d)", status_code)
            return True
        else:
            logger.info(
                "x-should-retry: false (status %d) — retrying anyway per policy",
                status_code,
            )

    return is_retryable


def _retry_delay(attempt: int, response: httpx.Response | None = None) -> float:
    """Compute retry delay from headers or exponential backoff with jitter."""
    if response is not None:
        retry_ms = response.headers.get("retry-after-ms")
        retry_after = response.headers.get("retry-after")
        should_retry = response.headers.get("x-should-retry")

        logger.info(
            "Retry headers: x-should-retry=%s, retry-after-ms=%s, retry-after=%s",
            should_retry,
            retry_ms,
            retry_after,
        )

        if retry_ms is not None:
            try:
                delay = float(retry_ms) / 1000.0
                if 0 <= delay <= _HEADER_DELAY_MAX:
                    logger.info("Using retry-after-ms: %.3fs", delay)
                    return delay
            except (ValueError, OverflowError):
                pass

        if retry_after is not None:
            try:
                delay = float(retry_after)
                if 0 <= delay <= _HEADER_DELAY_MAX:
                    logger.info("Using Retry-After: %.1fs", delay)
                    return delay
            except ValueError:
                try:
                    from datetime import datetime, timezone
                    target = parsedate_to_datetime(retry_after)
                    delay = (target - datetime.now(timezone.utc)).total_seconds()
                    if 0 <= delay <= _HEADER_DELAY_MAX:
                        logger.info("Using Retry-After (date): %.1fs", delay)
                        return delay
                except (ValueError, TypeError):
                    pass

    base = _BACKOFF_DELAYS[min(attempt, len(_BACKOFF_DELAYS) - 1)]
    jitter = random.uniform(0.75, 1.0)
    return min(base * jitter, _BACKOFF_CAP)


def _parse_sse_event(data: dict[str, Any]) -> StreamEvent | None:
    """Parse Anthropic SSE event dict into StreamEvent."""
    event_type = data.get("type")

    if event_type == "ping":
        return None

    if event_type == "error":
        error = data.get("error", {})
        return StreamEvent(
            type="error",
            text=f"{error.get('type', 'unknown')}: {error.get('message', '')}",
        )

    if event_type == "content_block_start":
        block = data.get("content_block", {})
        block_index = data.get("index", 0)
        block_type = block.get("type")
        if block_type == "tool_use":
            return StreamEvent(
                type="tool_start",
                tool_name=block.get("name", ""),
                tool_id=block.get("id", ""),
                block_index=block_index,
            )
        if block_type == "thinking":
            return StreamEvent(type="thinking_start", block_index=block_index)
        if block_type == "redacted_thinking":
            return StreamEvent(
                type="redacted_thinking",
                text=block.get("data", ""),
                block_index=block_index,
            )
        return StreamEvent(type="text_block_start", block_index=block_index)

    if event_type == "content_block_delta":
        delta = data.get("delta", {})
        block_index = data.get("index", 0)
        delta_type = delta.get("type")
        if delta_type == "text_delta":
            return StreamEvent(type="text_delta", text=delta.get("text", ""))
        if delta_type == "input_json_delta":
            return StreamEvent(
                type="tool_input_delta",
                text=delta.get("partial_json", ""),
                block_index=block_index,
            )
        if delta_type == "thinking_delta":
            return StreamEvent(
                type="thinking_delta",
                text=delta.get("thinking", ""),
                block_index=block_index,
            )
        if delta_type == "signature_delta":
            return StreamEvent(
                type="signature_delta",
                text=delta.get("signature", ""),
                block_index=block_index,
            )
        return None

    if event_type == "content_block_stop":
        return StreamEvent(type="block_stop", block_index=data.get("index", 0))

    if event_type == "message_delta":
        usage = data.get("usage")
        return StreamEvent(
            type="done",
            stop_reason=data.get("delta", {}).get("stop_reason", ""),
            usage=usage,
        )

    if event_type == "message_start":
        usage = data.get("message", {}).get("usage")
        return StreamEvent(type="message_start", usage=usage)

    if event_type == "message_stop":
        return StreamEvent(type="message_stop")

    return None


# ---------------------------------------------------------------------------
# HttpxAnthropicClient
# ---------------------------------------------------------------------------


class HttpxAnthropicClient:
    """Anthropic API client using direct httpx calls.

    Extracted from runner.py — preserves all retry logic, SSE parsing,
    header management, and auth handling.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._http: httpx.AsyncClient | None = None

    async def start(self) -> None:
        """Initialize the httpx client with auth and timeout settings."""
        settings = self._settings

        headers: dict[str, str] = {
            "anthropic-version": _API_VERSION,
            "content-type": "application/json",
            "accept": "application/json",
            "user-agent": "claude-cli/2.1.2 (external, cli)",
            "User-Agent": "Anthropic/JS 0.73.0",
            "x-app": "cli",
            "X-Stainless-Lang": "js",
            "X-Stainless-Package-Version": "0.73.0",
            "X-Stainless-OS": "Linux",
            "X-Stainless-Arch": "x64",
            "X-Stainless-Runtime": "node",
            "X-Stainless-Runtime-Version": "v22.22.0",
        }

        api_key = settings.anthropic_api_key or ""
        auth_token = settings.anthropic_auth_token or ""
        is_oat = False

        if auth_token:
            headers["authorization"] = f"Bearer {auth_token}"
            if "sk-ant-oat" in auth_token:
                is_oat = True
                headers["anthropic-dangerous-direct-browser-access"] = "true"
        elif api_key:
            if "sk-ant-oat" in api_key:
                is_oat = True
                headers["authorization"] = f"Bearer {api_key}"
                headers["anthropic-dangerous-direct-browser-access"] = "true"
            else:
                headers["x-api-key"] = api_key
        else:
            logger.warning(
                "Neither ANTHROPIC_API_KEY nor ANTHROPIC_AUTH_TOKEN is set -- "
                "API calls will fail"
            )

        # interleaved-thinking-2025-05-14 and fine-grained-tool-streaming-2025-05-14
        # were REMOVED — Anthropic's Claude 4 migration guide marks both obsolete.
        # Adaptive thinking (Opus 4.6+/Sonnet 4.6+) enables interleaved thinking
        # natively, and sending the legacy interleaved header alongside it broke
        # opus-4.8 streaming tool round-trips ("tool_use ids without tool_result").
        # Nous targets Claude 4.6+ only, so they are dropped unconditionally.
        beta_features: list[str] = [
            "claude-code-20250219",
        ]
        if is_oat:
            beta_features.append("oauth-2025-04-20")
        headers["anthropic-beta"] = ",".join(beta_features)

        timeout = httpx.Timeout(
            connect=settings.api_timeout_connect,
            read=settings.api_timeout_read,
            write=10.0,
            pool=10.0,
        )
        limits = httpx.Limits(
            max_connections=5,
            max_keepalive_connections=2,
        )

        # F048: TCP keep-alive on the transport. When transport= is set on
        # httpx.AsyncClient, http2= and limits= kwargs on AsyncClient itself
        # are silently ignored — they must live on the transport instead.
        # The helper also returns mounts= to preserve HTTP_PROXY/HTTPS_PROXY
        # env-proxy routing, which httpx otherwise disables when a custom
        # transport is passed (codex P1 on PR #337).
        sock_opts = _build_socket_options(settings)
        transport, mounts = _build_transport_with_env_proxies(
            http2=True, limits=limits, socket_options=sock_opts,
        )

        self._http = httpx.AsyncClient(
            base_url=settings.api_base_url,
            headers=headers,
            timeout=timeout,
            transport=transport,
            mounts=mounts,
        )

        auth_type = "OAT/subscription" if is_oat else ("Bearer token" if auth_token else "API key")
        logger.info(
            "httpx client initialized (auth: %s, http2: true, keepalive: %s, env_proxies: %s)",
            auth_type,
            bool(sock_opts),
            bool(mounts),
        )

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    async def call(self, payload: dict[str, Any]) -> ApiResponse:
        """Call Anthropic Messages API with retry and exponential backoff.

        The full payload dict is passed through to the API by design —
        this includes tools, tool_choice, and any other API parameters
        the caller sets.
        """
        if not self._http:
            raise RuntimeError("httpx client not initialized -- call start() first")

        last_error: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = await self._http.post(
                    "/v1/messages", json=payload,
                    headers={"X-Stainless-Retry-Count": str(attempt)},
                )

                if response.status_code == 200:
                    data = response.json()
                    return ApiResponse(
                        content=data["content"],
                        stop_reason=data["stop_reason"],
                        usage=data.get("usage"),
                    )

                try:
                    error_data = response.json()
                    error_type = error_data.get("error", {}).get("type", "unknown")
                    error_msg = error_data.get("error", {}).get("message", "unknown error")
                except Exception:
                    error_type = "http_error"
                    error_msg = f"HTTP {response.status_code}: {response.text[:500]}"

                logger.info(
                    "Anthropic API %d %s: %s (request_id=%s)",
                    response.status_code,
                    error_type,
                    error_msg,
                    response.headers.get("request-id", "n/a"),
                )
                if response.status_code >= 500:
                    logger.info(
                        "Anthropic API response headers: %s",
                        dict(response.headers),
                    )

                if _should_retry(response.status_code, response.headers) and attempt < _MAX_RETRIES:
                    delay = _retry_delay(attempt, response)
                    logger.warning(
                        "API error %d (%s), retry %d/%d in %.1fs: %s",
                        response.status_code,
                        error_type,
                        attempt + 1,
                        _MAX_RETRIES,
                        delay,
                        error_msg,
                    )
                    await asyncio.sleep(delay)
                    continue

                last_error = RuntimeError(
                    f"Anthropic API error ({response.status_code}): "
                    f"{error_type} - {error_msg}"
                )

            except httpx.TimeoutException as e:
                last_error = RuntimeError(f"API request timed out: {e}")
                if attempt < _MAX_RETRIES:
                    delay = _retry_delay(attempt)
                    logger.warning("API timeout, retry %d/%d in %.1fs: %s", attempt + 1, _MAX_RETRIES, delay, e)
                    await asyncio.sleep(delay)
                    continue
            except httpx.HTTPError as e:
                last_error = RuntimeError(f"HTTP error: {e}")
                break

        raise last_error or RuntimeError("API call failed with unknown error")

    async def stream(
        self,
        payload: dict[str, Any],
        *,
        timeout: httpx.Timeout | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Call Anthropic API with streaming enabled. Yields StreamEvent objects.

        F048: accepts an optional per-call ``timeout`` override. Used by
        ``call_streaming_aggregated`` to honor the background 600s read timeout
        without mutating the client-wide default.
        """
        if not self._http:
            raise RuntimeError("httpx client not initialized -- call start() first")

        # Ensure stream flag is set
        payload = {**payload, "stream": True}

        # Only pass timeout= into httpx when the caller supplied one; otherwise
        # fall through to the client-wide default configured in start().
        stream_kwargs: dict[str, Any] = {}
        if timeout is not None:
            stream_kwargs["timeout"] = timeout

        for attempt in range(_MAX_RETRIES + 1):
            try:
                async with self._http.stream(
                    "POST", "/v1/messages", json=payload,
                    headers={"X-Stainless-Retry-Count": str(attempt)},
                    **stream_kwargs,
                ) as response:
                    if response.status_code != 200:
                        error_body = await response.aread()
                        error_text = error_body.decode()[:500]
                        logger.info(
                            "Anthropic streaming API %d: %s (request_id=%s)",
                            response.status_code,
                            error_text,
                            response.headers.get("request-id", "n/a"),
                        )
                        if response.status_code >= 500:
                            logger.info(
                                "Anthropic streaming API response headers: %s",
                                dict(response.headers),
                            )

                        if _should_retry(response.status_code, response.headers) and attempt < _MAX_RETRIES:
                            delay = _retry_delay(attempt, response)
                            logger.warning(
                                "Streaming API error %d, retry %d/%d in %.1fs",
                                response.status_code,
                                attempt + 1,
                                _MAX_RETRIES,
                                delay,
                            )
                            await asyncio.sleep(delay)
                            continue

                        yield StreamEvent(type="error", text=error_text)
                        return

                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data = json.loads(line[6:])
                        event = _parse_sse_event(data)
                        if event:
                            if event.type == "error":
                                yield event
                                return
                            yield event
                    return

            except httpx.TimeoutException as e:
                if attempt < _MAX_RETRIES:
                    delay = _retry_delay(attempt)
                    logger.warning("Streaming API timeout, retry %d/%d in %.1fs: %s", attempt + 1, _MAX_RETRIES, delay, e)
                    await asyncio.sleep(delay)
                    continue
                yield StreamEvent(type="error", text=f"Stream timeout: {e}")
                return
            except httpx.HTTPError as e:
                yield StreamEvent(type="error", text=f"Stream HTTP error: {e}")
                return

    async def call_streaming_aggregated(self, payload: dict[str, Any]) -> ApiResponse:
        """Background-path call: stream under the hood, return an aggregated ApiResponse.

        F048 Mechanism B. Uses streaming SSE to keep the TCP socket warm with
        incremental bytes during long generations, then reconstructs the full
        content blocks + usage + stop_reason into an ApiResponse that callers
        can treat identically to ``call()``'s return value.

        Retry semantics are intentionally weaker than ``call()``: the outer
        heartbeat / subtask layers own whole-turn retries, and this method
        relies on ``stream()``'s built-in HTTP-error retry loop.
        """
        if not self._http:
            raise RuntimeError("httpx client not initialized -- call start() first")

        bg_timeout = httpx.Timeout(
            connect=self._settings.api_timeout_connect,
            read=self._settings.api_background_timeout_read,
            write=10.0,
            pool=10.0,
        )

        blocks: dict[int, dict[str, Any]] = {}
        tool_input_fragments: dict[int, list[str]] = {}
        text_parts: dict[int, list[str]] = {}
        thinking_parts: dict[int, list[str]] = {}
        signature_parts: dict[int, list[str]] = {}
        last_text_block_index: int | None = None
        stop_reason: str | None = None
        usage: dict[str, int] = {}
        terminal_seen = False  # F048: detect truncation mid-stream

        async for event in self.stream(payload, timeout=bg_timeout):
            if event.type == "message_start":
                if event.usage:
                    # Captures input_tokens + cache_read/creation_input_tokens
                    usage.update(event.usage)
            elif event.type == "text_block_start":
                blocks[event.block_index] = {"type": "text", "text": ""}
                text_parts[event.block_index] = []
                last_text_block_index = event.block_index
            elif event.type == "tool_start":
                blocks[event.block_index] = {
                    "type": "tool_use",
                    "id": event.tool_id,
                    "name": event.tool_name,
                    "input": {},
                }
                tool_input_fragments[event.block_index] = []
            elif event.type == "thinking_start":
                # F048 codex P2: preserve extended-thinking blocks so callers
                # that enable thinking_mode get the same content shape as
                # non-streaming call() returns.
                blocks[event.block_index] = {
                    "type": "thinking",
                    "thinking": "",
                    "signature": "",
                }
                thinking_parts[event.block_index] = []
                signature_parts[event.block_index] = []
            elif event.type == "redacted_thinking":
                # Redacted thinking blocks arrive as a single complete event.
                blocks[event.block_index] = {
                    "type": "redacted_thinking",
                    "data": event.text,
                }
            elif event.type == "text_delta":
                # _parse_sse_event drops block_index on text_delta; fragments
                # belong to the most recently opened text block (mirrors the
                # stream_chat reconstruction logic in runner.py).
                if last_text_block_index is not None:
                    text_parts.setdefault(last_text_block_index, []).append(event.text)
            elif event.type == "thinking_delta":
                thinking_parts.setdefault(event.block_index, []).append(event.text)
            elif event.type == "signature_delta":
                signature_parts.setdefault(event.block_index, []).append(event.text)
            elif event.type == "tool_input_delta":
                tool_input_fragments.setdefault(event.block_index, []).append(event.text)
            elif event.type == "block_stop":
                frags = tool_input_fragments.pop(event.block_index, None)
                if frags is not None and event.block_index in blocks:
                    joined = "".join(frags)
                    try:
                        blocks[event.block_index]["input"] = (
                            json.loads(joined) if joined else {}
                        )
                    except json.JSONDecodeError as e:
                        # Structural only -- the payload fragment is deliberately
                        # not logged (same reasoning as the streaming handler in
                        # runner.py: tool inputs carry commands, file bodies and
                        # message text, and this fires at the prod default level).
                        logger.warning(
                            "F048 aggregator: malformed tool_input JSON at block %d "
                            "(tool=%s, id=%s): %d bytes across %d streamed parts, "
                            "error=%s at offset %d",
                            event.block_index,
                            blocks[event.block_index].get("name", "?"),
                            blocks[event.block_index].get("id", "?"),
                            len(joined),
                            len(frags),
                            e.msg,
                            e.pos,
                        )
                        blocks[event.block_index]["input"] = {}
                # Finalize text / thinking block content on close
                block = blocks.get(event.block_index)
                if block is not None:
                    if block.get("type") == "text" and event.block_index in text_parts:
                        block["text"] = "".join(text_parts[event.block_index])
                    elif block.get("type") == "thinking":
                        block["thinking"] = "".join(
                            thinking_parts.get(event.block_index, [])
                        )
                        block["signature"] = "".join(
                            signature_parts.get(event.block_index, [])
                        )
            elif event.type == "done":
                stop_reason = event.stop_reason or stop_reason
                terminal_seen = True
                if event.usage:
                    # Merge output_tokens (and any new keys) without clobbering
                    # cache_*_tokens captured at message_start — the delta event
                    # only reports output_tokens on most Anthropic responses.
                    for k, v in event.usage.items():
                        if k in ("cache_read_input_tokens", "cache_creation_input_tokens"):
                            if not usage.get(k):
                                usage[k] = v
                        else:
                            usage[k] = v
            elif event.type == "message_stop":
                terminal_seen = True
            elif event.type == "error":
                raise RuntimeError(f"Anthropic streaming error: {event.text}")

        if not terminal_seen:
            # F048: HTTP 200 with a truncated SSE body (proxy/LB drop, client
            # cancel, malformed upstream) otherwise collapses to an empty
            # ApiResponse that callers treat as a successful zero-token turn.
            # Surface it so the outer retry/failure handling fires instead.
            raise RuntimeError(
                "Anthropic streaming ended without terminal event "
                "(no 'message_delta' or 'message_stop'); response truncated"
            )

        # Flush any text / thinking blocks that never saw a block_stop (defensive)
        for idx, parts in text_parts.items():
            if idx in blocks and blocks[idx].get("type") == "text" and not blocks[idx].get("text"):
                blocks[idx]["text"] = "".join(parts)
        for idx, parts in thinking_parts.items():
            block = blocks.get(idx)
            if block is not None and block.get("type") == "thinking" and not block.get("thinking"):
                block["thinking"] = "".join(parts)
                block["signature"] = "".join(signature_parts.get(idx, []))

        ordered_content = [blocks[i] for i in sorted(blocks)]
        return ApiResponse(
            content=ordered_content,
            stop_reason=stop_reason or "end_turn",
            usage=usage or None,
        )


# ---------------------------------------------------------------------------
# SdkAnthropicClient
# ---------------------------------------------------------------------------


class SdkAnthropicClient:
    """Anthropic API client using the official anthropic Python SDK.

    Uses anthropic.AsyncAnthropic with native retries, proper Python
    User-Agent headers, and correct auth handling. The SDK automatically
    sets anthropic-version, content-type, User-Agent, and X-Stainless-*
    headers with correct Python runtime values.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Any = None  # anthropic.AsyncAnthropic

    async def start(self) -> None:
        """Initialize the AsyncAnthropic client."""
        try:
            from anthropic import AsyncAnthropic
        except ImportError:
            raise RuntimeError(
                "anthropic package not installed. "
                "Install with: uv add anthropic"
            )

        settings = self._settings
        api_key = settings.anthropic_api_key or ""
        auth_token = settings.anthropic_auth_token or ""
        is_oat = False

        # Build SDK constructor kwargs
        kwargs: dict[str, Any] = {
            "max_retries": _MAX_RETRIES,
            "timeout": float(settings.api_timeout_read),
            "base_url": settings.api_base_url,
        }

        # Auth: OAT uses auth_token, regular uses api_key
        if auth_token:
            if "sk-ant-oat" in auth_token:
                is_oat = True
            kwargs["auth_token"] = auth_token
        elif api_key:
            if "sk-ant-oat" in api_key:
                is_oat = True
                kwargs["auth_token"] = api_key
            else:
                kwargs["api_key"] = api_key
        else:
            logger.warning(
                "Neither ANTHROPIC_API_KEY nor ANTHROPIC_AUTH_TOKEN is set -- "
                "API calls will fail"
            )
            kwargs["api_key"] = "missing"

        # Beta headers + OAT browser access header
        # interleaved-thinking-2025-05-14 and fine-grained-tool-streaming-2025-05-14
        # were REMOVED — Anthropic's Claude 4 migration guide marks both obsolete.
        # Adaptive thinking (Opus 4.6+/Sonnet 4.6+) enables interleaved thinking
        # natively, and sending the legacy interleaved header alongside it broke
        # opus-4.8 streaming tool round-trips ("tool_use ids without tool_result").
        # Nous targets Claude 4.6+ only, so they are dropped unconditionally.
        beta_features: list[str] = [
            "claude-code-20250219",
        ]
        if is_oat:
            beta_features.append("oauth-2025-04-20")

        default_headers: dict[str, str] = {
            "anthropic-beta": ",".join(beta_features),
        }
        if is_oat:
            default_headers["anthropic-dangerous-direct-browser-access"] = "true"
        kwargs["default_headers"] = default_headers

        # Pass a custom httpx client with HTTP/2 enabled plus F048 TCP keep-alive.
        # http2= and limits= live on the transport; passing them on AsyncClient
        # alongside transport= would be silently ignored. Also pass mounts= to
        # preserve HTTP_PROXY/HTTPS_PROXY env-proxy routing (codex P1 on PR #337).
        sock_opts = _build_socket_options(settings)
        sdk_transport, sdk_mounts = _build_transport_with_env_proxies(
            http2=True,
            limits=httpx.Limits(
                max_connections=5,
                max_keepalive_connections=2,
            ),
            socket_options=sock_opts,
        )
        kwargs["http_client"] = httpx.AsyncClient(
            transport=sdk_transport, mounts=sdk_mounts,
        )

        self._client = AsyncAnthropic(**kwargs)

        auth_type = "OAT/subscription" if is_oat else ("Bearer token" if auth_token else "API key")
        logger.info(
            "Anthropic SDK client initialized (auth: %s, http2: true, keepalive: %s, env_proxies: %s)",
            auth_type,
            bool(sock_opts),
            bool(sdk_mounts),
        )

    async def close(self) -> None:
        if self._client:
            await self._client.close()
            self._client = None

    async def call(self, payload: dict[str, Any]) -> ApiResponse:
        """Call Anthropic Messages API via SDK."""
        if not self._client:
            raise RuntimeError("SDK client not initialized -- call start() first")

        kwargs = self._payload_to_kwargs(payload)

        try:
            message = await self._client.messages.create(**kwargs)
        except Exception as e:
            self._log_sdk_error(e)
            raise RuntimeError(f"Anthropic SDK error: {e}") from e

        # Convert SDK Message to our ApiResponse
        content = self._message_to_content(message)
        return ApiResponse(
            content=content,
            stop_reason=message.stop_reason,
            usage={
                "input_tokens": message.usage.input_tokens,
                "output_tokens": message.usage.output_tokens,
                "cache_creation_input_tokens": getattr(message.usage, "cache_creation_input_tokens", 0),
                "cache_read_input_tokens": getattr(message.usage, "cache_read_input_tokens", 0),
            }
            if message.usage
            else None,
        )

    async def stream(self, payload: dict[str, Any]) -> AsyncGenerator[StreamEvent, None]:
        """Stream Anthropic Messages API via SDK. Yields StreamEvent objects.

        Uses messages.create(stream=True) which returns AsyncStream[RawMessageStreamEvent]
        with raw event types (message_start, content_block_start, etc.) matching
        our _convert_sdk_event expectations. Do NOT use messages.stream() — that
        yields high-level parsed events (text, input_json, thinking) with different
        type discriminators.

        Approved exception: :meth:`call_streaming_aggregated` **does** use
        ``messages.stream()`` with ``get_final_message()`` — but only to grab the
        final aggregated Message object, not to iterate parsed events. The
        warning above applies only to raw-event consumers.
        """
        if not self._client:
            raise RuntimeError("SDK client not initialized -- call start() first")

        kwargs = self._payload_to_kwargs(payload)
        kwargs["stream"] = True

        try:
            raw_stream = await self._client.messages.create(**kwargs)
            async for event in raw_stream:
                converted = self._convert_sdk_event(event)
                if converted is not None:
                    if converted.type == "error":
                        yield converted
                        return
                    yield converted
        except Exception as e:
            self._log_sdk_error(e)
            yield StreamEvent(type="error", text=f"SDK stream error: {e}")

    async def call_streaming_aggregated(self, payload: dict[str, Any]) -> ApiResponse:
        """Background-path call via SDK: stream + aggregate into a full Message.

        F048 Mechanism B for the SDK backend.

        NOTE: This is the approved exception to the "Do NOT use
        messages.stream()" guidance in :meth:`stream`'s docstring. We use
        ``messages.stream()`` with ``get_final_message()`` because we need the
        fully-aggregated Message object, NOT the raw event stream — so the
        high-level parsed event shape never touches our code path and the
        warning does not apply.
        """
        if not self._client:
            raise RuntimeError("SDK client not initialized -- call start() first")

        kwargs = self._payload_to_kwargs(payload)
        # messages.stream() does NOT accept the stream= kwarg that
        # _payload_to_kwargs may set; passing it would raise TypeError.
        kwargs.pop("stream", None)
        # Explicit timeout (SDK expects float seconds, not an httpx.Timeout).
        kwargs["timeout"] = float(self._settings.api_background_timeout_read)

        try:
            async with self._client.messages.stream(**kwargs) as s:
                message = await s.get_final_message()
        except Exception as e:
            self._log_sdk_error(e)
            raise RuntimeError(f"Anthropic SDK streaming error: {e}") from e

        return ApiResponse(
            content=self._message_to_content(message),
            stop_reason=message.stop_reason,
            usage={
                "input_tokens": message.usage.input_tokens,
                "output_tokens": message.usage.output_tokens,
                "cache_creation_input_tokens": getattr(message.usage, "cache_creation_input_tokens", 0),
                "cache_read_input_tokens": getattr(message.usage, "cache_read_input_tokens", 0),
            }
            if message.usage
            else None,
        )

    @staticmethod
    def _log_sdk_error(e: Exception) -> None:
        """Log detailed error info from Anthropic SDK exceptions."""
        # APIStatusError has status_code, request_id, body, response
        # APIError (base) has request with headers
        status = getattr(e, "status_code", None)
        request_id = getattr(e, "request_id", None)
        body = getattr(e, "body", None)
        response = getattr(e, "response", None)
        request = getattr(e, "request", None)

        # Log request headers (sanitize auth)
        if request is not None:
            req_headers = dict(request.headers)
            for key in ("authorization", "x-api-key"):
                if key in req_headers:
                    val = req_headers[key]
                    req_headers[key] = val[:12] + "..." if len(val) > 12 else "***"
            logger.info("Anthropic SDK request headers: %s", req_headers)

        if status is not None:
            error_type = "unknown"
            error_msg = str(e)
            if isinstance(body, dict):
                error_info = body.get("error", {})
                error_type = error_info.get("type", "unknown")
                error_msg = error_info.get("message", str(e))

            logger.error(
                "Anthropic SDK %d %s: %s (request_id=%s)",
                status, error_type, error_msg, request_id or "n/a",
            )
            if status >= 500 and response is not None:
                logger.info(
                    "Anthropic SDK response headers: %s",
                    dict(response.headers),
                )
        else:
            logger.error("Anthropic SDK error: %s", e)

    def _payload_to_kwargs(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Convert our payload dict to SDK messages.create() kwargs."""
        kwargs: dict[str, Any] = {
            "model": payload["model"],
            "max_tokens": payload["max_tokens"],
            "system": payload["system"],
            "messages": payload["messages"],
        }
        if payload.get("tools"):
            kwargs["tools"] = payload["tools"]
        if payload.get("tool_choice"):
            kwargs["tool_choice"] = payload["tool_choice"]
        if payload.get("stream"):
            kwargs["stream"] = True
        if payload.get("thinking"):
            kwargs["thinking"] = payload["thinking"]
        if payload.get("output_config"):
            kwargs["output_config"] = payload["output_config"]
        return kwargs

    @staticmethod
    def _message_to_content(message: Any) -> list[dict[str, Any]]:
        """Convert SDK Message.content blocks to raw dicts matching the API's
        wire format.

        The SDK's `messages.stream()` helper (used by call_streaming_aggregated)
        attaches helper-only fields (e.g. `parsed_output`) to some blocks that
        the server will reject on the next request as
        "Extra inputs are not permitted". model_dump() emits every field, so
        we explicitly build each block from the allow-listed set of fields
        per block type and drop anything else.
        """
        content: list[dict[str, Any]] = []
        for block in message.content:
            block_type = getattr(block, "type", None)

            if block_type == "text":
                out: dict[str, Any] = {
                    "type": "text",
                    "text": getattr(block, "text", "") or "",
                }
                citations = getattr(block, "citations", None)
                if citations:
                    out["citations"] = [
                        c.model_dump() if hasattr(c, "model_dump") else dict(c)
                        for c in citations
                    ]
            elif block_type == "tool_use":
                out = {
                    "type": "tool_use",
                    "id": getattr(block, "id", ""),
                    "name": getattr(block, "name", ""),
                    "input": getattr(block, "input", {}) or {},
                }
            elif block_type == "thinking":
                out = {
                    "type": "thinking",
                    "thinking": getattr(block, "thinking", "") or "",
                    "signature": getattr(block, "signature", "") or "",
                }
            elif block_type == "redacted_thinking":
                out = {
                    "type": "redacted_thinking",
                    "data": getattr(block, "data", "") or "",
                }
            else:
                # Unknown block type — model_dump but strip known helper-only
                # fields as a defensive fallback.
                raw = (
                    block.model_dump() if hasattr(block, "model_dump")
                    else dict(block)
                )
                raw.pop("parsed_output", None)
                out = raw

            content.append(out)
        return content

    @staticmethod
    def _convert_sdk_event(event: Any) -> StreamEvent | None:
        """Convert an SDK RawMessageStreamEvent to our StreamEvent type."""
        event_type = event.type

        if event_type == "message_start":
            usage = None
            if hasattr(event, "message") and hasattr(event.message, "usage"):
                u = event.message.usage
                usage = {
                    "input_tokens": getattr(u, "input_tokens", 0),
                    "output_tokens": getattr(u, "output_tokens", 0),
                    "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0),
                    "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0),
                }
            return StreamEvent(type="message_start", usage=usage)

        if event_type == "content_block_start":
            block = event.content_block
            block_index = event.index
            block_type = block.type
            if block_type == "tool_use":
                return StreamEvent(
                    type="tool_start",
                    tool_name=block.name,
                    tool_id=block.id,
                    block_index=block_index,
                )
            if block_type == "thinking":
                return StreamEvent(type="thinking_start", block_index=block_index)
            if block_type == "redacted_thinking":
                return StreamEvent(
                    type="redacted_thinking",
                    text=getattr(block, "data", ""),
                    block_index=block_index,
                )
            # text block
            return StreamEvent(type="text_block_start", block_index=block_index)

        if event_type == "content_block_delta":
            delta = event.delta
            block_index = event.index
            delta_type = delta.type
            if delta_type == "text_delta":
                return StreamEvent(type="text_delta", text=delta.text)
            if delta_type == "input_json_delta":
                return StreamEvent(
                    type="tool_input_delta",
                    text=delta.partial_json,
                    block_index=block_index,
                )
            if delta_type == "thinking_delta":
                return StreamEvent(
                    type="thinking_delta",
                    text=delta.thinking,
                    block_index=block_index,
                )
            if delta_type == "signature_delta":
                return StreamEvent(
                    type="signature_delta",
                    text=delta.signature,
                    block_index=block_index,
                )
            return None

        if event_type == "content_block_stop":
            return StreamEvent(type="block_stop", block_index=event.index)

        if event_type == "message_delta":
            usage = None
            if hasattr(event, "usage") and event.usage:
                usage = {
                    "input_tokens": getattr(event.usage, "input_tokens", 0),
                    "output_tokens": getattr(event.usage, "output_tokens", 0),
                    "cache_creation_input_tokens": getattr(event.usage, "cache_creation_input_tokens", 0),
                    "cache_read_input_tokens": getattr(event.usage, "cache_read_input_tokens", 0),
                }
            return StreamEvent(
                type="done",
                stop_reason=event.delta.stop_reason if hasattr(event.delta, "stop_reason") else "",
                usage=usage,
            )

        if event_type == "message_stop":
            return StreamEvent(type="message_stop")

        # Skip ping and unknown events
        return None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_client(settings: Settings) -> AnthropicClient:
    """Create the appropriate AnthropicClient based on settings.api_backend."""
    backend = settings.api_backend
    if backend == "sdk":
        return SdkAnthropicClient(settings)
    elif backend == "httpx":
        return HttpxAnthropicClient(settings)
    else:
        raise ValueError(f"Unknown api_backend: {backend!r} (expected 'sdk' or 'httpx')")
