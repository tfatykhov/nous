"""F048: Tests for TCP keep-alive socket options on httpx transports.

Covers:
- _build_socket_options builder under various platform hasattr matrices
- HttpxAnthropicClient.start() wires socket_options + http2 onto the transport
- SdkAnthropicClient.start() wires socket_options + http2 onto its inner
  httpx.AsyncClient transport
"""

from __future__ import annotations

import logging
import socket as _socket
from types import SimpleNamespace

import httpx
import pytest

import nous.api.anthropic_client as ac_mod
from nous.api.anthropic_client import (
    HttpxAnthropicClient,
    SdkAnthropicClient,
    _build_socket_options,
)
from nous.config import Settings


# ---------------------------------------------------------------------------
# _build_socket_options unit tests
# ---------------------------------------------------------------------------


def _make_settings(**overrides) -> Settings:
    return Settings(ANTHROPIC_API_KEY="test-key", **overrides)


def test_build_socket_options_returns_none_when_disabled():
    """F048: flag disabled ⇒ helper short-circuits to None."""
    settings = _make_settings(api_socket_keepalive_enabled=False)
    assert _build_socket_options(settings) is None


def test_build_socket_options_always_includes_so_keepalive():
    """F048: flag enabled ⇒ first option is (SOL_SOCKET, SO_KEEPALIVE, 1)."""
    settings = _make_settings(api_socket_keepalive_enabled=True)
    opts = _build_socket_options(settings)
    assert opts is not None
    assert opts[0] == (_socket.SOL_SOCKET, _socket.SO_KEEPALIVE, 1)


def test_build_socket_options_linux_uses_tcp_keepidle(monkeypatch):
    """F048: Linux path uses TCP_KEEPIDLE (not TCP_KEEPALIVE)."""
    import socket as inner_socket

    # Ensure Linux-style: KEEPIDLE present, KEEPALIVE absent.
    if hasattr(inner_socket, "TCP_KEEPALIVE"):
        monkeypatch.delattr(inner_socket, "TCP_KEEPALIVE", raising=False)
    monkeypatch.setattr(inner_socket, "TCP_KEEPIDLE", 4, raising=False)

    settings = _make_settings(
        api_socket_keepalive_enabled=True,
        api_socket_keepalive_idle=30,
    )
    opts = _build_socket_options(settings)
    assert opts is not None
    # One of the tuples must be TCP_KEEPIDLE with the configured value.
    assert any(
        o == (inner_socket.IPPROTO_TCP, inner_socket.TCP_KEEPIDLE, 30) for o in opts
    )


def test_build_socket_options_macos_uses_tcp_keepalive(monkeypatch):
    """F048: macOS path falls back to TCP_KEEPALIVE when TCP_KEEPIDLE missing."""
    import socket as inner_socket

    # Ensure macOS-style: KEEPIDLE absent, KEEPALIVE present.
    if hasattr(inner_socket, "TCP_KEEPIDLE"):
        monkeypatch.delattr(inner_socket, "TCP_KEEPIDLE", raising=False)
    monkeypatch.setattr(inner_socket, "TCP_KEEPALIVE", 16, raising=False)

    settings = _make_settings(
        api_socket_keepalive_enabled=True,
        api_socket_keepalive_idle=45,
    )
    opts = _build_socket_options(settings)
    assert opts is not None
    assert any(
        o == (inner_socket.IPPROTO_TCP, inner_socket.TCP_KEEPALIVE, 45) for o in opts
    )


def test_build_socket_options_includes_interval_and_count(monkeypatch):
    """F048: TCP_KEEPINTVL + TCP_KEEPCNT applied with configured values."""
    import socket as inner_socket

    # Force all tunables present.
    monkeypatch.setattr(inner_socket, "TCP_KEEPIDLE", 4, raising=False)
    monkeypatch.setattr(inner_socket, "TCP_KEEPINTVL", 5, raising=False)
    monkeypatch.setattr(inner_socket, "TCP_KEEPCNT", 6, raising=False)

    settings = _make_settings(
        api_socket_keepalive_enabled=True,
        api_socket_keepalive_idle=30,
        api_socket_keepalive_interval=10,
        api_socket_keepalive_count=3,
    )
    opts = _build_socket_options(settings)
    assert opts is not None
    assert any(
        o == (inner_socket.IPPROTO_TCP, inner_socket.TCP_KEEPINTVL, 10) for o in opts
    )
    assert any(
        o == (inner_socket.IPPROTO_TCP, inner_socket.TCP_KEEPCNT, 3) for o in opts
    )


