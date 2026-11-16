"""Test support for fetching model-supplied URLs: a mock transport that streams, and a patch for the client factory.

``httpx.MockTransport`` hands back responses whose body is already loaded, but the fetch reads its body as a stream,
so the helpers here turn every mocked response into a streamed one. No helper touches the network.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from unittest.mock import patch

import httpx

import nous.api.safe_fetch as sf


def stream_transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    """A mock transport whose responses arrive as streams."""

    def streamed(request: httpx.Request) -> httpx.Response:
        response = handler(request)
        return httpx.Response(
            response.status_code, headers=response.headers, stream=httpx.ByteStream(response.content), request=request
        )

    return httpx.MockTransport(streamed)


class FetchLog:
    """What a patched fetch saw: the requests, and the clients built and closed."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.clients: list[httpx.AsyncClient] = []

    @property
    def closed(self) -> list[bool]:
        return [client.is_closed for client in self.clients]


@contextlib.contextmanager
def patched_fetch_client(handler: Callable[[httpx.Request], httpx.Response]) -> Iterator[FetchLog]:
    """Make ``fetch_checked`` build its clients on a mock transport that answers with ``handler``."""
    log = FetchLog()

    def answer(request: httpx.Request) -> httpx.Response:
        log.requests.append(request)
        return handler(request)

    real = sf.make_fetch_client

    def make(**kwargs: object) -> httpx.AsyncClient:
        client = real(transport=stream_transport(answer), **kwargs)
        log.clients.append(client)
        return client

    with patch.object(sf, "make_fetch_client", make):
        yield log


# ---------------------------------------------------------------------------
# A fake network under the real httpx / httpcore stack: connections, TLS server names, raw responses
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402

import httpcore  # noqa: E402


def http_response(status: int = 200, headers: dict[str, str] | None = None, body: bytes = b"") -> bytes:
    """The bytes of an HTTP/1.1 response (Content-Length is added unless a header gives the framing)."""
    lines = [f"HTTP/1.1 {status} X"]
    given = {k.lower(): v for k, v in (headers or {}).items()}
    for key, value in (headers or {}).items():
        lines.append(f"{key}: {value}")
    if "content-length" not in given and "transfer-encoding" not in given:
        lines.append(f"Content-Length: {len(body)}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


def parse_head(head: str) -> tuple[str, dict[str, str]]:
    """The request line and the headers (lower-cased names) of a request head."""
    first, *rest = head.split("\r\n")
    return first, {k.lower(): v for k, v in (line.split(": ", 1) for line in rest if ": " in line)}


class _FakeStream(httpcore.AsyncNetworkStream):
    def __init__(self, backend: FakeServerBackend, address: str) -> None:
        self.backend, self.address = backend, address
        self._in = b""
        self._out = b""
        self.closed = False

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        if self._out:
            size = 1 if self.backend.drip_seconds else max_bytes
            chunk, self._out = self._out[:size], self._out[size:]
            if self.backend.drip_seconds:
                await asyncio.sleep(self.backend.drip_seconds)
            return chunk
        if self.backend.hang:
            await asyncio.sleep(3600)
        return b""

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._in += buffer
        while b"\r\n\r\n" in self._in:
            head, _, self._in = self._in.partition(b"\r\n\r\n")
            request_line, headers = parse_head(head.decode("latin-1"))
            self.backend.requests.append((self.address, request_line, headers))
            self._out += self.backend.respond(self.address, request_line, headers)

    async def aclose(self) -> None:
        self.closed = True

    async def start_tls(
        self, ssl_context: object, server_hostname: str | None = None, timeout: float | None = None
    ) -> httpcore.AsyncNetworkStream:
        self.backend.handshakes.append((self.address, server_hostname))
        return self

    def get_extra_info(self, info: str) -> object:
        return None


class FakeServerBackend(httpcore.AsyncNetworkBackend):
    """A network backend that serves ``respond(address, request_line, headers) -> bytes`` and records what happens."""

    def __init__(
        self,
        respond: Callable[[str, str, dict[str, str]], bytes],
        *,
        failing: frozenset[str] = frozenset(),
        hang: bool = False,
        drip_seconds: float = 0.0,
    ) -> None:
        self.respond = respond
        self.failing = failing
        self.hang = hang
        self.drip_seconds = drip_seconds
        self.connections: list[tuple[str, int]] = []
        self.handshakes: list[tuple[str, str | None]] = []
        self.requests: list[tuple[str, str, dict[str, str]]] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: object = None,
    ) -> httpcore.AsyncNetworkStream:
        self.connections.append((host, port))
        if host in self.failing:
            raise httpcore.ConnectError(f"connection to {host} refused")
        return _FakeStream(self, host)

    async def connect_unix_socket(self, *args: object, **kwargs: object) -> httpcore.AsyncNetworkStream:
        raise NotImplementedError

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


@contextlib.contextmanager
def fake_network(backend: FakeServerBackend) -> Iterator[FakeServerBackend]:
    """Every client built while this is active connects through ``backend`` (real httpx and httpcore above it)."""
    with patch("httpcore._async.connection_pool.AutoBackend", lambda: backend):
        yield backend
