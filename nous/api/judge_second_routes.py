"""F118 PR 3: the second judge's settings over REST, for the card "A second opinion on decisions" on the Autonomy
screen (``NOUS_OUTSIDE_JUDGE_SETTINGS_ENABLED``).

``GET /dashboard/outside-judge/second``: the settings in effect and where each comes from, whether a key is saved and
when (never the key, its length or any part of it). ``PATCH`` the same path: ``{on?, model?, daily_calls?,
confirm_send?}`` (a null model or limit is the server's value). ``PUT /dashboard/outside-judge/second/key``: ``{key,
confirm_send?}``. ``DELETE`` that path: ``{}`` or ``{confirm_send}``. ``POST /dashboard/outside-judge/second/check``:
``{}``, one request with a fixed record; the answer is ``ok``, ``refused`` or ``unreachable`` and nothing else.

Every route requires the owner's or the proxy's secret (``nous/api/owner_guard.py``), checked before the body is read;
the actor of a change is the proof the guard accepted. A body is at most ``BODY_MAX_BYTES``, a JSON object whose
fields are typed strictly, with no other field; a refusal names the field and the rule, never a value, and the
validation error (which carries the input) is never logged or answered. The key travels in a request body only. Every
failure is logged by its class name alone. The manager (``nous.judge.second_settings.SecondJudgeManager``, built by
``main.py``) decides everything else; this module imports nothing of the judge.

With the setting off: the read answers 200 ``{"enabled": false}`` and the changes 409, with no database session and
no body read. With it on and no manager (the outside judge did not start): 503.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, StrictStr, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from nous.api.owner_guard import OwnerGuard

logger = logging.getLogger(__name__)

PATH = "/dashboard/outside-judge/second"
BODY_MAX_BYTES = 4096
ECHO_CHARS = 40
OFF = {"error": "The second judge's settings are off on this server.", "enabled": False}
UNAVAILABLE = "The second judge's settings are not available now. Restarting Nous starts them again."
FAILED = "The request could not be processed."


class _Body(BaseModel):
    """A request body: an unknown field is refused, and every field has its exact type (a bool is not a number,
    50.0 is not a whole number)."""

    model_config = ConfigDict(extra="forbid")


class SettingsChange(_Body):
    """``PATCH``: the fields to change. A null ``model`` or ``daily_calls`` is the server's value."""

    on: StrictBool = None  # type: ignore[assignment]  # absent unless given: a null is refused
    model: StrictStr | None = None
    daily_calls: StrictInt | None = None
    confirm_send: StrictBool = False


class KeyChange(_Body):
    """``PUT .../key``: the key, and the confirmation when saving it starts the sending."""

    key: StrictStr
    confirm_send: StrictBool = False


class KeyRemoval(_Body):
    """``DELETE .../key``: empty, or the confirmation when the removal hands over to the server's key."""

    confirm_send: StrictBool = False


class NoBody(_Body):
    """``POST .../check``: empty, or ``{}``."""


class _BadRequest(Exception):
    """A body the route refuses (400); the text names the field and the rule, never the value."""


async def _parse(request: Request, model: type[_Body]) -> Any:
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > BODY_MAX_BYTES:
            raise _BadRequest(f"The body must be at most {BODY_MAX_BYTES} bytes.")
    try:
        data = json.loads(bytes(raw)) if bytes(raw).strip() else {}
    except (ValueError, RecursionError):
        raise _BadRequest("The body must be JSON.") from None
    if not isinstance(data, dict):
        raise _BadRequest("The body must be a JSON object.")
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        fields = sorted({str(error["loc"][0])[:ECHO_CHARS] for error in exc.errors() if error["loc"]})
        raise _BadRequest(f"Not accepted: {', '.join(fields) or 'body'}.") from None


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def build_judge_second_routes(*, settings: Any, manager: Any, guard: OwnerGuard) -> list[Route]:
    """The routes, every one behind ``guard``. ``manager`` is the app's ``SecondJudgeManager`` (main.py passes its
    lazy proxy, falsy until the lifespan built one and always with the setting off), read at each request."""

    def current() -> Any | None:
        return manager if settings.outside_judge_settings_enabled and manager else None

    def actor(request: Request) -> str:
        proof = guard.proof(request)
        assert proof is not None  # the guard let the call in on one
        return proof

    def answered(answer: Any) -> JSONResponse:
        return JSONResponse(answer.body, status_code=answer.status)

    async def read(request: Request) -> Response:
        """GET /dashboard/outside-judge/second"""
        if not settings.outside_judge_settings_enabled:
            return JSONResponse({"enabled": False})
        found = current()
        if found is None:
            return _error(503, UNAVAILABLE)
        try:
            return JSONResponse(found.state())
        except Exception as exc:
            logger.warning("F118: reading the second judge's settings failed (%s)", type(exc).__name__)
            return _error(500, FAILED)

    async def change(request: Request, body_type: type[_Body], call: Any) -> Response:
        if not settings.outside_judge_settings_enabled:
            return JSONResponse(OFF, status_code=409)
        found = current()
        if found is None:
            return _error(503, UNAVAILABLE)
        try:
            body = await _parse(request, body_type)
        except _BadRequest as exc:
            return _error(400, str(exc))
        try:
            return answered(await call(found, body, actor(request)))
        except Exception as exc:
            logger.warning("F118: a request on the second judge's settings failed (%s)", type(exc).__name__)
            return _error(500, FAILED)

    async def patch(request: Request) -> Response:
        """PATCH /dashboard/outside-judge/second"""

        async def call(found: Any, body: SettingsChange, who: str) -> Any:
            changes = {name: getattr(body, name) for name in body.model_fields_set if name != "confirm_send"}
            return await found.patch(changes, confirm=body.confirm_send, actor=who)

        return await change(request, SettingsChange, call)

    async def put_key(request: Request) -> Response:
        """PUT /dashboard/outside-judge/second/key"""

        async def call(found: Any, body: KeyChange, who: str) -> Any:
            return await found.put_key(body.key, confirm=body.confirm_send, actor=who)

        return await change(request, KeyChange, call)

    async def delete_key(request: Request) -> Response:
        """DELETE /dashboard/outside-judge/second/key"""

        async def call(found: Any, body: KeyRemoval, who: str) -> Any:
            return await found.remove_key(confirm=body.confirm_send, actor=who)

        return await change(request, KeyRemoval, call)

    async def check(request: Request) -> Response:
        """POST /dashboard/outside-judge/second/check"""

        async def call(found: Any, body: NoBody, who: str) -> Any:
            return await found.check()

        return await change(request, NoBody, call)

    return [
        guard.route(PATH, read, methods=["GET"]),
        guard.route(PATH, patch, methods=["PATCH"]),
        guard.route(f"{PATH}/key", put_key, methods=["PUT"]),
        guard.route(f"{PATH}/key", delete_key, methods=["DELETE"]),
        guard.route(f"{PATH}/check", check, methods=["POST"]),
    ]
