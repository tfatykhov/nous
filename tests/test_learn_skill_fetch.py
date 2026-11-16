"""learn_skill fetches a URL source only from public addresses, pinned to the address it checked.

The resolver is faked and requests go to an httpx.MockTransport: no test makes a network call.
"""

from __future__ import annotations

import ipaddress
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fetch_support import patched_fetch_client

import nous.api.safe_fetch as sf
from nous.api.tools import _SKILL_FETCH_MAX_BYTES, create_nous_tools

PUBLIC_V4 = "93.184.216.34"
SKILL = "---\nname: fetched-skill\ndescription: How to deploy\n---\nRun the checks, then deploy.\n"


def _tools() -> tuple[dict[str, Any], MagicMock]:
    heart = MagicMock()
    heart.get_procedure_by_name = AsyncMock(return_value=None)
    heart.store_procedure = AsyncMock(return_value=SimpleNamespace(id="proc-1"))
    settings = MagicMock()
    settings.workspace_dir = "."
    return create_nous_tools(MagicMock(), heart, settings), heart


def _use_transport(handler: Any):
    """Make learn_skill's clients talk to ``handler`` instead of the network."""
    return patched_fetch_client(handler)


def _text(result: dict[str, Any]) -> str:
    return result["content"][0]["text"]


@pytest.mark.asyncio
async def test_a_public_source_registers_and_connects_to_the_checked_address() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=SKILL)

    tools, heart = _tools()
    with patch.object(sf, "_resolve_host", return_value=[PUBLIC_V4]), _use_transport(handler):
        result = await tools["learn_skill"](source="https://example.com/SKILL.md")

    assert "registered successfully" in _text(result)
    heart.store_procedure.assert_awaited_once()
    assert [(r.url.host, r.headers["host"]) for r in seen] == [(PUBLIC_V4, "example.com")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source",
    [
        "http://127.0.0.1/SKILL.md",
        "http://169.254.169.254/page",
        "http://2130706433/SKILL.md",
        "http://0x7f.1/SKILL.md",
        "http://[::ffff:7f00:1]/SKILL.md",
        "http://[::1]/SKILL.md",
        "http://10.0.0.1/SKILL.md",
        "http://100.64.0.1/SKILL.md",
        "http://localhost/SKILL.md",
        "http://example.com@127.0.0.1/SKILL.md",
    ],
)
async def test_a_source_naming_a_non_public_address_is_refused_without_a_request(source: str) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=SKILL)

    tools, heart = _tools()
    with _use_transport(handler):
        result = await tools["learn_skill"](source=source)

    assert result.get("is_error") is True
    assert seen == []
    heart.store_procedure.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_hostname_with_a_non_public_address_is_refused() -> None:
    tools, heart = _tools()
    with (
        patch.object(sf, "_resolve_host", return_value=[PUBLIC_V4, "10.0.0.5"]),
        _use_transport(lambda r: httpx.Response(200, text=SKILL)),
    ):
        result = await tools["learn_skill"](source="https://example.com/SKILL.md")
    assert result.get("is_error") is True
    assert "10.0.0.5" not in _text(result)
    heart.store_procedure.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["http://127.0.0.1/SKILL.md", "http://[::ffff:a00:1]/", "ftp://example.com/x"])
async def test_a_redirect_to_a_refused_address_is_refused_and_nothing_is_fetched_from_it(location: str) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"location": location})

    tools, heart = _tools()
    with patch.object(sf, "_resolve_host", return_value=[PUBLIC_V4]), _use_transport(handler):
        result = await tools["learn_skill"](source="https://example.com/SKILL.md")

    assert result.get("is_error") is True
    assert "Blocked redirect" in _text(result)
    assert len(seen) == 1
    heart.store_procedure.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_redirect_to_a_public_source_is_followed_and_pinned() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(301, headers={"location": "https://cdn.example/SKILL.md"})
        return httpx.Response(200, text=SKILL)

    addresses = {"example.com": [PUBLIC_V4], "cdn.example": ["8.8.8.8"]}
    tools, _ = _tools()
    with patch.object(sf, "_resolve_host", lambda host: addresses[host]), _use_transport(handler):
        result = await tools["learn_skill"](source="https://example.com/SKILL.md")
    assert "registered successfully" in _text(result)
    assert [(r.url.host, r.headers["host"]) for r in seen] == [(PUBLIC_V4, "example.com"), ("8.8.8.8", "cdn.example")]


@pytest.mark.asyncio
async def test_a_redirect_chain_stops_after_five_hops() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"location": "/again"})

    tools, _ = _tools()
    with patch.object(sf, "_resolve_host", return_value=[PUBLIC_V4]), _use_transport(handler):
        result = await tools["learn_skill"](source="https://example.com/SKILL.md")
    assert "Too many redirects" in _text(result)
    assert len(seen) == 6


