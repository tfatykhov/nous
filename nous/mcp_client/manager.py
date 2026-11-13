"""F112 MCP client: registers the approved tools of external MCP servers, and runs their calls.

``start`` registers, from the stored rows and in sorted ``(server, tool)`` order, every tool whose permission is
``allow`` (at most ``TOOLS_PER_SERVER`` per server), and connects to no server: the tool array sits at the front of
the prompt-cache prefix, so it must not depend on which server answered at boot, and a server that is down must not
delay the boot. A tool stays registered while its server is down; its calls answer with an error.

The pin (``NOUS_MCP_CLIENT_PINNED_SERVERS``, set on the host, ``name=host[:port];tool|tool``): a stored tool
registers only when its server's name is pinned, the stored URL's host (and its port, when the URL names one) is the
pinned one, the tool's own name is in the pin's list and its stored permission is ``allow``. Background work may use
a server's tools only when ``NOUS_MCP_CLIENT_BACKGROUND_SERVERS`` (also on the host) names it AND its row's
background switch is on. The rows can be written by anything that reaches the database, the agent's own shell
included; the pin, outside the database, keeps such a row from adding a server, a tool or background use at the next
boot. It does not cover the URL's path, the descriptions and schemas stored for a pinned tool (a changed row is
registered as stored, cleaned and checked by ``prepare_tool``), or code the agent runs inside this process.

A server's connection is opened by the first call that needs it. One supervisor task per connection owns the SDK's
context managers (their task groups must be entered and left in one task); a call only waits for the session and
uses it, so a call cancelled by the runner's tool timeout leaves the connection as it was. A call also ends as soon
as its connection does (``_unless_ended``): when the transport fails mid-session (a 502, a reset), the SDK can leave
the call waiting until its timeout. The handshake (opening the session and ``initialize``) has its own bound, the
server's call timeout but at most ``HANDSHAKE_MAX_SECONDS``: the SDK can wait forever for an initialize reply that
never comes (an event stream that ends without it, or one past the response limit), and a handshake past the bound
ends that connection attempt, so a later call makes a new one. A failed connect is not tried again for a while (1 s,
doubling, at most 60 s). A JSON-RPC error from the server fails the call and keeps the session; any other failure
but the timeout drops the connection, and the next call opens a new one. A call is never retried.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any, Protocol

from nous.api.tool_classes import forget_mcp_tool, note_mcp_tool
from nous.config import Settings
from nous.events import Event, EventBus
from nous.mcp_client.bridge import (
    TOOLS_PER_SERVER,
    ServerErrorReply,
    error_class,
    flatten_result,
    prepare_tool,
    safe_error,
    wrap_result,
)
from nous.mcp_client.config import ServerConfig, ToolConfig, parse_host_list, parse_pins, pin_host

logger = logging.getLogger(__name__)

CALLS_PER_SERVER = 4  # calls in flight to one server at a time
_BACKOFF_FIRST_SECONDS = 1.0
_BACKOFF_MAX_SECONDS = 60.0
_CLOSE_WAIT_SECONDS = 5.0
HANDSHAKE_MAX_SECONDS = 30  # the handshake's bound is the server's call timeout, but never more than this

# (url, allow_private_hosts) -> an async context manager yielding an initialized session (transport.open_session).
Connector = Callable[[str, frozenset[str]], AbstractAsyncContextManager[Any]]


class Registrar(Protocol):
    """The part of ``ToolDispatcher`` the manager uses."""

    def is_registered(self, name: str) -> bool: ...

    def register(self, name: str, handler: Callable[..., Any], schema: dict[str, Any]) -> None: ...

    def unregister(self, name: str) -> bool: ...


class Registry(Protocol):
    """The part of ``McpRegistry`` the manager uses."""

    async def list_enabled(self) -> list[tuple[ServerConfig, list[ToolConfig]]]: ...


class ServerUnavailable(Exception):
    """A server whose last connect failed is not tried again yet, or its connection closed before it was ready."""


class ClientClosed(Exception):
    """The manager was closed: no connection is opened any more."""


class ConnectionEnded(Exception):
    """The connection a call was using ended before the call had its answer."""


class HandshakeTimedOut(Exception):
    """Opening the session and ``initialize`` did not finish within the handshake's bound."""


async def _unless_ended(ended: asyncio.Event, make_call: Callable[[], Awaitable[Any]]) -> Any:
    """``make_call()``'s result, or ``ConnectionEnded`` as soon as ``ended`` is set. When the transport fails
    mid-session the SDK can end the session without answering the calls waiting on it; this ends them with it. A
    connection that has already ended (closed or dropped while the call waited for it) gets no request at all."""
    if ended.is_set():
        raise ConnectionEnded()
    call_task = asyncio.ensure_future(make_call())
    call_task.add_done_callback(lambda task: task.cancelled() or task.exception())
    end_task = asyncio.ensure_future(ended.wait())
    try:
        await asyncio.wait({call_task, end_task}, return_when=asyncio.FIRST_COMPLETED)
        if call_task.done():
            return call_task.result()
        raise ConnectionEnded()
    finally:
        call_task.cancel()  # nothing when it is done
        end_task.cancel()


