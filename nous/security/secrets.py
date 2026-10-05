"""Secret detection shared by every path that sends or stores agent output.

``send_email`` refuses a message that matches; the F098 result memory writer
skips a result that matches rather than storing a redacted copy.
"""

from __future__ import annotations

import re

SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{12,}"),
    re.compile(r"password\s*[:=]", re.IGNORECASE),
    re.compile(r"Bearer [A-Za-z0-9._-]{20,}"),
    # Anthropic and OpenAI project keys: the hyphen after the prefix defeats the legacy sk- pattern.
    re.compile(r"\bsk-(?:ant|proj)-[A-Za-z0-9_-]{16,}"),
    # GitHub classic (ghp/gho/ghu/ghs/ghr) and fine-grained tokens.
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}"),
    # Slack bot, user and app tokens.
    re.compile(r"\bxox[bpa]-[0-9A-Za-z-]{20,}"),
    # Google API key: AIza plus 35 characters.
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}"),
    # Telegram bot token: bot id, colon, 35-char secret.
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
    # Credentials embedded in a URL: scheme://user:pass@
    re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s/@]+@", re.IGNORECASE),
    # JWT: base64url '{"' header and payload, then a signature.
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    # API_KEY=..., api_key: ..., "api_key": "...", x-api-key: ... with a token-like value.
    re.compile(r"api[_-]?key\b[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9_\-./+]{16,}", re.IGNORECASE),
)


def scan_secrets(text: str) -> bool:
    """Return True if the text matches any known secret pattern."""
    return any(p.search(text) for p in SECRET_PATTERNS)
