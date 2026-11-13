"""F112 MCP client: the one module that imports the MCP SDK's client.

``open_session`` opens a streamable-HTTP ``ClientSession`` over an HTTP client that:

- connects only to public addresses: ``_GuardedBackend`` resolves the host when a connection is opened, refuses it
  when any address is private, loopback, link-local, multicast, unspecified, reserved or a cloud metadata address,
  and dials the address it checked, so a DNS answer that changes between the check and the connect cannot steer it
  (TLS still verifies the certificate against the host name). A host in ``allow_private_hosts`` is not checked;
- follows no redirect itself (``follow_redirects`` is off). The SDK (1.30) follows a redirect on its own only when it
  stays on the server URL's origin (same scheme, host and port) and keeps the method (307 or 308 for a POST); any
  other redirect fails the request. Each request it sends, a followed one included, opens its connection through
  the guard above;
- ignores proxy settings from the environment: through a proxy, the address checked would be the proxy's;
- stops reading a response body past ``NOUS_MCP_CLIENT_MAX_RESPONSE_BYTES`` (``_LimitedTransport``);
- with ``auth`` (the manager's ``_ServerAuth``), sets the server's stored credentials on each request it sends to
  the server URL's own origin, and on no other. Without it, the session has no authentication.

Logging: the HTTP client's INFO line names every request URL, so it is dropped for the hosts this client opened
(``_QuietMcpRequests`` on the ``httpx`` logger; httpcore logs only at DEBUG); the SDK's streamable-HTTP logger, which
logs the session id at INFO, is raised to WARNING; and the SDK's WARNING-and-above lines are rewritten to a fixed
text (``_WithholdServerText``), since they quote what a server sent. A secret in a URL's path is not supported.

SDK symbols used (``mcp`` 1.30): ``mcp.client.session.ClientSession`` (``initialize``, ``send_request``,
``list_tools(params=)``), ``mcp.client.streamable_http.streamable_http_client(url, http_client=)``,
``mcp.shared.exceptions.McpError`` and ``mcp.types`` (``ClientRequest``, ``CallToolRequest``,
``CallToolRequestParams``, ``CallToolResult``, ``PaginatedRequestParams``, ``Tool``, ``CONNECTION_CLOSED``).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import Any, cast
from urllib.parse import urlsplit

import httpcore
import httpx
import mcp
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError

from nous.mcp_client.bridge import ServerErrorReply
from nous.mcp_client.config import BlockedAddress, url_problem

_METADATA_ADDRESSES = frozenset({ipaddress.ip_address("169.254.169.254"), ipaddress.ip_address("fd00:ec2::254")})
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
# The SDK's own defaults: 30 s for a request, 5 minutes between events of a server-sent stream.
_HTTP_TIMEOUT = httpx.Timeout(30.0, read=300.0)
DEFAULT_MAX_RESPONSE_BYTES = 2_000_000  # NOUS_MCP_CLIENT_MAX_RESPONSE_BYTES's default
# The code the SDK (1.26) gives the error it makes when the server answers 404 to a session ("Session terminated").
_SESSION_TERMINATED = 32600
# Hosts this client opened a session with: their request lines are dropped from the httpx logger.
_QUIET_HOSTS: set[str] = set()


class _QuietMcpRequests(logging.Filter):
    """Drops the HTTP client's INFO request line (``HTTP Request: <method> <url> ...``) for a host in
    ``_QUIET_HOSTS``, in either form (``URL.host`` decodes an ``xn--`` host to Unicode; ``raw_host`` keeps it as
    sent). Other hosts' lines, which the rest of Nous keeps, pass."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) > 1 and isinstance(args[1], httpx.URL):
            url = args[1]
            hosts = {url.host.lower(), url.raw_host.decode("ascii", "replace").lower()}
            return not hosts & _QUIET_HOSTS
        return True


