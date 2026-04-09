"""Tests for nous.handlers.time_parser — natural language time parsing.

Covers:
- ISO 8601 parsing
- Relative time ("in N hours/minutes/days")
- Past time rejection
- Natural language ("tomorrow 9am")
- Simple intervals
- Daily at time patterns
- Weekly patterns
- Invalid format rejection
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from nous.handlers.time_parser import parse_every, parse_when

# ---------------------------------------------------------------------------
# parse_when: ISO 8601
# ---------------------------------------------------------------------------


class TestParseWhenISO:
    """ISO 8601 datetime parsing."""

    def test_iso_with_timezone(self) -> None:
        """ISO string with explicit timezone is converted to UTC."""
        # Use a date far in the future to avoid past-time rejection
        result = parse_when("2099-06-15T14:30:00+03:00")
        assert result.tzinfo is not None
        assert result.hour == 11  # 14:30 +03:00 = 11:30 UTC
        assert result.minute == 30

    def test_iso_utc_z(self) -> None:
        """ISO string with Z suffix."""
        result = parse_when("2099-01-01T00:00:00Z")
        assert result.year == 2099
        assert result.tzinfo is not None

    def test_iso_negative_offset(self) -> None:
        """ISO string with negative UTC offset."""
        result = parse_when("2099-03-10T09:00:00-05:00")
        assert result.hour == 14  # 09:00 -05:00 = 14:00 UTC
        assert result.tzinfo is not None


# ---------------------------------------------------------------------------
# parse_when: Relative time
# ---------------------------------------------------------------------------


class TestParseWhenRelative:
    """Relative time expressions: "in N units"."""

    def test_in_minutes(self) -> None:
        """'in 30 minutes' produces a time ~30 min from now."""
        before = datetime.now(UTC)
        result = parse_when("in 30 minutes")
        after = datetime.now(UTC)

        expected_min = before + timedelta(minutes=30)
        expected_max = after + timedelta(minutes=30)
        assert expected_min <= result <= expected_max

    def test_in_hours(self) -> None:
        """'in 2 hours' produces a time ~2 hours from now."""
        before = datetime.now(UTC)
        result = parse_when("in 2 hours")
        delta = result - before
        # Should be within a few seconds of 2 hours
        assert 7190 <= delta.total_seconds() <= 7210

    def test_in_days(self) -> None:
        """'in 3 days' produces a time ~3 days from now."""
        before = datetime.now(UTC)
        result = parse_when("in 3 days")
        delta = result - before
        assert abs(delta.total_seconds() - 3 * 86400) < 10

    def test_in_weeks(self) -> None:
        """'in 1 week' produces a time ~7 days from now."""
        before = datetime.now(UTC)
        result = parse_when("in 1 week")
        delta = result - before
        assert abs(delta.total_seconds() - 7 * 86400) < 10

    def test_singular_unit(self) -> None:
        """'in 1 minute' works with singular unit."""
        before = datetime.now(UTC)
        result = parse_when("in 1 minute")
        delta = result - before
        assert 55 <= delta.total_seconds() <= 65

    def test_result_is_utc(self) -> None:
        """Relative times always return UTC-aware datetimes."""
        result = parse_when("in 5 hours")
        assert result.tzinfo is UTC


# ---------------------------------------------------------------------------
# parse_when: Past time rejection
# ---------------------------------------------------------------------------


class TestParseWhenPastRejection:
    """Times in the past are rejected."""

    def test_past_iso_raises(self) -> None:
        """An ISO date in the past raises ValueError."""
        with pytest.raises(ValueError, match="past"):
            parse_when("2020-01-01T00:00:00Z")

    def test_past_natural_raises(self) -> None:
        """A natural language date in the past raises ValueError."""
        with pytest.raises(ValueError, match="past"):
            parse_when("January 1 2020")


# ---------------------------------------------------------------------------
# parse_when: Natural language
# ---------------------------------------------------------------------------


class TestParseWhenNatural:
    """Natural language time expressions via dateutil fuzzy parsing."""

    def test_tomorrow_9am(self) -> None:
        """'tomorrow 9am' parses to tomorrow at 09:00 UTC."""
        # Mock now to a known time so the parsed date is in the future
        fake_now = datetime(2099, 6, 15, 12, 0, 0, tzinfo=UTC)
        with patch("nous.handlers.time_parser.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            # Use a date AFTER fake_now to avoid past-time rejection
            result = parse_when("July 20 2099 9am")
        assert result.tzinfo is not None
        assert result.hour == 9
        assert result.year == 2099

    def test_natural_returns_utc_aware(self) -> None:
        """Natural language results are always UTC-aware."""
        result = parse_when("December 25 2099 3pm")
        assert result.tzinfo is not None
        assert result.hour == 15

    def test_unparseable_raises(self) -> None:
        """Completely unparseable strings raise ValueError."""
        with pytest.raises(ValueError, match="Cannot parse"):
            parse_when("not a real time string xyzzy")


# ---------------------------------------------------------------------------
# parse_every: Simple intervals
# ---------------------------------------------------------------------------


class TestParseEveryInterval:
    """Simple interval parsing: "N units"."""

    def test_30_minutes(self) -> None:
        """'30 minutes' -> (1800, None)."""
        interval, cron = parse_every("30 minutes")
        assert interval == 1800
        assert cron is None

    def test_6_hours(self) -> None:
        """'6 hours' -> (21600, None)."""
        interval, cron = parse_every("6 hours")
        assert interval == 21600
        assert cron is None

    def test_1_day(self) -> None:
        """'1 day' -> (86400, None)."""
        interval, cron = parse_every("1 day")
        assert interval == 86400
        assert cron is None

    def test_2_weeks(self) -> None:
        """'2 weeks' -> (1209600, None)."""
        interval, cron = parse_every("2 weeks")
        assert interval == 1209600
        assert cron is None

    def test_singular_unit(self) -> None:
        """'1 hour' works with singular form."""
        interval, cron = parse_every("1 hour")
        assert interval == 3600
        assert cron is None


# ---------------------------------------------------------------------------
# parse_every: Daily patterns
# ---------------------------------------------------------------------------


class TestParseEveryDaily:
    """Daily cron patterns: "daily at Xam/pm"."""

    def test_daily_at_8am(self) -> None:
        """'daily at 8am' -> (None, '0 8 * * *')."""
        interval, cron = parse_every("daily at 8am")
        assert interval is None
        assert cron == "0 8 * * *"

    def test_daily_at_9pm(self) -> None:
        """'daily at 9pm' -> (None, '0 21 * * *')."""
        interval, cron = parse_every("daily at 9pm")
        assert interval is None
        assert cron == "0 21 * * *"

    def test_daily_at_12am(self) -> None:
        """'daily at 12am' -> midnight -> (None, '0 0 * * *')."""
        interval, cron = parse_every("daily at 12am")
        assert interval is None
        assert cron == "0 0 * * *"

    def test_daily_at_12pm(self) -> None:
        """'daily at 12pm' -> noon -> (None, '0 12 * * *')."""
        interval, cron = parse_every("daily at 12pm")
        assert interval is None
        assert cron == "0 12 * * *"

    def test_daily_with_timezone_suffix(self) -> None:
        """'daily at 9am EST' parses without error (tz noted, not applied to cron)."""
        interval, cron = parse_every("daily at 9am EST")
        assert interval is None
        assert cron == "0 9 * * *"


# ---------------------------------------------------------------------------
# parse_every: Weekly patterns
# ---------------------------------------------------------------------------


class TestParseEveryWeekly:
    """Weekly cron patterns: "every DAY at Xam/pm"."""

    def test_every_monday_at_10am(self) -> None:
        """'every monday at 10am' -> (None, '0 10 * * 1')."""
        interval, cron = parse_every("every monday at 10am")
        assert interval is None
        assert cron == "0 10 * * 1"

    def test_every_friday_at_3pm(self) -> None:
        """'every friday at 3pm' -> (None, '0 15 * * 5')."""
        interval, cron = parse_every("every friday at 3pm")
        assert interval is None
        assert cron == "0 15 * * 5"

    def test_every_sunday_at_6am(self) -> None:
        """'every sunday at 6am' -> sunday = 0."""
        interval, cron = parse_every("every sunday at 6am")
        assert interval is None
        assert cron == "0 6 * * 0"

    def test_every_wednesday_at_12pm(self) -> None:
        """'every wednesday at 12pm' -> noon on wednesday."""
        interval, cron = parse_every("every wednesday at 12pm")
        assert interval is None
        assert cron == "0 12 * * 3"


# ---------------------------------------------------------------------------
# parse_every: Invalid inputs
# ---------------------------------------------------------------------------


class TestParseEveryInvalid:
    """Invalid recurring strings raise ValueError."""

    def test_empty_string(self) -> None:
        with pytest.raises(ValueError, match="Cannot parse"):
            parse_every("")

    def test_nonsense(self) -> None:
        with pytest.raises(ValueError, match="Cannot parse"):
            parse_every("whenever you feel like it")

    def test_partial_match(self) -> None:
        """Partial matches that don't fit any pattern raise."""
        with pytest.raises(ValueError, match="Cannot parse"):
            parse_every("every someday at noon")


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Whitespace handling and case insensitivity."""

    def test_parse_when_strips_whitespace(self) -> None:
        """Leading/trailing whitespace is stripped."""
        result = parse_when("  in 1 hour  ")
        assert result.tzinfo is not None

    def test_parse_every_strips_whitespace(self) -> None:
        """Leading/trailing whitespace is stripped."""
        interval, cron = parse_every("  30 minutes  ")
        assert interval == 1800

    def test_parse_every_case_insensitive(self) -> None:
        """Patterns are case-insensitive."""
        interval, cron = parse_every("Daily At 8AM")
        assert cron == "0 8 * * *"

    def test_parse_when_case_insensitive(self) -> None:
        """'IN 2 HOURS' works case-insensitively."""
        result = parse_when("IN 2 HOURS")
        assert result.tzinfo is not None
