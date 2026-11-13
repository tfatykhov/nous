"""F112 MCP client: registers the approved tools of external MCP servers, and runs their calls.

``start`` registers, from the stored rows and in sorted ``(server, tool)`` order, every tool whose permission is
``allow`` (at most ``TOOLS_PER_SERVER`` per server), and connects to no server: the tool array sits at the front of
the prompt-cache prefix, so it must not depend on which server answered at boot, and a server that is down must not
delay the boot. A tool stays registered while its server is down; its calls answer with an error. ``reload`` does the
same again without a restart (the owner's routes call it after every change): it unregisters every MCP tool and
registers the current set, so the tool array is what a fresh start would build. ``plan`` is the one place that decides
what registers; ``start``, ``reload`` and the routes' listing all read it.

Two optional ceilings, set on the host only (outside the database). ``NOUS_MCP_CLIENT_PINNED_SERVERS``
(``name=host[:port];tool|tool``): empty, every ``allow`` tool of an enabled server registers (subject to the checks of
``plan``); set, a tool registers only when its server's name is pinned, the stored URL's host (and its port, when the
URL names one) is the pinned one and the tool's own name is in the pin's list. A set value that pins nothing valid is
still a ceiling: nothing registers. ``NOUS_MCP_CLIENT_BACKGROUND_SERVERS``: empty, the row's background switch alone
decides whether background work may use a server; set, the server must also be listed there. The rows are written by
the owner's routes (``nous/api/mcp_client_routes.py``) and the seed command, but anything that reaches the database
can write them, the agent's own shell included, and code running in this process can call the routes: the ceilings
are the hard limit for an owner who wants one. They do not cover the URL's path, the descriptions and schemas stored
for an allowed tool (a changed row is registered as stored, cleaned and checked by ``prepare_tool``), or code the
agent runs inside this process.

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

Stored credentials (PR 2c): a server's credential row (``registry.list_secrets``, sealed) is opened with the key in
``NOUS_MCP_CLIENT_KEY_FILE`` and the server's stored URL. A server whose credential cannot be opened (no key, another
key, a row that does not open) registers no tool, with the reason in ``plan``; a server without one is unaffected.
Each ``_Server`` is built with ``(credential row id, URL)`` and holds the opened headers in memory; ``_ServerAuth``
sets them on every request to that URL's origin. A reload keeps a server's connection only while both are unchanged,
and a retiring server keeps using what it was built with. A 401 or 403 answer to a POST is recorded on the
connection it came on (the SDK ends the session on it and the call sees only the end), and a 401 ends that
connection: a call that then fails on it answers with a fixed sentence asking the owner to replace the credential
(for a 403, only when one is stored); a call the server answered is never rewritten. The listing's status keeps the
refusal until a call is answered on a connection that was refused nothing, or the credential changes. Every result's
text is scrubbed of the server's credential values, in the forms a server may encode them (``bridge.withhold``).

OAuth (PR 2d): a credential row of kind ``oauth`` holds a token record; its server sends ``Authorization: Bearer``
per request (``_OAuthSource``), refreshed when the token expires within a minute and once after a 401 (retried once,
and only when the stored token is still the one sent). Every refresh goes through ``_refresh``: a task this manager
owns and callers only wait for, under a per-server lock that survives ``reload``, which reads the row again, requires
the same row id, refreshes only if still stale, and stores the result with ``UPDATE ... WHERE id`` before anything
uses it (and hands it to the live server). A row refreshed less than 30 s ago is not refreshed again, and a 401 to its
fresh token is a refusal (the second one needs a new sign-in). A refresh the provider refuses for good marks the row as
needing a new sign-in: calls then answer at once with a fixed sentence and contact neither the server nor the
provider. One that could not reach the provider holds refreshes for 30 s (the sign-in stands; calls that need a refresh
say the provider could not be reached). Nothing here ever starts a login: sign-ins are the owner's routes'
(``oauth.OAuthFlows``, held in ``flows``).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable, Iterator, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from nous.api.tool_classes import forget_mcp_tool, note_mcp_tool
from nous.config import Settings
from nous.events import Event, EventBus
from nous.mcp_client.bridge import (
    TOOLS_PER_SERVER,
    PreparedTool,
    ServerErrorReply,
    credential_needles,
    error_class,
    flatten_result,
    holds_credential,
    prepare_tool,
    result_chars,
    safe_error,
    serialize_schema,
    wrap_result,
)
from nous.mcp_client.config import (
    BlockedAddress,
    Pin,
    ServerConfig,
    ToolConfig,
    parse_host_list,
    parse_pins,
    pin_host,
)
from nous.mcp_client.oauth import (
    HttpFactory,
    OAuthFlows,
    OAuthProblem,
    expired,
    expiring,
    oauth_client,
    refresh_tokens,
)
from nous.mcp_client.secrets import (
    KeyFile,
    OAuthRecord,
    SealedSecret,
    StoredSecret,
    aad_fields,
    open_credential,
    seal_headers,
    seal_oauth,
)

logger = logging.getLogger(__name__)

CALLS_PER_SERVER = 4  # calls in flight to one server at a time
_BACKOFF_FIRST_SECONDS = 1.0
_BACKOFF_MAX_SECONDS = 60.0
_CLOSE_WAIT_SECONDS = 5.0
# PR 2d: at close, a refresh in flight gets the OAuth request's own bound and time to store its result: a provider that
# already rotated the refresh token would otherwise leave a dead one stored.
REFRESH_DRAIN_SECONDS = 20.0
HANDSHAKE_MAX_SECONDS = 30  # the handshake's bound is the server's call timeout, but never more than this
# A result this long (characters) is flattened, cleaned and scrubbed in a worker thread: a hostile 2 MB result costs
# about half a second, which must not be the event loop's.
OFF_LOOP_RESULT_CHARS = 64_000

# (url, allow_private_hosts, *, auth) -> an async context manager yielding an initialized session
# (transport.open_session); auth is the server's _ServerAuth, run on every request of the session.
Connector = Callable[..., AbstractAsyncContextManager[Any]]

SIGN_IN_NEEDED = "the sign-in has expired: the owner must sign in again"
PROVIDER_UNREACHABLE = "the sign-in provider could not be reached: try again in a moment"
# PR 2d: a refresh within this many seconds is not repeated, and a 401 to its token is a refusal, not a reason to
# refresh again (the second such refusal makes the server need a new sign-in).
REFRESH_WINDOW_SECONDS = 30
# PR 2d: after the provider could not be reached (no answer, a timeout, a 429, a 5xx), no refresh is asked this long.
PROVIDER_HOLD_SECONDS = 30
# PR 2d: this many refreshes in a row that the provider answered with a 4xx other than 400, 401 and 429 (a 403 or 404
# for good, say) make the server need a new sign-in, so the owner is told. No answer at all (a connect error, a DNS
# failure, a timeout), a 429 or a 5xx is never counted: an outage on Nous's side must not sign servers out.
TRANSIENT_FAILURES_MAX = 5
_UNREACHABLE = -1  # what _send reports for a call that failed because the sign-in provider could not be reached
PROBE_REFUSED = "the server refused the credentials"
PROBE_ASKS = "the server asks for credentials"
LISTING_REPEATS = "the server's tool list repeats the credentials it was sent"


class Registrar(Protocol):
    """The part of ``ToolDispatcher`` the manager uses."""

    def is_registered(self, name: str) -> bool: ...

    def register(self, name: str, handler: Callable[..., Any], schema: dict[str, Any]) -> None: ...

    def unregister(self, name: str) -> bool: ...


class Registry(Protocol):
    """The part of ``McpRegistry`` the manager uses."""

    async def list_enabled(self) -> list[tuple[ServerConfig, list[ToolConfig]]]: ...

    async def list_secrets(self) -> dict[str, StoredSecret]: ...

    async def update_secret_in_place(self, secret_id: uuid.UUID, secret: SealedSecret) -> bool: ...


class ServerUnavailable(Exception):
    """A server whose last connect failed is not tried again yet, or its connection closed before it was ready."""


class ClientClosed(Exception):
    """The manager was closed: no connection is opened any more."""


class ConnectionEnded(Exception):
    """The connection a call was using ended before the call had its answer."""


class HandshakeTimedOut(Exception):
    """Opening the session and ``initialize`` did not finish within the handshake's bound."""


