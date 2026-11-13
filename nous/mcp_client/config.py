"""F112 MCP client: a server row, a tool row and the headers of a stored credential, validated.

No SDK import. ``url_problem`` is also the transport's first check, so a URL that a row could not hold is never
opened either.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

# The middle part of a tool's name (mcp__<server>__<tool>): short, lowercase, and no "__", which would make the name
# ambiguous. The migration's CHECK holds the same shape.
SERVER_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,23}")

Permission = Literal["allow", "deny", "ask"]


class BlockedAddress(Exception):
    """A URL or host the client's address guard refuses (raised by ``transport``; defined here, with no SDK import,
    so the manager can tell a refusal from a failed connection)."""


def url_problem(url: str) -> str | None:
    """Why ``url`` cannot be an MCP server's address, or None when it can.

    https only, with a host, and with no user, password, query string or fragment: those are where a credential
    would sit, and the HTTP client logs every request URL at INFO. The answer never repeats the URL.
    """
    try:
        parts = urlsplit(url)
        _ = parts.port  # raises ValueError for a port that is not a number in range
    except ValueError:
        return "not a valid URL"
    if parts.scheme != "https":
        return "only https URLs are accepted"
    if not parts.hostname:
        return "the URL has no host"
    if parts.username is not None or parts.password is not None:
        return "the URL may not carry a user name or password"
    if parts.query or parts.fragment:
        return "the URL may not carry a query string or fragment"
    return None


def parse_host_list(value: str) -> frozenset[str]:
    """``NOUS_MCP_CLIENT_ALLOW_PRIVATE_HOSTS`` as a set of lowercase host names (a comma list; blanks ignored)."""
    return frozenset(host.strip().lower() for host in value.split(",") if host.strip())


# Characters a tool name may not carry to be pinned: the pin syntax uses them.
PIN_SEPARATORS = frozenset(",;|=")


@dataclass(frozen=True)
class Pin:
    """One entry of ``NOUS_MCP_CLIENT_PINNED_SERVERS``: the server's host (with ``:port`` when its URL names one)
    and the server's own names of the tools that may register."""

    host: str
    tools: frozenset[str]


def parse_pins(value: str) -> dict[str, Pin]:
    """``NOUS_MCP_CLIENT_PINNED_SERVERS`` as ``{name: Pin}``. The syntax: entries separated by ``,``, each
    ``name=host[:port];tool|tool`` (``deepwiki=mcp.deepwiki.com;read_wiki_structure|ask_question``). The host is
    compared lowercase; an IPv6 host is written in brackets. An entry without ``=`` or with an empty name or host is
    ignored (the setting is still a ceiling: a server it does not pin registers nothing); an entry with no tool list
    pins the server with no tool, so nothing of it registers. A WARNING (names
    only) for an entry that is ignored (``a=h;t,u`` leaves ``u`` as an entry of its own: tools are separated by
    ``|``), for a tool list that carries ``;`` or ``=``, and for a server pinned twice (the last entry counts)."""
    pins: dict[str, Pin] = {}
    for entry in value.split(","):
        if not entry.strip():
            continue
        name, sep, rest = entry.partition("=")
        host, _semicolon, tools = rest.partition(";")
        if not (sep and name.strip() and host.strip()):
            logger.warning("MCP client: pin entry %r is not name=host[:port];tool|tool; ignored", entry.strip()[:80])
            continue
        if any(ch in tools for ch in ";="):
            logger.warning(
                "MCP client: the pin of server %r has a malformed tool list (tools are separated by '|')", name.strip()
            )
        if name.strip() in pins:
            logger.warning("MCP client: server %r is pinned twice; the last entry counts", name.strip())
        names = frozenset(tool.strip() for tool in tools.split("|") if tool.strip())
        pins[name.strip()] = Pin(host.strip().lower(), names)
    return pins


