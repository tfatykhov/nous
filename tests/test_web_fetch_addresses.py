"""web_fetch fetches only public internet addresses, in every spelling, and connects to the address it checked.

No test makes a network call: the resolver is faked.
"""

from __future__ import annotations

import socket
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fetch_support import patched_fetch_client

import nous.api.safe_fetch as sf
import nous.api.web_tools as wt

PUBLIC_V4 = "93.184.216.34"


class _Recorder:
    """A resolver that records the names it is asked for (nothing the check catches can hide a call)."""

    def __init__(self, answer: list[str] | None = None) -> None:
        self.calls: list[str] = []
        self.answer = answer if answer is not None else []

    def __call__(self, host: str) -> list[str]:
        self.calls.append(host)
        return self.answer


def _settings() -> MagicMock:
    settings = MagicMock()
    settings.web_fetch_max_chars = 50000
    return settings


def _text(result: dict[str, Any]) -> str:
    return result["content"][0]["text"]


# ---------------------------------------------------------------------------
# Address literals, in every spelling: decided without resolving
# ---------------------------------------------------------------------------

REFUSED_LITERALS = [
    # dotted decimal
    "http://127.0.0.1/",
    "http://127.1.2.3/",
    "http://10.0.0.1/",
    "http://172.16.0.1/",
    "http://172.31.255.255/",
    "http://192.168.1.1/",
    "http://169.254.169.254/page",
    "http://0.0.0.0/",
    "http://0.1.2.3/",
    "http://100.64.0.1/",
    "http://100.127.255.255/",
    "http://224.0.0.1/",
    "http://239.255.255.255/",
    "http://240.0.0.1/",
    "http://255.255.255.255/",
    "http://192.0.0.1/",
    "http://198.18.0.1/",
    "http://192.0.2.1/",
    # integer, octal, hex and short forms
    "http://0/",
    "http://2130706433/",
    "http://0x7f000001/",
    "http://0x7f.1/",
    "http://017700000001/",
    "http://0177.0.0.1/",
    "http://127.1/",
    "http://127.0.1/",
    "http://0x7f.0x0.0x0.0x1/",
    "http://2852039166/",
    "http://0xa9fea9fe/",
    "http://012.0.0.1/",
    # a numeric last label that is not an address is never resolved
    "http://08.1/",
    "http://1.2.3.4.5/",
    "http://256.1.1.1/",
    "http://1.2.3.4294967296/",
    # IPv6
    "http://[::1]/",
    "http://[::]/",
    "http://[fc00::1]/",
    "http://[fd12:3456::1]/",
    "http://[fe80::1]/",
    "http://[fe80::1%25eth0]/",
    "http://[fec0::1]/",
    "http://[ff02::1]/",
    "http://[2001:db8::1]/",
    "http://[100::1]/",
    # IPv6 forms that carry an IPv4 address
    "http://[::ffff:127.0.0.1]/",
    "http://[::ffff:7f00:1]/",
    "http://[::ffff:10.0.0.1]/",
    "http://[::ffff:169.254.169.254]/",
    "http://[::127.0.0.1]/",
    "http://[::a00:1]/",
    "http://[2002:7f00:1::1]/",
    "http://[2002:a9fe:a9fe::1]/",
    "http://[64:ff9b::7f00:1]/",
    "http://[64:ff9b::a9fe:a9fe]/",
    "http://[64:ff9b::a00:1]/",
    "http://[2001:0:7f00:1::]/",
    # spelling of the URL around the host
    "http://127.0.0.1./",
    "http://127.0.0.1:8080/page",
    "http://%31%32%37.0.0.1/",
    "http://example.com@127.0.0.1/",
    "http://user:pw@[::1]:80/",
    "http://\uff11\uff12\uff17.0.0.1/",
    "HTTP://127.0.0.1/",
    "http://0.0.0.0./",
    "http://127.0.0.1\u3002/",
    "http://[2606:4700:4700::1111]:0/",
    "http://127.0.0.1:0/",
    "http://\u2460\u2461\u2466.0.0.1/",
]

# Hosts with characters that no host name has: refused whole, without resolving.
REFUSED_SPELLINGS = [
    "http://exa%00mple.com/",
    "http://example.com%00.internal.corp/",
    "http://example.com%20x/",
    "http://exa%20mple.com/",
    "http://a%40127.0.0.1/",
    "http://a%2f127.0.0.1/",
    "http://_example.com/",
    "http://example_.com/",
    "http://-/",
    "http://example.com:0/",
    "http://[::1%25eth0]/",
    "http://[2606:4700:4700::1111%25eth0]/",
    "http://exa mple.com/",
    "http://.example.com/",
    "http://example..com/",
]


@pytest.mark.parametrize("url", REFUSED_LITERALS)
def test_a_non_public_address_literal_is_refused_without_resolving(url: str) -> None:
    resolver = _Recorder([PUBLIC_V4])
    with patch.object(sf, "_resolve_host", resolver):
        target, error = sf._check_url(url)
    assert target is None
    assert error
    assert resolver.calls == []


@pytest.mark.parametrize("url", REFUSED_SPELLINGS)
def test_a_host_spelled_with_characters_no_host_name_has_is_refused_without_resolving(url: str) -> None:
    resolver = _Recorder([PUBLIC_V4])
    with patch.object(sf, "_resolve_host", resolver):
        target, _ = sf._check_url(url)
    assert target is None
    assert resolver.calls == []


