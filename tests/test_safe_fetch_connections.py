"""fetch_checked connects to the address it checked, on a client of its own, within limits.

No test makes a network call: the resolver is faked, and requests go either to a mock transport or to a fake
network backend under the real httpx and httpcore stack.
"""

from __future__ import annotations

import asyncio
import gzip
import time
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fetch_support import (
    FakeServerBackend,
    fake_network,
    http_response,
    patched_fetch_client,
)

import nous.api.safe_fetch as sf
import nous.api.web_tools as wt

pytestmark = pytest.mark.asyncio

PUBLIC_V4 = "93.184.216.34"
OTHER_V4 = "8.8.8.8"


class _Names:
    """A resolver over a fixed table that records the names it is asked for."""

    def __init__(self, table: dict[str, list[str]] | None = None, default: list[str] | None = None) -> None:
        self.table = table or {}
        self.default = default if default is not None else [PUBLIC_V4]
        self.calls: list[str] = []

    def __call__(self, host: str) -> list[str]:
        self.calls.append(host)
        return self.table.get(host, self.default)


def _ok(address: str, request_line: str, headers: dict[str, str]) -> bytes:
    return http_response(200, {"Content-Type": "text/plain"}, b"hello")


def _settings() -> MagicMock:
    settings = MagicMock()
    settings.web_fetch_max_chars = 50000
    return settings


def _text(result: dict[str, Any]) -> str:
    return result["content"][0]["text"]


# ---------------------------------------------------------------------------
# The request goes to the checked address (real httpx stack over a fake network)
# ---------------------------------------------------------------------------


async def test_the_request_connects_to_the_checked_address_not_a_second_resolution() -> None:
    names = _Names(default=[PUBLIC_V4])
    backend = FakeServerBackend(_ok)
    with patch.object(sf, "_resolve_host", names), fake_network(backend):
        response, final = await sf.fetch_checked("https://example.com/a/b?q=1")
    assert response.text == "hello" and final == "https://example.com/a/b?q=1"
    assert names.calls == ["example.com"]
    assert backend.connections == [(PUBLIC_V4, 443)]
    assert backend.requests[0][1] == "GET /a/b?q=1 HTTP/1.1"
    assert backend.requests[0][2]["host"] == "example.com"
    assert backend.handshakes == [(PUBLIC_V4, "example.com")]


async def test_the_host_header_carries_a_port_that_is_not_the_default() -> None:
    backend = FakeServerBackend(_ok)
    with patch.object(sf, "_resolve_host", _Names()), fake_network(backend):
        await sf.fetch_checked("https://Example.com:8443/")
        await sf.fetch_checked("http://example.com/")
    assert [r[2]["host"] for r in backend.requests] == ["example.com:8443", "example.com"]
    assert backend.connections == [(PUBLIC_V4, 8443), (PUBLIC_V4, 80)]
    assert backend.handshakes == [(PUBLIC_V4, "example.com")]


async def test_an_ipv6_address_is_connected_to_with_the_hostname_as_host() -> None:
    backend = FakeServerBackend(_ok)
    with patch.object(sf, "_resolve_host", _Names(default=["2606:4700:4700::1111"])), fake_network(backend):
        await sf.fetch_checked("http://example.com/")
    assert backend.connections == [("2606:4700:4700::1111", 80)]
    assert backend.requests[0][2]["host"] == "example.com"


async def test_an_address_literal_url_connects_to_the_address_it_names() -> None:
    names = _Names()
    backend = FakeServerBackend(_ok)
    with patch.object(sf, "_resolve_host", names), fake_network(backend):
        await sf.fetch_checked("https://134744072/")
    assert names.calls == []
    assert backend.connections == [(OTHER_V4, 443)]
    assert backend.requests[0][2]["host"] == OTHER_V4
    assert backend.handshakes == [(OTHER_V4, OTHER_V4)]


async def test_two_hostnames_at_one_address_each_get_a_connection_and_handshake_of_their_own() -> None:
    backend = FakeServerBackend(_ok)
    names = _Names(default=[PUBLIC_V4])
    with patch.object(sf, "_resolve_host", names), fake_network(backend):
        await sf.fetch_checked("https://a.example/")
        await sf.fetch_checked("https://b.example/")
    assert backend.connections == [(PUBLIC_V4, 443), (PUBLIC_V4, 443)]
    assert backend.handshakes == [(PUBLIC_V4, "a.example"), (PUBLIC_V4, "b.example")]
    assert [r[2]["host"] for r in backend.requests] == ["a.example", "b.example"]


