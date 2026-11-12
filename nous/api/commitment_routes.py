"""F111: the owner's close of a commitment over REST (``/drop C-n``, ``/done C-n`` in Telegram).

The ledger measures the agent, so the agent must not be able to close a row as the owner: its ``bash`` and
``run_python`` can reach this server on loopback. Each call therefore carries the owner's shared secret in
``X-Nous-Owner-Secret``, which the Telegram bot holds (``NOUS_OWNER_ROUTE_SECRET``, in the bot's process only); this
process holds only its SHA-256 (``NOUS_OWNER_ROUTE_SECRET_SHA256``), compared in constant time, so nothing the agent
can read here lets it forge the header. Without the digest the routes refuse every call (fail closed). The answer is
fixed vocabulary plus the id, never the row's text; the ``actor`` the bot sends is stored in the row's ``evidence``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from nous.brain.commitments import CommitmentStore, clean_text, format_short_id, parse_short_id

logger = logging.getLogger(__name__)

# The header the bot sends, and the env var that holds the secret in the bot's process (never in this one).
OWNER_SECRET_HEADER = "X-Nous-Owner-Secret"
OWNER_SECRET_ENV = "NOUS_OWNER_ROUTE_SECRET"
_ACTOR_MAX = 100

# The owner's verb -> the status it sets and the close_reason it records.
_CLOSES: dict[str, tuple[str, str]] = {"drop": ("cancelled", "owner_drop"), "done": ("done", "owner_done")}


def owner_secret_matches(supplied: str | None, digest: str) -> bool:
    """True when ``supplied`` hashes to ``digest`` (hex SHA-256), compared in constant time. False when either is
    empty."""
    if not supplied or not digest:
        return False
    return hmac.compare_digest(hashlib.sha256(supplied.encode()).hexdigest(), digest)


def build_commitment_routes(*, database: Any, settings: Any) -> list[Route]:
    """The owner's close routes, over a store for ``settings.agent_id``."""
    store = CommitmentStore(database, settings.agent_id)
    digest = settings.owner_route_secret_sha256.strip().lower()
    if not digest:
        logger.warning(
            "F111: NOUS_OWNER_ROUTE_SECRET_SHA256 is not set, so POST /commitments/{id}/drop|done refuses every call "
            "(Telegram /drop and /done will not work)"
        )
    if os.environ.get(OWNER_SECRET_ENV):
        logger.warning(
            "F111: %s is set in the API process; it belongs to the Telegram bot only (the agent's run_python runs "
            "in this process and can read its environment)",
            OWNER_SECRET_ENV,
        )

    async def close(request: Request) -> JSONResponse:
        """POST /commitments/{short_id}/{drop|done}: 200 with the row's id and status (``changed`` false when it was
        already closed), 404 for an id no row has, 400 for an id that is not one, 403 without the owner's secret."""
        if not owner_secret_matches(request.headers.get(OWNER_SECRET_HEADER), digest):
            logger.warning(
                "F111: refused a commitment close: %s",
                "NOUS_OWNER_ROUTE_SECRET_SHA256 is not set" if not digest else f"no valid {OWNER_SECRET_HEADER}",
            )
            return JSONResponse({"error": "forbidden"}, status_code=403)
        short_id = parse_short_id(str(request.path_params["short_id"]))
        verb = request.path_params["verb"]
        if verb not in _CLOSES:
            return JSONResponse({"error": "unknown action"}, status_code=404)
        if short_id is None:
            return JSONResponse({"error": "id must look like C-42"}, status_code=400)
        status, reason = _CLOSES[verb]
        try:
            body = await request.json()
        except ValueError:
            body = None
        actor = body.get("actor") if isinstance(body, dict) else None
        if not actor:
            logger.warning("F111: the close of %s names no actor; none is recorded", format_short_id(short_id))
        evidence = {"actor": clean_text(str(actor))[:_ACTOR_MAX]} if actor else None
        try:
            row, changed = await store.mark(short_id, status, close_reason=reason, evidence=evidence)
        except Exception:
            logger.exception("F111: closing commitment %s failed", format_short_id(short_id))
            return JSONResponse({"error": "the close could not be processed"}, status_code=500)
        if row is None:
            return JSONResponse({"error": "no such commitment"}, status_code=404)
        return JSONResponse({"short_id": row.label, "status": row.status, "changed": changed})

    return [Route("/commitments/{short_id}/{verb}", close, methods=["POST"])]
