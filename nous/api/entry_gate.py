"""Chat and the inbound MCP server require the owner's or the proxy's secret.

``NOUS_OWNER_ENTRY_MODE`` governs the chat routes (``POST /chat``, ``POST /chat/stream``, ``DELETE /chat/{session_id}``)
and the inbound MCP server at ``/mcp``:

- ``off``: no check.
- ``warn``: logs and counts calls that carry no proof, by session class (``claude``, ``pr-review``, ``background``,
  ``other`` or ``none``). ``daily_notice_loop`` tells the owner the count once a day and ``notice_at_shutdown`` at a
  graceful shutdown; the log lines are the record.
- ``enforce``: a call that carries no proof is refused before its body is read: 403, or 415 for a call with the
  proxy's secret alone whose media type is not exactly ``application/json`` (the owner guard's own rule).

On the chat routes the proof is the owner's or the proxy's secret (``OwnerGuard.grants(request, OWNER)``); a device
token is never one. On ``/mcp`` it is the owner's secret, or the MCP key (``NOUS_MCP_SERVER_SECRET_SHA256``) sent as
``Authorization: Bearer <key>`` or ``X-Nous-Mcp-Secret``; the proxy's secret is not one there.

A log line names the method (GET, POST, PUT, PATCH, DELETE, HEAD or OPTIONS; any other is ``?``), the route's
template, whether a secret header was absent, did not match or was the proxy's without JSON, and the session class.
Nothing else: never a header value, a client address, a session id or message text.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from nous.api.execution_context import BACKGROUND_SESSION_PREFIXES
from nous.api.owner_guard import (
    OWNER,
    OWNER_SECRET_HEADER,
    PROXY_SECRET_HEADER,
    OwnerGuard,
    owner_secret_matches,
    secret_digest,
)

__all__ = [
    "ENTRY_MODES",
    "MCP_SECRET_HEADER",
    "SESSION_CLASSES",
    "EntryGate",
    "session_class",
]

logger = logging.getLogger(__name__)

ENTRY_MODES = ("off", "warn", "enforce")
MCP_SECRET_HEADER = "X-Nous-Mcp-Secret"
SESSION_CLASSES = ("claude", "pr-review", "background", "other", "none")
LOG_INTERVAL_SECONDS = 600.0  # one line per (method, route, presentation, class) in this window; all are counted
NOTICE_INTERVAL_SECONDS = 86400.0
NOTICE_AT_SHUTDOWN_SECONDS = 5.0
_LOGGED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
_PENDING = "entry_pending"

Handler = Callable[[Request], Awaitable[Response]]


def session_class(session_id: object) -> str:
    """The class of a caller's session id, the only thing about it that is logged or counted: ``none`` (absent or
    empty), ``claude`` (``claude-``), ``pr-review`` (``pr-review-``), ``background`` (another prefix a background path
    mints) or ``other``. Any value is accepted; one that is not a string is ``other``."""
    if session_id is None or session_id == "":
        return "none"
    if not isinstance(session_id, str):
        return "other"
    if session_id.startswith("claude-"):
        return "claude"
    if session_id.startswith("pr-review-"):
        return "pr-review"
    if session_id.startswith(BACKGROUND_SESSION_PREFIXES):
        return "background"
    return "other"


class _Pending:
    """A call in ``warn`` that carried no proof, recorded once when its response is built."""

    __slots__ = ("session_class",)

    def __init__(self) -> None:
        self.session_class = "none"


def _method(scope: Scope) -> str:
    """The request's method when it is one of the seven common ones, ``WEBSOCKET`` for a WebSocket, else ``?``."""
    if scope["type"] == "websocket":
        return "WEBSOCKET"
    method = scope.get("method")
    return method if isinstance(method, str) and method in _LOGGED_METHODS else "?"