async def test_redirect_hops_to_one_host_each_make_a_connection_of_their_own() -> None:
    def respond(address: str, request_line: str, headers: dict[str, str]) -> bytes:
        if request_line.startswith("GET /start"):
            return http_response(302, {"Location": "/end"})
        return http_response(200, {"Content-Type": "text/plain"}, b"end")

    backend = FakeServerBackend(respond)
    with patch.object(sf, "_resolve_host", _Names()), fake_network(backend):
        response, _ = await sf.fetch_checked("https://example.com/start")
    assert response.text == "end"
    assert len(backend.connections) == 2 and len(backend.handshakes) == 2


async def test_a_cookie_set_for_one_hostname_is_not_sent_to_another_at_the_same_address() -> None:
    def respond(address: str, request_line: str, headers: dict[str, str]) -> bytes:
        if headers["host"] == "a.example":
            return http_response(200, {"Set-Cookie": "sid=1; Path=/", "Content-Type": "text/plain"}, b"a")
        return http_response(200, {"Content-Type": "text/plain"}, b"b")

    backend = FakeServerBackend(respond)
    with patch.object(sf, "_resolve_host", _Names()), fake_network(backend):
        await sf.fetch_checked("https://a.example/")
        await sf.fetch_checked("https://b.example/")
    assert "cookie" not in backend.requests[1][2]


async def test_a_cookie_set_before_a_redirect_is_not_sent_to_the_next_hop() -> None:
    def respond(address: str, request_line: str, headers: dict[str, str]) -> bytes:
        if headers["host"] == "a.example":
            return http_response(302, {"Location": "https://b.example/", "Set-Cookie": "sid=1; Path=/"})
        return http_response(200, {"Content-Type": "text/plain"}, b"b")

    backend = FakeServerBackend(respond)
    with patch.object(sf, "_resolve_host", _Names()), fake_network(backend):
        response, final = await sf.fetch_checked("https://a.example/")
    assert response.text == "b" and final == "https://b.example/"
    assert [r[2]["host"] for r in backend.requests] == ["a.example", "b.example"]
    assert "cookie" not in backend.requests[1][2]


async def test_the_fetch_client_ignores_the_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(variable, "http://proxy.invalid:3128")
    backend = FakeServerBackend(_ok)
    with patch.object(sf, "_resolve_host", _Names()), fake_network(backend):
        await sf.fetch_checked("http://example.com/")
        await sf.fetch_checked("https://example.com/")
    assert backend.connections == [(PUBLIC_V4, 80), (PUBLIC_V4, 443)]


# ---------------------------------------------------------------------------
# Bodies: no content encoding, a byte bound, a deadline
# ---------------------------------------------------------------------------


async def test_the_request_asks_for_an_unencoded_response() -> None:
    backend = FakeServerBackend(_ok)
    with patch.object(sf, "_resolve_host", _Names()), fake_network(backend):
        await sf.fetch_checked("http://example.com/")
    assert backend.requests[0][2]["accept-encoding"] == "identity"


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br", "zstd", "gzip, br"])
async def test_a_response_with_a_content_encoding_is_refused(encoding: str) -> None:
    body = gzip.compress(b"hello " * 10)
    backend = FakeServerBackend(lambda a, r, h: http_response(200, {"Content-Encoding": encoding}, body))
    with patch.object(sf, "_resolve_host", _Names()), fake_network(backend):
        with pytest.raises(sf.FetchRefused, match="content encoding"):
            await sf.fetch_checked("http://example.com/")


async def test_a_response_marked_identity_is_read_and_framing_headers_are_rebuilt() -> None:
    backend = FakeServerBackend(
        lambda a, r, h: http_response(200, {"Content-Encoding": "identity", "Content-Type": "text/plain"}, b"hello")
    )
    with patch.object(sf, "_resolve_host", _Names()), fake_network(backend):
        response, _ = await sf.fetch_checked("http://example.com/")
    assert response.text == "hello"
    assert "content-encoding" not in response.headers
    assert response.headers["content-length"] == "5"


async def test_a_body_longer_than_the_bound_is_refused_and_one_at_the_bound_is_read() -> None:
    backend = FakeServerBackend(lambda a, r, h: http_response(200, {"Content-Type": "text/plain"}, b"x" * 1000))
    with patch.object(sf, "_resolve_host", _Names()), fake_network(backend):
        response, _ = await sf.fetch_checked("http://example.com/", max_bytes=1000)
        assert len(response.content) == 1000
        with pytest.raises(sf.FetchRefused, match="longer than 999 bytes"):
            await sf.fetch_checked("http://example.com/", max_bytes=999)