_MCP_PACKAGE_DIR = os.path.normcase(os.path.dirname(os.path.abspath(mcp.__file__))) + os.sep


class _WithholdServerText(logging.Filter):
    """Rewrites a WARNING-or-above line the MCP SDK writes to a fixed text: such lines quote what a server sent
    ("Failed to validate notification: ... Message was: ...", "Unknown SSE event: ..."), and a traceback can carry
    the URL. Records from other code pass unchanged. Attached to the SDK's client loggers (``mcp.client.
    streamable_http`` and ``client``, the SDK's name for ``mcp.client.session``) and to the root logger, which
    ``mcp.shared.session`` writes to directly; a filter on a logger sees only the records made on that logger."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING and os.path.normcase(record.pathname).startswith(_MCP_PACKAGE_DIR):
            record.msg = "MCP SDK %s at %s:%d (its text is withheld: it can quote what a server sent)"
            record.args = (record.levelname, record.module, record.lineno)
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


logging.getLogger("httpx").addFilter(_QuietMcpRequests())
logging.getLogger("mcp.client.streamable_http").setLevel(logging.WARNING)  # it logs the session id at INFO
for _logger_name in ("mcp.client.streamable_http", "client", ""):
    logging.getLogger(_logger_name).addFilter(_WithholdServerText())


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


class ResponseTooLarge(Exception):
    """A response body passed the client's limit (``NOUS_MCP_CLIENT_MAX_RESPONSE_BYTES``), or came compressed."""


class _LimitedStream(httpx.AsyncByteStream):
    """A response body that raises ``ResponseTooLarge`` once more than ``limit`` bytes have arrived."""

    def __init__(self, inner: httpx.AsyncByteStream, limit: int) -> None:
        self._inner = inner
        self._limit = limit

    async def __aiter__(self) -> AsyncIterator[bytes]:
        seen = 0
        async for chunk in self._inner:
            seen += len(chunk)
            if seen > self._limit:
                raise ResponseTooLarge(f"a response passed {self._limit} bytes")
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


class _LimitedTransport(httpx.AsyncBaseTransport):
    """Bounds every response body before anything parses it, for a JSON response and for an event stream alike
    (an event stream counts all its events). The client asks for uncompressed bodies, and a compressed one is
    refused, so the bytes counted are the bytes parsed."""

    def __init__(self, inner: httpx.AsyncBaseTransport, limit: int) -> None:
        self._inner = inner
        self._limit = limit

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        if response.headers.get("content-encoding", "identity").strip().lower() not in ("", "identity"):
            await response.aclose()
            raise ResponseTooLarge("the server compressed a response the client asked for uncompressed")
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            stream=_LimitedStream(cast(httpx.AsyncByteStream, response.stream), self._limit),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


def quiet_host(url: str) -> None:
    """Drop the HTTP client's request lines for ``url``'s host from now on (an OAuth endpoint's host, PR 2d)."""
    _QUIET_HOSTS.add((urlsplit(url).hostname or "").lower())


def guarded_client(
    allow_private_hosts: frozenset[str],
    max_response_bytes: int,
    *,
    auth: httpx.Auth | None = None,
    timeout: httpx.Timeout | float = _HTTP_TIMEOUT,
) -> httpx.AsyncClient:
    """An HTTP client whose every connection goes through ``_GuardedBackend``, whose every response body is bounded
    (``_LimitedTransport``), that follows no redirect itself (the SDK follows a same-origin one on its own, through
    this client), reads no proxy from the environment and runs ``auth`` on every request it sends. Fails closed
    (RuntimeError) when the installed httpx no longer exposes the pool's network backend. The OAuth client (PR 2d) is
    one of these with no ``auth`` and a 10 s ``timeout``."""
    transport = httpx.AsyncHTTPTransport(retries=0)
    pool = getattr(transport, "_pool", None)
    if pool is None or not hasattr(pool, "_network_backend"):
        raise RuntimeError("the MCP client's address guard cannot be installed on this httpx version")
    pool._network_backend = _GuardedBackend(pool._network_backend, allow_private_hosts)
    return httpx.AsyncClient(
        transport=_LimitedTransport(transport, max_response_bytes),
        follow_redirects=False,
        trust_env=False,
        timeout=timeout,
        headers={"Accept-Encoding": "identity"},
        auth=auth,
    )


