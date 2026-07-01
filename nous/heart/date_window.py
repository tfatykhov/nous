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


import asyncio
import logging
import time

logger = logging.getLogger(__name__)

_WINDOW_SCHEMA = {
    "type": "object",
    "properties": {
        "has_date": {"type": "boolean"},
        "start_date": {"type": "string", "description": "YYYY-MM-DD or empty"},
        "end_date": {"type": "string", "description": "YYYY-MM-DD or empty"},
    },
    "required": ["has_date"],
    "additionalProperties": False,
}


def _prompt(query: str, today: datetime.date) -> str:
    return (
        f"Today is {today.isoformat()}. Read the question and extract the TIME "
        "PERIOD it asks about. If it references a date/month/period, has_date=true "
        "and give start_date/end_date (YYYY-MM-DD) that generously bound it: "
        "'late April 2026'->2026-04-20..2026-04-30; 'mid-May 2026'->2026-05-10.."
        "2026-05-20; 'April 2026'->2026-04-01..2026-04-30; 'around June 25 2026'->"
        "2026-06-22..2026-06-28. If no time reference, has_date=false.\n\n"
        f"Question: {query}"
    )


def _parse_iso(x: object) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(x).strip())
    except (ValueError, TypeError):
        return None


class DateWindowParser:
    """Parse a padded DateWindow from a query. Fail-open, budgeted, cached."""

    def __init__(self, llm_client, settings) -> None:
        self._client = llm_client
        self._settings = settings
        self._cache: dict[str, DateWindow | None] = {}
        self._budget_lock = asyncio.Lock()
        self._calls: list[float] = []  # unix timestamps within the trailing hour

    async def _within_budget(self) -> bool:
        cap = int(getattr(self._settings, "date_leg_max_per_hour", 500))
        now = time.monotonic()
        async with self._budget_lock:
            self._calls = [t for t in self._calls if now - t < 3600.0]
            if len(self._calls) >= cap:
                return False
            self._calls.append(now)
            return True

    async def parse(self, query: str, today: datetime.date) -> DateWindow | None:
        if not query or not has_temporal_signal(query):
            return None
        if query in self._cache:
            return self._cache[query]
        if not await self._within_budget():
            logger.warning("date-leg parser budget exhausted; failing open")
            return None
        window = await self._parse_llm(query, today)
        self._cache[query] = window
        return window

    async def _parse_llm(self, query: str, today: datetime.date) -> DateWindow | None:
        # Reuse the heart-layer structured-call helper (adds the Claude Code
        # preamble + cache_control, extracts the tool_use block, and returns
        # None on any client error). Wrap in wait_for for the timeout.
        from nous.handlers import call_background_llm_structured
        try:
            data = await asyncio.wait_for(
                call_background_llm_structured(
                    client=self._client,
                    model=self._settings.date_leg_model,
                    system_prompt="",
                    user_message=_prompt(query, today),
                    tool_name="emit_window",
                    tool_description="Extract the time period a question asks about.",
                    output_schema=_WINDOW_SCHEMA,
                    max_tokens=256,
                ),
                timeout=float(self._settings.date_leg_timeout_seconds),
            )
        except (asyncio.TimeoutError, Exception):  # fail-open on timeout/any error
            logger.warning("date-leg parse failed; failing open", exc_info=True)
            return None
        if not data or not data.get("has_date"):
            return None
        start, end = _parse_iso(data.get("start_date")), _parse_iso(data.get("end_date"))
        if start is None or end is None or start > end:
            return None
        pad = datetime.timedelta(days=int(getattr(self._settings, "date_leg_pad_days", 2)))
        return DateWindow(start=start - pad, end=end + pad)
