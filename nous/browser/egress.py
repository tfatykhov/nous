"""Egress proxy for browser_task: the browser's network boundary (F100).

Chromium sends every request through this proxy, loopback included (`--proxy-server`,
`--proxy-bypass-list=<-loopback>`). For each request the proxy:

1. checks the host against the task's allowlist;
2. resolves the host once;
3. refuses if ANY answer is not a public address (loopback, RFC 1918, CGNAT, link-local, reserved,
   documentation, multicast), IPv4-mapped IPv6 unwrapped first;
4. connects to an address from that answer itself, never to the name again, so a DNS rebind cannot move
   the connection after the check.

A refusal answers 403 and is counted. Handled: `CONNECT host:port` (https, wss) and plain http requests in
absolute form (`GET http://host/path`); anything else answers 405. Each plain http connection carries one
request, so a reused proxy connection can never carry a request for another host. Logs name the host and
the reason, never the path or query.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from collections.abc import Callable
from typing import Any, NoReturn, Protocol
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

HEAD_LIMIT = 64 * 1024
HEAD_TIMEOUT = 30.0
RESOLVE_TIMEOUT = 10.0
CONNECT_TIMEOUT = 10.0
CLOSE_TIMEOUT = 5.0
_CHUNK = 64 * 1024
# Hop-by-hop and proxy-only headers: never forwarded to the origin.
_HOP_BY_HOP = frozenset(
    {"connection", "keep-alive", "proxy-connection", "proxy-authorization", "te", "trailer", "upgrade"}
)
_REFUSED_BODY = b"Blocked by the browser_task egress proxy."
_PORT = re.compile(r"[0-9]{1,5}")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _permitted_address(host: str, address: IPAddress) -> bool:
    """The address rule: only globally routable unicast addresses (`is_global` alone admits multicast).
    Tests replace this function to reach a local server; no setting, tool argument or environment variable
    can change it."""
    if isinstance(address, ipaddress.IPv6Address) and address in _NAT64:
        return False  # a NAT64 gateway would turn it into any IPv4 address, private ones included
    return address.is_global and not address.is_multicast


def address_permitted(host: str, address: IPAddress) -> bool:
    """Whether the browser may connect to `address` (an answer for `host`)."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return _permitted_address(host, address)


async def _getaddrinfo(host: str, port: int) -> list[Any]:
    return await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)


