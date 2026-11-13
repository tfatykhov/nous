"""F112 MCP client: the one module that imports the MCP SDK's client.

``open_session`` opens a streamable-HTTP ``ClientSession`` with no authentication, over an HTTP client that:

- connects only to public addresses: ``_GuardedBackend`` resolves the host when a connection is opened, refuses it
  when any address is private, loopback, link-local, multicast, unspecified, reserved or a cloud metadata address,
  and dials the address it checked, so a DNS answer that changes between the check and the connect cannot steer it
  (TLS still verifies the certificate against the host name). A host in ``allow_private_hosts`` is not checked;
- follows no redirect: a redirect fails the request;
- ignores proxy settings from the environment: through a proxy, the address checked would be the proxy's.

SDK symbols used (``mcp`` 1.26): ``mcp.client.session.ClientSession`` (``initialize``, ``list_tools(params=)``,
``call_tool``), ``mcp.client.streamable_http.streamable_http_client(url, http_client=)`` and
``mcp.types.PaginatedRequestParams``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

import httpcore
import httpx
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from nous.mcp_client.config import url_problem

_METADATA_ADDRESSES = frozenset({ipaddress.ip_address("169.254.169.254"), ipaddress.ip_address("fd00:ec2::254")})
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
# The SDK's own defaults: 30 s for a request, 5 minutes between events of a server-sent stream.
_HTTP_TIMEOUT = httpx.Timeout(30.0, read=300.0)


class BlockedAddress(Exception):
    """A URL or host the client's address guard refuses."""


def is_public_address(ip: str) -> bool:
    """Whether ``ip`` is a public unicast address. An IPv4 address carried inside IPv6 (mapped, 6to4, NAT64) is
    judged as that IPv4 address; anything that does not parse is not public."""
    try:
        addr: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address):
        if addr in _METADATA_ADDRESSES:
            return False
        if addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        elif addr.sixtofour is not None:
            addr = addr.sixtofour
        elif addr in _NAT64:
            addr = ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
    if addr in _METADATA_ADDRESSES:
        return False
    return addr.is_global and not (addr.is_multicast or addr.is_reserved or addr.is_unspecified)


async def _resolve(host: str, port: int) -> list[str]:
    """Every address ``host`` resolves to."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


async def vetted_addresses(host: str, port: int, allow_private_hosts: frozenset[str]) -> list[str]:
    """The addresses to dial for ``host``: all it resolves to, each one public, or ``[host]`` itself (unresolved)
    when it is in ``allow_private_hosts``. Raises ``BlockedAddress`` when any address is not public."""
    if host.lower() in allow_private_hosts:
        return [host]
    addresses = await _resolve(host, port)
    if not addresses or not all(is_public_address(address) for address in addresses):
        raise BlockedAddress(f"{host} resolves to an address that is not public")
    return addresses


async def check_url(url: str, allow_private_hosts: frozenset[str]) -> None:
    """Raise ``BlockedAddress`` unless ``url`` is a URL the client may open (``url_problem``) whose host resolves
    only to public addresses (or is in ``allow_private_hosts``)."""
    problem = url_problem(url)
    if problem is not None:
        raise BlockedAddress(problem)
    parts = urlsplit(url)
    await vetted_addresses(parts.hostname or "", parts.port or 443, allow_private_hosts)


class _GuardedBackend(httpcore.AsyncNetworkBackend):
    """The connection pool's network backend: resolves and checks the host, then dials a checked address."""

    def __init__(self, inner: httpcore.AsyncNetworkBackend, allow_private_hosts: frozenset[str]) -> None:
        self._inner = inner
        self._allow = allow_private_hosts

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        async with asyncio.timeout(timeout):
            addresses = await vetted_addresses(host, port, self._allow)
        for address in addresses[:-1]:  # the next address when one cannot be reached (an IPv6 route that is missing)
            try:
                return await self._inner.connect_tcp(
                    address, port, timeout=timeout, local_address=local_address, socket_options=socket_options
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout):
                continue
        return await self._inner.connect_tcp(
            addresses[-1], port, timeout=timeout, local_address=local_address, socket_options=socket_options
        )

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Iterable[Any] | None = None
    ) -> httpcore.AsyncNetworkStream:
        raise BlockedAddress("a Unix socket is not an MCP server address")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def guarded_client(allow_private_hosts: frozenset[str]) -> httpx.AsyncClient:
    """An HTTP client whose every connection goes through ``_GuardedBackend``, that follows no redirect and reads
    no proxy from the environment. Fails closed (RuntimeError) when the installed httpx no longer exposes the pool's
    network backend."""
    transport = httpx.AsyncHTTPTransport(retries=0)
    pool = getattr(transport, "_pool", None)
    if pool is None or not hasattr(pool, "_network_backend"):
        raise RuntimeError("the MCP client's address guard cannot be installed on this httpx version")
    pool._network_backend = _GuardedBackend(pool._network_backend, allow_private_hosts)
    return httpx.AsyncClient(transport=transport, follow_redirects=False, trust_env=False, timeout=_HTTP_TIMEOUT)


@asynccontextmanager
async def open_session(url: str, allow_private_hosts: frozenset[str]) -> AsyncIterator[ClientSession]:
    """An initialized ``ClientSession`` with the server at ``url``, closed on exit. Enter and leave it in ONE task:
    the SDK's task groups require it."""
    await check_url(url, allow_private_hosts)
    async with guarded_client(allow_private_hosts) as client:
        async with streamable_http_client(url, http_client=client) as (read_stream, write_stream, _session_id):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session


async def list_all_tools(session: ClientSession) -> list[types.Tool]:
    """Every tool the server lists, across pages."""
    tools: list[types.Tool] = []
    cursor: str | None = None
    while True:
        params = types.PaginatedRequestParams(cursor=cursor) if cursor else None
        result = await session.list_tools(params=params)
        tools.extend(result.tools)
        cursor = result.nextCursor
        if not cursor:
            return tools
