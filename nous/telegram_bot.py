"""Telegram bot for Nous agent.

Polls Telegram for messages and forwards them to the Nous /chat REST API.
Sends responses back to the user.

Usage:
    TELEGRAM_BOT_TOKEN=... NOUS_API_URL=http://localhost:8000 python -m nous.telegram_bot

Environment:
    TELEGRAM_BOT_TOKEN  - Bot token from @BotFather
    NOUS_API_URL        - Nous REST API base URL (default: http://localhost:8000)
    NOUS_ALLOWED_USERS  - Comma-separated Telegram user IDs (optional, empty = allow all)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import os
import sys
import time
from typing import Any

import httpx

from nous.api.attachments import classify_attachment, sanitize_filename
from nous.api.models import Attachment

logger = logging.getLogger(__name__)

# Telegram Bot API base
TG_API = "https://api.telegram.org/bot{token}/{method}"

# Session TTL: if no activity for this long, start a new session.
# Mirrors server-side session_idle_timeout (default 1800s = 30min).
SESSION_TTL_SECONDS = 1800

# Max Telegram message length
TG_MAX_LEN = 4096

# Regex patterns for markdown sanitization
import html as html_module
import re

_FENCED_BLOCK_RE = re.compile(r"```[\s\S]*?```", re.MULTILINE)
_TABLE_SEP_RE = re.compile(r"^\|[-:| ]+\|$", re.MULTILINE)  # |---|---|
_TABLE_ROW_RE = re.compile(r"^\|(.+)\|$", re.MULTILINE)  # | col | col |
_HEADER_RE = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)  # ## Header
_HR_RE = re.compile(r"^-{3,}$", re.MULTILINE)  # ---

# Additional patterns for HTML conversion
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")  # **bold**
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")  # `inline code`
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")  # [text](url)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def sanitize_telegram(text: str) -> str:
    """Convert markdown patterns that don't render in Telegram.

    Fallback sanitizer for when the LLM ignores formatting instructions.
    - Markdown tables → bullet lists
    - ## Headers → **bold**
    - --- horizontal rules → removed
    - Fenced code blocks are preserved (not sanitized).
    """
    # Stash fenced code blocks to protect them from sanitization
    stash: list[str] = []

    def _stash_block(match: re.Match) -> str:
        stash.append(match.group(0))
        return f"\x00CODEBLOCK{len(stash) - 1}\x00"

    text = _FENCED_BLOCK_RE.sub(_stash_block, text)

    # Remove table separator rows first
    text = _TABLE_SEP_RE.sub("", text)

    # Convert table rows to bullet points
    text = _TABLE_ROW_RE.sub(_table_row_to_bullet, text)

    # Convert headers to bold
    text = _HEADER_RE.sub(r"**\1**", text)

    # Remove horizontal rules
    text = _HR_RE.sub("", text)

    # Clean up excessive blank lines left by removals
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Restore fenced code blocks
    for i, block in enumerate(stash):
        text = text.replace(f"\x00CODEBLOCK{i}\x00", block)

    return text.strip()


def _table_row_to_bullet(match: re.Match) -> str:
    """Convert a markdown table row to a bullet point."""
    cells = [c.strip() for c in match.group(1).split("|") if c.strip()]
    if len(cells) == 1:
        return f"• {cells[0]}"
    elif len(cells) == 2:
        return f"• {cells[0]} — {cells[1]}"
    else:
        return "• " + " — ".join(cells)


def format_telegram_html(text: str) -> str:
    """Convert markdown to Telegram-compatible HTML for parse_mode='HTML'.

    Full conversion for final message sends. Handles:
    - Fenced code blocks → <pre><code>
    - Inline code → <code>
    - **bold** → <b>
    - ## Headers → <b>
    - [text](url) → <a href>
    - Tables → bullet lists (plain text)
    - --- horizontal rules → removed
    - HTML entity escaping for safe rendering
    """
    # 1. Stash fenced code blocks (protect from all processing)
    code_stash: list[str] = []

    def _stash_code(match: re.Match) -> str:
        code_stash.append(match.group(0))
        return f"\x00CODEBLOCK{len(code_stash) - 1}\x00"

    text = _FENCED_BLOCK_RE.sub(_stash_code, text)

    # 2. Stash inline code (protect from HTML escaping)
    inline_stash: list[str] = []

    def _stash_inline(match: re.Match) -> str:
        inline_stash.append(match.group(1))
        return f"\x00INLINE{len(inline_stash) - 1}\x00"

    text = _INLINE_CODE_RE.sub(_stash_inline, text)

    # 3. Escape HTML entities in remaining text
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")

    # 4. Convert tables to bullet points (plain text)
    text = _TABLE_SEP_RE.sub("", text)
    text = _TABLE_ROW_RE.sub(_table_row_to_bullet, text)

    # 5. Convert ## Headers → <b>Header</b>
    text = _HEADER_RE.sub(r"<b>\1</b>", text)

    # 6. Convert **bold** → <b>bold</b>
    text = _BOLD_RE.sub(r"<b>\1</b>", text)

    # 7. Convert [text](url) → <a href="url">text</a>
    text = _LINK_RE.sub(r'<a href="\2">\1</a>', text)

    # 8. Remove horizontal rules
    text = _HR_RE.sub("", text)

    # 9. Clean up excessive blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)

    # 10. Restore inline code as <code> (with HTML escaping)
    for i, code in enumerate(inline_stash):
        escaped = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace(f"\x00INLINE{i}\x00", f"<code>{escaped}</code>")

    # 11. Restore code blocks as <pre> (with HTML escaping)
    for i, block in enumerate(code_stash):
        block_match = re.match(r"```(\w*)\n?([\s\S]*?)```", block)
        if block_match:
            lang = block_match.group(1)
            code = block_match.group(2).rstrip()
            code = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            if lang:
                text = text.replace(
                    f"\x00CODEBLOCK{i}\x00",
                    f'<pre><code class="language-{lang}">{code}</code></pre>',
                )
            else:
                text = text.replace(f"\x00CODEBLOCK{i}\x00", f"<pre>{code}</pre>")
        else:
            text = text.replace(f"\x00CODEBLOCK{i}\x00", block)

    return text.strip()


def _strip_html_tags(text: str) -> str:
    """Strip HTML tags and unescape entities for plain text fallback."""
    text = _HTML_TAG_RE.sub("", text)
    text = text.replace("&lt;", "<")
    text = text.replace("&gt;", ">")
    text = text.replace("&amp;", "&")
    return text


def format_usage_footer(usage: dict[str, int]) -> str:
    """Format token usage as a compact footer string."""
    inp = usage.get("input_tokens", 0)
    out = usage.get("output_tokens", 0)
    inp_str = f"{inp / 1000:.1f}K" if inp >= 1000 else str(inp)
    out_str = f"{out / 1000:.1f}K" if out >= 1000 else str(out)
    return f"\U0001f4ca {inp_str} in / {out_str} out"


def format_event_bus_status(stats: dict) -> str:
    """F035.1: Format event bus stats for Telegram /status output."""
    total = stats.get("total_processed", 0)
    dropped = stats.get("total_dropped", 0)
    queue = stats.get("queue_depth", 0)
    uptime_s = stats.get("uptime_seconds", 0)
    hours = int(uptime_s // 3600)
    mins = int((uptime_s % 3600) // 60)
    uptime_str = f"{hours}h {mins}m" if hours else f"{mins}m"

    lines = ["\n<b>Event Bus</b>", f"  {total} events processed, {dropped} dropped",
             f"  Queue: {queue} pending | Uptime: {uptime_str}", "", "  Handlers:"]

    for name, h in stats.get("handlers", {}).items():
        # Safe class name extraction that handles names without dots
        parts = name.split(".")
        short_name = parts[-2] if len(parts) >= 2 else name
        invocations = h.get("invocations", 0)
        successes = h.get("successes", 0)
        error_rate = h.get("error_rate", 0.0)
        flag = "!!" if error_rate > 0.10 else "OK"
        lines.append(f"  {flag} {short_name}: {successes}/{invocations}")
    return "\n".join(lines)


def format_trace_summary(trace_data: dict) -> str:
    """F035.2: Format a causal chain for Telegram."""
    events = trace_data.get("events", [])
    if not events:
        return "No events found for this trace."
    lines = [f"<b>Trace: {trace_data.get('trace_id', '?')}</b>",
             f"Root: {trace_data.get('root_event', '?')}",
             f"Depth: {trace_data.get('depth', 0)} events"]
    if trace_data.get("duration_ms"):
        lines.append(f"Duration: {trace_data['duration_ms']:.0f}ms")
    lines.append("")
    for e in events:
        indent = "  " if e.get("caused_by") else ""
        mod = " [MOD]" if e.get("data", {}).get("modifies") else ""
        lines.append(f"{indent}{e.get('type', '?')}{mod}")
    return "\n".join(lines)


def format_context_summary(entry_data: dict) -> str:
    """F035.4: Format context log entry for Telegram."""
    total = entry_data.get("total_tokens_est", 0)
    actual = entry_data.get("input_tokens_actual")
    utilization = entry_data.get("utilization_pct", 0)
    duration = entry_data.get("duration_ms")
    lines = [
        f"<b>Last API Call (Turn {entry_data.get('turn_number', '?')})</b>",
        f"  Model: {entry_data.get('model', '?')}",
        f"  Frame: {entry_data.get('frame_id', '?')}",
    ]
    token_str = f"  Tokens: ~{total:,} est"
    if actual:
        token_str += f" / {actual:,} actual"
    token_str += f" ({utilization:.1f}% of window)"
    lines.append(token_str)
    if duration:
        lines.append(f"  Duration: {duration / 1000:.1f}s")
    breakdown = entry_data.get("token_breakdown", {})
    if breakdown:
        lines.extend(["", "  Token Breakdown:"])
        for name, tokens in sorted(breakdown.items(), key=lambda x: x[1], reverse=True)[:5]:
            pct = (tokens / total * 100) if total else 0
            lines.append(f"  - {name}: {tokens:,} ({pct:.0f}%)")
    facts = entry_data.get("loaded_facts", 0)
    decisions = entry_data.get("loaded_decisions", 0)
    procedures = entry_data.get("loaded_procedures", 0)
    if facts or decisions or procedures:
        lines.append(f"\n  Memory: {facts} facts, {procedures} procedures, {decisions} decisions")
    lines.append(f"  Tools: {entry_data.get('tools_count', 0)}")
    return "\n".join(lines)


class StreamingMessage:
    """Manages progressive message editing for Telegram streaming."""

    # Tool name -> (emoji, display label)
    TOOL_INDICATORS: dict[str, tuple[str, str]] = {
        "web_search": ("\U0001f50d", "Searching"),
        "web_fetch": ("\U0001f310", "Fetching"),
        "recall_deep": ("\U0001f9e0", "Remembering"),
        "record_decision": ("\U0001f4dd", "Recording"),
        "learn_fact": ("\U0001f4a1", "Learning"),
        "bash": ("\u2699\ufe0f", "Running"),
        "read_file": ("\U0001f4c4", "Reading"),
        "write_file": ("\u270f\ufe0f", "Writing"),
        "run_python": ("\U0001f4bb", "Scripting"),
        "send_file": ("\U0001f4ce", "Sending file"),
    }

    def __init__(self, bot: NousTelegramBot, chat_id: int):
        self._bot = bot
        self.chat_id = chat_id
        self.message_id: int | None = None
        self.text = ""
        self._base_text = ""  # Text without tool indicators
        self._tool_counts: dict[str, int] = {}  # tool_name -> count
        self._last_edit = 0.0
        self._min_interval = 1.2  # N6: ~20 edits/msg limit
        self._pending = False
        self._usage: dict[str, int] | None = None  # Token usage stats
        self._thinking_text = ""           # accumulated thinking content
        self._thinking_count = 0           # number of thinking blocks seen
        self._thinking_displayed = False   # whether we've shown an indicator

    def set_usage(self, usage: dict[str, int]) -> None:
        """Set token usage for the footer."""
        self._usage = usage

    async def start_thinking(self) -> None:
        """Called on thinking_start event."""
        self._thinking_count += 1
        self._thinking_text = ""
        self._thinking_displayed = False

    async def append_thinking(self, text: str) -> None:
        """Called on thinking_delta event."""
        self._thinking_text += text
        # Show preview after accumulating enough text (first 100 chars)
        if not self._thinking_displayed and len(self._thinking_text) >= 50:
            self.text = self._build_display_text()
            await self._send_or_edit()
            self._thinking_displayed = True

    async def append_text(self, text: str) -> None:
        """Append text delta to the base text and update display."""
        await self.update(self._base_text + text)

    async def update(self, new_text: str) -> None:
        """Update message text. Creates on first call, edits after."""
        self._base_text = new_text
        self.text = self._build_display_text()
        now = time.time()
        if now - self._last_edit < self._min_interval:
            self._pending = True
            return
        await self._send_or_edit()

    async def append_tool_indicator(self, tool_name: str) -> None:
        """Track tool usage with grouped counters instead of appending lines."""
        self._tool_counts[tool_name] = self._tool_counts.get(tool_name, 0) + 1
        self.text = self._build_display_text()
        await self._send_or_edit()

    def _build_display_text(self) -> str:
        """Build display text: thinking indicator + base text + collapsed tool indicators."""
        parts = []

        # Thinking indicator (before response text)
        if self._thinking_count > 0 and self._thinking_text:
            preview = self._thinking_text[:100].replace("\n", " ").strip()
            if len(self._thinking_text) > 100:
                preview += "..."
            if self._thinking_count > 1:
                parts.append(f"\U0001f4ad Thinking ({self._thinking_count}): {preview}")
            else:
                parts.append(f"\U0001f4ad {preview}")

        if self._base_text:
            parts.append(self._base_text)

        if self._tool_counts:
            indicators: list[str] = []
            for tool_name, count in self._tool_counts.items():
                emoji, label = self.TOOL_INDICATORS.get(
                    tool_name, ("\U0001f527", tool_name)
                )
                if count > 1:
                    indicators.append(f"{emoji} {label}... ({count})")
                else:
                    indicators.append(f"{emoji} {label}...")
            parts.append(" ".join(indicators))

        return "\n\n".join(parts)

    async def finalize(self) -> None:
        """Send final version of message with HTML formatting and usage footer."""
        parts = []

        # Thinking summary (before response text)
        if self._thinking_count > 0:
            if self._thinking_text:
                preview = self._thinking_text[:100].replace("\n", " ").strip()
                if len(self._thinking_text) > 100:
                    preview += "..."
                if self._thinking_count > 1:
                    parts.append(f"\U0001f4ad Thinking ({self._thinking_count}): {preview}")
                else:
                    parts.append(f"\U0001f4ad {preview}")
            else:
                # Redacted thinking — no content available
                parts.append("\U0001f4ad Thinking... (redacted)")

        # Tool summary
        if self._tool_counts:
            total = sum(self._tool_counts.values())
            parts.append(f"\U0001f527 Ran {total} tool{'s' if total > 1 else ''}")

        # Response text
        if self._base_text:
            parts.append(self._base_text)

        # Usage footer
        if self._usage:
            parts.append(format_usage_footer(self._usage))

        self.text = "\n\n".join(parts)
        self._tool_counts.clear()

        await self._send_or_edit(parse_mode="HTML")

    async def _send_or_edit(self, parse_mode: str | None = None) -> None:
        if not self.text.strip():
            return

        if parse_mode == "HTML":
            display_text = format_telegram_html(self.text)
        else:
            display_text = sanitize_telegram(self.text)

        # N7: Handle 4096 char overflow
        if len(display_text) > 4000 and self.message_id is not None:
            overflow = display_text[4000:]
            truncated = display_text[:4000] + "\n\n(continued...)"
            edit_params: dict[str, Any] = {
                "chat_id": self.chat_id,
                "message_id": self.message_id,
                "text": truncated,
            }
            if parse_mode:
                edit_params["parse_mode"] = parse_mode
            await self._bot._tg("editMessageText", params=edit_params)
            result = await self._bot._send(self.chat_id, overflow, parse_mode=parse_mode)
            if isinstance(result, dict) and "message_id" in result:
                self.message_id = result["message_id"]
            self.text = overflow
            self._last_edit = time.time()
            self._pending = False
            return

        if self.message_id is None:
            result = await self._bot._send(
                self.chat_id, display_text, parse_mode=parse_mode
            )
            if isinstance(result, dict) and "message_id" in result:
                self.message_id = result["message_id"]
        else:
            edit_params = {
                "chat_id": self.chat_id,
                "message_id": self.message_id,
                "text": display_text,
            }
            if parse_mode:
                edit_params["parse_mode"] = parse_mode
            await self._bot._tg("editMessageText", params=edit_params)
        self._last_edit = time.time()
        self._pending = False


class NousTelegramBot:
    """Lightweight Telegram bot that proxies to Nous /chat API."""

    def __init__(
        self,
        bot_token: str,
        nous_url: str = "http://localhost:8000",
        allowed_users: set[int] | None = None,
        attachments_enabled: bool = False,
        attachments_default_prompt: str = "What can you tell me about this?",
    ):
        self.bot_token = bot_token
        self.nous_url = nous_url.rstrip("/")
        self.allowed_users = allowed_users
        self.attachments_enabled = attachments_enabled
        self.attachments_default_prompt = attachments_default_prompt
        self._offset = 0
        # Map telegram chat_id -> nous session_id for continuity
        self._sessions: dict[int, str] = {}
        # Track last activity time per chat to detect stale sessions
        self._session_last_active: dict[int, float] = {}
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=300, write=10, pool=10))

    async def start(self) -> None:
        """Start polling loop."""
        me = await self._tg("getMe")
        logger.info("Bot started: @%s (%s)", me.get("username"), me.get("id"))
        print(f"Nous Telegram bot started: @{me.get('username')}")

        while True:
            try:
                updates = await self._tg(
                    "getUpdates",
                    params={"offset": self._offset, "timeout": 30},
                )
                for update in updates:
                    self._offset = update["update_id"] + 1
                    await self._handle_update(update)
            except httpx.ReadTimeout:
                continue  # Normal for long polling
            except Exception as e:
                logger.error("Polling error: %s", e)
                await asyncio.sleep(5)

    async def _download_telegram_file(self, file_id: str) -> bytes:
        """Resolve file_id via getFile, then GET the file-download URL.

        Telegram files are downloadable for ~1h; Bot API max 20 MB.
        """
        info = await self._tg("getFile", params={"file_id": file_id})
        file_path = info.get("file_path")
        if not file_path:
            raise ValueError(f"getFile returned no file_path for {file_id}")
        url = f"https://api.telegram.org/file/bot{self.bot_token}/{file_path}"
        resp = await self._http.get(url, timeout=60)
        resp.raise_for_status()
        data = resp.content
        if not data:
            raise ValueError(f"Empty download for file_id={file_id}")
        return data

    async def _handle_update(self, update: dict[str, Any]) -> None:
        """Handle a single Telegram update."""
        message = update.get("message")
        if not message:
            return

        chat_id = message["chat"]["id"]
        user_id = message.get("from", {}).get("id")
        # Photos/documents/stickers arrive with a caption (or no text at all).
        text = (message.get("text") or message.get("caption") or "").strip()

        # Access control — must precede any download or forwarding.
        if self.allowed_users and user_id not in self.allowed_users:
            await self._send(chat_id, "⛔ Not authorized.")
            return

        # Handle /start
        if text == "/start":
            await self._send(chat_id, "🧠 *Nous* is ready\\. Send me a message\\!", parse_mode="MarkdownV2")
            return

        # Handle /new - end current session properly, then start fresh
        if text == "/new":
            old_session_id = self._sessions.pop(chat_id, None)
            self._session_last_active.pop(chat_id, None)
            if old_session_id:
                try:
                    await self._http.delete(
                        f"{self.nous_url}/chat/{old_session_id}",
                        timeout=10,
                    )
                except Exception:
                    logger.warning("Failed to end session %s via API", old_session_id)
            await self._send(chat_id, "🔄 New session started.")
            return

        # Handle /debug - toggle debug mode
        if text == "/debug":
            await self._chat(chat_id, "Show me your current status and available tools.", debug=True)
            return

        # Handle /identity - show current agent identity
        if text == "/identity":
            await self._show_identity(chat_id)
            return

        # Download inbound attachments (photos/documents/stickers).
        attachments: list[Attachment] = []
        if self.attachments_enabled:
            try:
                photos = message.get("photo") or []
                if photos:
                    p = photos[-1]  # largest size
                    raw = await self._download_telegram_file(p["file_id"])
                    attachments.append(Attachment(
                        filename=f"photo_{p['file_unique_id']}.jpg",
                        media_type="image/jpeg",
                        data_base64=base64.b64encode(raw).decode(),
                        size_bytes=len(raw), source="telegram", content_type="image"))

                doc = message.get("document")
                if doc:
                    raw = await self._download_telegram_file(doc["file_id"])
                    mime = (doc.get("mime_type")
                            or mimetypes.guess_type(doc.get("file_name", ""))[0]
                            or "application/octet-stream")
                    fname = sanitize_filename(doc.get("file_name") or "document")
                    attachments.append(Attachment(
                        filename=fname, media_type=mime,
                        data_base64=base64.b64encode(raw).decode(),
                        size_bytes=len(raw), source="telegram",
                        content_type=classify_attachment(fname, mime)))

                sticker = message.get("sticker")
                if sticker and not sticker.get("is_animated") and not sticker.get("is_video"):
                    raw = await self._download_telegram_file(sticker["file_id"])
                    attachments.append(Attachment(
                        filename=f"sticker_{sticker['file_unique_id']}.webp",
                        media_type="image/webp",
                        data_base64=base64.b64encode(raw).decode(),
                        size_bytes=len(raw), source="telegram", content_type="image"))

                if (message.get("voice") or message.get("audio")) and not attachments:
                    await self._send(chat_id, "\U0001F3A4 I can't process audio yet. "
                                              "Send text, images, PDFs, or code files.")
                    if not text:
                        return
            except Exception as e:
                logger.error("Attachment download failed: %s", e, exc_info=True)
                if not text:
                    await self._send(chat_id, "⚠️ I couldn't download that file. Please try again.")
                    return
                attachments = []  # continue text-only

        if not text and not attachments:
            return

        # If the user sent only an attachment (no caption), give the agent a prompt.
        if not text and attachments:
            text = self.attachments_default_prompt

        # Forward to Nous (streaming) — 007.4: pass user identity
        user_display_name = message.get("from", {}).get("first_name")
        await self._chat_streaming(
            chat_id, text, user_id=str(user_id) if user_id else None,
            user_display_name=user_display_name, attachments=attachments or None)

    async def _show_identity(self, chat_id: int) -> None:
        """Show current agent identity via REST API."""
        try:
            from html import escape
            resp = await self._http.get(f"{self.nous_url}/identity", timeout=10)
            if resp.status_code != 200:
                await self._send(chat_id, f"❌ Failed to fetch identity: {escape(resp.text)}")
                return
            data = resp.json()

            if not data.get("sections"):
                await self._send(chat_id, "🧠 No identity configured yet. Start a conversation to begin initiation.")
                return

            parts = [f"🧠 <b>Agent Identity</b> ({escape(str(data.get('agent_id', 'unknown')))})"]
            parts.append(f"Initiated: {'✅' if data.get('is_initiated') else '❌'}\n")
            for section, content in data.get("sections", {}).items():
                parts.append(f"<b>{escape(section.title())}</b>\n{escape(content)}")
            await self._send(chat_id, "\n\n".join(parts), parse_mode="HTML")
        except Exception as e:
            await self._send(chat_id, f"❌ Error: {e}")

    async def _chat(self, chat_id: int, text: str, debug: bool = False) -> None:
        """Send message to Nous API and relay response to Telegram."""
        # Send typing indicator
        await self._tg("sendChatAction", params={"chat_id": chat_id, "action": "typing"})

        # Zombie session fix: expire stale session IDs
        last_active = self._session_last_active.get(chat_id, 0)
        if chat_id in self._sessions and (time.time() - last_active) > SESSION_TTL_SECONDS:
            logger.info("Session %s expired (idle %.0fs), starting fresh",
                        self._sessions[chat_id], time.time() - last_active)
            del self._sessions[chat_id]
        session_id = self._sessions.get(chat_id)
        payload: dict[str, Any] = {"message": text, "platform": "telegram"}
        if session_id:
            payload["session_id"] = session_id
        if debug:
            payload["debug"] = True

        try:
            response = await self._http.post(
                f"{self.nous_url}/chat",
                json=payload,
                timeout=120,
            )
            data = response.json()

            if response.status_code != 200:
                await self._send(chat_id, f"❌ Error: {data.get('error', 'Unknown error')}")
                return

            # Store session for continuity
            if "session_id" in data:
                self._sessions[chat_id] = data["session_id"]
                self._session_last_active[chat_id] = time.time()

            # Build response text (convert LLM markdown to Telegram HTML)
            reply = format_telegram_html(data.get("response", "No response"))
            frame = data.get("frame", "unknown")

            # Add frame indicator
            frame_emoji = {
                "task": "🔧",
                "question": "❓",
                "decision": "⚖️",
                "creative": "🎨",
                "conversation": "💬",
                "debug": "🐛",
            }
            frame_tag = f"{frame_emoji.get(frame, '🧠')} [{frame}]"

            # Add debug info if requested
            if debug and "debug" in data:
                d = data["debug"]
                prompt = html_module.escape(d.get("system_prompt", "(empty)"), quote=False)
                debug_text = (
                    f"\n\n---\n🔍 Debug Info:\n"
                    f"Frame: {frame} (confidence: {d.get('frame_confidence', '?')})\n"
                    f"Censors: {d.get('active_censors', 0)}\n"
                    f"Decisions: {d.get('related_decisions', 0)}\n"
                    f"Facts: {d.get('related_facts', 0)}\n"
                    f"Episodes: {d.get('related_episodes', 0)}\n"
                    f"Context tokens: {d.get('context_tokens', 0)}\n"
                    f"\n📋 SYSTEM PROMPT:\n<pre>{prompt}</pre>"
                )
                reply += debug_text

            # Add usage footer if available
            usage = data.get("usage")
            if usage:
                reply += f"\n\n{format_usage_footer(usage)}"

            # Split long messages
            full_reply = f"{frame_tag}\n\n{reply}"
            await self._send_long(chat_id, full_reply, parse_mode="HTML")

            # If decision was recorded, add a subtle indicator
            if data.get("decision_id"):
                await self._tg(
                    "setMessageReaction",
                    params={
                        "chat_id": chat_id,
                        "message_id": (await self._get_last_bot_message_id(chat_id)),
                        "reaction": json.dumps([{"type": "emoji", "emoji": "🧠"}]),
                    },
                )

        except httpx.TimeoutException:
            await self._send(chat_id, "⏱ Request timed out. Nous might be thinking hard...")
        except Exception as e:
            logger.error("Chat error: %s", e)
            await self._send(chat_id, f"❌ Error: {e}")

    async def _chat_streaming(
        self, chat_id: int, text: str,
        user_id: str | None = None, user_display_name: str | None = None,
        attachments: list[Attachment] | None = None,
    ) -> None:
        """Send message to Nous streaming API and progressively edit Telegram message."""
        from uuid import uuid4

        await self._tg("sendChatAction", params={"chat_id": chat_id, "action": "typing"})

        # P1 fix: generate session_id upfront so client and server use same ID
        # Zombie session fix: expire stale session IDs to match server-side idle timeout
        last_active = self._session_last_active.get(chat_id, 0)
        if chat_id in self._sessions and (time.time() - last_active) > SESSION_TTL_SECONDS:
            logger.info("Session %s expired (idle %.0fs), starting fresh",
                        self._sessions[chat_id], time.time() - last_active)
            del self._sessions[chat_id]
        session_id = self._sessions.setdefault(chat_id, str(uuid4()))
        self._session_last_active[chat_id] = time.time()
        payload: dict[str, Any] = {"message": text, "session_id": session_id, "platform": "telegram"}
        # 007.4: Pass user identity for episode tracking
        if user_id:
            payload["user_id"] = user_id
        if user_display_name:
            payload["user_display_name"] = user_display_name
        if attachments:
            payload["attachments"] = [
                {"filename": a.filename, "media_type": a.media_type,
                 "data_base64": a.data_base64, "size_bytes": a.size_bytes}
                for a in attachments
            ]

        streamer = StreamingMessage(self, chat_id)

        # Background task: send typing indicator every 4s while streaming
        typing_active = True

        async def _typing_loop():
            while typing_active:
                try:
                    await self._tg(
                        "sendChatAction",
                        params={"chat_id": chat_id, "action": "typing"},
                    )
                except Exception:
                    pass
                await asyncio.sleep(4)

        typing_task = asyncio.create_task(_typing_loop())

        try:
            # Override read timeout to None for the streaming call only.
            # The server emits SSE comment-line keepalives every
            # NOUS_SSE_PING_INTERVAL seconds (default 15s) so the socket
            # stays warm even during long pre_turn / compaction / tool
            # phases. The shared client's 300s read timeout would otherwise
            # cap turn duration arbitrarily. connect/write/pool timeouts
            # are still enforced to catch real network failures fast.
            stream_timeout = httpx.Timeout(
                connect=10, read=None, write=10, pool=10
            )
            async with self._http.stream(
                "POST",
                f"{self.nous_url}/chat/stream",
                json=payload,
                timeout=stream_timeout,
            ) as response:
                if response.status_code != 200:
                    error_body = await response.aread()
                    await self._send(chat_id, f"\u274c Error: {error_body.decode()[:200]}")
                    return

                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    event = json.loads(line[6:])

                    if event.get("type") == "text_delta":
                        await streamer.append_text(event.get("text", ""))
                    elif event.get("type") == "thinking_start":
                        await streamer.start_thinking()
                    elif event.get("type") == "thinking_delta":
                        await streamer.append_thinking(event.get("text", ""))
                    elif event.get("type") == "redacted_thinking":
                        await streamer.start_thinking()
                    elif event.get("type") == "keepalive":
                        pass  # typing_loop handles indicators
                    elif event.get("type") == "tool_start":
                        await streamer.append_tool_indicator(event.get("tool_name", ""))
                    elif event.get("type") == "error":
                        await self._send(chat_id, f"\u274c {event.get('text', 'Unknown error')}")
                        return
                    elif event.get("type") == "done":
                        if event.get("usage"):
                            streamer.set_usage(event["usage"])
                        break

        except httpx.TimeoutException:
            await self._send(chat_id, "\u23f1 Request timed out.")
        except Exception as e:
            logger.error("Streaming chat error: %s", e)
            await self._send(chat_id, f"\u274c Error: {e}")
        finally:
            typing_active = False
            typing_task.cancel()
            try:
                await typing_task
            except asyncio.CancelledError:
                pass
            await streamer.finalize()

    async def _send(self, chat_id: int, text: str, parse_mode: str | None = None) -> dict:
        """Send a message to Telegram."""
        params: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if parse_mode:
            params["parse_mode"] = parse_mode
        return await self._tg("sendMessage", params=params)

    async def _send_long(
        self, chat_id: int, text: str, parse_mode: str | None = None
    ) -> None:
        """Send a long message, splitting if needed."""
        if len(text) <= TG_MAX_LEN:
            await self._send(chat_id, text, parse_mode=parse_mode)
            return

        # Split on newlines, respecting max length
        chunks: list[str] = []
        current = ""
        for line in text.split("\n"):
            if len(current) + len(line) + 1 > TG_MAX_LEN:
                if current:
                    chunks.append(current)
                current = line
            else:
                current = f"{current}\n{line}" if current else line
        if current:
            chunks.append(current)

        for chunk in chunks:
            await self._send(chat_id, chunk, parse_mode=parse_mode)
            await asyncio.sleep(0.3)  # Rate limit

    async def _get_last_bot_message_id(self, chat_id: int) -> int | None:
        """Best-effort: return None if we can't track it."""
        return None  # TODO: track sent message IDs

    async def _tg(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Call Telegram Bot API with parse_mode fallback.

        If a request with parse_mode fails (e.g. malformed HTML from LLM output),
        retries without parse_mode using plain text as a fallback.
        """
        url = TG_API.format(token=self.bot_token, method=method)
        response = await self._http.get(url, params=params)
        data = response.json()
        if not data.get("ok"):
            # Retry without parse_mode if formatting caused the error
            if params and "parse_mode" in params:
                logger.warning(
                    "Telegram API error with parse_mode=%s, retrying plain: %s",
                    params["parse_mode"],
                    data.get("description", ""),
                )
                fallback_params = {k: v for k, v in params.items() if k != "parse_mode"}
                if "text" in fallback_params:
                    fallback_params["text"] = _strip_html_tags(fallback_params["text"])
                response = await self._http.get(url, params=fallback_params)
                data = response.json()
                if data.get("ok"):
                    return data.get("result", {})
            logger.warning("Telegram API error: %s", data)
            return data.get("result", [])
        return data.get("result", {})

    async def close(self) -> None:
        """Cleanup."""
        await self._http.aclose()


async def main() -> None:
    """Entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        print("Error: TELEGRAM_BOT_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    nous_url = os.environ.get("NOUS_API_URL", "http://localhost:8000")

    allowed_str = os.environ.get("NOUS_ALLOWED_USERS", "")
    allowed_users = None
    if allowed_str:
        allowed_users = {int(uid.strip()) for uid in allowed_str.split(",") if uid.strip()}

    from nous.config import Settings
    _settings = Settings()
    bot = NousTelegramBot(
        bot_token, nous_url, allowed_users,
        attachments_enabled=_settings.attachments_enabled,
        attachments_default_prompt=_settings.attachments_default_prompt,
    )
    try:
        await bot.start()
    finally:
        await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
