"""Owner routes require the owner's or the proxy's secret.

The owner reaches Nous through two front doors, and each holds one secret. The Telegram bot holds the owner's secret
(``NOUS_OWNER_ROUTE_SECRET``, in the bot's process only) and sends it as ``X-Nous-Owner-Secret``. The login proxy adds
its own secret as ``X-Nous-Proxy-Secret`` once the browser has signed in. This process holds only the SHA-256 of each
(``NOUS_OWNER_ROUTE_SECRET_SHA256``, ``NOUS_MCP_CLIENT_UI_SECRET_SHA256``), compared in constant time.

``OwnerGuard.protect`` wraps a route: a request that carries neither secret is answered 403 before its body or any id
is read. With neither digest set, every call is refused (fail closed). A call let in by the proxy's secret alone must
have the media type exactly ``application/json`` (415 otherwise), because the proxy adds its header to whatever the
signed-in browser sends, and a JSON body cannot be sent from another site without a preflight.

Two capabilities: ``owner`` (every guarded route) and ``card_decision`` (the companion verbs registered with
``owner_proof=True``). ``GRANTS`` is the whole rule of which proof grants which. A device token grants only
``card_decision``, and only ``decide`` looks one up, through the optional ``devices`` store; without a store a device
token grants nothing. Only header names are ever logged, never a value.
"""

from __future__ import annotations

import functools
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from nous.api.commitment_routes import OWNER_SECRET_HEADER, owner_secret_matches

__all__ = [
    "CARD_DECISION",
    "DEVICE_TOKEN_HEADER",
    "DEVICE_TOKEN_REJECTED",
    "GRANTS",
    "OWNER",
    "OWNER_SECRET_HEADER",
    "PROXY_SECRET_HEADER",
    "OwnerGuard",
    "json_media_type",
    "owner_secret_matches",
    "secret_digest",
]

logger = logging.getLogger(__name__)

PROXY_SECRET_HEADER = "X-Nous-Proxy-Secret"  # the name the MCP client routes read too
DEVICE_TOKEN_HEADER = "X-Nous-Device-Token"
DEVICE_TOKEN_MAX_CHARS = 200
DEVICE_TOKEN_REJECTED = "device_token_rejected"
# A device token is this prefix and 256 random bits, URL-safe base64 without padding (43 characters).
DEVICE_TOKEN_PREFIX = "nd1_"
_DEVICE_TOKEN_RE = re.compile(r"nd1_[A-Za-z0-9_-]{43}")

# Capabilities, and which proof grants which. This table is the whole rule.
OWNER = "owner"
CARD_DECISION = "card_decision"
GRANTS: dict[str, frozenset[str]] = {
    "owner": frozenset({OWNER, CARD_DECISION}),
    "proxy": frozenset({OWNER, CARD_DECISION}),
    "device": frozenset({CARD_DECISION}),
}

_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}")
_READS = frozenset({"GET", "HEAD"})

Handler = Callable[[Request], Awaitable[Response]]


def secret_digest(raw: object) -> str:
    """The setting as a lowercase hex SHA-256, or "" when it is not one (fail closed, and never a non-ASCII value
    in ``compare_digest``, which raises on one). The first field only: ``sha256sum`` prints the digest followed by
    "  -"."""
    fields = raw.split() if isinstance(raw, str) else []
    digest = fields[0].lower() if fields else ""
    return digest if _SHA256_HEX_RE.fullmatch(digest) else ""


def device_token_well_formed(value: object) -> bool:
    """Whether ``value`` has the shape of a device token, so that any other value is refused with no lookup."""
    return isinstance(value, str) and _DEVICE_TOKEN_RE.fullmatch(value) is not None


def json_media_type(content_type: str | None) -> bool:
    """Whether the media type is exactly ``application/json`` (parameters such as a charset aside). A substring match
    would pass ``text/plain; application/json``, which a page on another site can send with no preflight."""
    return (content_type or "").split(";")[0].strip().lower() == "application/json"


class OwnerGuard:
    """Did this request prove the owner: the digests, the check and the route wrapper."""

    def __init__(self, settings: Any, devices: Any | None = None) -> None:
        self._owner = secret_digest(getattr(settings, "owner_route_secret_sha256", ""))
        self._proxy = secret_digest(getattr(settings, "mcp_client_ui_secret_sha256", ""))
        self._devices = devices

    @property
    def configured(self) -> bool:
        """Whether either digest is set (else every guarded call is refused)."""
        return bool(self._owner or self._proxy)

    def proof(self, request: Request) -> str | None:
        """``"owner"`` when the owner's header matches its digest, else ``"proxy"`` when the proxy's does, else None.
        Never reads a device token."""
        if owner_secret_matches(request.headers.get(OWNER_SECRET_HEADER), self._owner):
            return "owner"
        if owner_secret_matches(request.headers.get(PROXY_SECRET_HEADER), self._proxy):
            return "proxy"
        return None

    def allows(self, request: Request) -> bool:
        """Whether the request carries the owner's or the proxy's secret."""
        return self.proof(request) is not None

    @staticmethod
    def _proxy_media_ok(request: Request) -> bool:
        return request.method in _READS or json_media_type(request.headers.get("content-type"))

    async def decide(self, request: Request, capability: str) -> tuple[bool, str | None]:
        """``(granted, refusal)``. ``refusal`` is ``DEVICE_TOKEN_REJECTED`` only when a device token was looked up
        without error and no live device has it; otherwise None."""
        proof = self.proof(request)
        if proof is not None:
            granted = capability in GRANTS[proof] and (proof != "proxy" or self._proxy_media_ok(request))
            return granted, None
        if capability not in GRANTS["device"] or self._devices is None:
            return False, None
        token = request.headers.get(DEVICE_TOKEN_HEADER)
        if not token or len(token) > DEVICE_TOKEN_MAX_CHARS:
            return False, None
        try:
            known = bool(await self._devices.verify(token))
        except Exception as exc:
            # The class only: an error's text could repeat what it was given.
            logger.warning("a %s could not be checked (%s)", DEVICE_TOKEN_HEADER, type(exc).__name__)
            return False, None
        return (True, None) if known else (False, DEVICE_TOKEN_REJECTED)

    async def grants(self, request: Request, capability: str) -> bool:
        """Whether the request holds ``capability``."""
        return (await self.decide(request, capability))[0]

    def protect(self, handler: Handler, path: str) -> Handler:
        """``handler`` behind the ``owner`` capability: 403 without the owner's or the proxy's secret, 415 for a
        proxy-only call that is not JSON, both before the body is read. Never looks a device up. ``path`` is the
        route's template: a refusal logs it, never the path the caller sent."""

        @functools.wraps(handler)
        async def guarded(request: Request) -> Response:
            proof = self.proof(request)
            if proof is None or OWNER not in GRANTS[proof]:
                logger.warning(
                    "refused %s %s: no valid %s or %s",
                    request.method,
                    path,
                    OWNER_SECRET_HEADER,
                    PROXY_SECRET_HEADER,
                )
                return JSONResponse({"error": "forbidden"}, status_code=403)
            if proof == "proxy" and not self._proxy_media_ok(request):
                return JSONResponse({"error": "Content-Type must be application/json"}, status_code=415)
            return await handler(request)

        guarded.owner_guarded = True  # type: ignore[attr-defined]  # the route-table tests read it
        return guarded

    def route(self, path: str, handler: Handler, *, methods: list[str]) -> Route:
        """A ``Route`` at ``path`` for ``handler`` behind ``protect``."""
        return Route(path, self.protect(handler, path), methods=methods)
