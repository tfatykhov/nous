"""Post one message to chat with the owner's secret: ``python -m nous.ping SESSION_ID MESSAGE``.

Run it where the owner's secret is, the Telegram bot's container:
``docker exec nous-telegram python -m nous.ping pr-review-897 "Please review PR 897"``. It reads ``NOUS_API_URL``
(default ``http://localhost:8000``) and ``NOUS_OWNER_ROUTE_SECRET`` from its environment, posts
``{"message", "session_id"}`` to ``/chat`` with ``X-Nous-Owner-Secret``, prints ``HTTP <status>`` and then the reply's
``response`` field (at most 4000 characters), and exits 0 on a 200 and 1 on any other answer. It exits 2 without
posting when the secret is not set here, the session id is not 1 to 100 of ``A-Z a-z 0-9 . _ -``, or the message is
empty. It never prints the secret or a request header, and an error by its class only.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import httpx

OWNER_SECRET_HEADER = "X-Nous-Owner-Secret"
SESSION_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,100}")
MAX_PRINTED_CHARS = 4000
TIMEOUT_SECONDS = 600
USAGE = "usage: python -m nous.ping SESSION_ID MESSAGE"


def _fail(line: str) -> int:
    print(line, file=sys.stderr)
    return 2


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    post: Callable[..., Any] = httpx.post,
) -> int:
    """Post the message and print the answer; the exit code (0, 1 or 2) as described above."""
    args = list(sys.argv[1:] if argv is None else argv)
    environ = os.environ if env is None else env
    if len(args) != 2:
        return _fail(USAGE)
    session_id, message = args
    secret = environ.get("NOUS_OWNER_ROUTE_SECRET", "")
    if not secret:
        return _fail("NOUS_OWNER_ROUTE_SECRET is not set here: run this in the container that holds it (the bot's)")
    if not SESSION_ID_RE.fullmatch(session_id):
        return _fail("the session id must be 1 to 100 letters, digits, dots, underscores or dashes")
    if not message:
        return _fail("the message is empty")
    url = environ.get("NOUS_API_URL", "http://localhost:8000").rstrip("/") + "/chat"
    try:
        response = post(
            url,
            json={"message": message, "session_id": session_id},
            headers={OWNER_SECRET_HEADER: secret},
            timeout=TIMEOUT_SECONDS,
        )
    except Exception as exc:
        # The class only: an error's text can repeat the address or what was sent.
        print(f"the request failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    print(f"HTTP {response.status_code}")
    try:
        body = response.json()
    except Exception:
        body = None
    reply = body.get("response") if isinstance(body, dict) else None
    if isinstance(reply, str):
        print(reply[:MAX_PRINTED_CHARS])
    return 0 if response.status_code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
