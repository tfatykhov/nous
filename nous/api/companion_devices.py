"""Companion devices paired with this Nous by a code, each proving itself with its own token.

The owner opens a pairing (an 8-character code shown once on the dashboard) and types the code into the phone. The
phone redeems it and is given a device token (``nd1_`` and 256 random bits) in that one response. The token is
stored only as its SHA-256 (``nous_system.a2ui_devices``, migration 115), so it cannot be read back. The phone sends
it as ``X-Nous-Device-Token``; the owner guard looks it up (``OwnerGuard.resolve``) and a live device holds the
``card_decision`` and ``companion`` capabilities, never ``owner``.

``DeviceStore`` keeps the devices of one agent. Revoking a device runs the revoke hooks (``revoke_hooks``): the
application drops the device's window of the action limiter and its push installation there. A hook that fails is
logged by its exception class and does not undo the revoke.

Nothing here logs, raises or stores a token, a pairing code or a digest of either: an exception is logged by its
class only, because a database error's text can carry the statement's parameters.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select, update

from nous.api.owner_guard import DEVICE_TOKEN_PREFIX, device_token_well_formed
from nous.storage.models import A2uiDevice

__all__ = [
    "MAX_LIVE_DEVICES",
    "NAME_MAX_CHARS",
    "Device",
    "DeviceStore",
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

RevokeHook = Callable[["Device"], Awaitable[None]]


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
