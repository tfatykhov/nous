"""Configure a Nous process's logging so that credentials stay out of it.

Two libraries write credentials to their own loggers, in lines Nous never
wrote itself:

- The HTTP client logs every request URL at INFO, and the Telegram Bot API
  carries the bot token in the URL path
  (``https://api.telegram.org/bot<token>/<method>``).
- The HTTP/2 header encoder logs every header it encodes at DEBUG, the model
  API's key among them.

So the entry points do not configure logging themselves. They call
``configure_logging``, which is the one place that does: it sets up the root
logger, redacts a bot token in whatever the root handlers write, keeps the
header encoder's logger above DEBUG, and installs a filter on the HTTP
client's loggers that redacts a bot token in each record itself, so no
handler gets one (the HTTP client's request lines are redacted in the record).
"""

from __future__ import annotations

import logging
import re

_LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"
# The HTTP client's loggers, and those of the transport under it. A filter on a logger applies to its own records
# only, not to those of the loggers under it, so each is named.
HTTP_CLIENT_LOGGERS = (
    "httpx",
    "httpcore",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
)

# A bot token as it appears in a URL path: ``/bot<bot id>:<secret>``. Matched
# by shape, not by host, so a self-hosted Bot API server is covered as well.
# The lengths keep look-alikes (``/bot3:latest``, ``/bot42:8080``) as they are.
_BOT_TOKEN_IN_PATH = re.compile(r"(/bot)\d{5,}:[A-Za-z0-9_-]{10,}")


def redact(text: str) -> str:
    """Return ``text`` with every bot token in a URL path replaced."""
    return _BOT_TOKEN_IN_PATH.sub(r"\1<redacted>", text)


class _RedactingFormatter(logging.Formatter):
    """Formats with the handler's own formatter, then redacts the result.

    The whole formatted line goes through ``redact``: the message with its
    arguments, the exception text and the traceback.
    """

    def __init__(self, inner: logging.Formatter | None) -> None:
        super().__init__()
        self._inner = inner or logging.Formatter()

    def format(self, record: logging.LogRecord) -> str:
        return redact(self._inner.format(record))


class RedactingFilter(logging.Filter):
    """Redacts a bot token in a record's message, its arguments included, so the record itself never holds one."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True  # a record that cannot be formatted is left to the handler's own error path
        redacted = redact(message)
        if redacted != message:
            record.msg, record.args = redacted, None
        return True


def install_log_redaction() -> None:
    """Redact what every handler of the root logger writes.

    Calling it again wraps only handlers added since. A handler added to the
    root logger later is not covered, and neither is a handler that another
    logger owns (uvicorn's, or SQLAlchemy's echo handler at DEBUG): what
    reaches those is written as it is.
    """
    for handler in logging.getLogger().handlers:
        if not isinstance(handler.formatter, _RedactingFormatter):
            handler.setFormatter(_RedactingFormatter(handler.formatter))


def configure_logging(level: int) -> None:
    """Set up the root logger for a Nous process. Call it once, first thing."""
    logging.basicConfig(level=level, format=_LOG_FORMAT)
    install_log_redaction()
    # The HTTP/2 header encoder's DEBUG lines are a dump of every header sent.
    logging.getLogger("hpack").setLevel(logging.INFO)
    # The HTTP client's request lines are redacted in the record.
    for name in HTTP_CLIENT_LOGGERS:
        client_logger = logging.getLogger(name)
        if not any(isinstance(f, RedactingFilter) for f in client_logger.filters):
            client_logger.addFilter(RedactingFilter())