def test_build_socket_options_warns_when_neither_keepidle_available(
    monkeypatch, caplog
):
    """F048: missing both TCP_KEEPIDLE and TCP_KEEPALIVE ⇒ warning logged,
    opts still returned with SO_KEEPALIVE only (no per-probe tunables)."""
    import socket as inner_socket

    # Strip both primary and macOS fallback attrs.
    if hasattr(inner_socket, "TCP_KEEPIDLE"):
        monkeypatch.delattr(inner_socket, "TCP_KEEPIDLE", raising=False)
    if hasattr(inner_socket, "TCP_KEEPALIVE"):
        monkeypatch.delattr(inner_socket, "TCP_KEEPALIVE", raising=False)
    # Also strip interval/count to verify SO_KEEPALIVE-only fallback.
    if hasattr(inner_socket, "TCP_KEEPINTVL"):
        monkeypatch.delattr(inner_socket, "TCP_KEEPINTVL", raising=False)
    if hasattr(inner_socket, "TCP_KEEPCNT"):
        monkeypatch.delattr(inner_socket, "TCP_KEEPCNT", raising=False)

    settings = _make_settings(api_socket_keepalive_enabled=True)
    with caplog.at_level(logging.WARNING, logger="nous.api.anthropic_client"):
        opts = _build_socket_options(settings)

    assert opts is not None
    # Only SO_KEEPALIVE should remain (no TCP-level tunables).
    assert opts == [(_socket.SOL_SOCKET, _socket.SO_KEEPALIVE, 1)]
    # Warning must have been emitted.
    assert any(
        "keep-alive tunables not available" in record.message
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# HttpxAnthropicClient.start() transport wiring
# ---------------------------------------------------------------------------


async def test_httpx_client_transport_has_socket_options():
    """F048: HttpxAnthropicClient.start() attaches socket_options + http2=True
    to its AsyncHTTPTransport pool."""
    settings = _make_settings(api_socket_keepalive_enabled=True)
    client = HttpxAnthropicClient(settings)

    await client.start()
    try:
        assert client._http is not None
        transport = client._http._transport
        assert isinstance(transport, httpx.AsyncHTTPTransport)
        pool = transport._pool

        # Socket options were applied.
        sock_opts = pool._socket_options
        assert sock_opts is not None
        # SO_KEEPALIVE is always first.
        assert sock_opts[0] == (_socket.SOL_SOCKET, _socket.SO_KEEPALIVE, 1)
    finally:
        await client.close()


async def test_httpx_client_no_socket_options_when_disabled():
    """F048: flag off ⇒ transport has socket_options=None."""
    settings = _make_settings(api_socket_keepalive_enabled=False)
    client = HttpxAnthropicClient(settings)

    await client.start()
    try:
        transport = client._http._transport
        assert isinstance(transport, httpx.AsyncHTTPTransport)
        assert transport._pool._socket_options is None
    finally:
        await client.close()


async def test_httpx_client_http2_still_active_after_keepalive():
    """F048 P2: transport-mounted keep-alive MUST preserve HTTP/2. The P0
    concern was that moving http2= onto AsyncHTTPTransport could silently
    drop it — confirm it didn't."""
    settings = _make_settings(api_socket_keepalive_enabled=True)
    client = HttpxAnthropicClient(settings)

    await client.start()
    try:
        transport = client._http._transport
        assert isinstance(transport, httpx.AsyncHTTPTransport)
        # _http2 on the pool reflects the constructor kwarg.
        assert transport._pool._http2 is True
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# SdkAnthropicClient.start() transport wiring
# ---------------------------------------------------------------------------


class _SpyAsyncAnthropic:
    """Captures kwargs passed to AsyncAnthropic(...) without contacting
    the real SDK or Anthropic API."""

    last_kwargs: dict | None = None

    def __init__(self, **kwargs):
        type(self).last_kwargs = kwargs
        # Minimum surface the rest of the client expects at teardown.
        self._closed = False

    async def close(self):
        self._closed = True


async def test_sdk_client_transport_has_socket_options(monkeypatch):
    """F048: SdkAnthropicClient.start() passes a custom http_client whose
    transport carries socket_options."""
    # Replace the SDK import inside start() with our spy.
    import anthropic as _anthropic_mod

    monkeypatch.setattr(_anthropic_mod, "AsyncAnthropic", _SpyAsyncAnthropic)

    settings = _make_settings(api_socket_keepalive_enabled=True)
    client = SdkAnthropicClient(settings)
    await client.start()
    try:
        kwargs = _SpyAsyncAnthropic.last_kwargs
        assert kwargs is not None
        http_client = kwargs["http_client"]
        assert isinstance(http_client, httpx.AsyncClient)
        transport = http_client._transport
        assert isinstance(transport, httpx.AsyncHTTPTransport)
        assert transport._pool._socket_options is not None
        assert transport._pool._socket_options[0] == (
            _socket.SOL_SOCKET,
            _socket.SO_KEEPALIVE,
            1,
        )
        assert transport._pool._http2 is True
    finally:
        # Avoid invoking the spy's SDK close path (it's async and would run
        # fine, but teardown must not rely on SDK internals).
        await http_client.aclose()


async def test_sdk_client_no_socket_options_when_disabled(monkeypatch):
    """F048: flag off on the SDK path ⇒ transport socket_options is None."""
    import anthropic as _anthropic_mod

    monkeypatch.setattr(_anthropic_mod, "AsyncAnthropic", _SpyAsyncAnthropic)

    settings = _make_settings(api_socket_keepalive_enabled=False)
    client = SdkAnthropicClient(settings)
    await client.start()
    try:
        kwargs = _SpyAsyncAnthropic.last_kwargs
        http_client = kwargs["http_client"]
        transport = http_client._transport
        assert isinstance(transport, httpx.AsyncHTTPTransport)
        assert transport._pool._socket_options is None
        # HTTP/2 still on.
        assert transport._pool._http2 is True
    finally:
        await http_client.aclose()


# ---------------------------------------------------------------------------
# F048 codex P1: env-proxy preservation
# ---------------------------------------------------------------------------


async def test_httpx_client_preserves_https_env_proxy(monkeypatch):
    """F048 codex P1: setting HTTPS_PROXY must result in an https:// mount
    on the httpx client, even though transport= is set for socket_options."""
    monkeypatch.setenv("HTTPS_PROXY", "http://corp-proxy.example:3128")
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("ALL_PROXY", raising=False)
    monkeypatch.delenv("http_proxy", raising=False)
    monkeypatch.delenv("all_proxy", raising=False)

    client = HttpxAnthropicClient(_make_settings())
    await client.start()
    try:
        # httpx stores mounts as {URLPattern: AsyncBaseTransport}; URLPattern
        # exposes .scheme for the scheme filter.
        schemes = [pat.scheme for pat in client._http._mounts.keys()]
        assert "https" in schemes, (
            f"expected an https:// mount from HTTPS_PROXY; got schemes={schemes}"
        )
    finally:
        await client.close()


async def test_httpx_client_no_proxy_mount_when_env_unset(monkeypatch):
    """F048: when no env proxies are set, no F048-added mounts are created."""
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(key, raising=False)

    client = HttpxAnthropicClient(_make_settings())
    await client.start()
    try:
        schemes = [pat.scheme for pat in client._http._mounts.keys()]
        assert "http" not in schemes and "https" not in schemes and "all" not in schemes, (
            f"expected no scheme mounts when env unset; got schemes={schemes}"
        )
    finally:
        await client.close()


async def test_sdk_client_preserves_http_env_proxy(monkeypatch):
    """F048 codex P1: HTTP_PROXY must produce an http:// mount on the SDK
    backend's inner httpx.AsyncClient."""
    import anthropic as _anthropic_mod

    monkeypatch.setattr(_anthropic_mod, "AsyncAnthropic", _SpyAsyncAnthropic)
    monkeypatch.setenv("HTTP_PROXY", "http://corp-proxy.example:3128")
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("ALL_PROXY", raising=False)

    client = SdkAnthropicClient(_make_settings())
    await client.start()
    try:
        http_client = _SpyAsyncAnthropic.last_kwargs["http_client"]
        schemes = [pat.scheme for pat in http_client._mounts.keys()]
        assert "http" in schemes, (
            f"expected an http:// mount from HTTP_PROXY; got schemes={schemes}"
        )
    finally:
        await http_client.aclose()


def test_build_transport_with_env_proxies_returns_empty_mounts_when_trust_env_false(monkeypatch):
    """F048: trust_env=False short-circuits env iteration regardless of env state."""
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    limits = httpx.Limits(max_connections=5, max_keepalive_connections=2)

    _transport, mounts = ac_mod._build_transport_with_env_proxies(
        http2=True, limits=limits, socket_options=None, trust_env=False,
    )
    assert mounts == {}
