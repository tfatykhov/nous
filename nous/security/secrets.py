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
)


def scan_secrets(text: str) -> bool:
    """Return True if the text matches any known secret pattern."""
    return any(p.search(text) for p in SECRET_PATTERNS)