class ProbeRefused(Exception):
    """The address guard refused the URL a probe was given. The message is the guard's: a host name at most."""


class ProbeFailed(Exception):
    """A probe could not list the server's tools. The message is ``safe_error``'s: never the error's own text."""


class ProbeCredentials(Exception):
    """A probe the server answered with 401 or 403 (``PROBE_REFUSED`` when credentials were sent, ``PROBE_ASKS``
    when none were), or whose listing repeats a credential value it was sent (``LISTING_REPEATS``). Fixed text."""


class SignInNeeded(Exception):
    """PR 2d: a server's OAuth tokens are expired and could not be refreshed (or the owner must sign in again): the
    request is not sent."""


class ProviderUnreachable(Exception):
    """PR 2d: a refresh was needed and the sign-in provider could not be reached now (or was, less than
    ``PROVIDER_HOLD_SECONDS`` ago): the request is not sent, and the stored sign-in stays as it was."""


class _OAuthSource:
    """PR 2d: a server's OAuth credential. It holds the token record in memory (the one its server was built with,
    then each refresh's, its own or one a route made) and refreshes it through the manager's one entry point
    (``refresh``: the record to use now, or None when there is none; it raises ``ProviderUnreachable``). ``dead``:
    whether the owner must sign in again, so nothing is sent. ``refused``: a 401 to a token the provider gave moments
    ago (the manager counts it)."""

    def __init__(
        self,
        record: OAuthRecord,
        refresh: Callable[[str | None], Awaitable[OAuthRecord | None]],
        dead: Callable[[], bool],
        refused: Callable[[OAuthRecord], None],
    ) -> None:
        self.record = record
        self._previous: OAuthRecord | None = None
        self._refresh = refresh
        self._dead = dead
        self._refused = refused

    def adopt(self, record: OAuthRecord) -> None:
        """Use ``record`` from now on (the one before stays known to the result scrub)."""
        if record.access_token != self.record.access_token:
            self._previous, self.record = self.record, record

    async def before(self) -> str:
        """The access token to send, refreshed first when it is about to expire. Raises ``SignInNeeded`` or
        ``ProviderUnreachable``."""
        if self._dead():
            raise SignInNeeded()
        if expiring(self.record):
            try:
                fresh = await self._refresh(None)
            except ProviderUnreachable:
                if expired(self.record):
                    raise
                return self.record.access_token  # still valid: sent, the refresh is tried again after the hold
            if fresh is None:
                raise SignInNeeded()
            self.adopt(fresh)
        return self.record.access_token

    async def after_401(self, sent: str) -> str | None:
        """The token to retry with once after a 401 to a request that carried ``sent`` (refreshed only when the stored
        token is still ``sent`` and was not refreshed moments ago), or None when there is none. Raises
        ``ProviderUnreachable``."""
        fresh = await self._refresh(sent)
        if fresh is None:
            return None
        self.adopt(fresh)
        return fresh.access_token

    def refused_again(self) -> None:
        """The retry, with the token ``after_401`` gave, was answered 401 too."""
        self._refused(self.record)

    def values(self) -> list[str]:
        """The secret values a result must not repeat: the current record's and the one before it."""
        return self.record.values() + (self._previous.values() if self._previous is not None else [])


def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
    """What ``_ServerAuth`` compares: the scheme, the host as sent (an ``xn--`` host stays encoded) and the port
    (None for the scheme's default, as httpx gives it)."""
    port = None if url.port == {"https": 443, "http": 80}.get(url.scheme) else url.port
    return url.scheme, url.raw_host.decode("ascii", "replace").lower(), port


class _ServerAuth(httpx.Auth):
    """A server's credential on the requests of one session (``httpx.Auth``: run per request, not fixed at connect).
    ``source`` gives the headers to set; nothing is set on a request to another origin than the URL's (scheme, host
    and port; the SDK follows a redirect only within that origin, so this is a second guard). A 401 or 403 answer to
    a POST (an MCP request; the SDK swallows the GET event stream's errors) is kept in ``refused`` (never cleared: a
    later answer on the same session does not unsay it) and passed to ``on_refused``.

    With ``oauth`` (PR 2d) the header is ``Authorization: Bearer`` with the source's token, refreshed before a request
    when it is about to expire; a 401 to it is retried once with the token the source gives after it (a refresh only
    when the stored token is the one that was sent), and only the last answer counts as a refusal (a 401 to the retry
    is also told to the source). When the source has no token to send (``SignInNeeded``, ``ProviderUnreachable``),
    nothing is sent and ``on_oauth_failure`` ends the connection, saying which."""

    def __init__(
        self,
        url: str,
        source: Callable[[], Mapping[str, str]],
        on_refused: Callable[[int], None] | None = None,
        *,
        oauth: _OAuthSource | None = None,
        on_oauth_failure: Callable[[str], None] | None = None,
    ) -> None:
        self._origin = _origin(httpx.URL(url))
        self._source = source
        self._on_refused = on_refused
        self._oauth = oauth
        self._on_oauth_failure = on_oauth_failure
        self.refused: int | None = None

    def _oauth_failed(self, exc: Exception) -> None:
        if self._on_oauth_failure is not None:
            self._on_oauth_failure("unreachable" if isinstance(exc, ProviderUnreachable) else "sign_in")

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        own = _origin(request.url) == self._origin
        sent: str | None = None
        if own and self._oauth is not None:
            try:
                sent = await self._oauth.before()
            except (SignInNeeded, ProviderUnreachable) as exc:
                self._oauth_failed(exc)
                raise
            request.headers["Authorization"] = f"Bearer {sent}"
        elif own:
            for name, value in self._source().items():
                request.headers[name] = value
        response = yield request
        if sent is not None and self._oauth is not None and response.status_code == 401:
            try:
                again = await self._oauth.after_401(sent)
            except ProviderUnreachable as exc:
                self._oauth_failed(exc)
                raise
            if again is not None:
                request.headers["Authorization"] = f"Bearer {again}"
                response = yield request
                if response.status_code == 401:
                    self._oauth.refused_again()
        if request.method == "POST" and response.status_code in (401, 403):
            self.refused = response.status_code
            if self._on_refused is not None:
                self._on_refused(response.status_code)


