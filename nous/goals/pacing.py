"""F114 goal pacing: a goal that works once a day, from a time of day in the one time zone the server is set to.

With ``NOUS_GOALS_PACING_ENABLED`` on and ``NOUS_GOALS_TIMEZONE`` naming a zone, a goal whose ``daily_at_minute`` is
set (migration 120) starts at most one session per calendar day of that zone, not before that minute of the day; the
goal driver asks ``hold`` at each start and moves the goal's wake with ``wake_after_hold`` and ``wake_after_close``.
Only the count of a paced goal's sessions follows the local day: the day budget and the job launches stay on the UTC
day for every goal, and so do quiet hours.

Every function here is pure: it takes aware datetimes and returns UTC instants, and builds each boundary from a local
date and a wall time, never by adding 24 hours to an instant, so a day the clock changes is 23 or 25 hours long. A wall
time the clock skips is read with the offset before the change (one hour later on the wall); one that occurs twice is
the first.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# The store's refusals while pacing is off.
CREATE_OFF = "Once-a-day goals are off on this server."
CHANGE_OFF = "Changing a goal's session settings is off on this server."
PACED = "paced"  # it is not yet the time of day set for the goal
DAY_SESSIONS = "day_sessions"  # the day's one session has started
MINUTES_PER_DAY = 24 * 60
# "HH:MM", 00:00 to 23:59, ASCII digits only.
_AT = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")


def zone(settings: Any) -> ZoneInfo | None:
    """The configured zone while pacing is on, else None: the flag off (or missing from ``settings``), or a zone
    name that is not a zone (the setting's validator turns the flag off then)."""
    if getattr(settings, "goals_pacing_enabled", False) is not True:
        return None
    name = getattr(settings, "goals_timezone", "")
    if not isinstance(name, str) or not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):  # not a zone, a malformed name, or a directory of zones
        return None


def local_date(now: datetime, tz: ZoneInfo) -> date:
    """The calendar date of ``now`` in ``tz``."""
    return now.astimezone(tz).date()


def day_start(now: datetime, tz: ZoneInfo) -> datetime:
    """The instant local midnight began the day ``now`` is in, in ``tz``."""
    return datetime.combine(local_date(now, tz), time.min, tzinfo=tz).astimezone(UTC)


def slot(day: date, minute: int, tz: ZoneInfo) -> datetime:
    """The instant of the wall time ``minute`` (minutes after midnight) on ``day`` in ``tz``."""
    return datetime.combine(day, time(minute // 60, minute % 60), tzinfo=tz).astimezone(UTC)


def hold(now: datetime, tz: ZoneInfo, minute: int, started_today: int) -> str | None:
    """Why a timer start of a paced goal waits at ``now``: ``DAY_SESSIONS`` once a session started this local day,
    ``PACED`` before today's time; None when it may start."""
    if started_today >= 1:
        return DAY_SESSIONS
    if now < slot(local_date(now, tz), minute, tz):
        return PACED
    return None


def wake_after_hold(held: str, now: datetime, tz: ZoneInfo, minute: int) -> datetime:
    """The wake of a paced goal held at ``now``: today's time when held ``PACED``, tomorrow's when held
    ``DAY_SESSIONS``."""
    today = local_date(now, tz)
    if held == PACED:
        return slot(today, minute, tz)
    if held == DAY_SESSIONS:
        return slot(today + timedelta(days=1), minute, tz)
    raise ValueError(f"not a hold of pacing: {held!r}")


def wake_after_close(started_at: datetime, tz: ZoneInfo, minute: int) -> datetime:
    """The time of the day after the local day a session started on: a session counts for the day it started,
    however late it ends."""
    return slot(local_date(started_at, tz) + timedelta(days=1), minute, tz)


def format_at(minute: int) -> str:
    """``"09:00"`` for 540."""
    if isinstance(minute, bool) or not isinstance(minute, int) or not 0 <= minute < MINUTES_PER_DAY:
        raise ValueError("a time of day is a whole number of minutes from 0 to 1439")
    return f"{minute // 60:02d}:{minute % 60:02d}"


def parse_at(text: str) -> int:
    """540 for ``"09:00"``: two digits, a colon, two digits, 00:00 to 23:59. ``ValueError`` for anything else."""
    if not isinstance(text, str) or _AT.fullmatch(text) is None:
        raise ValueError("a time of day is written HH:MM, from 00:00 to 23:59")
    return int(text[:2]) * 60 + int(text[3:])


def at_words(minute: int, tz: ZoneInfo) -> str:
    """``"09:00 (America/New_York)"``."""
    return f"{format_at(minute)} ({tz.key})"


def pace_line(now: datetime, tz: ZoneInfo, minute: int) -> str:
    """The line a paced goal's session task carries after its budget."""
    today = local_date(now, tz)
    return f"This goal runs one session a day, from {at_words(minute, tz)}. Today is {today:%Y-%m-%d} there."