async def _open_connection(address: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection(address, port)


async def resolve(host: str, port: int = 443) -> list[IPAddress]:
    """Every address `host` resolves to, in the resolver's order. Raises OSError when it does not resolve."""
    addresses: list[IPAddress] = []
    for *_, sockaddr in await _getaddrinfo(host, port):
        address = ipaddress.ip_address(sockaddr[0])
        if address not in addresses:
            addresses.append(address)
    return addresses


class Allowlist(Protocol):
    def allows(self, url: str) -> bool: ...


class _Refused(Exception):
    """The request is not forwarded; `status` and the reason are what the client and the log get."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status


class EgressProxy:
    """One browser task's proxy on 127.0.0.1 (a free port). `on_refuse` runs once per refused request."""

    def __init__(self, allowlist: Allowlist, on_refuse: Callable[[], None] | None = None) -> None:
        self.allowlist = allowlist
        self.on_refuse = on_refuse
        self.refused = 0
        self.port = 0
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task[Any]] = set()

    async def start(self, port: int = 0) -> int:
        """Listen on 127.0.0.1:`port` (0: any free port). Returns the port."""
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", port, limit=HEAD_LIMIT)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def close(self) -> None:
        """Stop listening and cut every open connection, tunnels included."""
        server, self._server = self._server, None
        if server is None:
            return
        server.close()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await asyncio.wait_for(server.wait_closed(), CLOSE_TIMEOUT)
        except TimeoutError:
            logger.warning("browser_task egress: the proxy did not close in time")

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        upstream: asyncio.StreamWriter | None = None
        try:
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError, ConnectionError):
                return
            request_line, *header_lines = head[:-4].decode("latin-1").split("\r\n")
            parts = request_line.split(" ")
            try:
                if len(parts) != 3 or not parts[2].startswith("HTTP/"):
                    raise _Refused(400, "malformed request line")
                method, target, version = parts
                if method == "CONNECT":
                    up_reader, upstream = await self._open(*_connect_target(target), f"https://{target}/")
                    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    await writer.drain()
                    await _tunnel(reader, writer, up_reader, upstream)
                elif target.lower().startswith("http://"):
                    host, port, origin_target = _absolute_target(target)
                    length = _body_length(header_lines)
                    up_reader, upstream = await self._open(host, port, target)
                    upstream.write(_origin_request(method, origin_target, version, header_lines))
                    await _copy(reader, upstream, length)
                    await _pipe(up_reader, writer)
                else:
                    raise _Refused(405, "only CONNECT and absolute http requests are proxied")
            except _Refused as refusal:
                body = _REFUSED_BODY if refusal.status == 403 else str(refusal).encode()
                await _reply(writer, refusal.status, body)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception:
            logger.warning("browser_task egress: request handling failed", exc_info=True)
        finally:
            if task is not None:
                self._tasks.discard(task)
            for w in (upstream, writer):
                if w is not None:
                    w.close()

    async def _open(self, host: str, port: int, url: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Check `host` (allowlist, then every resolved address) and connect to a checked address."""
        if not self.allowlist.allows(url):
            self._refuse(host, "not on the allowlist")
        try:
            addresses = await asyncio.wait_for(resolve(host, port), RESOLVE_TIMEOUT)
        except (OSError, TimeoutError, ValueError):
            addresses = []
        if not addresses:
            logger.info("browser_task egress: %s does not resolve", host)
            raise _Refused(502, "the host does not resolve")
        private = [a for a in addresses if not address_permitted(host, a)]
        if private:
            self._refuse(host, f"resolves to a non-public address ({private[0]})")
        for address in addresses:
            try:
                return await asyncio.wait_for(_open_connection(str(address), port), CONNECT_TIMEOUT)
            except (OSError, TimeoutError):
                continue
        raise _Refused(502, "the host could not be reached")

    def _refuse(self, host: str, reason: str) -> NoReturn:
        self.refused += 1
        if self.on_refuse is not None:
            self.on_refuse()
        logger.info("browser_task egress: refused %s: %s", host, reason)
        raise _Refused(403, reason)


def _connect_target(target: str) -> tuple[str, int]:
    """`name:port` or `[v6]:port`, nothing else."""
    if target.startswith("["):
        host, sep, port = target[1:].partition("]:")
    else:
        host, sep, port = target.rpartition(":")
    bad_host = not host or (":" in host and not target.startswith("["))
    if not sep or bad_host or not _PORT.fullmatch(port) or not 0 < int(port) < 65536:
        raise _Refused(400, "malformed CONNECT target")
    return host, int(port)


def _absolute_target(target: str) -> tuple[str, int, str]:
    try:
        parts = urlsplit(target)
        port = parts.port or 80
    except ValueError:
        raise _Refused(400, "malformed request target") from None
    if parts.scheme.lower() != "http" or not parts.hostname:
        raise _Refused(400, "malformed request target")
    path = parts.path or "/"
    return parts.hostname, port, f"{path}?{parts.query}" if parts.query else path


def _header(lines: list[str], name: str) -> str | None:
    for line in lines:
        key, sep, value = line.partition(":")
        if sep and key.strip().lower() == name:
            return value.strip()
    return None


def _body_length(lines: list[str]) -> int:
    if _header(lines, "transfer-encoding") is not None:
        raise _Refused(411, "chunked request bodies are not proxied")
    value = _header(lines, "content-length") or "0"
    if not re.fullmatch(r"[0-9]{1,15}", value):
        raise _Refused(400, "bad Content-Length")
    return int(value)


def _origin_request(method: str, target: str, version: str, lines: list[str]) -> bytes:
    kept = [line for line in lines if line.partition(":")[0].strip().lower() not in _HOP_BY_HOP]
    return "\r\n".join([f"{method} {target} {version}", *kept, "Connection: close", "", ""]).encode("latin-1")


async def _reply(writer: asyncio.StreamWriter, status: int, body: bytes) -> None:
    reason = {400: "Bad Request", 403: "Forbidden", 405: "Method Not Allowed", 411: "Length Required"}.get(
        status, "Bad Gateway"
    )
    head = f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\n"
    writer.write(head.encode() + b"Connection: close\r\n\r\n" + body)
    try:
        await writer.drain()
    except ConnectionError:
        pass


async def _copy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, length: int) -> None:
    while length > 0:
        data = await reader.read(min(length, _CHUNK))
        if not data:
            raise ConnectionError("the client closed before sending its request body")
        writer.write(data)
        await writer.drain()
        length -= len(data)


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while data := await reader.read(_CHUNK):
        writer.write(data)
        await writer.drain()


async def _tunnel(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
) -> None:
    """Relay both ways until either side closes."""
    pipes = [
        asyncio.ensure_future(_pipe(client_reader, upstream_writer)),
        asyncio.ensure_future(_pipe(upstream_reader, client_writer)),
    ]
    try:
        await asyncio.wait(pipes, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for pipe in pipes:
            pipe.cancel()
        await asyncio.gather(*pipes, return_exceptions=True)
