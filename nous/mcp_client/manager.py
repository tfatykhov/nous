"""F112 MCP client: registers the approved tools of external MCP servers, and runs their calls.

``start`` registers, from the stored rows and in sorted ``(server, tool)`` order, every tool whose permission is
``allow``, and connects to no server: the tool array sits at the front of the prompt-cache prefix, so it must not
depend on which server answered at boot, and a server that is down must not delay the boot. A tool stays registered
while its server is down; its calls answer with an error.

A server's connection is opened by the first call that needs it. One supervisor task per connection owns the SDK's
context managers (their task groups must be entered and left in one task); a call only waits for the session and
uses it, so a call cancelled by the runner's tool timeout leaves the connection as it was. A failed connect is not
tried again for a while (1 s, doubling, at most 60 s); a call that fails for another reason than its timeout drops
the connection, and the next call opens a new one. A call is never retried.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any, Protocol

from nous.api.tool_classes import forget_mcp_tool, note_mcp_tool
from nous.config import Settings
from nous.events import Event, EventBus
from nous.mcp_client.bridge import (
    clean_description,
    flatten_result,
    has_reserved_property,
    input_schema_ok,
    safe_error,
    tool_name,
    wrap_result,
)
from nous.mcp_client.config import ServerConfig, ToolConfig, parse_host_list

logger = logging.getLogger(__name__)

CALLS_PER_SERVER = 4  # calls in flight to one server at a time
_BACKOFF_FIRST_SECONDS = 1.0
_BACKOFF_MAX_SECONDS = 60.0
_CLOSE_WAIT_SECONDS = 5.0

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


@dataclass
class _Connection:
    """One attempt at a session: its supervisor task, the session once ready, and the signal that ends it."""

    ready: asyncio.Future[Any]
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None

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
    ) -> None:
        self.config = config
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

    async def session(self) -> Any:
        """The open session, connecting first when there is none. Raises ``ServerUnavailable`` while a failed
        connect is backing off, and the connect's own error when it fails now."""
        conn = self._conn
        if conn is None or conn.closed:
            if asyncio.get_running_loop().time() < self._retry_at:
                raise ServerUnavailable()
            conn = self._conn = self._open()
        # Shielded: a caller that is cancelled or times out leaves the connect running for the next call.
        return await asyncio.shield(conn.ready)

    def drop(self) -> None:
        """End the current connection (its supervisor leaves the session's context); the next call opens one."""
        if self._conn is not None:
            self._conn.stop.set()

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
        try:
            async with self._connect(self.config.url, self._allow) as session:
                self._failures = 0
                conn.ready.set_result(session)
                await conn.stop.wait()
        except Exception as exc:  # an ExceptionGroup from the SDK's task groups included; CancelledError passes
            # The class only: an HTTP error's text carries the URL.
            if not conn.ready.done():
                self._failures += 1
                backoff = min(_BACKOFF_MAX_SECONDS, _BACKOFF_FIRST_SECONDS * 2 ** (self._failures - 1))
                self._retry_at = asyncio.get_running_loop().time() + backoff
                conn.ready.set_exception(exc)
                logger.warning("MCP client: could not connect to server %r (%s)", name, type(exc).__name__)
            else:
                logger.warning("MCP client: the connection to server %r ended (%s)", name, type(exc).__name__)
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

            connect = open_session
        self._registrar = registrar
        self._registry = registry
        self._bus = bus
        self._agent_id = settings.agent_id
        self._max_chars = settings.mcp_client_max_result_chars
        self._allow = parse_host_list(settings.mcp_client_allow_private_hosts)
        self._connect = connect
        self._servers: dict[str, _Server] = {}
        self._registered: dict[str, str] = {}  # tool name -> server name

    async def start(self) -> None:
        """Register every allowed tool of every enabled server, from the stored rows, in sorted (server, tool)
        order. Connects to no server. A tool whose name cannot be built, that collides with a registered tool, whose
        input schema is not an object schema, or that declares an argument starting with ``_`` is skipped and
        logged by name."""
        entries: list[tuple[str, str, ToolConfig]] = []
        for config, tools in await self._registry.list_enabled():
            self._servers[config.name] = _Server(config, self._connect, self._allow, self._emit_error)
            entries += [(config.name, tool.tool, tool) for tool in tools if tool.permission == "allow"]
        for server_name, raw_name, tool in sorted(entries, key=lambda entry: (entry[0], entry[1])):
            server = self._servers[server_name]
            name = tool_name(server_name, raw_name)
            if name is None:
                logger.warning(
                    "MCP client: skipped %r of server %r: its name is empty or too long", raw_name[:80], server_name
                )
                continue
            if self._registrar.is_registered(name):
                logger.warning("MCP client: skipped %s: a tool of that name is already registered", name)
                continue
            if not input_schema_ok(tool.input_schema):
                logger.warning("MCP client: skipped %s: its input schema is not a valid object schema", name)
                continue
            if has_reserved_property(tool.input_schema):
                logger.warning("MCP client: skipped %s: it declares an argument starting with '_'", name)
                continue
            schema = {**tool.input_schema, "description": clean_description(server_name, tool.description)}
            self._registrar.register(name, self._handler(server, raw_name, name), schema)
            note_mcp_tool(name, background=server.config.background)
            self._registered[name] = server_name
        logger.info("MCP client: %d tool(s) registered from %d server(s)", len(self._registered), len(self._servers))

    async def close(self) -> None:
        """Unregister every tool this manager registered and close every connection (bounded wait, then cancel)."""
        for name in self._registered:
            self._registrar.unregister(name)
            forget_mcp_tool(name)
        self._registered.clear()
        tasks: set[asyncio.Task[None]] = set()
        for server in self._servers.values():
            server.drop()
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
                    "background": server.config.background,
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
        timeout = server.config.call_timeout_seconds
        try:
            # One budget for the wait for a call slot, the connect and the call (which can list the server's tools
            # first: the SDK does that to check a result's structured content).
            async with asyncio.timeout(timeout):
                async with server.semaphore:
                    session = await server.session()
                    raw = await session.call_tool(raw_name, arguments)  # the server's own name for the tool
        except TimeoutError:
            logger.warning("MCP client: %s timed out after %d s", name, timeout)
            return _error(f'the call to the MCP server "{server_name}" timed out after {timeout} s.')
        except ServerUnavailable:
            return _error(f'the MCP server "{server_name}" is not reachable now: try again later.')
        except Exception as exc:
            # The session is in an unknown state (the server may have dropped it): the next call connects again.
            server.drop()
            logger.warning("MCP client: %s failed (%s)", name, type(exc).__name__)
            logger.debug("MCP client: %s error", name, exc_info=True)
            return _error(f'the call to the MCP server "{server_name}" failed: {safe_error(exc)}.')
        text, is_error = flatten_result(raw, self._max_chars)
        return {"content": [{"type": "text", "text": wrap_result(server_name, text)}], "is_error": is_error}

    async def _emit_error(self, server_name: str, exc: BaseException) -> None:
        """An ``mcp_client_server_error`` event: the server's name and the error's class, never its text."""
        if self._bus is None:
            return
        try:
            await self._bus.emit(
                Event(
                    type="mcp_client_server_error",
                    agent_id=self._agent_id,
                    data={"server": server_name, "error": type(exc).__name__},
                )
            )
        except Exception:
            logger.debug("MCP client: event not emitted", exc_info=True)


def _error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"Tool error: {message}"}], "is_error": True}
