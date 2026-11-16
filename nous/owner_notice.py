"""Goals PR 0c: one plain text to the owner's Telegram chat, sent by the process itself (no agent turn).

The direct ``httpx`` call the heartbeat's urgent send uses. Only the HTTP status is ever logged: the URL holds the bot
token, so neither it, the request nor an httpx error text reaches a log line.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


async def send_owner_text(settings: Any, text: str, *, http: httpx.AsyncClient | None = None) -> bool:
    """Send ``text`` to the owner's chat when a bot token and chat id are configured. True when Telegram accepted it.
    Never raises."""
    token = getattr(settings, "telegram_bot_token", "")
    chat_id = getattr(settings, "telegram_chat_id", "")
    if not token or not chat_id:
        return False
    try:
        client = http or httpx.AsyncClient()
        try:
            response = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=10
            )
        finally:
            if http is None:
                await client.aclose()
    except Exception:
        logger.warning("Owner notice: the Telegram send failed")
        return False
    status = getattr(response, "status_code", None)
    if not isinstance(status, int) or not 200 <= status < 300:
        logger.warning("Owner notice: Telegram answered HTTP %s", status)
        return False
    return True