@dataclass
class _Connection:
    """One attempt at a session: its supervisor task, the session once ready, and the signal that ends it."""

    ready: asyncio.Future[Any]
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    session: Any = None

    @property
    def closed(self) -> bool:
        return self.stop.is_set() or self.task is None or self.task.done()


class _Server:
    """One configured server: its call limit and its current connection."""

    def __init__(
        self,
        config: ServerConfig,
        connect: Connector,
        allow: frozenset[str],
        on_error: Callable[[str, BaseException], Awaitable[None]],
        background: bool,
    ) -> None:
        self.config = config
        self.background = background  # the row's switch AND the host's NOUS_MCP_CLIENT_BACKGROUND_SERVERS
        self.closed = False
        self.semaphore = asyncio.Semaphore(CALLS_PER_SERVER)
        self._connect = connect
        self._allow = allow
        self._on_error = on_error
        self._conn: _Connection | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._failures = 0
        self._retry_at = 0.0

    @property
    def connected(self) -> bool:
        conn = self._conn
        return (
            conn is not None
            and not conn.closed
            and conn.ready.done()
            and not conn.ready.cancelled()
            and conn.ready.exception() is None
        )

    async def connection(self) -> _Connection:
        """The open connection (its ``session`` set), connecting first when there is none. Raises ``ClientClosed``
        once the server is closed, ``ServerUnavailable`` while a failed connect is backing off, and the connect's own
        error when it fails now."""
        if self.closed:
            raise ClientClosed()
        conn = self._conn
        if conn is None or conn.closed:
            if asyncio.get_running_loop().time() < self._retry_at:
                raise ServerUnavailable()
            conn = self._conn = self._open()
        # Shielded: a caller that is cancelled or times out leaves the connect running for the next call.
        await asyncio.shield(conn.ready)
        if self.closed:  # closed while the connect was in flight: nothing is sent on it
            raise ClientClosed()
        return conn

    def close(self) -> None:
        """No new connection from now on, and the current one ends."""
        self.closed = True
        self.drop()

    def drop(self, session: Any = None) -> None:
        """End the current connection (its supervisor leaves the session's context); the next call opens one. With
        ``session``, only while the current connection is the one holding it: a call that failed on an older session
        does not end a newer one."""
        conn = self._conn
        if conn is not None and (session is None or conn.session is session):
            conn.stop.set()

    def tasks(self) -> set[asyncio.Task[None]]:
        """The supervisor tasks still running, the closing ones included."""
        return set(self._tasks)

    def _open(self) -> _Connection:
        conn = _Connection(ready=asyncio.get_running_loop().create_future())
        # Mark an error retrieved even when every waiter was cancelled, so asyncio logs nothing about it.
        conn.ready.add_done_callback(lambda f: f.cancelled() or f.exception())
        conn.task = asyncio.create_task(self._run(conn), name=f"mcp-client-{self.config.name}")
        self._tasks.add(conn.task)
        conn.task.add_done_callback(self._tasks.discard)
        return conn

    async def _run(self, conn: _Connection) -> None:
        name = self.config.name
        handshake = min(self.config.call_timeout_seconds, HANDSHAKE_MAX_SECONDS)
        try:
            async with contextlib.AsyncExitStack() as stack:
                try:
                    async with asyncio.timeout(handshake):
                        session = await stack.enter_async_context(self._connect(self.config.url, self._allow))
                except TimeoutError:
                    raise HandshakeTimedOut() from None
                self._failures = 0
                conn.session = session
                conn.ready.set_result(session)
                await conn.stop.wait()
        except Exception as exc:  # an ExceptionGroup from the SDK's task groups included; CancelledError passes
            # The class only: an HTTP error's text carries the URL.
            if not conn.ready.done():
                self._failures += 1
                backoff = min(_BACKOFF_MAX_SECONDS, _BACKOFF_FIRST_SECONDS * 2 ** (self._failures - 1))
                self._retry_at = asyncio.get_running_loop().time() + backoff
                conn.ready.set_exception(exc)
                logger.warning("MCP client: could not connect to server %r (%s)", name, error_class(exc))
            else:
                logger.warning("MCP client: the connection to server %r ended (%s)", name, error_class(exc))
            logger.debug("MCP client: server %r error", name, exc_info=True)
            await self._on_error(name, exc)
        finally:
            conn.stop.set()
            if not conn.ready.done():
                conn.ready.set_exception(ServerUnavailable())


