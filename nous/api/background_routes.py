"""Goals PR 0c: the halt switch over REST.

- ``GET /background/state``: whether background work is halted, since when, by whom, and whether the halt is saved.
- ``POST /background/halt``: halts at once and runs the cancel sweep. No secret: stopping is the safe direction.
- ``POST /background/resume``: an owner route behind ``OwnerGuard`` (``nous/api/owner_guard.py``): the owner's or the
  proxy's secret, 403 before the body is read without one, 415 for a proxy-only call that is not JSON, every call
  refused with neither digest set. Refused under the host's hold.

``POST /background/halt`` takes only the media type ``application/json`` (415 otherwise), so a cross-site page cannot
send it through the owner's browser as a simple request. A halt or a state read that carries the owner's or the
proxy's secret is marked verified (``OwnerGuard.grants``). Answers are fixed vocabulary plus counts; header values
are never logged.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from nous import background_halt, owner_notice
from nous.api.owner_guard import OWNER, OwnerGuard, json_media_type

RESUMED_TEXT = (
    "Background work is running again. Work that was cancelled was not restarted. "
    "One-time schedules that came due while halted run now."
)
NOT_HALTED_TEXT = "Background work is not halted."
_NO_STORE = {"Cache-Control": "no-store"}


async def _body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _present(component: Any) -> Any:
    """main.py passes lazy proxies, falsy while their component is None."""
    return component if component else None


def build_background_routes(
    *, settings: Any, guard: OwnerGuard, sweeper: Any = None, continuation_runner: Any = None
) -> list[Route]:
    """The three routes, over the process's installed halt (``background_halt.current()``). ``guard`` is the app's
    owner guard (rest.py warns once at start when it has no digest)."""

    async def _verified(request: Request) -> bool:
        return await guard.grants(request, OWNER)

    async def state(request: Request) -> JSONResponse:
        """GET /background/state. With a valid owner or proxy secret, ``by`` is the actor and ``reason`` the reason
        given; without one, ``by`` is the surface kind only and ``reason`` is empty."""
        return JSONResponse(background_halt.current().snapshot(full=await _verified(request)), headers=_NO_STORE)

    async def halt(request: Request) -> JSONResponse:
        """POST /background/halt: 200 with the sweep's report and its one message; a repeat halts again and re-runs
        the sweep."""
        if not json_media_type(request.headers.get("content-type")):
            return JSONResponse({"error": "Content-Type must be application/json"}, status_code=415)
        body = await _body(request)
        verified = await _verified(request)
        current = background_halt.current()
        try:
            outcome = await current.halt(
                actor=body.get("actor"), surface=body.get("surface"), reason=body.get("reason"), verified=verified
            )
        except RuntimeError:
            return JSONResponse({"error": "the halt is not available"}, status_code=503)
        swept = _present(sweeper)
        if swept is not None:
            report = await swept.run(actor=background_halt.clean(body.get("actor"), 100), epoch=outcome.epoch)
            report_body, text = report.as_dict(), report.describe()
        else:
            report_body, text = None, "Background work is halted."
        if outcome.announce:
            # A halt from the agent or the host: nobody waits on the answer, so the owner gets the one message.
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
        """POST /background/resume, reached only through the owner guard: 200 when resumed (or not halted), 409
        under the host's hold or when a halt arrived during it, 503 when it could not be saved."""
        body = await _body(request)
        current = background_halt.current()
        actor = background_halt.clean(body.get("actor"), 100)
        via = background_halt.clean_surface(body.get("surface"))
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
        guard.route("/background/resume", resume, methods=["POST"]),
    ]