def is_server_error(exc: BaseException) -> bool:
    """True when ``exc`` is a JSON-RPC error the server answered with: the call failed and the session is still
    good. False for a closed connection or a terminated session (the SDK reports both as ``McpError`` too) and for
    every other exception."""
    if not isinstance(exc, McpError):
        return False
    error = exc.error
    return error.code not in (types.CONNECTION_CLOSED, _SESSION_TERMINATED) and error.message != "Session terminated"


class McpSession:
    """An open session, reduced to the two requests the client makes."""

    def __init__(self, session: ClientSession) -> None:
        self._session = session

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        """``tools/call`` sent as a plain request. Not ``ClientSession.call_tool``: that checks a result's structured
        content against the server's output schema with jsonschema (listing the tools first when it has not), and a
        server's schema can name a ``$ref`` URL that would be fetched outside the address guard or a pattern that
        stalls the event loop. A JSON-RPC error from the server raises ``ServerErrorReply`` with its code."""
        request = types.ClientRequest(
            types.CallToolRequest(params=types.CallToolRequestParams(name=name, arguments=arguments))
        )
        try:
            return await self._session.send_request(request, types.CallToolResult)
        except McpError as exc:
            if is_server_error(exc):
                raise ServerErrorReply(exc.error.code) from None  # the server's message stays out
            raise

    async def list_tools(self) -> list[types.Tool]:
        """Every tool the server lists, across pages."""
        tools: list[types.Tool] = []
        cursor: str | None = None
        while True:
            params = types.PaginatedRequestParams(cursor=cursor) if cursor else None
            result = await self._session.list_tools(params=params)
            tools.extend(result.tools)
            cursor = result.nextCursor
            if not cursor:
                return tools


async def _end_on_transport_error(message: Any) -> None:
    """The session's message handler. A JSON response past the limit reaches it as ``ResponseTooLarge`` (the SDK
    cannot tie it to its request): raising it ends the session, so the calls waiting on it fail at once
    (``CONNECTION_CLOSED``) instead of at their timeout. Every other message is ignored, as by default: a late reply
    to a call that timed out also arrives here (an unknown request id), and must not end the session the other calls
    share. An event-stream response past the limit never reaches here (the SDK ends that stream quietly): its call
    fails at its timeout."""
    if isinstance(message, ResponseTooLarge):
        raise message


@asynccontextmanager
async def open_session(
    url: str,
    allow_private_hosts: frozenset[str],
    *,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    auth: httpx.Auth | None = None,
) -> AsyncIterator[McpSession]:
    """An initialized session with the server at ``url``, closed on exit, every request of it run through ``auth``
    (per request, not fixed at connect). Enter and leave it in ONE task: the SDK's task groups require it."""
    await check_url(url, allow_private_hosts)
    _QUIET_HOSTS.add((urlsplit(url).hostname or "").lower())
    async with guarded_client(allow_private_hosts, max_response_bytes, auth=auth) as client:
        async with _session_over(client, url) as session:
            yield session


@asynccontextmanager
async def _session_over(client: httpx.AsyncClient, url: str) -> AsyncIterator[McpSession]:
    """An initialized session with the server at ``url`` over ``client`` (``open_session`` passes the guarded one)."""
    async with streamable_http_client(url, http_client=client) as (read_stream, write_stream, _session_id):
        async with ClientSession(read_stream, write_stream, message_handler=_end_on_transport_error) as session:
            await session.initialize()
            yield McpSession(session)
