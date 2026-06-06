"""Guarded send_email tool (F078.1).

Provides a `send_email` tool that validates the recipient against an
allowlist before sending, scans the message for obvious secrets, and
rate-limits sends. This is the *safe chokepoint* alongside the existing
agent-authored bash+smtplib path (which stays untouched for BC).

Mirrors the structure of telegram_tools.py: closure factory +
``_SEND_EMAIL_SCHEMA`` + ``register_email_tools(dispatcher, settings)``.

Security note (lesson from decision 73ed8f2d): smtplib/login errors can
embed credentials or the SMTP host. We log the full error server-side but
return only a generic ``email send failed: <ExceptionType>`` to the model.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import smtplib
import time
from email.mime.text import MIMEText
from typing import Any

from nous.config import Settings

logger = logging.getLogger(__name__)

# Secret patterns scanned across subject + body. A hit rejects the send.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{12,}"),
    re.compile(r"password\s*[:=]", re.IGNORECASE),
    re.compile(r"Bearer [A-Za-z0-9._-]{20,}"),
)


def _ok(text: str) -> dict[str, Any]:
    """MCP-compliant success response."""
    return {"content": [{"type": "text", "text": text}]}


def _error(text: str) -> dict[str, Any]:
    """MCP-compliant error response."""
    return {"content": [{"type": "text", "text": f"Error: {text}"}]}


def _normalize_recipients(value: Any) -> list[str]:
    """Coerce a recipient field (str or list) into a list of stripped addresses."""
    if value is None:
        return []
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple)):
        items = []
        for v in value:
            items.extend(str(v).split(","))
    else:
        items = [str(value)]
    return [a.strip() for a in items if a and a.strip()]


def _parse_allowlist(raw: str) -> set[str]:
    """Parse the CSV allowlist into a set of lowercased addresses."""
    return {a.strip().lower() for a in (raw or "").split(",") if a.strip()}


def _read_allowlist_file(path: str, cache: dict[str, Any]) -> set[str]:
    """Read the hot-reloadable allowlist file, mtime-cached.

    One address per line or CSV; '#' starts a comment. Re-reads only when the
    file's mtime changes, so a live edit takes effect on the NEXT send with no
    restart. On any read error, returns the last-known set (fail-closed — an
    error never widens the allowlist).
    """
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return cache.get("addrs", set())
    if cache.get("mtime") == mtime:
        return cache["addrs"]
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return cache.get("addrs", set())
    addrs: set[str] = set()
    for line in raw.splitlines():
        line = line.split("#", 1)[0]
        for a in line.split(","):
            a = a.strip().lower()
            if a:
                addrs.add(a)
    cache["mtime"] = mtime
    cache["addrs"] = addrs
    return addrs


def _scan_secrets(text: str) -> bool:
    """Return True if the text matches any known secret pattern."""
    return any(p.search(text) for p in _SECRET_PATTERNS)


def _send_email_sync(
    settings: Settings,
    recipients: list[str],
    msg: MIMEText,
) -> None:
    """Blocking SMTP send. Runs in a worker thread via asyncio.to_thread."""
    server = smtplib.SMTP(settings.email_smtp_host, settings.email_smtp_port)
    try:
        server.starttls()
        server.login(settings.email_user, settings.email_password)
        server.send_message(msg, to_addrs=recipients)
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001 — quit() errors must not mask the real result
            pass


def create_send_email_tool(settings: Settings):
    """Create the send_email tool closure.

    The rate-limit window lives in this factory scope so it persists across
    calls. Only successful sends record a timestamp.
    """
    # Monotonic timestamps of successful sends, within the current hour window.
    _send_times: list[float] = []
    # F078.1.1: mtime-cache for the hot-reloadable allowlist file.
    _file_cache: dict[str, Any] = {"mtime": None, "addrs": set()}

    async def send_email(
        to: Any,
        subject: str,
        body: str,
        cc: Any = None,
    ) -> dict[str, Any]:
        """Send an email to allowlisted recipient(s).

        Args:
            to: Recipient address(es) — string or list. Each must be allowlisted.
            subject: Email subject line.
            body: Plain-text email body.
            cc: Optional CC address(es) — string or list. Each must be allowlisted.

        Returns:
            MCP-compliant response confirming the send or naming the rejection reason.
        """
        # 1. Enabled + creds check.
        if not settings.email_tool_enabled:
            return _error("send_email tool is disabled (NOUS_EMAIL_TOOL_ENABLED=false).")
        if not settings.email_user or not settings.email_password:
            return _error(
                "Email credentials not configured. Set NOUS_EMAIL_USER and "
                "NOUS_EMAIL_PASSWORD."
            )

        to_list = _normalize_recipients(to)
        cc_list = _normalize_recipients(cc)
        if not to_list:
            return _error("No recipient provided in 'to'.")

        # 2. Recipient allowlist (the core guard). Empty allowlist => reject all.
        # Effective = env CSV (static base) UNION the hot-reloadable file (read at
        # send-time, mtime-cached) — adding an address to the file needs no restart.
        allowlist = _parse_allowlist(settings.email_allowlist)
        if settings.email_allowlist_file:
            allowlist |= _read_allowlist_file(settings.email_allowlist_file, _file_cache)
        for addr in to_list + cc_list:
            if addr.lower() not in allowlist:
                return _error(
                    f"Recipient '{addr}' is not on the email allowlist; refusing to "
                    "send. To allow it, add the address to NOUS_EMAIL_ALLOWLIST "
                    "(operator action)."
                )

        # 3. Secret scan (secondary guard).
        if _scan_secrets(f"{subject}\n{body}"):
            return _error(
                "email appears to contain a secret (API key, password, or token); "
                "refusing to send."
            )

        # 4. Rate limit — in-process sliding window over the last hour.
        now = time.monotonic()
        cutoff = now - 3600.0
        _send_times[:] = [t for t in _send_times if t > cutoff]
        if len(_send_times) >= settings.email_max_per_hour:
            return _error(
                f"email rate limit reached ({settings.email_max_per_hour}/hour); "
                "try again later."
            )

        # 5. Send (smtplib is blocking → run in a worker thread).
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = settings.email or settings.email_user
        msg["To"] = ", ".join(to_list)
        if cc_list:
            msg["Cc"] = ", ".join(cc_list)
        all_recipients = to_list + cc_list

        try:
            await asyncio.to_thread(_send_email_sync, settings, all_recipients, msg)
        except Exception as e:  # noqa: BLE001 — sanitize: never leak smtplib/creds detail
            # 6. Sanitize exceptions: log full server-side, return generic to the model.
            logger.error(
                "send_email failed (to=%s): %s: %s",
                to_list,
                type(e).__name__,
                e,
            )
            return _error(f"email send failed: {type(e).__name__}")

        # 7. Record success for rate limiting and return confirmation.
        _send_times.append(now)
        cc_note = f" (cc: {', '.join(cc_list)})" if cc_list else ""
        return _ok(f"Email sent to {', '.join(to_list)}{cc_note}.")

    return send_email


# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------

_SEND_EMAIL_SCHEMA = {
    "description": (
        "Send a plain-text email to an allowlisted recipient. The recipient must be "
        "on the configured allowlist (NOUS_EMAIL_ALLOWLIST) or the send is refused. "
        "Prefer this tool over ad-hoc bash+smtplib for sending email — it is the safe, "
        "guarded path. The message is also scanned for secrets and rate-limited."
    ),
    "type": "object",
    "properties": {
        "to": {
            "type": ["string", "array"],
            "items": {"type": "string"},
            "description": "Recipient email address(es). String or list. Must be allowlisted.",
        },
        "subject": {
            "type": "string",
            "description": "Email subject line.",
        },
        "body": {
            "type": "string",
            "description": "Plain-text email body.",
        },
        "cc": {
            "type": ["string", "array"],
            "items": {"type": "string"},
            "description": "Optional CC address(es). String or list. Must be allowlisted.",
        },
    },
    "required": ["to", "subject", "body"],
}


def register_email_tools(dispatcher, settings: Settings) -> None:
    """Register the guarded send_email tool with the dispatcher."""
    handler = create_send_email_tool(settings)
    dispatcher.register("send_email", handler, _SEND_EMAIL_SCHEMA)
