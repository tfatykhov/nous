"""Companion devices paired with this Nous by a code, each proving itself with its own token.

The owner opens a pairing (an 8-character code shown once on the dashboard) and types the code into the phone. The
phone redeems it and is given a device token (``nd1_`` and 256 random bits) in that one response. The token is
stored only as its SHA-256 (``nous_system.a2ui_devices``, migration 115), so it cannot be read back. The phone sends
it as ``X-Nous-Device-Token``; the owner guard looks it up (``OwnerGuard.resolve``) and a live device holds the
``card_decision`` and ``companion`` capabilities, never ``owner``.

``DeviceStore`` keeps the devices of one agent. Revoking a device runs the revoke hooks (``revoke_hooks``): the
application drops the device's window of the action limiter and its push installation there. A hook that fails is
logged by its exception class and does not undo the revoke.

``PairingDesk`` holds the one open pairing, in memory only and as the code's digest: 8 characters of a 30-symbol
alphabet, five minutes, closed by five wrong tries. A restart forgets it. Redeeming is limited to 10 calls a minute
for the whole process; a call refused by that limit does not look at the code.

The routes (``build_device_routes``): ``POST /a2ui/devices/pairings``, ``GET /a2ui/devices`` and
``DELETE /a2ui/devices/{id}`` need the owner's or the proxy's secret; ``POST /a2ui/devices/redeem`` needs none (the
code is the proof); ``POST /a2ui/devices/unpair`` needs the calling device's own token.

Nothing here logs, raises or stores a token, a pairing code or a digest of either: an exception is logged by its
class only, because a database error's text can carry the statement's parameters. The redeem response that carries
a new token is the only response that does.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any

import httpx
from pydantic import BaseModel, ConfigDict, StrictStr, StringConstraints, ValidationError, field_validator
from sqlalchemy import func, or_, select, update
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from nous.api.owner_guard import (
    COMPANION,
    DEVICE_TOKEN_PREFIX,
    DEVICE_TOKEN_REJECTED,
    DEVICE_TOKEN_REJECTED_CODE,
    DEVICE_TOKEN_TEXT,
    OwnerGuard,
    device_token_well_formed,
    json_media_type,
)
from nous.events import Event
from nous.storage.models import A2uiDevice

__all__ = [
    "CODE_ALPHABET",
    "CODE_LENGTH",
    "CODE_MAX_MISSES",
    "CODE_TTL_SECONDS",
    "DEVICE_EVENT",
    "MAX_LIVE_DEVICES",
    "NAME_MAX_CHARS",
    "NOT_VALID",
    "REDEEM_PER_MINUTE",
    "Device",
    "DeviceStore",
    "PairingDesk",
    "build_device_routes",
    "clean_name",
    "token_digest",
]

logger = logging.getLogger(__name__)

MAX_LIVE_DEVICES = 10
NAME_MAX_CHARS = 60
LIST_REVOKED = 20  # the list shows the live devices and this many of the most recently revoked
LAST_SEEN_EVERY = timedelta(minutes=1)
# Letters and digits of any script, and these. Nothing a Telegram client turns into a link, a mention, a hashtag or a
# command in plain text (a dot would let "evil.com" through), and no markup character.
_NAME_MARKS = frozenset(" _'()-")
_NAME_DIGIT_RUN = 6  # a longer run of digits is cut: a Telegram client links a phone number in plain text
_NAME_SCAN_CHARS = 1000  # a longer value is cut before it is read
DEFAULT_NAME = "Android"
INSTALLATION_ID_MAX_CHARS = 100

# A pairing code: 8 characters of 30 symbols (no 0 1 I L O U), shown as two groups of four.
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 8
CODE_TTL_SECONDS = 300
CODE_MAX_MISSES = 5
_CODE_SCAN_CHARS = 64  # a longer value is malformed and is not read
REDEEM_PER_MINUTE = 10  # redeem calls a minute, for the whole process
_REDEEM_WINDOW_SECONDS = 60.0

DEVICE_EVENT = "companion_device_changed"
# Every refused redeem answers exactly this (with 404): a wrong, malformed, expired, used or closed code, or none open.
NOT_VALID = {"error": "that code is not valid"}
UNPROCESSABLE = {"error": "the request could not be processed"}
FORBIDDEN = {"error": "forbidden"}
NO_STORE = {"Cache-Control": "no-store"}

RevokeHook = Callable[["Device"], Awaitable[None]]
Handler = Callable[[Request], Awaitable[Response]]


@dataclass(frozen=True)
class Device:
    """A live device, as a lookup by its token found it."""

    id: uuid.UUID
    name: str
    installation_id: str


def clean_name(value: object) -> str | None:
    """A device's name as shown: letters and digits (``str.isalnum``), a space and ``_ ' ( ) -``; a run of digits cut
    at six (so it cannot read as a phone number); whitespace collapsed to one space; at most 60 characters. Anything
    else is dropped. None when nothing is left or ``value`` is not text."""
    if not isinstance(value, str):
        return None
    kept = []
    digits = 0  # the length of the run of digits the last kept character ends
    for char in value[:_NAME_SCAN_CHARS]:
        if char.isspace():
            char = " "
        elif not (char.isalnum() or char in _NAME_MARKS):
            continue
        digits = digits + 1 if char.isdigit() else 0
        if digits <= _NAME_DIGIT_RUN:
            kept.append(char)
    name = " ".join("".join(kept).split())[:NAME_MAX_CHARS].strip()
    return name or None


def token_digest(token: str) -> str:
    """The lowercase hex SHA-256 of a device token: what the table stores."""
    return hashlib.sha256(token.encode()).hexdigest()


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class DeviceStore:
    """The paired devices of one agent (``nous_system.a2ui_devices``). The database is read only when a method runs,
    so a lazy component or a stand-in is fine at construction."""

    def __init__(self, database: Any, agent_id: str) -> None:
        self._db = database
        self._agent_id = agent_id
        self.revoke_hooks: list[RevokeHook] = []

    async def create(self, name: str, installation_id: str) -> tuple[Device, str]:
        """A new device and its token, which is returned here and kept nowhere. In the same transaction a live device
        bound to the same installation is revoked (superseded); the revoke hooks then run for it."""
        token = DEVICE_TOKEN_PREFIX + secrets.token_urlsafe(32)
        device = Device(id=uuid.uuid4(), name=name, installation_id=installation_id)
        async with self._db.session() as session:
            superseded = (
                await session.execute(
                    update(A2uiDevice)
                    .where(
                        A2uiDevice.agent_id == self._agent_id,
                        A2uiDevice.installation_id == installation_id,
                        A2uiDevice.revoked_at.is_(None),
                    )
                    .values(revoked_at=func.now())
                    .returning(A2uiDevice.id, A2uiDevice.name, A2uiDevice.installation_id)
                )
            ).all()
            session.add(
                A2uiDevice(
                    agent_id=self._agent_id,
                    id=device.id,
                    name=name,
                    token_sha256=token_digest(token),
                    installation_id=installation_id,
                )
            )
            await session.commit()
        for row in superseded:
            await self._run_hooks(Device(id=row.id, name=row.name, installation_id=row.installation_id))
        return device, token

    async def verify(self, token: object) -> Device | None:
        """The live device holding ``token``, or None. A value that is not token-shaped is None with no query. Writes
        ``last_seen_at`` when it is unset or more than a minute old. Nothing is cached between calls, so a revoke holds
        from the next one."""
        if not device_token_well_formed(token):
            return None
        assert isinstance(token, str)
        stale = or_(A2uiDevice.last_seen_at.is_(None), A2uiDevice.last_seen_at < func.now() - LAST_SEEN_EVERY)
        async with self._db.session() as session:
            row = (
                await session.execute(
                    select(A2uiDevice.id, A2uiDevice.name, A2uiDevice.installation_id, stale.label("stale")).where(
                        A2uiDevice.agent_id == self._agent_id,
                        A2uiDevice.token_sha256 == token_digest(token),
                        A2uiDevice.revoked_at.is_(None),
                    )
                )
            ).one_or_none()
            if row is None:
                return None
            if row.stale:
                await session.execute(
                    update(A2uiDevice)
                    .where(A2uiDevice.agent_id == self._agent_id, A2uiDevice.id == row.id, stale)
                    .values(last_seen_at=func.now())
                )
                await session.commit()
        return Device(id=row.id, name=row.name, installation_id=row.installation_id)

    async def list_devices(self) -> list[dict[str, Any]]:
        """The live devices (oldest first) and the 20 most recently revoked: id, name, created_at, last_seen_at and
        revoked_at. Never a digest or an installation id."""
        columns = (
            A2uiDevice.id,
            A2uiDevice.name,
            A2uiDevice.created_at,
            A2uiDevice.last_seen_at,
            A2uiDevice.revoked_at,
        )
        async with self._db.session() as session:
            live = (
                await session.execute(
                    select(*columns)
                    .where(A2uiDevice.agent_id == self._agent_id, A2uiDevice.revoked_at.is_(None))
                    .order_by(A2uiDevice.created_at, A2uiDevice.id)
                )
            ).all()
            revoked = (
                await session.execute(
                    select(*columns)
                    .where(A2uiDevice.agent_id == self._agent_id, A2uiDevice.revoked_at.is_not(None))
                    .order_by(A2uiDevice.revoked_at.desc(), A2uiDevice.id)
                    .limit(LIST_REVOKED)
                )
            ).all()
        return [
            {
                "id": str(row.id),
                "name": row.name,
                "created_at": _iso(row.created_at),
                "last_seen_at": _iso(row.last_seen_at),
                "revoked_at": _iso(row.revoked_at),
            }
            for row in [*live, *revoked]
        ]

    async def revoke(self, device_id: object) -> bool | None:
        """Revoke a device once: True when this call revoked it, False when it already was, None when this agent has
        no such device (or the id is not a UUID). The revoke hooks run only when this call revoked it."""
        try:
            wanted = device_id if isinstance(device_id, uuid.UUID) else uuid.UUID(str(device_id))
        except ValueError:
            return None
        async with self._db.session() as session:
            row = (
                await session.execute(
                    update(A2uiDevice)
                    .where(
                        A2uiDevice.agent_id == self._agent_id,
                        A2uiDevice.id == wanted,
                        A2uiDevice.revoked_at.is_(None),
                    )
                    .values(revoked_at=func.now())
                    .returning(A2uiDevice.id, A2uiDevice.name, A2uiDevice.installation_id)
                )
            ).one_or_none()
            if row is None:
                known = (
                    await session.execute(
                        select(A2uiDevice.id).where(A2uiDevice.agent_id == self._agent_id, A2uiDevice.id == wanted)
                    )
                ).first()
                return False if known is not None else None
            await session.commit()
        await self._run_hooks(Device(id=row.id, name=row.name, installation_id=row.installation_id))
        return True

    async def live_count(self) -> int:
        """How many devices are live."""
        async with self._db.session() as session:
            return int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(A2uiDevice)
                        .where(A2uiDevice.agent_id == self._agent_id, A2uiDevice.revoked_at.is_(None))
                    )
                ).scalar_one()
            )

    async def installation_bound(self, installation_id: str) -> bool:
        """Whether a live device holds ``installation_id``."""
        async with self._db.session() as session:
            found = (
                await session.execute(
                    select(A2uiDevice.id).where(
                        A2uiDevice.agent_id == self._agent_id,
                        A2uiDevice.installation_id == installation_id,
                        A2uiDevice.revoked_at.is_(None),
                    )
                )
            ).first()
        return found is not None

    async def _run_hooks(self, device: Device) -> None:
        for hook in list(self.revoke_hooks):
            try:
                await hook(device)
            except Exception as exc:
                # The class only: an error's text could repeat what it was given.
                logger.warning("a companion device revoke hook failed (%s)", type(exc).__name__)


# ---- the pairing desk -------------------------------------------------------------------------------------------


def _code_digest(code: str) -> bytes:
    return hashlib.sha256(code.encode("ascii")).digest()


@dataclass
class _Pairing:
    digest: bytes
    name: str | None
    expires_at: float
    misses: int = 0
    state: str = "open"  # open, used or closed_tries; an open one past expires_at reads as expired


class PairingDesk:
    """The one open pairing, in this process's memory only and as the digest of its code, and the redeem limit.

    ``take`` is synchronous on purpose: nothing awaits between "the code is open and matches" and "it is used", so two
    redeems of one code cannot both succeed."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._pairing: _Pairing | None = None
        self._redeems: list[float] = []
        # Compared against when no pairing is open, so every redeem does the same work. Random: nothing matches it.
        self._dummy = hashlib.sha256(secrets.token_bytes(32)).digest()

    def open(self, name: str | None) -> str:
        """Open a pairing (replacing any other) for a device the owner names ``name`` (None: the device's own name).
        Returns the code, ``XXXX-XXXX``, which is kept nowhere."""
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        self._pairing = _Pairing(digest=_code_digest(code), name=name, expires_at=self._clock() + CODE_TTL_SECONDS)
        return f"{code[:4]}-{code[4:]}"

    def _prune(self) -> float:
        now = self._clock()
        self._redeems = [t for t in self._redeems if now - t < _REDEEM_WINDOW_SECONDS]
        return now

    def admit_redeem(self) -> int | None:
        """Take a slot of the redeem limit: None when the call may go on, else the seconds until a slot is free."""
        now = self._prune()
        if len(self._redeems) >= REDEEM_PER_MINUTE:
            return max(1, math.ceil(_REDEEM_WINDOW_SECONDS - (now - self._redeems[0])))
        self._redeems.append(now)
        return None

    def limited(self) -> bool:
        """Whether the redeem limit is full now."""
        self._prune()
        return len(self._redeems) >= REDEEM_PER_MINUTE

    def take(self, code: object) -> tuple[bool, str | None]:
        """``(True, the owner's name or None)`` when ``code`` is the open pairing's, which is then used; else
        ``(False, None)``. The code is normalised (upper case, whitespace and ``-`` removed), hashed and compared in
        constant time with the open pairing's digest, or with a dummy when none is open, so the work is the same
        whatever the outcome. A well-formed code that does not match is a miss, and the fifth miss closes the pairing;
        a malformed one (wrong length, a symbol outside the alphabet) is no miss."""
        text = code if isinstance(code, str) and len(code) <= _CODE_SCAN_CHARS else ""
        normalised = "".join(text.upper().split()).replace("-", "")
        well_formed = len(normalised) == CODE_LENGTH and all(char in CODE_ALPHABET for char in normalised)
        candidate = _code_digest(normalised if well_formed else "0" * CODE_LENGTH)
        pairing = self._pairing
        live = pairing is not None and pairing.state == "open" and self._clock() < pairing.expires_at
        matched = hmac.compare_digest(candidate, pairing.digest if live and pairing is not None else self._dummy)
        if not (live and well_formed) or pairing is None:
            return False, None
        if matched:
            pairing.state = "used"
            return True, pairing.name
        pairing.misses += 1
        if pairing.misses >= CODE_MAX_MISSES:
            pairing.state = "closed_tries"
            logger.warning("the open pairing code was closed after %d wrong tries", CODE_MAX_MISSES)
        return False, None

    def status(self) -> dict[str, Any] | None:
        """The pairing's state for the owner: None (none opened since the start); ``open`` with ``expires_in``
        (seconds) and ``tries_left``; ``used``; ``closed_tries``; ``expired``. Never the code or its digest."""
        pairing = self._pairing
        if pairing is None:
            return None
        if pairing.state != "open":
            return {"state": pairing.state}
        left = pairing.expires_at - self._clock()
        if left <= 0:
            return {"state": "expired"}
        return {"state": "open", "expires_in": math.ceil(left), "tries_left": CODE_MAX_MISSES - pairing.misses}


