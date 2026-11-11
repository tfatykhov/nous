"""F111: the owner's close of a commitment over REST (``/drop C-n``, ``/done C-n`` in Telegram).

Same posture as ``intention_routes``: no in-app authentication (the gate is the network and the bot's owner-chat
check), no agent tool reaches these routes, and the answer is fixed vocabulary plus the id, never the row's text.
"""

from __future__ import annotations

import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from nous.brain.commitments import CommitmentStore, format_short_id, parse_short_id

logger = logging.getLogger(__name__)

# The owner's verb -> the status it sets and the close_reason it records.
_CLOSES: dict[str, tuple[str, str]] = {"drop": ("cancelled", "owner_drop"), "done": ("done", "owner_done")}


def build_commitment_routes(*, database: Any, settings: Any) -> list[Route]:
    """The owner's close routes, over a store for ``settings.agent_id``."""
    store = CommitmentStore(database, settings.agent_id)

    async def close(request: Request) -> JSONResponse:
        """POST /commitments/{short_id}/{drop|done}: 200 with the row's id and status (``changed`` false when it was
        already closed), 404 for an id no row has, 400 for an id that is not one."""
        short_id = parse_short_id(str(request.path_params["short_id"]))
        verb = request.path_params["verb"]
        if verb not in _CLOSES:
            return JSONResponse({"error": "unknown action"}, status_code=404)
        if short_id is None:
            return JSONResponse({"error": "id must look like C-42"}, status_code=400)
        status, reason = _CLOSES[verb]
        try:
            row, changed = await store.mark(short_id, status, close_reason=reason)
        except Exception:
            logger.exception("F111: closing commitment %s failed", format_short_id(short_id))
            return JSONResponse({"error": "the close could not be processed"}, status_code=500)
        if row is None:
            return JSONResponse({"error": "no such commitment"}, status_code=404)
        return JSONResponse({"short_id": row.label, "status": row.status, "changed": changed})

    return [Route("/commitments/{short_id}/{verb}", close, methods=["POST"])]
