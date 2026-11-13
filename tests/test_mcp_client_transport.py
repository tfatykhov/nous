"""F112 MCP client PR 1: the address guard of the client's HTTP connections. No network: the resolver is replaced
where a host name is resolved, and IP literals resolve locally."""

from __future__ import annotations

import inspect
import logging
from typing import Any

import httpx
import pytest

from nous.mcp_client import transport
from nous.mcp_client.transport import BlockedAddress, _GuardedBackend, check_url, guarded_client, is_public_address

NONE: frozenset[str] = frozenset()


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.5.4",
        "192.168.1.141",
        "100.64.0.1",  # shared address space (a tailnet)
        "169.254.169.254",  # cloud metadata
        "169.254.1.1",  # link-local
        "0.0.0.0",
        "224.0.0.1",  # multicast
        "240.0.0.1",
        "255.255.255.255",
        "::1",
        "::",
        "fe80::1",
        "fe80::1%eth0",
        "fc00::1",
        "fd00:ec2::254",  # cloud metadata over IPv6
        "ff02::1",
        "::ffff:127.0.0.1",  # IPv4-mapped
        "::ffff:169.254.169.254",
        "::ffff:10.0.0.1",
        "64:ff9b::7f00:1",  # NAT64 of 127.0.0.1
        "2002:7f00:1::",  # 6to4 of 127.0.0.1
        "::7f00:1",
        "not-an-address",
    ],
)
def test_addresses_that_are_not_public_are_refused(ip):
    assert not is_public_address(ip)


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111", "::ffff:8.8.8.8"])
def test_public_addresses_are_allowed(ip):
    assert is_public_address(ip)


def _resolver(addresses: list[str]):
    async def resolve(host: str, port: int) -> list[str]:
        return addresses

    return resolve


@pytest.mark.parametrize(
    "url",
    [
        "http://mcp.example.com/mcp",
        "https://mcp.example.com/mcp?key=x",
        "https://user:pw@mcp.example.com/mcp",
        "https://127.0.0.1/mcp",
        "https://[::1]/mcp",
        "https://169.254.169.254/latest/meta-data",
    ],
)
async def test_check_url_refuses(url):
    with pytest.raises(BlockedAddress):
        await check_url(url, NONE)


async def test_check_url_resolves_the_host_and_refuses_any_private_answer(monkeypatch):
    monkeypatch.setattr(transport, "_resolve", _resolver(["93.184.216.34"]))
    await check_url("https://mcp.example.com/mcp", NONE)
    monkeypatch.setattr(transport, "_resolve", _resolver(["93.184.216.34", "10.0.0.5"]))
    with pytest.raises(BlockedAddress):
        await check_url("https://mcp.example.com/mcp", NONE)


async def test_an_allowed_private_host_is_not_resolved(monkeypatch):
    monkeypatch.setattr(transport, "_resolve", _resolver(["10.0.0.5"]))
    await check_url("https://Intranet.Lan/mcp", frozenset({"intranet.lan"}))
    with pytest.raises(BlockedAddress):
        await check_url("https://other.lan/mcp", frozenset({"intranet.lan"}))


async def test_a_redirect_hop_to_a_private_address_is_refused(monkeypatch):
    # The client follows no redirect (a 3xx fails the request); a hop the SDK or a later PR followed would be judged
    # by check_url, and its connection by the backend below.
    client = guarded_client(NONE)
    assert client.follow_redirects is False and client.trust_env is False
    monkeypatch.setattr(transport, "_resolve", _resolver(["192.168.1.141"]))
    with pytest.raises(BlockedAddress):
        await check_url("https://rebind.example.com/next", NONE)
    await client.aclose()


class _Inner:
    def __init__(self) -> None:
        self.dialed: list[str] = []

    async def connect_tcp(self, host: str, port: int, **_kwargs: Any) -> str:
        self.dialed.append(host)
        return f"stream to {host}"


async def test_the_backend_dials_the_address_it_checked(monkeypatch):
    inner = _Inner()
    backend = _GuardedBackend(inner, frozenset({"intranet.lan"}))  # type: ignore[arg-type]
    monkeypatch.setattr(transport, "_resolve", _resolver(["93.184.216.34"]))
    assert await backend.connect_tcp("mcp.example.com", 443) == "stream to 93.184.216.34"
    monkeypatch.setattr(transport, "_resolve", _resolver(["10.1.1.1"]))
    with pytest.raises(BlockedAddress):
        await backend.connect_tcp("rebind.example.com", 443)
    assert await backend.connect_tcp("intranet.lan", 443) == "stream to intranet.lan"
    with pytest.raises(BlockedAddress):
        await backend.connect_unix_socket("/var/run/docker.sock")
    assert inner.dialed == ["93.184.216.34", "intranet.lan"]


async def test_every_connection_of_the_real_client_goes_through_the_guard():
    client = guarded_client(NONE)
    assert isinstance(client._transport._pool._network_backend, _GuardedBackend)
    with pytest.raises(BlockedAddress):
        await client.get("https://127.0.0.1:9/mcp")  # refused before any socket is opened
    with pytest.raises(BlockedAddress):
        await client.get("https://[::ffff:169.254.169.254]/latest")
    await client.aclose()


async def test_the_session_refuses_a_url_before_connecting():
    with pytest.raises(BlockedAddress):
        async with transport.open_session("https://10.0.0.1/mcp", NONE):
            pass


async def test_a_url_with_a_query_string_is_never_requested(caplog):
    # The HTTP client logs every request URL at INFO: a URL that could carry a credential is refused first.
    caplog.set_level(logging.DEBUG)
    with pytest.raises(BlockedAddress) as refused:
        async with transport.open_session("https://mcp.example.com/mcp?key=SECRET-91c2", NONE):
            pass
    assert "SECRET-91c2" not in str(refused.value)
    assert all("SECRET-91c2" not in record.getMessage() for record in caplog.records)


def test_the_sdk_still_has_what_the_transport_uses():
    # CI installs the newest mcp 1.x the range allows, not the locked 1.26: pin the signatures the transport calls.
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.types import PaginatedRequestParams

    assert "http_client" in inspect.signature(streamable_http_client).parameters
    assert "params" in inspect.signature(ClientSession.list_tools).parameters
    assert list(inspect.signature(ClientSession.call_tool).parameters)[1:3] == ["name", "arguments"]
    assert PaginatedRequestParams(cursor="c").cursor == "c"


def test_no_proxy_from_the_environment(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://10.0.0.9:3128")
    client = guarded_client(NONE)
    # A proxy mount would carry requests past the guarded transport; with its own transport and trust_env off, the
    # client builds none.
    assert client.trust_env is False and not client._mounts
    assert isinstance(client._transport, httpx.AsyncHTTPTransport)
