"""F075 Layer 3: parse a date window from a query for the date-window retrieval leg."""
from __future__ import annotations

import asyncio
import datetime
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Month names, 4-digit years, ISO dates, and vague-period words. A hit means the
# query is worth a Haiku parse; a miss skips the LLM entirely (hot-path guard).
_MONTHS = (r"jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|jun(e)?|jul(y)?|"
           r"aug(ust)?|sep(tember)?|oct(ober)?|nov(ember)?|dec(ember)?")
# Deliberately generous (recall-over-precision): false-positives here are cheap since
# the LLM parse is fail-open, budget-capped, and cached.
_TEMPORAL_RE = re.compile(
    r"\b(" + _MONTHS + r")\b"
    r"|\b(19|20)\d{2}\b"                 # 4-digit year
    r"|\d{4}-\d{2}-\d{2}"                # ISO date
    r"|\b(yesterday|today|tomorrow|last|recent(ly)?|ago|"
    r"early|mid|late|around|when|during|after|before|since)\b",
    re.IGNORECASE,
)

# Hard cap: evict oldest entry when this limit is reached (OrderedDict.popitem(last=False)).
_CACHE_MAX_ENTRIES = 2048


@dataclass(frozen=True, slots=True)
class DateWindow:
    start: datetime.date
    end: datetime.date


def has_temporal_signal(query: str) -> bool:
    """Cheap pre-gate: True when the query plausibly references a time period."""
    return bool(_TEMPORAL_RE.search(query or ""))


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
        # OrderedDict maps query -> (insertion_monotonic, DateWindow | None).
        # Size-bounded at _CACHE_MAX_ENTRIES (evict oldest) and TTL-gated via
        # date_leg_cache_ttl_days. ttl_days <= 0 means "never cache".
        self._cache: OrderedDict[str, tuple[float, DateWindow | None]] = OrderedDict()
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

    def _cache_get(self, query: str) -> tuple[bool, DateWindow | None]:
        """Return (hit, value). Treats expired entries as misses (fail-open)."""
        try:
            ttl_days = int(getattr(self._settings, "date_leg_cache_ttl_days", 30))
            if ttl_days <= 0:
                return False, None  # "never cache" mode
            entry = self._cache.get(query)
            if entry is None:
                return False, None
            inserted_at, value = entry
            if time.monotonic() - inserted_at > ttl_days * 86400:
                del self._cache[query]
                return False, None
            return True, value
        except Exception:
            return False, None

    def _cache_put(self, query: str, value: DateWindow | None) -> None:
        """Insert into cache, evicting oldest when at cap (fail-open on error)."""
        try:
            ttl_days = int(getattr(self._settings, "date_leg_cache_ttl_days", 30))
            if ttl_days <= 0:
                return  # "never cache" mode
            cap = _CACHE_MAX_ENTRIES
            while len(self._cache) >= cap:
                self._cache.popitem(last=False)  # evict oldest-inserted entry
            self._cache[query] = (time.monotonic(), value)
        except Exception:
            pass

    async def parse(self, query: str, today: datetime.date) -> DateWindow | None:
        if not query or not has_temporal_signal(query):
            return None
        hit, cached_value = self._cache_get(query)
        if hit:
            return cached_value
        if not await self._within_budget():
            logger.warning("date-leg parser budget exhausted; failing open")
            return None
        window = await self._parse_llm(query, today)
        self._cache_put(query, window)
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
        try:
            pad = datetime.timedelta(days=int(getattr(self._settings, "date_leg_pad_days", 2)))
            return DateWindow(start=start - pad, end=end + pad)
        except OverflowError:
            return None