async def test_a_body_without_a_length_is_counted_as_it_arrives() -> None:
    def respond(address: str, request_line: str, headers: dict[str, str]) -> bytes:
        return b"HTTP/1.1 200 X\r\nContent-Type: text/plain\r\n\r\n" + b"x" * 5000

    backend = FakeServerBackend(respond)
    with patch.object(sf, "_resolve_host", _Names()), fake_network(backend):
        with pytest.raises(sf.FetchRefused, match="longer than"):
            await sf.fetch_checked("http://example.com/", max_bytes=1000)


async def test_web_fetch_reads_at_most_the_web_fetch_bound() -> None:
    backend = FakeServerBackend(lambda a, r, h: http_response(200, {"Content-Type": "text/plain"}, b"x" * 300))
    with (
        patch.object(sf, "_resolve_host", _Names()),
        fake_network(backend),
        patch.object(sf, "WEB_FETCH_MAX_BYTES", 200),
    ):
        result = await wt._web_fetch("http://example.com/", _settings=_settings())
    assert result["is_error"] is True
    assert "longer than 200 bytes" in _text(result)


async def test_the_default_web_fetch_bound_is_ten_mebibytes() -> None:
    assert sf.WEB_FETCH_MAX_BYTES == 10 * 1024 * 1024


async def test_a_response_that_never_finishes_ends_at_the_deadline() -> None:
    backend = FakeServerBackend(lambda a, r, h: b"", hang=True)
    with patch.object(sf, "_resolve_host", _Names()), fake_network(backend):
        started = time.monotonic()
        with pytest.raises(sf.FetchRefused, match="took longer than 0.3 seconds"):
            await asyncio.wait_for(sf.fetch_checked("http://example.com/", deadline=0.3), 5)
    assert time.monotonic() - started < 5


async def test_a_body_that_arrives_slowly_ends_at_the_deadline_though_every_read_succeeds() -> None:
    backend = FakeServerBackend(
        lambda a, r, h: http_response(200, {"Content-Type": "text/plain"}, b"x" * 500), drip_seconds=0.02
    )
    with patch.object(sf, "_resolve_host", _Names()), fake_network(backend):
        with pytest.raises(sf.FetchRefused, match="took longer"):
            await asyncio.wait_for(sf.fetch_checked("http://example.com/", deadline=0.3), 5)


async def test_the_deadline_covers_the_redirect_hops_together() -> None:
    def respond(address: str, request_line: str, headers: dict[str, str]) -> bytes:
        return http_response(302, {"Location": "/again"})

    backend = FakeServerBackend(respond, drip_seconds=0.05)
    with patch.object(sf, "_resolve_host", _Names()), fake_network(backend):
        with pytest.raises(sf.FetchRefused, match="took longer"):
            await asyncio.wait_for(sf.fetch_checked("http://example.com/", deadline=0.3), 5)


async def test_the_default_deadline_is_sixty_seconds() -> None:
    assert sf.FETCH_DEADLINE_SECONDS == 60.0


async def test_a_resolver_that_does_not_answer_in_time_refuses_the_url() -> None:
    def slow(host: str) -> list[str]:
        time.sleep(0.5)
        return [PUBLIC_V4]

    with patch.object(sf, "_resolve_host", slow), patch.object(sf, "RESOLVE_TIMEOUT_SECONDS", 0.05):
        target, error = await sf.check_url("http://example.com/")
    assert target is None
    assert "public internet address" in error


# ---------------------------------------------------------------------------
# Fallback between checked addresses
# ---------------------------------------------------------------------------


async def test_at_most_four_checked_addresses_are_tried_and_the_host_is_resolved_once() -> None:
    addresses = [f"93.184.216.{n}" for n in range(1, 7)]
    names = _Names(default=addresses)
    backend = FakeServerBackend(_ok, failing=frozenset(addresses))
    with patch.object(sf, "_resolve_host", names), fake_network(backend):
        with pytest.raises(sf.FetchRefused, match="Could not connect"):
            await sf.fetch_checked("http://example.com/")
    assert [c[0] for c in backend.connections] == addresses[:4]
    assert names.calls == ["example.com"]


