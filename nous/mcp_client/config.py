"""F112 MCP client: a server row and a tool row, validated.

No SDK import. ``url_problem`` is also the transport's first check, so a URL that a row could not hold is never
opened either.
"""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

# The middle part of a tool's name (mcp__<server>__<tool>): short, lowercase, and no "__", which would make the name
# ambiguous. The migration's CHECK holds the same shape.
SERVER_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,23}")

Permission = Literal["allow", "deny", "ask"]


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


class ServerConfig(BaseModel):
    """One external MCP server (a ``nous_system.mcp_servers`` row)."""

    name: str
    url: str = Field(repr=False)
    transport: Literal["streamable_http"] = "streamable_http"
    enabled: bool = True
    background: bool = False  # the owner's "available to background work" switch
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
