"""Natural language time parsing for schedule_task tool.

Provides two functions:
- parse_when: Parse one-shot time strings into UTC-aware datetimes
- parse_every: Parse recurring schedule strings into interval/cron pairs
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from dateutil import parser as dateutil_parser

logger = logging.getLogger(__name__)

# Common US timezone abbreviations → IANA zones. Abbreviations are inherently
# ambiguous (EST vs EDT), so we resolve to the IANA zone and let ZoneInfo pick
# the correct offset for the current date (DST-aware at parse time).
_TZ_ABBREV: dict[str, str] = {
    "UTC": "UTC", "GMT": "UTC", "Z": "UTC",
    "ET": "America/New_York", "EST": "America/New_York", "EDT": "America/New_York",
    "CT": "America/Chicago", "CST": "America/Chicago", "CDT": "America/Chicago",
    "MT": "America/Denver", "MST": "America/Denver", "MDT": "America/Denver",
    "PT": "America/Los_Angeles", "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles",
}


def _local_hour_to_utc(hour24: int, tz_token: str | None) -> int:
    """Convert a wall-clock hour in ``tz_token`` to the UTC hour for a cron.

    Audit HD-5 (2026-06-09): the daily pattern captured a timezone token then
    discarded it, so "daily at 9am EST" generated ``0 9 * * *`` and fired at
    09:00 UTC. The scheduler evaluates crons in UTC, so we bake the conversion
    here. Returns ``hour24`` unchanged when the token is absent, UTC, or
    unrecognized (safe — preserves prior behavior).

    Limitation: a fixed-hour cron baked from the current offset drifts by one
    hour across a DST transition until the schedule is re-created. True
    DST-stable firing requires a timezone-aware scheduler (deferred).
    """
    if not tz_token:
        return hour24
    zone_name = _TZ_ABBREV.get(tz_token.strip().upper())
    if zone_name is None:
        zone_name = tz_token  # maybe a full IANA name, e.g. "America/New_York"
    if zone_name == "UTC":
        return hour24
    try:
        tz = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError, ModuleNotFoundError):
        logger.warning(
            "Unrecognized timezone %r in schedule; treating hour as UTC", tz_token
        )
        return hour24
    local_dt = datetime.now(tz).replace(
        hour=hour24, minute=0, second=0, microsecond=0
    )
    return local_dt.astimezone(UTC).hour

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Relative time: "in N hours/minutes/days/weeks"
_RELATIVE_RE = re.compile(
    r"in\s+(\d+)\s+(minute|hour|day|week)s?", re.IGNORECASE
)

_UNIT_SECONDS = {
    "minute": 60,
    "hour": 3600,
    "day": 86400,
    "week": 604800,
}

# Simple interval: "N hours/minutes/days"
_INTERVAL_RE = re.compile(
    r"^(\d+)\s+(minute|hour|day|week)s?$", re.IGNORECASE
)

# "daily at 8am", "daily at 9am EST", "daily at 2pm UTC"
_DAILY_RE = re.compile(
    r"daily\s+at\s+(\d{1,2})\s*(am|pm)(?:\s+([\w/]+))?", re.IGNORECASE
)

# "every monday at 10am", "every friday at 3pm"
_WEEKLY_RE = re.compile(
    r"every\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"\s+at\s+(\d{1,2})\s*(am|pm)",
    re.IGNORECASE,
)

_DOW_MAP = {
    "monday": 1,
    "tuesday": 2,
    "wednesday": 3,
    "thursday": 4,
    "friday": 5,
    "saturday": 6,
    "sunday": 0,
}


# ---------------------------------------------------------------------------
# parse_when — one-shot time strings
# ---------------------------------------------------------------------------


def parse_when(when: str) -> datetime:
    """Parse a one-shot time string into a UTC-aware datetime.

    Supported formats:
    - ISO 8601: "2026-03-10T09:00:00-05:00"
    - Relative: "in 2 hours", "in 30 minutes", "in 3 days"
    - Natural: "tomorrow 9am", "next monday 8am EST" (via dateutil fallback)

    Raises:
        ValueError: If the time is in the past or unparseable.

    Returns:
        UTC-aware datetime.
    """
    when = when.strip()
    now = datetime.now(UTC)

    # Try relative time first
    m = _RELATIVE_RE.match(when)
    if m:
        amount = int(m.group(1))
        unit = m.group(2).lower()
        delta = timedelta(seconds=amount * _UNIT_SECONDS[unit])
        return now + delta

    # Try ISO 8601 / natural language via dateutil
    try:
        dt = dateutil_parser.parse(when, fuzzy=True)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"Cannot parse time: {when!r}") from exc

    # Make timezone-aware (assume UTC if naive)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)

    # Reject past times
    if dt <= now:
        raise ValueError(f"Time is in the past: {dt.isoformat()}")

    return dt


# ---------------------------------------------------------------------------
# parse_every — recurring schedule strings
# ---------------------------------------------------------------------------


def _to_24h(hour: int, ampm: str) -> int:
    """Convert 12-hour time to 24-hour."""
    ampm = ampm.lower()
    if ampm == "am":
        return 0 if hour == 12 else hour
    else:  # pm
        return hour if hour == 12 else hour + 12


def parse_every(every: str) -> tuple[int | None, str | None]:
    """Parse a recurring schedule string.

    Returns (interval_seconds, cron_expr) — one or both populated.

    Supported formats:
    - Simple intervals: "30 minutes" -> (1800, None)
    - Daily patterns: "daily at 8am" -> (None, "0 8 * * *")
    - Weekly patterns: "every monday at 10am" -> (None, "0 10 * * 1")

    Raises:
        ValueError: If the string cannot be parsed.
    """
    every = every.strip()

    # Try weekly pattern first (more specific)
    m = _WEEKLY_RE.match(every)
    if m:
        day_name = m.group(1).lower()
        hour = int(m.group(2))
        ampm = m.group(3)
        h24 = _to_24h(hour, ampm)
        dow = _DOW_MAP[day_name]
        cron = f"0 {h24} * * {dow}"
        if not croniter.is_valid(cron):
            raise ValueError(f"Generated invalid cron: {cron!r}")
        return (None, cron)

    # Try daily pattern
    m = _DAILY_RE.match(every)
    if m:
        hour = int(m.group(1))
        ampm = m.group(2)
        tz_token = m.group(3)
        # Audit HD-5: convert the wall-clock hour in the named timezone to UTC
        # (the scheduler evaluates crons in UTC). Previously the tz token was
        # discarded, so "daily at 9am EST" fired at 09:00 UTC.
        h24 = _local_hour_to_utc(_to_24h(hour, ampm), tz_token)
        cron = f"0 {h24} * * *"
        if not croniter.is_valid(cron):
            raise ValueError(f"Generated invalid cron: {cron!r}")
        return (None, cron)

    # Try simple interval: "30 minutes", "6 hours"
    m = _INTERVAL_RE.match(every)
    if m:
        amount = int(m.group(1))
        unit = m.group(2).lower()
        if amount <= 0:
            raise ValueError(f"Interval must be positive: {every!r}")
        seconds = amount * _UNIT_SECONDS[unit]
        return (seconds, None)

    raise ValueError(f"Cannot parse recurring schedule: {every!r}")