# ---- the request bodies -----------------------------------------------------------------------------------------


class _PairingBody(BaseModel):
    """``POST /a2ui/devices/pairings``: an optional name the owner gives the device."""

    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    name: StrictStr | None = None


class _RedeemBody(BaseModel):
    """``POST /a2ui/devices/redeem``: the code, the device's push installation id and the name it reports."""

    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    code: StrictStr
    installation_id: Annotated[
        StrictStr, StringConstraints(strip_whitespace=True, min_length=1, max_length=INSTALLATION_ID_MAX_CHARS)
    ]
    name: Any = None  # any value: clean_name keeps what it can and ignores the rest

    @field_validator("installation_id")
    @classmethod
    def _printable(cls, value: str) -> str:
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
            raise ValueError("installation_id has a control character")
        return value


# ---- the routes -------------------------------------------------------------------------------------------------


def _json(payload: dict[str, Any], status_code: int = 200, **kwargs: Any) -> JSONResponse:
    headers = {**NO_STORE, **kwargs.pop("headers", {})}
    return JSONResponse(payload, status_code=status_code, headers=headers, **kwargs)


async def _body(request: Request) -> Any:
    """The JSON body, ``{}`` when there is none; raises ValueError when it is not JSON."""
    raw = await request.body()
    if not raw.strip():
        return {}
    return json.loads(raw)


