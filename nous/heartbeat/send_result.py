"""F111 PR B1: what a Telegram send is known to have done (``HeartbeatRunner._send_telegram``).

The overdue watcher pings a row at most once, so it needs to tell "not sent" from "maybe sent":

- ``SENT``: Telegram answered with a status below 400.
- ``NOT_SENT``: certainly not delivered: Telegram is not configured, the request could not be built or the connection
  failed before it was written (a bad URL, refused, DNS, connect or pool timeout, a proxy that refused it), or Telegram
  answered with an HTTP error (429 and 5xx included).
- ``UNKNOWN``: the request may have been delivered: a timeout or a dropped connection after it was written, or any
  other exception. The watcher counts these and stops after a few.
"""

from __future__ import annotations

import enum

import httpx


class SendResult(enum.Enum):
    SENT = "sent"
    NOT_SENT = "not_sent"
    UNKNOWN = "unknown"


# httpx raises these before the request is on the wire (ConnectError covers a refused connection and a DNS failure; a
# bad character in the bot token is an InvalidURL; a ProxyError is the proxy refusing to connect to Telegram).
_BEFORE_THE_REQUEST: tuple[type[BaseException], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.InvalidURL,
    httpx.UnsupportedProtocol,
    httpx.ProxyError,
)


def result_for_status(status: object) -> SendResult:
    """``NOT_SENT`` for an HTTP error status, else ``SENT`` (a stub client that answers with no integer status is
    treated as the sent message it stands for, as the logging around it always has)."""
    return SendResult.NOT_SENT if isinstance(status, int) and status >= 400 else SendResult.SENT


def result_for_exception(exc: BaseException) -> SendResult:
    """``NOT_SENT`` when the exception proves the request never left, else ``UNKNOWN`` (a read or write timeout, a
    dropped connection, a protocol error, or anything this cannot place)."""
    return SendResult.NOT_SENT if isinstance(exc, _BEFORE_THE_REQUEST) else SendResult.UNKNOWN
