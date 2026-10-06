"""F099 Phase 2c: the owner push (spec 4.5.8).

Owner-facing rows (a REPORT, a QUESTION) are written to the inbox at once, so chat shows them at any
hour. Telegram is the push: each row carries ``push_after`` (now, or the end of the quiet hours, set by
``continuation.push_after_for`` when the row was written), and this sweep sends the rows that are due,
once each. 2d extends it with PROPOSAL rows, inline buttons and force_reply.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select, update

from nous.brain import continuation
from nous.storage.models import ResultInbox

logger = logging.getLogger(__name__)

PUSH_BATCH = 20
TELEGRAM_TEXT_MAX = continuation.RAW_PUSH_CHARS  # 3900: one constant for the raw pushes
# 2d adds continuation.MSG_PROPOSAL (with its buttons).
PUSHED_KINDS = (continuation.MSG_REPORT, continuation.MSG_QUESTION)


class OwnerPublisher:
    """Sends the owner-facing rows that are due to Telegram, once each (see the module docstring)."""

    def __init__(self, *, database: Any, settings: Any, http_client: Any = None) -> None:
        self._db = database
        self._settings = settings
        self._http = http_client
        self._lock = asyncio.Lock()  # one sweep at a time: the stamp is the idempotence, this spares the duplicate send

    async def push_due(self, limit: int = PUSH_BATCH, *, now: datetime | None = None) -> int:
        """Send up to ``limit`` due rows; the number sent. Inert without the flag or a bot token."""
        settings = self._settings
        token = getattr(settings, "telegram_bot_token", "") or ""
        if not continuation.enabled(settings) or not token:
            return 0
        async with self._lock:
            now = now or datetime.now(UTC)
            since = now - timedelta(hours=settings.result_inbox_max_age_hours)
            async with self._db.session() as session:
                rows = (
                    (
                        await session.execute(
                            select(ResultInbox)
                            .where(
                                ResultInbox.agent_id == settings.agent_id,
                                ResultInbox.source_kind == continuation.SOURCE_INTENTION_REPORT,
                                ResultInbox.msg_type.in_(PUSHED_KINDS),
                                ResultInbox.pushed_at.is_(None),
                                ResultInbox.push_after.is_not(None),
                                ResultInbox.push_after <= now,
                                ResultInbox.channel.like("telegram:%"),
                                ResultInbox.created_at > since,
                            )
                            .order_by(ResultInbox.push_after, ResultInbox.id)
                            .limit(limit)
                        )
                    )
                    .scalars()
                    .all()
                )
            pushed = 0
            for row in rows:
                sent, message_id = await self._send(token, row)
                if not sent:
                    break  # an outage is not hammered: the rows stay due for the next sweep
                async with self._db.session() as session:
                    stamped = (
                        await session.execute(
                            update(ResultInbox)
                            .where(ResultInbox.id == row.id, ResultInbox.pushed_at.is_(None))
                            .values(pushed_at=datetime.now(UTC), push_message_id=message_id)
                            .returning(ResultInbox.id)
                            .execution_options(synchronize_session=False)
                        )
                    ).scalar_one_or_none()
                    await session.commit()
                if stamped is not None:
                    pushed += 1
            return pushed

    async def _send(self, token: str, row: ResultInbox) -> tuple[bool, int | None]:
        """One sendMessage. ``(sent, message_id)``. The URL carries the bot token, so nothing here logs it, the
        response, or a traceback: a failure names the row and the exception class only."""
        chat_id = row.channel.split(":", 1)[1]
        body = continuation.clip_body(row.body, self._settings)  # carry-over 7, C20: the store's one clip
        payload = {"chat_id": chat_id, "text": f"{row.title}\n\n{body}"[:TELEGRAM_TEXT_MAX]}
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            if self._http is not None:
                response = await self._http.post(url, json=payload, timeout=10)
            else:
                async with httpx.AsyncClient() as client:
                    response = await client.post(url, json=payload, timeout=10)
        except Exception as exc:
            logger.warning("F099: the Telegram push of row %s failed (%s)", row.id.hex[:8], type(exc).__name__)
            return False, None
        if response.status_code >= 400:
            logger.warning(
                "F099: the Telegram push of row %s was refused (HTTP %s)", row.id.hex[:8], response.status_code
            )
            return False, None
        try:
            return True, int(response.json()["result"]["message_id"])
        except Exception:
            return True, None  # sent, but the id could not be read: still stamped, so it is not sent twice
