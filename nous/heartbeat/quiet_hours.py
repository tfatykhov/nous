"""Quiet hours: one definition for the heartbeat and for the owner pushes of F099 (spec 4.5.8)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any


def in_quiet_hours(settings: Any, now: datetime | None = None) -> bool:
    """Whether ``now`` (default: the current time) falls in the configured quiet range.

    Hours are compared in UTC, so a deployment must configure ``heartbeat_quiet_start`` and
    ``heartbeat_quiet_end`` in UTC. A range with start == end is never quiet.
    """
    hour = (now or datetime.now(UTC)).astimezone(UTC).hour
    start = settings.heartbeat_quiet_start
    end = settings.heartbeat_quiet_end
    if start <= end:
        # Simple range: e.g. 9-17
        return start <= hour < end
    # Wraps midnight: e.g. 23-8
    return hour >= start or hour < end


def quiet_hours_end(settings: Any, now: datetime | None = None) -> datetime:
    """The instant the quiet hours that ``now`` is in end; ``now`` itself when it is not quiet."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    if not in_quiet_hours(settings, now):
        return now
    candidate = now.replace(hour=settings.heartbeat_quiet_end % 24, minute=0, second=0, microsecond=0)
    return candidate if candidate > now else candidate + timedelta(days=1)
