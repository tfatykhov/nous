"""F075 Layer 3: parse a date window from a query for the date-window retrieval leg."""
from __future__ import annotations

import datetime
import re
from dataclasses import dataclass

# Month names, 4-digit years, ISO dates, and vague-period words. A hit means the
# query is worth a Haiku parse; a miss skips the LLM entirely (hot-path guard).
_MONTHS = (r"jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|jun(e)?|jul(y)?|"
           r"aug(ust)?|sep(tember)?|oct(ober)?|nov(ember)?|dec(ember)?")
_TEMPORAL_RE = re.compile(
    r"\b(" + _MONTHS + r")\b"
    r"|\b(19|20)\d{2}\b"                 # 4-digit year
    r"|\d{4}-\d{2}-\d{2}"                # ISO date
    r"|\b(yesterday|today|tomorrow|last|recent(ly)?|ago|"
    r"early|mid|late|around|when|during|after|before|since)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class DateWindow:
    start: datetime.date
    end: datetime.date


def has_temporal_signal(query: str) -> bool:
    """Cheap pre-gate: True when the query plausibly references a time period."""
    return bool(_TEMPORAL_RE.search(query or ""))
