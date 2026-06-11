"""Telegram file delivery tool (Issue #220 Phase 1).

Provides send_file tool for delivering generated files to Telegram
via sendPhoto (images) and sendDocument (everything else).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

from nous.config import Settings

logger = logging.getLogger(__name__)


def _read_bytes(path: str) -> bytes:
    """Read a file's bytes (run off-loop via asyncio.to_thread — AS-6)."""
    with open(path, "rb") as f:
        return f.read()

# Telegram Bot API base URL
_TG_API = "https://api.telegram.org/bot{token}/{method}"

# Extensions that should use sendPhoto
_PHOTO_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "gif", "webp"})

# Size limits (bytes)
_MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB — Telegram absolute max
_MAX_PHOTO_SIZE = 10 * 1024 * 1024  # 10MB — sendPhoto limit


def _detect_send_method(file_path: str) -> str:
    """Determine Telegram API method based on file extension.

    Images (png, jpg, jpeg, gif, webp) use sendPhoto.
    Everything else (docx, pdf, svg, etc.) uses sendDocument.
    """
    ext = os.path.splitext(file_path)[1].lstrip(".").lower()
    if ext in _PHOTO_EXTENSIONS:
        return "sendPhoto"
    return "sendDocument"


def _validate_file_size(file_path: str) -> tuple[bool, str]:
    """Validate file size against Telegram limits.

    Returns (ok, message). If ok=True but message is non-empty,
    it's a warning (e.g., photo will be sent as document).
    """
    size = os.path.getsize(file_path)
    if size > _MAX_FILE_SIZE:
        return False, f"File too large ({size / 1024 / 1024:.1f}MB). Telegram limit is 50MB."
    if size > _MAX_PHOTO_SIZE and _detect_send_method(file_path) == "sendPhoto":
        return True, f"Photo is {size / 1024 / 1024:.1f}MB (>10MB). Will send as document instead."
    return True, ""


def create_send_file_tool(settings: Settings, http_client: httpx.AsyncClient):
    """Create the send_file tool closure.

    Args:
        settings: App settings (telegram_bot_token, telegram_chat_id).
        http_client: httpx client for Telegram API calls.

    Returns:
        Async callable matching ToolDispatcher signature.
    """

    async def send_file(
        file_path: str,
        caption: str | None = None,
        chat_id: str | None = None,
        cleanup: bool = False,
    ) -> dict[str, Any]:
        """Send a file to Telegram via sendPhoto or sendDocument.

        Args:
            file_path: Path to the file to send.
            caption: Optional caption for the file.
            chat_id: Telegram chat ID. Defaults to NOUS_TELEGRAM_CHAT_ID.
            cleanup: If True, delete the file after successful send.

        Returns:
            MCP-compliant response with send confirmation or error.
        """
        # Validate configuration
        token = settings.telegram_bot_token
        if not token:
            return _error("Telegram bot token not configured. Set NOUS_TELEGRAM_BOT_TOKEN.")

        target_chat = chat_id or settings.telegram_chat_id
        if not target_chat:
            return _error("No chat_id provided and NOUS_TELEGRAM_CHAT_ID not configured.")
        # AS-3: outbound recipient allowlist. When a default chat is configured,
        # an explicit chat_id must match it — prevents file exfiltration to an
        # arbitrary chat if the tool call is manipulated.
        if (
            settings.telegram_chat_id
            and str(target_chat) != str(settings.telegram_chat_id)
        ):
            return _error("chat_id not permitted; files may only be sent to the configured chat.")

        # Validate file exists
        if not os.path.isfile(file_path):
            return _error(f"File not found: {file_path}")

        # Validate size
        ok, size_msg = _validate_file_size(file_path)
        if not ok:
            return _error(size_msg)

        # Determine send method
        method = _detect_send_method(file_path)
        if size_msg:
            # Photo too large — downgrade to document
            method = "sendDocument"

        # Build multipart upload
        file_key = "photo" if method == "sendPhoto" else "document"
        url = _TG_API.format(token=token, method=method)

        try:
            # AS-6: read off the event loop so a large/slow file doesn't block it.
            content = await asyncio.to_thread(_read_bytes, file_path)
            files = {file_key: (os.path.basename(file_path), content)}
            data: dict[str, str] = {"chat_id": target_chat}
            if caption:
                data["caption"] = caption

            response = await http_client.post(url, files=files, data=data)

            result = response.json()
            if not result.get("ok"):
                desc = result.get("description", "Unknown Telegram error")
                return _error(f"Telegram API error: {desc}")

            # Success — optionally clean up
            if cleanup:
                try:
                    os.remove(file_path)
                except OSError:
                    logger.warning("Failed to clean up file: %s", file_path)

            filename = os.path.basename(file_path)
            warning = f" (Note: {size_msg})" if size_msg else ""
            return _ok(
                f"File sent successfully: {filename} via {method} to chat {target_chat}.{warning}"
            )

        except httpx.HTTPError as e:
            logger.error("Telegram send failed for %s: %s", file_path, type(e).__name__)
            return _error(f"Failed to send file: network error ({type(e).__name__})")
        except Exception as e:
            logger.error("Unexpected error sending file %s: %s", file_path, type(e).__name__)
            return _error(f"Failed to send file: {type(e).__name__}")

    return send_file


def _ok(text: str) -> dict[str, Any]:
    """MCP-compliant success response."""
    return {"content": [{"type": "text", "text": text}]}


def _error(text: str) -> dict[str, Any]:
    """MCP-compliant error response."""
    return {"content": [{"type": "text", "text": f"Error: {text}"}]}


# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------

_SEND_FILE_SCHEMA = {
    "description": (
        "Send a file to Telegram. Images (png, jpg, gif, webp) are sent as photos; "
        "all other files (docx, pdf, xlsx, csv, svg, etc.) as documents. "
        "Use after generating a file with write_file, bash, or run_python."
    ),
    "type": "object",
    "properties": {
        "file_path": {
            "type": "string",
            "description": "Absolute path to the file to send.",
        },
        "caption": {
            "type": "string",
            "description": "Optional caption displayed with the file.",
        },
        "chat_id": {
            "type": "string",
            "description": "Telegram chat ID. Defaults to configured NOUS_TELEGRAM_CHAT_ID.",
        },
        "cleanup": {
            "type": "boolean",
            "description": "If true, delete the file after successful send. Default: false.",
        },
    },
    "required": ["file_path"],
}


def register_telegram_tools(
    dispatcher, settings: Settings, http_client: httpx.AsyncClient,
) -> None:
    """Register Telegram tools with the dispatcher."""
    handler = create_send_file_tool(settings, http_client)
    dispatcher.register("send_file", handler, _SEND_FILE_SCHEMA)