async def test_repeated_addresses_are_tried_once_and_the_next_one_serves() -> None:
    names = _Names(default=[PUBLIC_V4, PUBLIC_V4, PUBLIC_V4, OTHER_V4])
    backend = FakeServerBackend(_ok, failing=frozenset({PUBLIC_V4}))
    with patch.object(sf, "_resolve_host", names), fake_network(backend):
        response, _ = await sf.fetch_checked("http://example.com/")
    assert response.text == "hello"
    assert [c[0] for c in backend.connections] == [PUBLIC_V4, OTHER_V4]
    assert names.calls == ["example.com"]


async def test_the_connect_failure_sentence_names_no_address() -> None:
    backend = FakeServerBackend(_ok, failing=frozenset({PUBLIC_V4}))
    with patch.object(sf, "_resolve_host", _Names()), fake_network(backend):
        result = await wt._web_fetch("http://example.com/", _settings=_settings())
    assert result["is_error"] is True
    assert "Could not connect to http://example.com/" in _text(result)
    assert PUBLIC_V4 not in _text(result)


# ---------------------------------------------------------------------------
# Refusals and redirects (mock transport)
# ---------------------------------------------------------------------------


def _redirect(location: str) -> httpx.Response:
    return httpx.Response(302, headers={"location": location})


def _page(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/plain"}, text="hello")


async def test_a_hostname_with_a_refused_address_makes_no_request_and_the_sentence_names_no_address() -> None:
    with (
        patch.object(sf, "_resolve_host", _Names(default=[PUBLIC_V4, "10.0.0.5"])),
        patched_fetch_client(_page) as log,
    ):
        result = await wt._web_fetch("http://example.com/", _settings=_settings())
    assert result["is_error"] is True
    assert "does not resolve to a public internet address" in _text(result)
    assert "10.0.0.5" not in _text(result)
    assert log.requests == []


async def test_the_sentence_is_the_same_whether_a_host_does_not_resolve_or_resolves_to_a_private_address() -> None:
    def missing(host: str) -> list[str]:
        raise OSError("no such host")

    with patch.object(sf, "_resolve_host", missing):
        _, gone = await sf.check_url("http://nonexistent.invalid/")
    with patch.object(sf, "_resolve_host", _Names(default=["10.0.0.5"])):
        _, private = await sf.check_url("http://other.example/")
    assert gone == private and "10.0.0.5" not in private


@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1/page",
        "http://[::ffff:7f00:1]/",
        "http://2130706433/",
        "http://169.254.169.254/",
        "ftp://example.com/",
    ],
)
async def test_a_redirect_to_a_refused_address_is_refused_and_nothing_is_fetched_from_it(location: str) -> None:
    with patch.object(sf, "_resolve_host", _Names()), patched_fetch_client(lambda r: _redirect(location)) as log:
        result = await wt._web_fetch("http://example.com/", _settings=_settings())
    assert "Blocked redirect" in _text(result)
    assert [r.url.host for r in log.requests] == [PUBLIC_V4]


async def test_a_redirect_to_a_hostname_with_a_refused_address_is_refused() -> None:
    names = _Names({"other.example": ["10.1.2.3"]})
    with (
        patch.object(sf, "_resolve_host", names),
        patched_fetch_client(lambda r: _redirect("https://other.example/x")) as log,
    ):
        result = await wt._web_fetch("https://example.com/", _settings=_settings())
    assert "Blocked redirect" in _text(result)
    assert "10.1.2.3" not in _text(result)
    assert len(log.requests) == 1