def pin_host(url: str) -> str:
    """What a pin's host part must be for ``url``: its lowercase host (an IPv6 one in brackets), with ``:port`` only
    when the URL names a port, so a stored URL with a port matches only a pin that names the same port."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    return host if parts.port is None else f"{host}:{parts.port}"


HEADERS_MAX = 8
# At least the result scrub's shortest needle (bridge.WITHHELD_MIN_CHARS): a shorter value could never be withheld.
HEADER_VALUE_MIN_CHARS = 8
HEADER_VALUE_MAX_CHARS = 4096
_HEADER_NAME = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}")
# Headers the transport, the SDK or HTTP itself owns: a stored credential may not replace them.
_RESERVED_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "content-type",
        "content-encoding",
        "transfer-encoding",
        "trailer",
        "connection",
        "keep-alive",
        "proxy-connection",
        "expect",
        "accept",
        "accept-encoding",
        "cookie",
        "origin",
        "user-agent",
        "mcp-session-id",
        "mcp-protocol-version",
        "last-event-id",
        "te",
        "upgrade",
        "proxy-authorization",
    }
)
_RESERVED_HEADER_PREFIX = "x-nous-"
# A value is printable ASCII without '"' and '\': the result scrub runs on text json.dumps may have escaped, and these
# two are the characters it changes.
_HEADER_VALUE = re.compile(r"[\x20\x21\x23-\x5b\x5d-\x7e]+")


def validate_headers(value: Any) -> dict[str, str]:
    """``value`` as the headers a server's credential may hold: 1 to ``HEADERS_MAX`` names (a token of at most 64
    characters, none the transport, the SDK or HTTP owns, none twice whatever the case) with values of
    ``HEADER_VALUE_MIN_CHARS`` to ``HEADER_VALUE_MAX_CHARS`` printable ASCII characters (no ``"``, no ``\\``, no
    space at either end; a shorter value could never be withheld from a result). Raises
    ``ValueError`` naming the header (by position when its name is not a valid one) and the rule, never a value."""
    if not isinstance(value, dict):
        raise ValueError("headers must be an object of header name to value")
    if not 1 <= len(value) <= HEADERS_MAX:
        raise ValueError(f"give 1 to {HEADERS_MAX} headers")
    seen: set[str] = set()
    for position, (name, item) in enumerate(value.items(), start=1):
        if not isinstance(name, str) or not _HEADER_NAME.fullmatch(name):
            raise ValueError(
                f"header {position}: a name is 1 to 64 letters, digits or any of !#$%&'*+.^_`|~- (no space)"
            )
        lowered = name.lower()
        if lowered in _RESERVED_HEADERS or lowered.startswith(_RESERVED_HEADER_PREFIX):
            raise ValueError(f"header {name}: the client sets this header itself, so it cannot be stored")
        if lowered in seen:
            raise ValueError(f"header {name}: named twice (names are compared without case)")
        seen.add(lowered)
        if not isinstance(item, str) or not HEADER_VALUE_MIN_CHARS <= len(item) <= HEADER_VALUE_MAX_CHARS:
            raise ValueError(
                f"header {name}: a value is {HEADER_VALUE_MIN_CHARS} to {HEADER_VALUE_MAX_CHARS} characters"
            )
        if not _HEADER_VALUE.fullmatch(item):
            raise ValueError(f'header {name}: a value holds printable ASCII characters only, and no " or \\')
        if item != item.strip(" "):
            raise ValueError(f"header {name}: a value may not start or end with a space")
    return dict(value)


class ServerConfig(BaseModel):
    """One external MCP server (a ``nous_system.mcp_servers`` row)."""

    name: str
    url: str = Field(repr=False)
    transport: Literal["streamable_http"] = "streamable_http"
    enabled: bool = True
    background: bool = False  # the row's background switch (and the host's list, when one is set)
    call_timeout_seconds: int = Field(default=30, ge=1, le=600)

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not SERVER_NAME.fullmatch(value) or "__" in value:
            raise ValueError("a server name is 1 to 24 of a-z, 0-9, _ and -, starts with a letter or digit, no '__'")
        return value

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        problem = url_problem(value)
        if problem is not None:
            raise ValueError(problem)
        return value


class ToolConfig(BaseModel):
    """One tool a server listed, with the owner's permission (a ``nous_system.mcp_server_tools`` row)."""

    server: str
    tool: str = Field(min_length=1)  # the server's own name for it
    description: str = ""
    input_schema: dict[str, Any]
    permission: Permission = "deny"