class McpClientManager:
    """The MCP client: one per process, built in ``create_components`` when ``NOUS_MCP_CLIENT_ENABLED`` is on."""

    def __init__(
        self,
        registrar: Registrar,
        registry: Registry,
        settings: Settings,
        bus: EventBus | None = None,
        *,
        connect: Connector | None = None,
    ) -> None:
        if connect is None:
            from nous.mcp_client.transport import open_session  # the SDK is imported only when the client runs

            connect = functools.partial(open_session, max_response_bytes=settings.mcp_client_max_response_bytes)
        self._registrar = registrar
        self._registry = registry
        self._bus = bus
        self._agent_id = settings.agent_id
        self._max_chars = settings.mcp_client_max_result_chars
        self._allow = parse_host_list(settings.mcp_client_allow_private_hosts)
        self._pins = parse_pins(settings.mcp_client_pinned_servers)
        self._background_servers = parse_host_list(settings.mcp_client_background_servers)
        self._connect = connect
        self._servers: dict[str, _Server] = {}
        self._registered: dict[str, str] = {}  # tool name -> server name
        self._closed = False

    def registered(self) -> list[str]:
        """The names of the tools this manager has registered, in registration order."""
        return list(self._registered)

    def _pinned_tools(self, config: ServerConfig, tools: list[ToolConfig]) -> list[ToolConfig] | None:
        """The server's tools the host's pin lets register (stored ``allow`` and named in the pin), or None when the
        server itself is not pinned with its stored host and port. Logs why, by the server's name only."""
        pin = self._pins.get(config.name)
        if pin is None:
            logger.warning("MCP client: server %r is not pinned (NOUS_MCP_CLIENT_PINNED_SERVERS); skipped", config.name)
            return None
        if pin_host(config.url) != pin.host:
            logger.warning("MCP client: server %r: its stored host or port is not its pinned one; skipped", config.name)
            return None
        allowed = [tool for tool in tools if tool.permission == "allow"]
        pinned = [tool for tool in allowed if tool.tool in pin.tools]
        if len(pinned) < len(allowed):
            logger.warning(
                "MCP client: server %r: %d allowed tool(s) are not in its pin; skipped",
                config.name,
                len(allowed) - len(pinned),
            )
        return pinned

    async def start(self) -> None:
        """Register every pinned, allowed tool of every enabled, pinned server (``_pinned_tools``), from the stored
        rows, in sorted (server, tool) order, as ``prepare_tool`` builds it. Connects to no server. A tool that
        ``prepare_tool`` refuses, that collides with a registered tool, or past a server's first ``TOOLS_PER_SERVER``
        is skipped and logged by name. Ends with one ``mcp_client_started`` event and an INFO line naming the servers
        (with their background use) and tools registered, never a URL. A failure part way logs what was registered
        before it and is raised."""
        entries: list[tuple[str, str, ToolConfig]] = []
        for config, tools in await self._registry.list_enabled():
            pinned = self._pinned_tools(config, tools)
            if pinned is None:
                continue
            background = config.background and config.name in self._background_servers
            self._servers[config.name] = _Server(config, self._connect, self._allow, self._emit_error, background)
            entries += [(config.name, tool.tool, tool) for tool in pinned]
        over_cap: dict[str, int] = {}
        try:
            for server_name, raw_name, tool in sorted(entries, key=lambda entry: (entry[0], entry[1])):
                server = self._servers[server_name]
                prepared = prepare_tool(server_name, raw_name, tool.description, tool.input_schema)
                if prepared.problem is not None or prepared.name is None or prepared.schema is None:
                    logger.warning(
                        "MCP client: skipped %r of server %r: %s", raw_name[:80], server_name, prepared.problem
                    )
                    continue
                if self._registrar.is_registered(prepared.name):
                    logger.warning("MCP client: skipped %s: a tool of that name is already registered", prepared.name)
                    continue
                if sum(1 for owner in self._registered.values() if owner == server_name) >= TOOLS_PER_SERVER:
                    over_cap[server_name] = over_cap.get(server_name, 0) + 1
                    continue
                self._registrar.register(prepared.name, self._handler(server, raw_name, prepared.name), prepared.schema)
                note_mcp_tool(prepared.name, background=server.background)
                self._registered[prepared.name] = server_name
        except Exception:
            logger.warning(
                "MCP client: start stopped after registering %d tool(s): %s",
                len(self._registered),
                ", ".join(self._registered) or "none",
            )
            raise
        for server_name, skipped in sorted(over_cap.items()):
            logger.warning(
                "MCP client: skipped %d tool(s) of server %r: over the %d-tool cap",
                skipped,
                server_name,
                TOOLS_PER_SERVER,
            )
        servers = [
            {"name": name, "background": self._servers[name].background}
            for name in sorted(set(self._registered.values()))
        ]
        logger.info(
            "MCP client: %d tool(s) registered from server(s) %s: %s",
            len(self._registered),
            ", ".join(f"{s['name']} (background {'on' if s['background'] else 'off'})" for s in servers) or "none",
            ", ".join(self._registered) or "none",
        )
        await self._emit("mcp_client_started", {"servers": servers, "tools": list(self._registered)})

    async def close(self) -> None:
        """Unregister every tool this manager registered and close every connection (bounded wait, then cancel). A
        handler called after this answers with an error and opens no connection."""
        self._closed = True
        for name in self._registered:
            self._registrar.unregister(name)
            forget_mcp_tool(name)
        self._registered.clear()
        tasks: set[asyncio.Task[None]] = set()
        for server in self._servers.values():
            server.close()
            tasks |= server.tasks()
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=_CLOSE_WAIT_SECONDS)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    def status(self) -> dict[str, Any]:
        """Each server's name, whether it is connected, its background switch and its registered tool count. No
        URL."""
        counts: dict[str, int] = {}
        for server_name in self._registered.values():
            counts[server_name] = counts.get(server_name, 0) + 1
        return {
            "servers": [
                {
                    "name": name,
                    "connected": server.connected,
                    "background": server.background,
                    "tools": counts.get(name, 0),
                }
                for name, server in sorted(self._servers.items())
            ]
        }

    def _handler(self, server: _Server, raw_name: str, name: str) -> Callable[..., Awaitable[dict[str, Any]]]:
        async def call(**arguments: Any) -> dict[str, Any]:
            return await self._call(server, raw_name, name, arguments)

        return call

    async def _call(self, server: _Server, raw_name: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """One call, as the dispatcher's result. Never raises but ``CancelledError``, which it re-raises: the
        runner's tool timeout, or a shutdown."""
        server_name = server.config.name
        closed = _error(f'the MCP client is closed: the server "{server_name}" is not called.')
        if self._closed:
            return closed
        timeout = server.config.call_timeout_seconds
        session: Any = None
        try:
            # One budget for the wait for a call slot, the connect and the call.
            async with asyncio.timeout(timeout):
                async with server.semaphore:
                    if self._closed:  # closed while this call waited for a slot: open nothing
                        return closed
                    conn = await server.connection()  # raises ClientClosed when closed meanwhile
                    session = conn.session
                    # The server's own name for the tool; the call ends with its connection.
                    raw = await _unless_ended(conn.stop, lambda: session.call_tool(raw_name, arguments))
        except ClientClosed:
            return closed
        except HandshakeTimedOut:
            bound = min(timeout, HANDSHAKE_MAX_SECONDS)
            return _error(f'the MCP server "{server_name}" did not finish its handshake within {bound} s: try again.')
        except ConnectionEnded:
            logger.warning("MCP client: %s failed: its connection ended during the call", name)
            return _error(f'the connection to the MCP server "{server_name}" ended during the call.')
        except TimeoutError:
            logger.warning("MCP client: %s timed out after %d s", name, timeout)
            return _error(f'the call to the MCP server "{server_name}" timed out after {timeout} s.')
        except ServerUnavailable:
            return _error(f'the MCP server "{server_name}" is not reachable now: try again later.')
        except ServerErrorReply as exc:
            # The server answered: the session is still good, and the calls sharing it go on.
            logger.warning("MCP client: %s failed: the server answered with error code %d", name, exc.code)
            return _error(f'the MCP server "{server_name}" answered the call with an error (code {exc.code}).')
        except Exception as exc:
            # The session is in an unknown state (the server may have dropped it): the next call connects again. A
            # failed connect left no session to end.
            if session is not None:
                server.drop(session)
            logger.warning("MCP client: %s failed (%s)", name, error_class(exc))
            logger.debug("MCP client: %s error", name, exc_info=True)
            return _error(f'the call to the MCP server "{server_name}" failed: {safe_error(exc)}.')
        text, is_error = flatten_result(raw, self._max_chars)
        return {"content": [{"type": "text", "text": wrap_result(server_name, text)}], "is_error": is_error}

    async def _emit_error(self, server_name: str, exc: BaseException) -> None:
        """An ``mcp_client_server_error`` event: the server's name and the error's class, never its text."""
        await self._emit("mcp_client_server_error", {"server": server_name, "error": error_class(exc)})

    async def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        """One event on the bus, when there is one. Never a URL: the agent's reader can read the events table."""
        if self._bus is None:
            return
        try:
            await self._bus.emit(Event(type=event_type, agent_id=self._agent_id, data=data))
        except Exception:
            logger.debug("MCP client: event not emitted", exc_info=True)


def _error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"Tool error: {message}"}], "is_error": True}