@pytest.mark.parametrize("location", ["https://127.0.0.1/", "https://[::ffff:7f00:1]/", "https://2130706433/"])
async def test_a_second_redirect_hop_to_a_refused_address_is_refused(location: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["host"] == "example.com":
            return _redirect("https://second.example/")
        return _redirect(location)

    with patch.object(sf, "_resolve_host", _Names()), patched_fetch_client(handler) as log:
        result = await wt._web_fetch("https://example.com/", _settings=_settings())
    assert _text(result).startswith("Blocked redirect to unsafe URL")
    assert [r.headers["host"] for r in log.requests] == ["example.com", "second.example"]


async def test_a_relative_and_a_protocol_relative_redirect_are_checked_and_pinned() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/":
            return _redirect("/next")
        if path == "/next":
            return _redirect("//other.example/final")
        return _page(request)

    names = _Names({"example.com": [PUBLIC_V4], "other.example": [OTHER_V4]})
    with patch.object(sf, "_resolve_host", names), patched_fetch_client(handler) as log:
        result = await wt._web_fetch("https://example.com/", _settings=_settings())
    assert "hello" in _text(result)
    assert [(r.url.host, r.headers["host"], r.url.path) for r in log.requests] == [
        (PUBLIC_V4, "example.com", "/"),
        (PUBLIC_V4, "example.com", "/next"),
        (OTHER_V4, "other.example", "/final"),
    ]


async def test_a_redirect_chain_stops_after_five_hops() -> None:
    with patch.object(sf, "_resolve_host", _Names()), patched_fetch_client(lambda r: _redirect("/again")) as log:
        result = await wt._web_fetch("https://example.com/", _settings=_settings())
    assert "Too many redirects" in _text(result)
    assert len(log.requests) == 6


async def test_a_redirect_from_https_to_http_is_refused_and_one_from_http_to_https_is_followed() -> None:
    with (
        patch.object(sf, "_resolve_host", _Names()),
        patched_fetch_client(lambda r: _redirect("http://example.com/x")) as log,
    ):
        result = await wt._web_fetch("https://example.com/", _settings=_settings())
    assert "from https to http" in _text(result)
    assert len(log.requests) == 1

    def upgrade(request: httpx.Request) -> httpx.Response:
        return _page(request) if request.url.scheme == "https" else _redirect("https://example.com/")

    with patch.object(sf, "_resolve_host", _Names()), patched_fetch_client(upgrade) as log:
        result = await wt._web_fetch("http://example.com/", _settings=_settings())
    assert "hello" in _text(result)


@pytest.mark.parametrize(
    "location", ["http://[::1", "data:text/plain,hello", "javascript:alert(1)", "http://[", "mailto:a@b.c"]
)
async def test_a_location_that_is_not_a_fetchable_url_gives_the_blocked_redirect_sentence(location: str) -> None:
    with patch.object(sf, "_resolve_host", _Names()), patched_fetch_client(lambda r: _redirect(location)) as log:
        result = await wt._web_fetch("https://example.com/", _settings=_settings())
    assert _text(result).startswith("Blocked redirect to unsafe URL")
    assert len(log.requests) == 1


# ---------------------------------------------------------------------------
# One client per request, closed afterwards
# ---------------------------------------------------------------------------


async def test_every_request_gets_a_client_of_its_own_that_is_closed_when_the_fetch_ends() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _redirect("/b") if request.url.path == "/a" else _page(request)

    with patch.object(sf, "_resolve_host", _Names()), patched_fetch_client(handler) as log:
        await sf.fetch_checked("https://example.com/a")
    assert len(log.requests) == 2
    assert len(log.clients) == 2 and len({id(c) for c in log.clients}) == 2
    assert log.closed == [True, True]


async def test_the_client_is_closed_when_the_fetch_fails() -> None:
    def refuse_connection(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with patch.object(sf, "_resolve_host", _Names()), patched_fetch_client(refuse_connection) as log:
        with pytest.raises(sf.FetchRefused):
            await sf.fetch_checked("https://example.com/a")
    assert log.closed and all(log.closed)


# ---------------------------------------------------------------------------
# A Location with a byte no URL has, and callers' fixed sentences
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("location", [b"http://\xff/", b"http://exa\xffmple.com/", b"/\xff\xfe"])
async def test_a_location_with_a_non_ascii_byte_gives_the_blocked_redirect_sentence(location: bytes) -> None:
    def respond(address: str, request_line: str, headers: dict[str, str]) -> bytes:
        return b"HTTP/1.1 302 X\r\nLocation: " + location + b"\r\nContent-Length: 0\r\n\r\n"

    backend = FakeServerBackend(respond)
    with patch.object(sf, "_resolve_host", _Names()), fake_network(backend):
        result = await wt._web_fetch("http://example.com/", _settings=_settings())
    assert _text(result) == "Blocked redirect to unsafe URL: the Location header is not a URL."
    assert len(backend.requests) == 1


async def test_an_unexpected_failure_in_web_fetch_gives_a_fixed_sentence_without_the_exception_text() -> None:
    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom 93.184.216.34")

    with patch.object(wt, "fetch_checked", boom):
        result = await wt._web_fetch("http://example.com/", _settings=_settings())
    assert result["is_error"] is True
    assert _text(result) == "The page could not be fetched."


async def test_every_fetch_client_verifies_with_one_shared_tls_context() -> None:
    first, second = sf.make_fetch_client(), sf.make_fetch_client()
    try:
        assert first._transport._pool._ssl_context is second._transport._pool._ssl_context
        assert first is not second
    finally:
        await first.aclose()
        await second.aclose()