def _strings(value: Any) -> Iterator[str]:
    """Every string of a JSON value, its keys included."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _repeats(tools: list[ProbedTool], needles: tuple[str, ...]) -> bool:
    """Whether a listing (the server's text, and what ``prepare_tool`` makes of it) holds a needle
    (``bridge.holds_credential``, the result scrub's finder): in a name, a description, a string of a schema, or a
    serialized schema."""
    for tool in tools:
        texts = [tool.tool, tool.description, json.dumps(tool.input_schema, ensure_ascii=False, default=str)]
        texts += _strings(tool.input_schema)
        if tool.prepared.name is not None:
            texts.append(tool.prepared.name)
        if tool.prepared.schema is not None:
            texts.append(serialize_schema(tool.prepared.schema))
            texts += _strings(tool.prepared.schema)
        if any(holds_credential(text, needles) for text in texts):
            return True
    return False


@dataclass(frozen=True)
class ToolVerdict:
    """What the client does with one stored tool: registers it as ``prepared.name`` with ``prepared.schema`` when
    ``reason`` is None, else skips it for ``reason`` (``skip`` is its kind: ``disabled``, ``server_ceiling``,
    ``credentials``, ``schema``, ``permission``, ``ceiling``, ``collision`` or ``cap``). ``ceiling``: the host's pin
    keeps it out, whatever its permission."""

    tool: str  # the server's own name
    permission: str
    prepared: PreparedTool
    reason: str | None
    skip: str | None
    ceiling: bool


@dataclass(frozen=True)
class ServerVerdict:
    """One stored server as the client applies it. ``reason``: why none of its tools registers (disabled, kept out
    by the pin, or a stored credential that cannot be opened), else None. ``background``: background work may use it
    (the row's switch, and the host's list when one is set); ``background_ceiling``: the row's switch is on but the
    host's list leaves it out. ``tools``: by tool name. ``credentials``: why its stored credential cannot be opened
    (``secrets.NO_KEY``, ``OTHER_KEY`` or ``UNREADABLE``), whether or not it is enabled; None without one, or when it
    opens."""

    config: ServerConfig
    reason: str | None
    ceiling: bool
    background: bool
    background_ceiling: bool
    tools: list[ToolVerdict]
    credentials: str | None = None


@dataclass(frozen=True)
class ProbedTool:
    """One tool a server listed: its description and input schema as the server gave them (what a row stores), and
    ``prepared``, what ``prepare_tool`` makes of them (what the model would be offered, or why it would not be)."""

    tool: str
    description: str
    input_schema: dict[str, Any]
    prepared: PreparedTool


async def _unless_ended(ended: asyncio.Event, make_call: Callable[[], Awaitable[Any]]) -> Any:
    """``make_call()``'s result, or ``ConnectionEnded`` as soon as ``ended`` is set. When the transport fails
    mid-session the SDK can end the session without answering the calls waiting on it; this ends them with it. A
    connection that has already ended (closed or dropped while the call waited for it) gets no request at all."""
    if ended.is_set():
        raise ConnectionEnded()

    async def _call() -> Any:
        # The task starts a loop step later: a close or drop queued before it must still stop the request.
        if ended.is_set():
            raise ConnectionEnded()
        return await make_call()

    call_task = asyncio.ensure_future(_call())
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
    """One attempt at a session: its supervisor task, the session once ready, the signal that ends it, and the 401
    or 403 a POST of it was answered with (``refused``: kept for the life of the attempt, so a call that failed on it
    finds its own refusal whatever another call's answer said)."""

    ready: asyncio.Future[Any]
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    session: Any = None
    refused: int | None = None
    # PR 2d: a request of it was not sent because the owner must sign in again ("sign_in") or the sign-in provider
    # could not be reached ("unreachable").
    oauth_failure: str | None = None

    @property
    def closed(self) -> bool:
        return self.stop.is_set() or self.task is None or self.task.done()


class _Server:
    """One configured server: its call limit, its current connection, and the credential it was built with."""

    def __init__(
        self,
        config: ServerConfig,
        connect: Connector,
        allow: frozenset[str],
        on_error: Callable[[str, BaseException], Awaitable[None]],
        background: bool,
        *,
        secret_id: uuid.UUID | None = None,
        headers: Mapping[str, str] | None = None,
        oauth: _OAuthSource | None = None,
    ) -> None:
        self.config = config  # replaced by a reload that keeps the server (same URL, same credential)
        # What it was built for: a reload keeps it only while both are unchanged.
        self.url = config.url
        self.secret_id = secret_id
        # The opened credential, held in memory: a retiring server keeps using it and never re-reads by name (an
        # OAuth source re-reads only its own row, by id, to refresh).
        held = dict(headers or {})
        self._source: Callable[[], Mapping[str, str]] = lambda: held
        self.oauth = oauth
        self.has_credentials = bool(held) or oauth is not None
        self._withheld = credential_needles(held.values())
        # The last 401 or 403 any of its connections was answered with: the listing's status. Cleared when a call is
        # answered on a connection that was refused nothing.
        self.refusal: int | None = None
        self.background = background  # the row's switch, AND the host's NOUS_MCP_CLIENT_BACKGROUND_SERVERS when set
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

    def current(self) -> _Connection | None:
        """The current connection attempt (a failed one included, until the next attempt replaces it)."""
        return self._conn

    @property
    def withheld(self) -> tuple[str, ...]:
        """What a result's text is scrubbed of: the stored header values, or the OAuth tokens held now."""
        return credential_needles(self.oauth.values()) if self.oauth is not None else self._withheld

    def auth_for(self, conn: _Connection | None) -> _ServerAuth:
        """The credential, per request, of the session ``conn`` opens: its refusals are recorded on ``conn``, and a
        request not sent for want of a sign-in ends it."""
        on_refused = None if conn is None else functools.partial(self._refused, conn)
        on_failure = None if conn is None else functools.partial(self._oauth_failed, conn)
        return _ServerAuth(self.url, self._source, on_refused=on_refused, oauth=self.oauth, on_oauth_failure=on_failure)

    @staticmethod
    def _oauth_failed(conn: _Connection, kind: str) -> None:
        conn.oauth_failure = kind
        conn.stop.set()

    def _refused(self, conn: _Connection, status: int) -> None:
        """A POST of ``conn`` was answered 401 or 403. A 401 ends that connection (the one it came on, never a newer
        one), so the calls waiting on it end at once, whatever the SDK does with the answer; a 403 does not (it can be
        one tool's permission)."""
        conn.refused = status
        self.refusal = status
        if status == 401:
            conn.stop.set()

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
                        connected = self._connect(self.url, self._allow, auth=self.auth_for(conn))
                        session = await stack.enter_async_context(connected)
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
        oauth_http: HttpFactory | None = None,
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
        # A ceiling is in effect whenever its setting is not blank, even when nothing in it parses: a malformed pin
        # must not open every server (fail closed).
        pinned = settings.mcp_client_pinned_servers
        self._pins: dict[str, Pin] | None = parse_pins(pinned) if pinned.strip() else None
        listed = settings.mcp_client_background_servers
        self._background_servers: frozenset[str] | None = parse_host_list(listed) if listed.strip() else None
        self._keys = KeyFile(settings.mcp_client_key_file)
        self._keys.box()  # read once at startup (and again at a later use while no key is loaded)
        self._connect = connect
        self._servers: dict[str, _Server] = {}
        self._registered: dict[str, str] = {}  # tool name -> server name
        self._sources: dict[str, str] = {}  # tool name -> the server's own name of the tool its handler calls
        self._closed = False
        self._lock = asyncio.Lock()  # one start or reload at a time
        # PR 2d: OAuth. The sign-ins in progress; the client every OAuth request goes through (its own, with no
        # credential: never a session's); one refresh lock per server name (a reload replaces _Server objects, not
        # these); the refresh tasks, owned here so a caller that goes away cannot cancel a refresh after the provider
        # rotated the token; and, by server name, the credential row whose refresh failed (the owner must sign in).
        self.flows = OAuthFlows()
        self._oauth_http: HttpFactory = oauth_http or functools.partial(oauth_client, self._allow)
        self._refresh_locks: dict[str, asyncio.Lock] = {}
        self._refresh_tasks: set[asyncio.Task[OAuthRecord | None]] = set()
        self._needs_sign_in: dict[str, uuid.UUID] = {}
        # By server name, for one credential row: when it was last refreshed (monotonic), how many 401s its fresh
        # token got since, and until when no refresh is asked after the provider could not be reached.
        self._refreshed: dict[str, tuple[uuid.UUID, float]] = {}
        self._fresh_refusals: dict[str, tuple[uuid.UUID, int]] = {}
        self._transient_failures: dict[str, tuple[uuid.UUID, int]] = {}
        self._held: dict[str, tuple[uuid.UUID, float]] = {}
        self._clock: Callable[[], float] = time.monotonic  # the windows' clock (a test sets its own)

    @property
    def oauth_http(self) -> HttpFactory:
        """The client factory for OAuth requests (discovery, registration, the code exchange, refreshes)."""
        return self._oauth_http

    @property
    def allow_private_hosts(self) -> frozenset[str]:
        return self._allow

    def registered(self) -> list[str]:
        """The names of the tools this manager has registered, in registration order."""
        return list(self._registered)

    @property
    def closed(self) -> bool:
        """True once ``close`` ran: a ``reload`` does nothing from then on."""
        return self._closed

    def registered_source(self, name: str) -> tuple[str, str] | None:
        """``(server, tool)``: the server, and its own name of the tool, whose handler this manager registered as
        ``name``; None when it registered no tool of that name. Two of a server's tools can map to one name
        (``a.b`` and ``a_b``): only the one that registered is its source."""
        server = self._registered.get(name)
        return None if server is None else (server, self._sources[name])

    def ceilings(self) -> dict[str, bool]:
        """Which of the host's two optional ceilings is set."""
        return {"pinned_servers": self._pins is not None, "background_servers": self._background_servers is not None}

    def _server_ceiling(self, config: ServerConfig) -> str | None:
        """Why the host's pin keeps the whole server out, or None when there is no pin or it lets the server in."""
        if self._pins is None:
            return None
        pin = self._pins.get(config.name)
        if pin is None:
            return "it is not pinned (NOUS_MCP_CLIENT_PINNED_SERVERS)"
        if pin_host(config.url) != pin.host:
            return "its stored host or port is not its pinned one"
        return None

    def _open(
        self, config: ServerConfig, secret: StoredSecret
    ) -> tuple[dict[str, str] | OAuthRecord | None, str | None]:
        """``secret`` opened for ``config``'s name and stored URL: ``(headers or token record, None)`` or
        ``(None, reason)``."""
        return open_credential(
            self._keys.box(), aad_fields(self._agent_id, config.name, config.url, secret.kind), secret
        )

    def plan(
        self,
        rows: list[tuple[ServerConfig, list[ToolConfig]]],
        secrets: Mapping[str, StoredSecret] | None = None,
    ) -> list[ServerVerdict]:
        """What ``start`` and ``reload`` do with ``rows`` (servers enabled or not) and the servers' stored
        credentials ``secrets`` (by server name), changing and logging nothing (but the key file is read while no key
        is loaded).

        A tool registers when its server is enabled and inside the pin (when one is set), its server's stored
        credential (when it has one) opens, ``prepare_tool`` accepts it, its permission is ``allow``, the pin (when
        set) lists it, no other tool holds its name (one registered by anything but this manager, or one before it in
        sorted ``(server, tool)`` order), and its server has fewer than ``TOOLS_PER_SERVER`` tools before it. The
        reasons are checked in that order; the first one that applies is the tool's ``reason``. ``prepare_tool`` runs
        for every tool, so a denied tool also shows what the model would be offered."""
        secrets = secrets or {}
        unusable = {
            config.name: problem
            for config, _tools in rows
            if config.name in secrets and (problem := self._open(config, secrets[config.name])[1]) is not None
        }
        first: dict[tuple[str, str], tuple[str, str] | None] = {}  # (server, tool) -> (skip, reason), None: a candidate
        prepared_of: dict[tuple[str, str], PreparedTool] = {}
        candidates: list[tuple[str, str, str]] = []  # (server, tool, registered name)
        for config, tools in rows:
            server_reason = self._server_ceiling(config)
            pin = self._pins.get(config.name) if self._pins is not None else None
            for tool in tools:
                key = (config.name, tool.tool)
                prepared = prepare_tool(config.name, tool.tool, tool.description, tool.input_schema)
                prepared_of[key] = prepared
                outside_pin = server_reason is not None or (pin is not None and tool.tool not in pin.tools)
                if not config.enabled:
                    first[key] = ("disabled", "the server is disabled")
                elif server_reason is not None:
                    first[key] = ("server_ceiling", server_reason)
                elif config.name in unusable:
                    first[key] = ("credentials", f"credentials unavailable: {unusable[config.name]}")
                elif prepared.problem is not None or prepared.name is None:
                    first[key] = ("schema", prepared.problem or "it cannot be registered")
                elif tool.permission != "allow":
                    note = ", which is not offered yet" if tool.permission == "ask" else ""
                    first[key] = ("permission", f"its permission is {tool.permission}{note}")
                elif outside_pin:
                    first[key] = ("ceiling", "it is not in its server's pin (NOUS_MCP_CLIENT_PINNED_SERVERS)")
                else:
                    first[key] = None
                    candidates.append((config.name, tool.tool, prepared.name))
        taken: set[str] = set()
        counts: dict[str, int] = {}
        for server, tool, name in sorted(candidates, key=lambda c: (c[0], c[1])):
            if name in taken or (self._registrar.is_registered(name) and name not in self._registered):
                first[(server, tool)] = ("collision", "a tool of that name is already registered")
            elif counts.get(server, 0) >= TOOLS_PER_SERVER:
                first[(server, tool)] = ("cap", f"over the {TOOLS_PER_SERVER}-tool cap")
            else:
                taken.add(name)
                counts[server] = counts.get(server, 0) + 1
        out: list[ServerVerdict] = []
        for config, tools in sorted(rows, key=lambda row: row[0].name):
            server_reason = self._server_ceiling(config)
            pin = self._pins.get(config.name) if self._pins is not None else None
            verdicts = []
            for tool in sorted(tools, key=lambda t: t.tool):
                skip_reason = first[(config.name, tool.tool)]
                verdicts.append(
                    ToolVerdict(
                        tool=tool.tool,
                        permission=tool.permission,
                        prepared=prepared_of[(config.name, tool.tool)],
                        reason=skip_reason[1] if skip_reason else None,
                        skip=skip_reason[0] if skip_reason else None,
                        ceiling=server_reason is not None or (pin is not None and tool.tool not in pin.tools),
                    )
                )
            listed = self._background_servers is None or config.name in self._background_servers
            problem = unusable.get(config.name)
            if not config.enabled:
                reason: str | None = "the server is disabled"
            elif server_reason is not None:
                reason = server_reason
            else:
                reason = f"credentials unavailable: {problem}" if problem is not None else None
            out.append(
                ServerVerdict(
                    config=config,
                    reason=reason,
                    ceiling=server_reason is not None,
                    background=config.background and listed,
                    background_ceiling=config.background and not listed,
                    tools=verdicts,
                    credentials=problem,
                )
            )
        return out

    async def start(self) -> None:
        """Register, from the stored rows of the enabled servers and in sorted (server, tool) order, every tool
        ``plan`` lets register, as ``prepare_tool`` builds it. Connects to no server. A skipped allowed tool is logged
        by name, never a URL. Ends with one ``mcp_client_started`` event and an INFO line naming the servers (with
        their background use) and tools registered. A failure part way logs what was registered before it and is
        raised."""
        await self._apply("start", "mcp_client_started")

    async def reload(self) -> None:
        """Apply the stored rows again, without a restart: what ``start`` does, after unregistering every MCP tool
        this manager registered, so the MCP tools are registered in the order a fresh start would give them (one
        prompt-cache miss). A server whose URL and stored credential (its row's id) are unchanged keeps its
        connection, and its calls in flight finish; a server that is gone, disabled, outside the pin, at another URL
        or with another credential (set, replaced or removed) is closed, and its calls in flight end with an error.
        Reloads run one at a time; nothing is done once the manager is closed. Ends with one ``mcp_client_reloaded``
        event and the same INFO line as ``start``."""
        await self._apply("reload", "mcp_client_reloaded")

    async def _apply(self, what: str, event_type: str) -> None:
        async with self._lock:
            if self._closed:
                return
            rows = await self._registry.list_enabled()
            secrets = await self._registry.list_secrets()
            if self._closed:  # closed while the rows were read
                return
            plan = self.plan(rows, secrets)
            servers: dict[str, _Server] = {}
            for verdict in plan:
                if verdict.reason is not None:
                    continue
                config = verdict.config
                secret = secrets.get(config.name)
                secret_id = secret.id if secret is not None else None
                server = self._servers.get(config.name)
                if server is not None and server.url == config.url and server.secret_id == secret_id:
                    server.config = config  # the same connection; a new call timeout applies to the next call
                    server.background = verdict.background
                else:
                    opened = self._open(config, secret)[0] if secret is not None else None
                    source = None
                    if isinstance(opened, OAuthRecord) and secret_id is not None:
                        source = _OAuthSource(
                            opened,
                            functools.partial(self._refresh, config.name, config.url, secret_id),
                            functools.partial(self.needs_sign_in, config.name, secret_id),
                            functools.partial(self._refused_fresh, config.name, secret_id),
                        )
                    server = _Server(
                        config,
                        self._connect,
                        self._allow,
                        self._emit_error,
                        verdict.background,
                        secret_id=secret_id,
                        headers=opened if isinstance(opened, dict) else None,
                        oauth=source,
                    )
                servers[config.name] = server
            retiring = [server for name, server in self._servers.items() if servers.get(name) is not server]
            for server in retiring:
                server.close()
            self._servers = servers
            self._log_skips(plan)
            # From here to the end of the loop nothing awaits: no turn sees a part of the MCP tools.
            for name in self._registered:
                self._registrar.unregister(name)
                forget_mcp_tool(name)
            self._registered.clear()
            self._sources.clear()
            entries = sorted(
                ((verdict.config.name, tool) for verdict in plan for tool in verdict.tools if tool.reason is None),
                key=lambda entry: (entry[0], entry[1].tool),
            )
            try:
                for server_name, tool in entries:
                    name, schema = tool.prepared.name, tool.prepared.schema
                    if name is None or schema is None:  # plan never lets such a tool register
                        continue
                    server = servers[server_name]
                    self._registrar.register(name, self._handler(server, tool.tool, name), schema)
                    note_mcp_tool(name, background=server.background)
                    self._registered[name] = server_name
                    self._sources[name] = tool.tool
            except Exception:
                logger.warning(
                    "MCP client: %s stopped after registering %d tool(s): %s",
                    what,
                    len(self._registered),
                    ", ".join(self._registered) or "none",
                )
                raise
            registered_servers = [
                {"name": name, "background": servers[name].background}
                for name in sorted(set(self._registered.values()))
            ]
            logger.info(
                "MCP client: %d tool(s) registered from server(s) %s: %s",
                len(self._registered),
                ", ".join(f"{s['name']} (background {'on' if s['background'] else 'off'})" for s in registered_servers)
                or "none",
                ", ".join(self._registered) or "none",
            )
            await self._emit(event_type, {"servers": registered_servers, "tools": list(self._registered)})
            await _wait_ended(set().union(*(server.tasks() for server in retiring)))

    def _log_skips(self, plan: list[ServerVerdict]) -> None:
        """One WARNING per server the pin keeps out or whose credential cannot be opened, and per allowed tool that
        does not register, by name only."""
        for verdict in plan:
            name = verdict.config.name
            if verdict.ceiling:
                logger.warning("MCP client: server %r: %s; skipped", name, verdict.reason)
                continue
            if verdict.config.enabled and verdict.credentials is not None:
                logger.warning("MCP client: server %r: %s; skipped", name, verdict.reason)
                continue
            allowed = [tool for tool in verdict.tools if tool.permission == "allow"]
            outside_pin = sum(1 for tool in allowed if tool.skip == "ceiling")
            if outside_pin:
                logger.warning(
                    "MCP client: server %r: %d allowed tool(s) are not in its pin; skipped", name, outside_pin
                )
            for tool in allowed:
                if tool.skip == "schema":
                    logger.warning("MCP client: skipped %r of server %r: %s", tool.tool[:80], name, tool.reason)
                elif tool.skip == "collision":
                    logger.warning(
                        "MCP client: skipped %s: a tool of that name is already registered", tool.prepared.name
                    )
            over_cap = sum(1 for tool in allowed if tool.skip == "cap")
            if over_cap:
                logger.warning(
                    "MCP client: skipped %d tool(s) of server %r: over the %d-tool cap",
                    over_cap,
                    name,
                    TOOLS_PER_SERVER,
                )

    async def probe(
        self,
        server: str,
        url: str,
        headers: Mapping[str, str] | None = None,
        *,
        withheld: Iterable[str] = (),
    ) -> list[ProbedTool]:
        """Connect once to ``url`` through the client's connector (the address guard and the response cap of
        ``transport.open_session``), with ``headers`` on every request (and nothing else: a stored credential is
        never attached here), list its tools and return each, by name, with what ``prepare_tool`` makes of it for a
        server named ``server``. Writes nothing and registers nothing. The connect and the listing together are
        bounded by ``HANDSHAKE_MAX_SECONDS``. Raises ``ProbeRefused`` when the guard refuses the URL,
        ``ProbeCredentials`` when the server answers 401 or 403 or its listing repeats a value of ``headers`` or of
        ``withheld`` (the server's stored credential, which the caller passes even when it sends other headers),
        ``ProbeFailed`` (``safe_error`` text) for any other failure, and ``ProbeFailed`` for a listing that no set of
        tool rows can hold: a tool with no name, or a name listed twice."""
        sent = dict(headers or {})
        auth = _ServerAuth(url, lambda: sent)
        try:
            async with asyncio.timeout(HANDSHAKE_MAX_SECONDS):
                async with self._connect(url, self._allow, auth=auth) as session:
                    listed = await session.list_tools()
        except BlockedAddress as exc:
            raise ProbeRefused(str(exc)) from None
        except Exception as exc:  # an ExceptionGroup from the SDK's task groups included; CancelledError passes
            logger.warning("MCP client: the probe for server %r failed (%s)", server, error_class(exc))
            if auth.refused is not None:
                raise ProbeCredentials(PROBE_REFUSED if sent else PROBE_ASKS) from None
            raise ProbeFailed(safe_error(exc)) from None
        names = [tool.name for tool in listed]
        if any(not name for name in names):
            raise ProbeFailed("the server lists a tool with no name")
        if len(set(names)) < len(names):
            raise ProbeFailed("the server lists a tool name twice")
        out: list[ProbedTool] = []
        for tool in sorted(listed, key=lambda t: t.name):
            description = tool.description or ""
            prepared = prepare_tool(server, tool.name, description, tool.inputSchema)
            out.append(ProbedTool(tool.name, description, dict(tool.inputSchema), prepared))
        if _repeats(out, credential_needles([*sent.values(), *withheld])):
            logger.warning("MCP client: the probe for server %r: its tool list repeats a credential", server)
            raise ProbeCredentials(LISTING_REPEATS)
        return out

    def secrets_available(self) -> bool:
        """Whether a key is loaded (``NOUS_MCP_CLIENT_KEY_FILE``), so a credential can be stored and opened."""
        return self._keys.box() is not None

    def _opened(
        self, name: str, url: str, secret: StoredSecret
    ) -> tuple[dict[str, str] | OAuthRecord | None, str | None]:
        return open_credential(self._keys.box(), aad_fields(self._agent_id, name, url, secret.kind), secret)

    def credential_values(self, name: str, url: str, secret: StoredSecret) -> list[str]:
        """The secret values of the stored credential ``secret`` of the server ``name``, opened as bound to ``url``
        (header values, or an OAuth record's tokens and client secret); none when it cannot be opened."""
        opened, _problem = self._opened(name, url, secret)
        if isinstance(opened, OAuthRecord):
            return opened.values()
        return list(opened.values()) if opened else []

    async def credential_headers(
        self, name: str, url: str, secret: StoredSecret
    ) -> tuple[dict[str, str] | None, str | None]:
        """The headers the stored credential ``secret`` of the server ``name`` sends, opened as bound to ``url``:
        ``(headers, None)`` or ``(None, reason)`` (``url`` is not the one it was sealed for, no key, another key...).
        An OAuth record about to expire is refreshed first, through the one refresh entry point; one that cannot be
        is ``SIGN_IN_NEEDED``."""
        opened, problem = self._opened(name, url, secret)
        if not isinstance(opened, OAuthRecord):
            return opened, problem
        if self.needs_sign_in(name, secret.id):
            return None, SIGN_IN_NEEDED
        if expiring(opened):
            try:
                fresh = await self._refresh(name, url, secret.id, None)
            except ProviderUnreachable:
                return None, PROVIDER_UNREACHABLE
            if fresh is None:
                return None, SIGN_IN_NEEDED
            opened = fresh
        return {"Authorization": f"Bearer {opened.access_token}"}, None

    def seal_credentials(self, name: str, url: str, headers: Mapping[str, str]) -> SealedSecret | None:
        """``headers`` sealed for the server ``name`` bound to ``url``; None when no key is loaded."""
        box = self._keys.box()
        return None if box is None else seal_headers(box, aad_fields(self._agent_id, name, url, "headers"), headers)

    def stored_oauth(self, name: str, url: str, secret: StoredSecret) -> OAuthRecord | None:
        """The token record of the stored credential ``secret`` opened as bound to ``url``, or None (another kind, or
        it cannot be opened): a new sign-in reuses its client when the issuer is unchanged."""
        opened, _problem = self._opened(name, url, secret)
        return opened if isinstance(opened, OAuthRecord) else None

    def seal_oauth_record(self, name: str, url: str, record: OAuthRecord) -> SealedSecret | None:
        """PR 2d: a token record sealed for the server ``name`` bound to ``url``; None when no key is loaded."""
        box = self._keys.box()
        return None if box is None else seal_oauth(box, aad_fields(self._agent_id, name, url, "oauth"), record)

    def needs_sign_in(self, name: str, secret_id: uuid.UUID | None) -> bool:
        """Whether the OAuth credential row ``secret_id`` of the server ``name`` can no longer be used, so the owner
        must sign in again: the provider refused its refresh (400 or 401), it has no refresh token or the provider's
        tokens were unusable, or the server refused two freshly refreshed tokens. A new sign-in is a new row; a restart
        forgets it, and the next use tries one refresh."""
        return secret_id is not None and self._needs_sign_in.get(name) == secret_id

    def provider_unreachable(self, name: str, secret_id: uuid.UUID | None) -> bool:
        """Whether a refresh for the credential row ``secret_id`` of the server ``name`` found the provider unreachable
        less than ``PROVIDER_HOLD_SECONDS`` ago (no refresh is asked until then; the sign-in itself still stands)."""
        held = self._held.get(name)
        return secret_id is not None and held is not None and held[0] == secret_id and self._clock() < held[1]

    def _recently_refreshed(self, name: str, secret_id: uuid.UUID, record: OAuthRecord) -> bool:
        """Whether the row was refreshed within the window (``REFRESH_WINDOW_SECONDS``, or half the token's lifetime
        when that is shorter) and its token has not expired: an expired token is refreshed whatever the window."""
        if expired(record):
            return False
        window: float = REFRESH_WINDOW_SECONDS
        if record.lifetime is not None:
            window = min(window, record.lifetime / 2)
        last = self._refreshed.get(name)
        return last is not None and last[0] == secret_id and self._clock() - last[1] < window

    def _refused_fresh(self, name: str, secret_id: uuid.UUID, record: OAuthRecord) -> None:
        """A 401 to ``record``'s token, of the credential row ``secret_id``: counted when the row was refreshed within
        the window and the token has not expired (``_recently_refreshed``); the second such refusal makes the server
        need a new sign-in."""
        if not self._recently_refreshed(name, secret_id, record):
            return
        previous = self._fresh_refusals.get(name)
        count = previous[1] + 1 if previous is not None and previous[0] == secret_id else 1
        self._fresh_refusals[name] = (secret_id, count)
        if count >= 2:
            self._needs_sign_in[name] = secret_id
            logger.warning("MCP client: server %r refused freshly refreshed tokens twice: it needs a new sign-in", name)

    async def _refresh(self, name: str, url: str, secret_id: uuid.UUID, sent: str | None) -> OAuthRecord | None:
        """The OAuth record to use now for the server ``name``'s credential row ``secret_id`` bound to ``url``,
        refreshed when stale: about to expire (``sent`` None), or still the token ``sent`` with a request that was
        answered 401. A row refreshed less than ``REFRESH_WINDOW_SECONDS`` ago is not refreshed again: its record is
        returned, and a 401 to its token is a refusal (counted by ``_refused_fresh``; None). None when there is no
        record to use: the row was replaced, removed or cannot be opened, or the owner must sign in again
        (``needs_sign_in``: a definitive refusal from the provider). Raises ``ProviderUnreachable`` when the provider
        could not be reached, now or less than ``PROVIDER_HOLD_SECONDS`` ago (then no request is made). It runs in a
        task this manager owns, which the caller only waits for (shielded): a caller that is cancelled leaves the
        refresh to finish and store what the provider rotated. A successful refresh is handed to the live server of
        that row too, so its next request and its result scrub use it."""
        if self._closed:  # nothing new is asked of a provider once the client is closed
            return None
        task = asyncio.create_task(self._refresh_locked(name, url, secret_id, sent), name=f"mcp-client-refresh-{name}")
        self._refresh_tasks.add(task)
        task.add_done_callback(self._refresh_tasks.discard)
        return await asyncio.shield(task)

    async def _refresh_locked(self, name: str, url: str, secret_id: uuid.UUID, sent: str | None) -> OAuthRecord | None:
        """``_refresh``'s work, under the server's lock: the row is read again (another caller may have refreshed it),
        must still be ``secret_id``, and is refreshed only if still stale; the new record is stored, in place and only
        while the row is still ``secret_id`` (``UPDATE ... WHERE id``), before anything uses it."""
        lock = self._refresh_locks.setdefault(name, asyncio.Lock())
        async with lock:
            if self.needs_sign_in(name, secret_id):
                return None
            try:
                secret = (await self._registry.list_secrets()).get(name)
                if secret is None or secret.id != secret_id or secret.kind != "oauth":
                    return None  # replaced, removed or signed out meanwhile: what it held is gone
                record, _problem = self._opened(name, url, secret)
                box = self._keys.box()
                if not isinstance(record, OAuthRecord) or box is None:
                    return None
                recent = self._recently_refreshed(name, secret_id, record)
                if sent is not None:
                    if record.access_token != sent:
                        return record  # refreshed meanwhile: retry with the stored token
                    if recent:  # a 401 to a token the provider gave moments ago: a refusal, not a reason to refresh
                        self._refused_fresh(name, secret_id, record)
                        return None
                elif recent or not expiring(record):
                    return record
                if self.provider_unreachable(name, secret_id):
                    raise ProviderUnreachable()
                try:
                    fresh = await refresh_tokens(record, self._oauth_http, self._allow)
                except OAuthProblem as exc:
                    failures = self._transient_failures.get(name)
                    count = failures[1] if failures is not None and failures[0] == secret_id else 0
                    if exc.answered is not None and 400 <= exc.answered < 500 and exc.answered not in (400, 401, 429):
                        count += 1
                        self._transient_failures[name] = (secret_id, count)
                    if exc.transient and count < TRANSIENT_FAILURES_MAX:
                        self._held[name] = (secret_id, self._clock() + PROVIDER_HOLD_SECONDS)
                        logger.warning(
                            "MCP client: server %r: the token refresh failed (%s): tried again in %d s",
                            name,
                            exc,
                            PROVIDER_HOLD_SECONDS,
                        )
                        raise ProviderUnreachable() from None
                    self._needs_sign_in[name] = secret_id
                    logger.warning(
                        "MCP client: server %r: the token refresh failed (%s): it needs a new sign-in", name, exc
                    )
                    return None
                sealed = seal_oauth(box, aad_fields(self._agent_id, name, url, "oauth"), fresh)
                if not await self._registry.update_secret_in_place(secret_id, sealed):
                    logger.warning("MCP client: server %r: its credential changed during a refresh; discarded", name)
                    return None
                self._refreshed[name] = (secret_id, self._clock())
                self._fresh_refusals.pop(name, None)
                self._held.pop(name, None)
                self._transient_failures.pop(name, None)
                live = self._servers.get(name)
                if live is not None and live.oauth is not None and live.secret_id == secret_id and live.url == url:
                    live.oauth.adopt(fresh)  # a route's refresh too: the server sends it, and its scrub knows it
                return fresh
            except ProviderUnreachable:
                raise
            except Exception as exc:  # the class only, never a traceback: its text can repeat what a server was sent
                logger.warning("MCP client: server %r: the token refresh failed (%s)", name, error_class(exc))
                return None

    def credentials_refused(self) -> dict[str, int]:
        """The servers whose last refusal was a 401, or a 403 with a stored credential, with that status (cleared
        when a call is answered on a connection that was refused nothing, or by a credential change)."""
        return {
            name: server.refusal
            for name, server in self._servers.items()
            if server.refusal == 401 or (server.refusal == 403 and server.has_credentials)
        }

    async def close(self) -> None:
        """Unregister every tool this manager registered and close every connection (bounded wait, then cancel). A
        handler called after this answers with an error and opens no connection; a later ``reload`` does nothing."""
        self._closed = True
        for name in self._registered:
            self._registrar.unregister(name)
            forget_mcp_tool(name)
        self._registered.clear()
        self._sources.clear()
        tasks: set[asyncio.Task[Any]] = set()
        for server in self._servers.values():
            server.close()
            tasks |= server.tasks()
        # A refresh in flight stores what the provider rotated: it gets its own deadline, the OAuth request's bound
        # and the write (REFRESH_DRAIN_SECONDS), not the connections' five seconds.
        await asyncio.gather(_wait_ended(tasks), _wait_ended(set(self._refresh_tasks), REFRESH_DRAIN_SECONDS))

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
        """One call, as the dispatcher's result (``_send``). A call that failed (never one the server answered) on a
        connection refused with a 401 (in the middle of the session, at the connect, or while that failed connect
        backs off) is the fixed sentence that asks the owner for the credential; one refused with a 403 while a
        credential is stored is the sentence that it may be wrong or lack access. A 403 without a stored credential
        keeps its plain error."""
        result, refused = await self._send(server, raw_name, name, arguments)
        if refused is None or self._closed or server.closed:
            return result
        server_name = server.config.name
        if refused == _UNREACHABLE:
            logger.warning("MCP client: %s failed: the sign-in provider could not be reached", name)
            return _error(
                f'the sign-in provider of the MCP server "{server_name}" could not be reached: the call can be tried '
                "again in a moment."
            )
        if refused == 401:
            logger.warning("MCP client: %s failed: the server answered 401, or it needs a new sign-in", name)
            if server.oauth is not None:
                return _sign_in_error(server_name)
            if server.has_credentials:
                return _error(
                    f'the MCP server "{server_name}" refused the stored credentials. The owner can replace them on '
                    "the Integrations screen."
                )
            return _error(
                f'the MCP server "{server_name}" asks for credentials. The owner can add them on the Integrations '
                "screen."
            )
        if refused == 403 and server.has_credentials:
            logger.warning("MCP client: %s failed: the server answered 403", name)
            return _error(
                f'the MCP server "{server_name}" answered 403 (forbidden). The stored credentials may be wrong or '
                "lack access. The owner can replace them on the Integrations screen."
            )
        return result

    async def _send(
        self, server: _Server, raw_name: str, name: str, arguments: dict[str, Any]
    ) -> tuple[dict[str, Any], int | None]:
        """One call, as the dispatcher's result, and the 401 or 403 its connection was refused with when the call
        failed with an exception (None for a result the server returned, or a call never sent). Never raises but
        ``CancelledError``, which it re-raises: the runner's tool timeout, or a shutdown."""
        server_name = server.config.name
        # The credential values as the call is sent: a refresh (or two) during the call must not drop them from the
        # scrub of its own result (a server can echo the token the call carried).
        sent_values = server.withheld
        closed = _error(f'the MCP client is closed: the server "{server_name}" is not called.')
        if self._closed:
            return closed, None
        if server.oauth is not None and self.needs_sign_in(server_name, server.secret_id):
            return _sign_in_error(server_name), None  # fail fast: the provider is not contacted again
        timeout = server.config.call_timeout_seconds
        session: Any = None
        conn: _Connection | None = None

        def refusal() -> int | None:
            # The connection the call used, else the attempt it waited on (a failed connect, or one backing off). A
            # request not sent for want of a sign-in counts as a 401; one not sent because the sign-in provider could
            # not be reached has its own answer.
            used = conn if conn is not None else server.current()
            if used is None:
                return None
            if used.oauth_failure == "unreachable":
                return _UNREACHABLE
            return 401 if used.oauth_failure == "sign_in" else used.refused

        try:
            # One budget for the wait for a call slot, the connect and the call.
            async with asyncio.timeout(timeout):
                async with server.semaphore:
                    if self._closed:  # closed while this call waited for a slot: open nothing
                        return closed, None
                    conn = await server.connection()  # raises ClientClosed when closed meanwhile
                    session = conn.session
                    # The server's own name for the tool; the call ends with its connection.
                    raw = await _unless_ended(conn.stop, lambda: session.call_tool(raw_name, arguments))
        except ClientClosed:
            if self._closed:
                return closed, None
            # A reload closed this server (removed, disabled, or moved to another URL) while the call waited.
            return _error(f'the MCP server "{server_name}" was removed or changed: the call was not sent.'), None
        except HandshakeTimedOut:
            bound = min(timeout, HANDSHAKE_MAX_SECONDS)
            message = f'the MCP server "{server_name}" did not finish its handshake within {bound} s: try again.'
            return _error(message), refusal()
        except ConnectionEnded:
            logger.warning("MCP client: %s failed: its connection ended during the call", name)
            return _error(f'the connection to the MCP server "{server_name}" ended during the call.'), refusal()
        except TimeoutError:
            logger.warning("MCP client: %s timed out after %d s", name, timeout)
            return _error(f'the call to the MCP server "{server_name}" timed out after {timeout} s.'), refusal()
        except ServerUnavailable:
            return _error(f'the MCP server "{server_name}" is not reachable now: try again later.'), refusal()
        except ServerErrorReply as exc:
            # The server answered: the session is still good, and the calls sharing it go on.
            if conn is not None and conn.refused is None:
                server.refusal = None
            logger.warning("MCP client: %s failed: the server answered with error code %d", name, exc.code)
            return _error(f'the MCP server "{server_name}" answered the call with an error (code {exc.code}).'), None
        except Exception as exc:
            # The session is in an unknown state (the server may have dropped it): the next call connects again. A
            # failed connect left no session to end.
            if session is not None:
                server.drop(session)
            logger.warning("MCP client: %s failed (%s)", name, error_class(exc))
            return _error(f'the call to the MCP server "{server_name}" failed: {safe_error(exc)}.'), refusal()
        if conn.refused is None:  # answered on a connection that was refused nothing: the listing's status clears
            server.refusal = None
        # What the call was sent with, and what is held now.
        withheld = tuple(sorted(set(sent_values) | set(server.withheld), key=len, reverse=True))
        if result_chars(raw) > OFF_LOOP_RESULT_CHARS:
            text, is_error = await asyncio.to_thread(flatten_result, raw, self._max_chars, withheld)
        else:
            text, is_error = flatten_result(raw, self._max_chars, withheld)
        return {"content": [{"type": "text", "text": wrap_result(server_name, text)}], "is_error": is_error}, None

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


def _sign_in_error(server_name: str) -> dict[str, Any]:
    return _error(f'the MCP server "{server_name}" needs the owner to sign in again on the Integrations screen.')


async def _wait_ended(tasks: set[asyncio.Task[Any]], timeout: float | None = None) -> None:
    """Wait for closing tasks, at most ``timeout`` (``_CLOSE_WAIT_SECONDS`` by default), then cancel those still
    running."""
    if not tasks:
        return
    _done, pending = await asyncio.wait(tasks, timeout=_CLOSE_WAIT_SECONDS if timeout is None else timeout)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
