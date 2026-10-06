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

# What one send came to. A refusal is final for its row (the bot is blocked, the chat is gone): the row is
# stamped unsent, so it cannot hold the rows behind it. Any other failure leaves every row due.
SENT, TRANSIENT, REFUSED = "sent", "transient", "refused"
REFUSED_STATUSES = frozenset({400, 403})  # 401 and 404 are a wrong token: a fault for every row, never refused


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
                result, message_id = await self._send(token, row)
                if result == TRANSIENT:
                    break  # an outage is not hammered: the rows stay due for the next sweep
                # Sent, or refused for good: stamped either way (a refusal with no message id), never sent again.
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
                if stamped is not None and result == SENT:
                    pushed += 1
            return pushed

    async def _send(self, token: str, row: ResultInbox) -> tuple[str, int | None]:
        """One sendMessage. ``(result, message_id)``: SENT; REFUSED for HTTP 400 or 403, or a row with no chat id
        (never sent); TRANSIENT for an exception or any other failed status (401, 404, 429, 5xx...). The URL
        carries the bot token, so nothing here logs it, the response, or a traceback: a failure names the row
        and the exception class or the status only."""
        chat_id = row.channel.split(":", 1)[1]
        if not chat_id:
            logger.warning("F099: row %s has no Telegram chat id; it is not pushed", row.id.hex[:8])
            return REFUSED, None
        # The body gets the room the title leaves, so its [truncated] marker survives the Telegram cut.
        room = max(100, TELEGRAM_TEXT_MAX - len(row.title) - 2)
        body = continuation.clip_body(row.body, self._settings, limit=room)  # carry-over 7, C20: the store's one clip
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
            return TRANSIENT, None
        status = response.status_code
        if status in REFUSED_STATUSES:
            logger.warning(
                "F099: the Telegram push of row %s was refused (HTTP %s); it is not retried", row.id.hex[:8], status
            )
            return REFUSED, None
        if status >= 400:
            logger.warning("F099: the Telegram push of row %s failed (HTTP %s); it stays due", row.id.hex[:8], status)
            return TRANSIENT, None
        try:
            return SENT, int(response.json()["result"]["message_id"])
        except Exception:
            return SENT, None  # sent, but the id could not be read: still stamped, so it is not sent twice