@pytest.mark.parametrize("url", REFUSED_LITERALS)
def test_the_refusal_never_echoes_an_address(url: str) -> None:
    with patch.object(sf, "_resolve_host", _Recorder()):
        _, error = sf._check_url(url)
    assert not any(ch.isdigit() for ch in error.replace("http", "")), error


# ---------------------------------------------------------------------------
# Hostnames: every resolved address is checked
# ---------------------------------------------------------------------------

REFUSED_HOSTNAMES = [
    "http://localhost/",
    "http://LOCALHOST/",
    "http://localhost\u3002/",
    "http://localhost./",
    "http://postgres:5432/",
    "http://Redis/",
]


@pytest.mark.parametrize("url", REFUSED_HOSTNAMES)
def test_a_listed_hostname_is_refused_without_resolving(url: str) -> None:
    resolver = _Recorder([PUBLIC_V4])
    with patch.object(sf, "_resolve_host", resolver):
        target, error = sf._check_url(url)
    assert target is None
    assert error.startswith("Blocked hostname")
    assert resolver.calls == []


@pytest.mark.parametrize(
    "addresses",
    [
        ["10.0.0.5"],
        ["::1"],
        ["::ffff:127.0.0.1"],
        ["64:ff9b::7f00:1"],
        [PUBLIC_V4, "10.0.0.5"],
        ["10.0.0.5", PUBLIC_V4],
        [PUBLIC_V4, "2606:4700:4700::1111", "fe80::1"],
        [PUBLIC_V4, "100.64.0.9"],
        [],
    ],
)
def test_a_hostname_with_any_refused_address_is_refused(addresses: list[str]) -> None:
    with patch.object(sf, "_resolve_host", return_value=addresses):
        target, error = sf._check_url("https://example.com/page")
    assert target is None
    assert "10.0.0.5" not in error


def test_a_hostname_that_does_not_resolve_is_refused() -> None:
    with patch.object(sf, "_resolve_host", side_effect=socket.gaierror("no such host")):
        target, error = sf._check_url("https://nonexistent.invalid/")
    assert target is None
    assert "resolve" in error


# ---------------------------------------------------------------------------
# Allowed public forms
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "connect_to"),
    [
        ("http://8.8.8.8/", "8.8.8.8"),
        ("https://1.1.1.1:8443/x?y=1", "1.1.1.1"),
        ("http://134744072/", "8.8.8.8"),
        ("http://0x8.8.8.8/", "8.8.8.8"),
        ("http://[2606:4700:4700::1111]/", "2606:4700:4700::1111"),
        ("http://[2001:4860:4860::8888]:8080/", "2001:4860:4860::8888"),
        ("http://[::ffff:8.8.8.8]/", "::ffff:8.8.8.8"),
        ("http://[64:ff9b::808:808]/", "64:ff9b::808:808"),
    ],
)
def test_a_public_address_literal_is_allowed_without_resolving(url: str, connect_to: str) -> None:
    resolver = _Recorder()
    with patch.object(sf, "_resolve_host", resolver):
        target, error = sf._check_url(url)
    assert target is not None, error
    assert target.addresses == [connect_to]
    assert resolver.calls == []


@pytest.mark.parametrize(
    ("url", "resolved_name"),
    [
        ("https://example.com/page", "example.com"),
        ("https://Example.COM./page", "example.com"),
        ("https://example.com\u3002/page", "example.com"),
        ("https://example.com%2e/page", "example.com"),
        ("https://%65xample.com/page", "example.com"),
        ("https://exa_mple.example/page", "exa_mple.example"),
        ("https://user:pw@example.com/page", "example.com"),
        ("https://b\u00fccher.example/", "xn--bcher-kva.example"),
    ],
)
def test_an_ordinary_hostname_is_allowed_and_normalised(url: str, resolved_name: str) -> None:
    with patch.object(sf, "_resolve_host", return_value=[PUBLIC_V4, "2606:4700:4700::1111"]) as resolve:
        target, error = sf._check_url(url)
    assert target is not None, error
    resolve.assert_called_once_with(resolved_name)
    assert target.host == resolved_name
    assert target.addresses == [PUBLIC_V4, "2606:4700:4700::1111"]


# ---------------------------------------------------------------------------
# Schemes and malformed URLs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file",
        "file:///tmp/page",
        "gopher://example.com/",
        "javascript:alert(1)",
        "data:text/plain,hello",
        "//example.com/",
        "example.com",
        "",
        "http://",
        "http:///path",
        "http://:80/",
        "http://example.com:notaport/",
        "http://exa\tmple.com/",
        "http://example.com/\nHost: x",
        "http://example.com\\@127.0.0.1/",
    ],
)
def test_only_http_and_https_urls_with_a_host_are_accepted(url: str) -> None:
    with patch.object(sf, "_resolve_host", return_value=[PUBLIC_V4]):
        target, _ = sf._check_url(url)
    assert target is None


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["ftp://example.com/file", "file:///tmp/page", "gopher://example.com/"])
async def test_a_non_http_scheme_makes_no_request(url: str) -> None:
    with patched_fetch_client(lambda r: httpx.Response(200)) as log:
        result = await wt._web_fetch(url, _settings=_settings())
    assert "http" in _text(result)
    assert log.requests == []
