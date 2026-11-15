"""Goals PR 0c: the halt switch over REST.

- ``GET /background/state``: whether background work is halted, since when, by whom, and whether the halt is saved.
- ``POST /background/halt``: halts at once and runs the cancel sweep. No secret: stopping is the safe direction.
- ``POST /background/resume``: needs the owner route secret (``X-Nous-Owner-Secret``, the Telegram bot's) or the
  login proxy's (``X-Nous-Proxy-Secret``). This process holds only their SHA-256 digests, compared in constant time.
  With neither digest set, every resume is refused (fail closed). Refused under the host's hold.

Both POST routes take only the media type ``application/json`` (415 otherwise), so a cross-site page cannot send them
through the owner's browser as a simple request. Answers are fixed vocabulary plus counts; header values are never
logged.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from nous import background_halt, owner_notice
from nous.api.commitment_routes import OWNER_SECRET_HEADER, owner_secret_matches
from nous.background_halt import AuditEntry

logger = logging.getLogger(__name__)

PROXY_SECRET_HEADER = "X-Nous-Proxy-Secret"  # the same header as the MCP client routes (pinned by a test)
RESUMED_TEXT = (
    "Background work is running again. Work that was cancelled was not restarted. "
    "One-time schedules that came due while halted run now."
)
NOT_HALTED_TEXT = "Background work is not halted."
NOT_CONFIGURED_TEXT = (
    "Resume is not configured on this server: set NOUS_OWNER_ROUTE_SECRET_SHA256 or NOUS_PROXY_SECRET_SHA256."
)
_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}")
_NO_STORE = {"Cache-Control": "no-store"}


def _digest(raw: Any) -> str:
    """The setting as a lowercase hex SHA-256, or "" when it is not one (fail closed). The first field only:
    ``sha256sum`` prints the digest followed by "  -"."""
    fields = raw.split() if isinstance(raw, str) else []
    digest = fields[0].lower() if fields else ""
    return digest if _SHA256_HEX_RE.fullmatch(digest) else ""


def _is_json(request: Request) -> bool:
    media_type = (request.headers.get("content-type", "") or "").split(";")[0]
    return media_type.strip().lower() == "application/json"


async def _body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _present(component: Any) -> Any:
    """main.py passes lazy proxies, falsy while their component is None."""
    return component if component else None


def build_background_routes(*, settings: Any, sweeper: Any = None, continuation_runner: Any = None) -> list[Route]:
    """The three routes, over the process's installed halt (``background_halt.current()``)."""
    owner_digest = _digest(getattr(settings, "owner_route_secret_sha256", ""))
    proxy_digest = _digest(getattr(settings, "proxy_secret_sha256", "")) or _digest(
        getattr(settings, "mcp_client_ui_secret_sha256", "")
    )
    if not owner_digest and not proxy_digest:
        logger.warning(
            "Background halt: neither NOUS_OWNER_ROUTE_SECRET_SHA256 nor NOUS_PROXY_SECRET_SHA256 is set, so "
            "POST /background/resume refuses every call; a stored halt can then be cleared only on the host"
        )

    def _verified(request: Request) -> bool:
        return owner_secret_matches(request.headers.get(OWNER_SECRET_HEADER), owner_digest) or owner_secret_matches(
            request.headers.get(PROXY_SECRET_HEADER), proxy_digest
        )

    async def state(request: Request) -> JSONResponse:
        """GET /background/state."""
        return JSONResponse(background_halt.current().snapshot(), headers=_NO_STORE)

    async def halt(request: Request) -> JSONResponse:
        """POST /background/halt: 200 with the sweep's report and its one message; a repeat halts again and re-runs
        the sweep."""
        if not _is_json(request):
            return JSONResponse({"error": "Content-Type must be application/json"}, status_code=415)
        body = await _body(request)
        verified = _verified(request)
        current = background_halt.current()
        try:
            outcome = await current.halt(
                actor=body.get("actor"), surface=body.get("surface"), reason=body.get("reason"), verified=verified
            )
        except RuntimeError:
            return JSONResponse({"error": "the halt is not available"}, status_code=503)
        swept = _present(sweeper)
        if swept is not None:
            report = await swept.run(actor=background_halt.clean(body.get("actor"), 100))
            report_body, text = report.as_dict(), report.describe()
        else:
            report_body, text = None, "Background work is halted."
        if background_halt.clean_surface(body.get("surface")) in ("agent", "host"):
            # Nobody is waiting on the answer: the owner gets the one message in their chat.
            await owner_notice.send_owner_text(settings, text)
        return JSONResponse(
            {
                "halted": True,
                "already": outcome.already,
                "verified": verified,
                "persisted": current.persisted,
                "report": report_body,
                "text": text,
            },
            headers=_NO_STORE,
        )

    async def resume(request: Request) -> JSONResponse:
        """POST /background/resume: 200 when resumed (or not halted), 403 without a valid secret or with no digest
        configured, 409 under the host's hold or when a halt arrived during it, 503 when it could not be saved."""
        if not _is_json(request):
            return JSONResponse({"error": "Content-Type must be application/json"}, status_code=415)
        body = await _body(request)
        current = background_halt.current()
        actor = background_halt.clean(body.get("actor"), 100)
        via = background_halt.clean_surface(body.get("surface"))
        if not owner_digest and not proxy_digest:
            await current.record(AuditEntry("resume_refused", actor, via, False, None, {"why": "not_configured"}))
            return JSONResponse(
                {"error": "forbidden", "reason": "not_configured", "text": NOT_CONFIGURED_TEXT}, status_code=403
            )
        if not _verified(request):
            logger.warning("Background halt: refused a resume with no valid secret")
            await current.record(AuditEntry("resume_refused", actor, via, False, None, {"why": "no_secret"}))
            return JSONResponse({"error": "forbidden", "reason": "no_secret"}, status_code=403)
        outcome = await current.resume(actor=actor, via=via, verified=True)
        if outcome.status == "host_hold":
            return JSONResponse({"host_hold": True}, status_code=409)
        if outcome.status == "raced":
            return JSONResponse({"raced": True}, status_code=409)
        if outcome.status == "failed":
            return JSONResponse({"error": "not_saved"}, status_code=503)
        if outcome.changed and _present(continuation_runner) is not None:
            continuation_runner.wake()
        return JSONResponse(
            {"halted": False, "changed": outcome.changed, "text": RESUMED_TEXT if outcome.changed else NOT_HALTED_TEXT}
        )

    return [
        Route("/background/state", state, methods=["GET"]),
        Route("/background/halt", halt, methods=["POST"]),
        Route("/background/resume", resume, methods=["POST"]),
    ]