def build_device_routes(
    *,
    store: DeviceStore,
    desk: PairingDesk,
    guard: OwnerGuard,
    settings: Any,
    bus: Any = None,
    http: httpx.AsyncClient | None = None,
) -> list[Route]:
    """The five device routes over ``store`` and ``desk``. The owner's three go behind ``guard``. ``bus`` may be a
    lazy component; ``http`` is the client the Telegram line is sent with (a new one per send when None)."""

    def safe(handler: Handler) -> Handler:
        async def run(request: Request) -> Response:
            try:
                return await handler(request)
            except Exception as exc:
                # The class only, never a traceback: a database error's text can carry a digest it was given.
                logger.warning("a companion device route failed: %s (%s)", handler.__name__, type(exc).__name__)
                return _json(UNPROCESSABLE, 500)

        run.__name__ = handler.__name__
        return run

    async def notify_owner(device: Device) -> None:
        """One plain-text Telegram line naming the device, and the change event (the action and the device's id).
        Never fails the pairing."""
        if bus:
            try:
                data = {"action": "paired", "device_id": str(device.id)}  # no name: it is the caller's choice
                await bus.emit(Event(type=DEVICE_EVENT, agent_id=settings.agent_id, data=data))
            except Exception as exc:
                logger.warning("the companion device event was not emitted (%s)", type(exc).__name__)
        token = getattr(settings, "telegram_bot_token", None)
        chat_id = getattr(settings, "telegram_chat_id", None)
        if not token or not chat_id:
            return
        text = (
            f"Companion device paired: {device.name}. "
            "If you did not just do this, open Devices in the dashboard and revoke it."
        )
        client = http or httpx.AsyncClient()
        try:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=10,
            )
        except Exception as exc:
            logger.warning("the companion device pairing line was not sent (%s)", type(exc).__name__)
        finally:
            if http is None:
                await client.aclose()

    async def open_pairing(request: Request) -> Response:
        """POST /a2ui/devices/pairings (owner): a new code, shown once."""
        try:
            body = _PairingBody.model_validate(await _body(request))
        except (ValueError, ValidationError):
            return _json({"error": "the body must be a JSON object with an optional string name"}, 400)
        name = None
        if body.name is not None and body.name.strip():
            name = clean_name(body.name)
            if name is None:
                return _json({"error": "the name has no letter or digit that can be shown"}, 400)
        if await store.live_count() >= MAX_LIVE_DEVICES:
            return _json({"error": "revoke a device first"}, 409)
        return _json({"code": desk.open(name), "expires_in": CODE_TTL_SECONDS})

    async def list_devices(request: Request) -> Response:
        """GET /a2ui/devices (owner): the devices, the pairing's state and whether redeeming is limited now."""
        devices = await store.list_devices()
        return _json({"devices": devices, "pairing": desk.status(), "redeem": {"limited": desk.limited()}})

    async def revoke_device(request: Request) -> Response:
        """DELETE /a2ui/devices/{id} (owner)."""
        revoked = await store.revoke(request.path_params["id"])
        if revoked is None:
            return _json({"error": "no such device"}, 404)
        return _json({"revoked": revoked})

    async def redeem(request: Request) -> Response:
        """POST /a2ui/devices/redeem (no secret: the code is the proof). Every refused code answers ``NOT_VALID``."""
        if not json_media_type(request.headers.get("content-type")):
            return _json({"error": "Content-Type must be application/json"}, 415)
        try:
            body = _RedeemBody.model_validate(await _body(request))
        except (ValueError, ValidationError):
            # A fixed text: a validation error's own text could repeat the code.
            return _json({"error": "the body must be a JSON object with a string code and installation_id"}, 400)
        wait = desk.admit_redeem()
        if wait is not None:
            return _json({"error": "too many tries: wait a minute"}, 429, headers={"Retry-After": str(wait)})
        matched, owner_name = desk.take(body.code)
        if not matched:
            return _json(NOT_VALID, 404)
        name = owner_name or clean_name(body.name) or DEFAULT_NAME
        device, token = await store.create(name, body.installation_id)
        public = getattr(settings, "a2ui_public_base_url", "")
        payload = {
            "device_id": str(device.id),
            "name": device.name,
            "token": token,
            "public_base_url": public.strip() if isinstance(public, str) and public.strip() else None,
        }
        return _json(payload, background=BackgroundTask(notify_owner, device))

    async def unpair(request: Request) -> Response:
        """POST /a2ui/devices/unpair: the calling device revokes itself, by its own token only. The owner's and the
        proxy's secrets resolve to no device and are refused here."""
        caller = await guard.resolve(request, COMPANION)
        if not caller.granted or caller.device is None:
            if caller.refusal == DEVICE_TOKEN_REJECTED:
                return _json({"error": {"code": DEVICE_TOKEN_REJECTED_CODE, "message": DEVICE_TOKEN_TEXT}}, 403)
            return _json(FORBIDDEN, 403)
        await store.revoke(caller.device.id)
        return _json({"revoked": True})

    return [
        guard.route("/a2ui/devices/pairings", safe(open_pairing), methods=["POST"]),
        Route("/a2ui/devices/redeem", safe(redeem), methods=["POST"]),
        Route("/a2ui/devices/unpair", safe(unpair), methods=["POST"]),
        guard.route("/a2ui/devices/{id}", safe(revoke_device), methods=["DELETE"]),
        guard.route("/a2ui/devices", safe(list_devices), methods=["GET"]),
    ]