@pytest.mark.asyncio
async def test_a_body_longer_than_the_bound_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (_SKILL_FETCH_MAX_BYTES + 1))

    tools, heart = _tools()
    with patch.object(sf, "_resolve_host", return_value=[PUBLIC_V4]), _use_transport(handler):
        result = await tools["learn_skill"](source="https://example.com/SKILL.md")
    assert result.get("is_error") is True
    assert "longer than" in _text(result)
    heart.store_procedure.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_error_status_is_reported_from_the_code_and_the_source_and_nothing_is_registered() -> None:
    tools, heart = _tools()
    with (
        patch.object(sf, "_resolve_host", return_value=[PUBLIC_V4]),
        _use_transport(lambda r: httpx.Response(404, text="missing")),
    ):
        result = await tools["learn_skill"](source="https://example.com/SKILL.md")
    assert result.get("is_error") is True
    assert _text(result) == "Error: https://example.com/SKILL.md returned HTTP 404"
    assert PUBLIC_V4 not in _text(result)
    heart.store_procedure.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_connection_failure_is_reported_without_the_address() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    tools, _ = _tools()
    with patch.object(sf, "_resolve_host", return_value=[PUBLIC_V4]), _use_transport(refuse):
        result = await tools["learn_skill"](source="https://example.com/SKILL.md")
    assert result.get("is_error") is True
    assert PUBLIC_V4 not in _text(result)


# ---------------------------------------------------------------------------
# The client ignores proxy environment variables
# ---------------------------------------------------------------------------

PROXY_VARIABLES = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]


@pytest.mark.parametrize("variable", PROXY_VARIABLES)
def test_a_proxy_variable_does_not_change_where_the_fetch_client_connects(
    monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    monkeypatch.setenv(variable, "http://proxy.invalid:3128")
    client = sf.make_fetch_client()
    urls = [httpx.URL("http://93.184.216.34/"), httpx.URL("https://93.184.216.34/")]
    assert all(client._transport_for_url(u) is client._transport for u in urls)
    # the same variable does reroute a client that reads the environment
    reading = httpx.AsyncClient()
    assert any(reading._transport_for_url(u) is not reading._transport for u in urls)


# ---------------------------------------------------------------------------
# The explicit address rules decide on their own
# ---------------------------------------------------------------------------


class _PropertiesOverridden:
    """Override the ipaddress property answers for IPv6, so the explicit rules decide on their own."""

    def __enter__(self) -> None:
        self._patches = [
            patch.object(ipaddress.IPv6Address, "is_private", property(lambda self: False)),
            patch.object(ipaddress.IPv6Address, "is_reserved", property(lambda self: False)),
            patch.object(ipaddress.IPv6Address, "is_global", property(lambda self: True)),
        ]
        for p in self._patches:
            p.start()

    def __exit__(self, *exc: object) -> None:
        for p in self._patches:
            p.stop()


@pytest.mark.parametrize("literal", ["::ffff:127.0.0.1", "::ffff:7f00:1", "::ffff:10.0.0.1", "::ffff:169.254.169.254"])
def test_an_ipv4_mapped_address_is_judged_by_the_address_inside_it(literal: str) -> None:
    with _PropertiesOverridden():
        # the properties answer "global" for every address here, so the embedded address decides
        assert ipaddress.ip_address(literal).is_global
        assert not sf.is_public_address(ipaddress.ip_address(literal))
    assert sf.is_public_address(ipaddress.ip_address("::ffff:8.8.8.8"))


@pytest.mark.parametrize("literal", ["::127.0.0.1", "::7f00:1", "::a00:1", "::169.254.169.254"])
def test_an_ipv4_compatible_address_is_judged_by_the_address_inside_it(literal: str) -> None:
    with _PropertiesOverridden():
        assert ipaddress.ip_address(literal).is_global
        assert not sf.is_public_address(ipaddress.ip_address(literal))


@pytest.mark.parametrize("literal", ["100.64.0.1", "100.100.100.100", "100.127.255.255"])
def test_a_shared_address_space_address_is_refused_by_the_explicit_rule(literal: str) -> None:
    with patch.object(ipaddress.IPv4Address, "is_global", property(lambda self: True)):
        assert ipaddress.ip_address(literal).is_global
        assert not sf.is_public_address(ipaddress.ip_address(literal))
    assert sf.is_public_address(ipaddress.ip_address("100.128.0.1"))


@pytest.mark.asyncio
async def test_a_location_with_a_non_ascii_byte_is_reported_as_a_blocked_redirect() -> None:
    from fetch_support import FakeServerBackend, fake_network

    def respond(address: str, request_line: str, headers: dict[str, str]) -> bytes:
        return b"HTTP/1.1 302 X\r\nLocation: http://\xff/\r\nContent-Length: 0\r\n\r\n"

    backend = FakeServerBackend(respond)
    tools, heart = _tools()
    with patch.object(sf, "_resolve_host", return_value=[PUBLIC_V4]), fake_network(backend):
        result = await tools["learn_skill"](source="https://example.com/SKILL.md")
    assert _text(result) == "Error: Blocked redirect to unsafe URL: the Location header is not a URL."
    heart.store_procedure.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unexpected_failure_while_fetching_gives_a_fixed_sentence_without_the_exception_text() -> None:
    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom 93.184.216.34")

    tools, heart = _tools()
    with patch.object(sf, "fetch_checked", boom):
        result = await tools["learn_skill"](source="https://example.com/SKILL.md")
    assert result.get("is_error") is True
    assert _text(result) == "Error: the source could not be fetched."
    heart.store_procedure.assert_not_awaited()
