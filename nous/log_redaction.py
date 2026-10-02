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
logger, redacts a bot token in whatever the root handlers write, and keeps the
header encoder's logger above DEBUG.
"""

from __future__ import annotations

import logging
import re

_LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"

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