class EntryGate:
    """The rollout switch for the chat routes and the MCP server, over the owner guard's check of a secret."""

    def __init__(self, settings: Any, guard: OwnerGuard, *, clock: Callable[[], float] = time.monotonic) -> None:
        mode = getattr(settings, "owner_entry_mode", "warn")
        # Settings refuses any other value at start; on any other settings object it counts as enforce.
        self.mode: str = mode if isinstance(mode, str) and mode in ENTRY_MODES else "enforce"
        self._guard = guard
        self._owner_set = bool(secret_digest(getattr(settings, "owner_route_secret_sha256", "")))
        self._mcp = secret_digest(getattr(settings, "mcp_server_secret_sha256", ""))
        self._mcp_enabled = getattr(settings, "mcp_enabled", False) is True
        self._clock = clock
        self._counts: Counter[str] = Counter()
        self._last_logged: dict[tuple[str, str, str, str], float] = {}

    def startup_report(self) -> None:
        """One line naming the mode, and an ERROR for each entry point that refuses every call in ``enforce``."""
        if self.mode == "off":
            logger.warning("NOUS_OWNER_ENTRY_MODE=off: the chat routes and the MCP server check for no proof")
            return
        if self.mode == "warn":
            logger.warning(
                "NOUS_OWNER_ENTRY_MODE=warn: the chat routes and the MCP server log and count calls that carry no "
                "proof; enforce refuses them"
            )
            return
        logger.info("NOUS_OWNER_ENTRY_MODE=enforce: the chat routes and the MCP server require a secret")
        if not self._guard.configured:
            logger.error(
                "NOUS_OWNER_ENTRY_MODE=enforce and no owner or proxy digest is set: the chat routes refuse every call"
            )
        if self._mcp_enabled and not (self._owner_set or self._mcp):
            logger.error(
                "NOUS_OWNER_ENTRY_MODE=enforce and neither the owner digest nor NOUS_MCP_SERVER_SECRET_SHA256 is set: "
                "the MCP server refuses every call"
            )

    # ---- the chat routes -----------------------------------------------------------------------------------------

    def protect(self, handler: Handler, path: str) -> Handler:
        """``handler`` behind the switch. ``enforce`` is the owner guard's ``protect``; ``warn`` logs and counts a call
        that carries no proof once its response is built. ``path`` is the route's template."""
        enforced = self._guard.protect(handler, path)

        @functools.wraps(handler)
        async def gated(request: Request) -> Response:
            if self.mode == "off":
                return await handler(request)
            if self.mode != "warn":
                return await enforced(request)
            if await self._guard.grants(request, OWNER):
                return await handler(request)
            pending = _Pending()
            setattr(request.state, _PENDING, pending)
            try:
                return await handler(request)
            finally:
                self._record(request, path, pending.session_class, self._chat_presented(request))

        gated.entry_gated = True  # type: ignore[attr-defined]  # the route-table tests read it
        return gated

    def route(self, path: str, handler: Handler, *, methods: list[str]) -> Route:
        """A ``Route`` at ``path`` for ``handler`` behind ``protect``."""
        return Route(path, self.protect(handler, path), methods=methods)

    def note_session(self, request: Request, session_id: object) -> None:
        """Called by a chat handler once it has read the caller's session id: in ``warn``, a call that carries no proof
        is recorded under that id's class. No effect on any other call."""
        pending = getattr(request.state, _PENDING, None)
        if isinstance(pending, _Pending):
            pending.session_class = session_class(session_id)

    def _chat_presented(self, conn: HTTPConnection) -> str:
        if self._guard.proof(conn) == "proxy":  # type: ignore[arg-type]  # it reads the headers only
            return "the proxy's secret, not JSON"
        sent = any(name in conn.headers for name in (OWNER_SECRET_HEADER, PROXY_SECRET_HEADER))
        return "a wrong secret" if sent else "no secret header"

    # ---- the MCP server ------------------------------------------------------------------------------------------

    def asgi(self, app: ASGIApp, *, name: str) -> ASGIApp:
        """``app`` (the ``/mcp`` mount) behind the switch. Scopes other than HTTP and WebSocket pass through."""

        async def gated(scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] not in ("http", "websocket") or self.mode == "off":
                await app(scope, receive, send)
                return
            conn = HTTPConnection(scope)
            if self._mcp_proof(conn):
                await app(scope, receive, send)
                return
            if self.mode == "warn":
                self._record(conn, name, "none", self._mcp_presented(conn))
                await app(scope, receive, send)
                return
            logger.warning(
                "refused %s %s: no valid %s, %s or bearer key",
                _method(scope),
                name,
                OWNER_SECRET_HEADER,
                MCP_SECRET_HEADER,
            )
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            await JSONResponse({"error": "forbidden"}, status_code=403)(scope, receive, send)

        gated.entry_gated = True  # type: ignore[attr-defined]
        return gated

    def _mcp_proof(self, conn: HTTPConnection) -> bool:
        if self._guard.proof(conn) == "owner":  # type: ignore[arg-type]  # it reads the headers only
            return True
        if owner_secret_matches(conn.headers.get(MCP_SECRET_HEADER), self._mcp):
            return True
        scheme, _, key = (conn.headers.get("authorization") or "").partition(" ")
        return scheme.lower() == "bearer" and owner_secret_matches(key.strip(), self._mcp)

    @staticmethod
    def _mcp_presented(conn: HTTPConnection) -> str:
        names = (OWNER_SECRET_HEADER, MCP_SECRET_HEADER, "authorization", PROXY_SECRET_HEADER)
        return "a wrong secret" if any(name in conn.headers for name in names) else "no secret header"

    # ---- the count, the log and the daily notice -----------------------------------------------------------------

    def _record(self, conn: HTTPConnection, path: str, cls: str, presented: str) -> None:
        """Count a call that carried no proof, and log it at most once per (method, route, presentation, class) per
        interval."""
        self._counts[cls] += 1
        method = _method(conn.scope)
        key = (method, path, presented, cls)
        now = self._clock()
        last = self._last_logged.get(key)
        if last is not None and now - last < LOG_INTERVAL_SECONDS:
            return
        self._last_logged[key] = now
        logger.warning("owner entry: %s %s carried no proof (%s; session class %s)", method, path, presented, cls)

    def drain_counts(self) -> dict[str, int]:
        """The calls that carried no proof since the last drain, by session class; the count starts again."""
        counts, self._counts = dict(self._counts), Counter()
        return counts

    async def daily_notice_loop(
        self,
        send: Callable[[str], Awaitable[object]],
        *,
        interval: float = NOTICE_INTERVAL_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """In ``warn`` only: every ``interval`` seconds, send the owner one line with the count since the last line
        that was sent, when it is above zero. A failed send is logged by its class and the loop goes on. Returns at
        once in any other mode."""
        if self.mode != "warn":
            return
        while True:
            await sleep(interval)
            await self._send_count(send, "daily notice")

    async def notice_at_shutdown(
        self, send: Callable[[str], Awaitable[object]], *, timeout: float = NOTICE_AT_SHUTDOWN_SECONDS
    ) -> None:
        """In ``warn`` only, at a graceful shutdown: send the owner the count since the last line once, when it is
        above zero, waiting at most ``timeout`` seconds. A failure is logged by its class only."""
        if self.mode != "warn":
            return
        await self._send_count(send, "notice at shutdown", timeout=timeout)

    async def _send_count(
        self, send: Callable[[str], Awaitable[object]], what: str, *, timeout: float | None = None
    ) -> None:
        """Send the count so far when it is above zero. It is taken off only once the line went out: a send that
        fails, or is cancelled (a shutdown while the daily line is in flight), leaves it for the next line."""
        counts = dict(self._counts)
        total = sum(counts.values())
        if not total:
            return
        try:
            line = send(_notice_text(counts, total))
            sent = await (line if timeout is None else asyncio.wait_for(line, timeout))
        except Exception as exc:
            logger.warning("owner entry: the %s could not be sent (%s)", what, type(exc).__name__)
            return
        if sent is False:
            logger.warning("owner entry: the %s could not be sent", what)
            return
        self._counts.subtract(counts)
        self._counts = +self._counts  # calls counted while the line was in flight stay


def _notice_text(counts: dict[str, int], total: int) -> str:
    by_class = ", ".join(f"{cls}: {counts[cls]}" for cls in SESSION_CLASSES if counts.get(cls))
    noun = "call" if total == 1 else "calls"
    return (
        f"Nous: {total} {noun} to chat or the MCP server carried no proof since the last notice ({by_class}). "
        'The log lines ("owner entry") are the record. '
        "Set NOUS_OWNER_ENTRY_MODE=enforce once every known caller sends its secret."
    )
