"""F114 goal pacing: the day and the time of a goal that works once a day, as pure functions of instants.

Every case is a fixed instant, the two New York days on which the clock changes included; nothing reads a clock.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from nous.goals import pacing

NY = ZoneInfo("America/New_York")
KOLKATA = ZoneInfo("Asia/Kolkata")


def _utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


# ---- the day ------------------------------------------------------------------------------------------------------


def test_the_zone_files_are_there():
    assert ZoneInfo("America/New_York").key == "America/New_York"


def test_day_start_on_a_normal_new_york_day():
    assert pacing.day_start(_utc(2026, 11, 18, 15, 0), NY) == _utc(2026, 11, 18, 5, 0)
    # 00:40Z on the 18th is still the evening of the 17th in New York
    assert pacing.day_start(_utc(2026, 11, 18, 0, 40), NY) == _utc(2026, 11, 17, 5, 0)


def test_the_day_the_clock_moves_forward_is_23_hours():
    start = pacing.day_start(_utc(2026, 3, 8, 12, 0), NY)
    end = pacing.day_start(_utc(2026, 3, 9, 12, 0), NY)
    assert (start, end) == (_utc(2026, 3, 8, 5, 0), _utc(2026, 3, 9, 4, 0))
    assert end - start == timedelta(hours=23)
    assert pacing.day_start(_utc(2026, 3, 9, 3, 59), NY) == start  # 23:59 local, still the 8th


def test_the_day_the_clock_moves_back_is_25_hours():
    start = pacing.day_start(_utc(2026, 11, 1, 12, 0), NY)
    end = pacing.day_start(_utc(2026, 11, 2, 12, 0), NY)
    assert (start, end) == (_utc(2026, 11, 1, 4, 0), _utc(2026, 11, 2, 5, 0))
    assert end - start == timedelta(hours=25)
    assert pacing.day_start(_utc(2026, 11, 2, 4, 59), NY) == start  # 23:59 local, still the 1st


def test_day_start_in_other_zones():
    assert pacing.day_start(_utc(2026, 11, 18, 20, 0), KOLKATA) == _utc(2026, 11, 18, 18, 30)
    assert pacing.day_start(_utc(2026, 11, 18, 17, 0), KOLKATA) == _utc(2026, 11, 17, 18, 30)
    assert pacing.day_start(_utc(2026, 11, 18, 23, 59), ZoneInfo("UTC")) == _utc(2026, 11, 18, 0, 0)


# ---- the time of day ----------------------------------------------------------------------------------------------


def test_slot_on_a_normal_day():
    assert pacing.slot(date(2026, 11, 18), 9 * 60, NY) == _utc(2026, 11, 18, 14, 0)
    assert pacing.slot(date(2026, 7, 1), 9 * 60, NY) == _utc(2026, 7, 1, 13, 0)
    assert pacing.slot(date(2026, 11, 18), 0, KOLKATA) == _utc(2026, 11, 17, 18, 30)
    assert pacing.slot(date(2026, 11, 18), 1439, ZoneInfo("UTC")) == _utc(2026, 11, 18, 23, 59)


def test_a_wall_time_the_clock_skips_is_one_hour_later_on_the_wall():
    assert pacing.slot(date(2026, 3, 8), 150, NY) == _utc(2026, 3, 8, 7, 30)  # 02:30 does not exist; 03:30 EDT


def test_a_wall_time_that_occurs_twice_is_the_first():
    assert pacing.slot(date(2026, 11, 1), 90, NY) == _utc(2026, 11, 1, 5, 30)  # 01:30 EDT, not 01:30 EST


# ---- the hold -----------------------------------------------------------------------------------------------------


def test_hold_before_and_after_the_time():
    nine = 9 * 60
    assert pacing.hold(_utc(2026, 11, 18, 13, 59), NY, nine, 0) == "paced"
    assert pacing.hold(_utc(2026, 11, 18, 14, 0), NY, nine, 0) is None  # at the time
    assert pacing.hold(_utc(2026, 11, 18, 23, 0), NY, nine, 0) is None


def test_hold_with_one_session_started_today():
    nine = 9 * 60
    assert pacing.hold(_utc(2026, 11, 18, 13, 0), NY, nine, 1) == "day_sessions"
    assert pacing.hold(_utc(2026, 11, 18, 15, 0), NY, nine, 1) == "day_sessions"
    assert pacing.hold(_utc(2026, 11, 18, 15, 0), NY, nine, 2) == "day_sessions"


def test_a_late_evening_session_and_a_start_after_utc_midnight_are_the_same_new_york_day():
    """A session at 23:37Z on the 17th (18:37 in New York) and a start at 00:40Z on the 18th (19:40 there): one local
    day, so the start is held although the UTC day changed."""
    started = _utc(2026, 11, 17, 23, 37)
    now = _utc(2026, 11, 18, 0, 40)
    since = pacing.day_start(now, NY)
    assert since <= started < now
    assert pacing.hold(now, NY, 9 * 60, 1) == "day_sessions"


# ---- the wake -----------------------------------------------------------------------------------------------------


def test_wake_after_a_paced_hold_is_todays_time():
    assert pacing.wake_after_hold("paced", _utc(2026, 11, 18, 12, 0), NY, 9 * 60) == _utc(2026, 11, 18, 14, 0)


def test_wake_after_a_day_sessions_hold_is_tomorrows_time():
    assert pacing.wake_after_hold("day_sessions", _utc(2026, 11, 18, 15, 0), NY, 9 * 60) == _utc(2026, 11, 19, 14, 0)
    # 00:40Z on the 18th is the 17th in New York: tomorrow there is the 18th
    assert pacing.wake_after_hold("day_sessions", _utc(2026, 11, 18, 0, 40), NY, 9 * 60) == _utc(2026, 11, 18, 14, 0)


def test_wake_after_hold_on_the_days_the_clock_changes():
    nine = 9 * 60
    # the day before each change, held for the day: the next is 09:00 local on the change day
    assert pacing.wake_after_hold("day_sessions", _utc(2026, 3, 7, 18, 0), NY, nine) == _utc(2026, 3, 8, 13, 0)
    assert pacing.wake_after_hold("day_sessions", _utc(2026, 10, 31, 18, 0), NY, nine) == _utc(2026, 11, 1, 14, 0)
    # on the change day itself, before the time
    assert pacing.wake_after_hold("paced", _utc(2026, 3, 8, 6, 0), NY, nine) == _utc(2026, 3, 8, 13, 0)
    assert pacing.wake_after_hold("paced", _utc(2026, 11, 1, 6, 0), NY, nine) == _utc(2026, 11, 1, 14, 0)


def test_wake_after_hold_refuses_another_reason():
    with pytest.raises(ValueError):
        pacing.wake_after_hold("quiet", _utc(2026, 11, 18, 12, 0), NY, 540)


def test_wake_after_close_is_the_time_of_the_day_after_the_session_started():
    # started 23:50 local on the 17th (04:50Z on the 18th), closed after midnight: the next is the 18th at 09:00
    assert pacing.wake_after_close(_utc(2026, 11, 18, 4, 50), NY, 9 * 60) == _utc(2026, 11, 18, 14, 0)
    assert pacing.wake_after_close(_utc(2026, 11, 18, 14, 0), NY, 9 * 60) == _utc(2026, 11, 19, 14, 0)
    assert pacing.wake_after_close(_utc(2026, 11, 18, 14, 0), KOLKATA, 9 * 60) == _utc(2026, 11, 19, 3, 30)


# ---- the time as the owner writes it ------------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "minute"), [("00:00", 0), ("09:00", 540), ("13:05", 785), ("23:59", 1439)])
def test_parse_and_format_round_trip(text, minute):
    assert pacing.parse_at(text) == minute
    assert pacing.format_at(minute) == text


@pytest.mark.parametrize(
    "text", ["24:00", "9:00", "09:60", "", " 09:00", "09:00 ", "09:00\n", "0900", "09-00", "٠٩:٠٠", "0٩:00"]
)
def test_parse_refuses_anything_else(text):
    with pytest.raises(ValueError):
        pacing.parse_at(text)


@pytest.mark.parametrize("minute", [-1, 1440, True])
def test_format_refuses_a_minute_out_of_the_day(minute):
    with pytest.raises(ValueError):
        pacing.format_at(minute)


def test_the_words_of_a_time_and_its_zone():
    assert pacing.at_words(540, NY) == "09:00 (America/New_York)"


def test_the_pace_line_names_the_time_the_zone_and_the_local_date():
    assert pacing.pace_line(_utc(2026, 11, 18, 0, 40), NY, 540) == (
        "This goal runs one session a day, from 09:00 (America/New_York). Today is 2026-11-17 there."
    )


# ---- the switch ---------------------------------------------------------------------------------------------------


def test_zone_is_none_unless_the_flag_is_on():
    assert pacing.zone(SimpleNamespace()) is None
    assert pacing.zone(SimpleNamespace(goals_pacing_enabled=False, goals_timezone="America/New_York")) is None
    assert pacing.zone(SimpleNamespace(goals_pacing_enabled="true", goals_timezone="America/New_York")) is None
    assert pacing.zone(SimpleNamespace(goals_pacing_enabled=True, goals_timezone="America/New_York")) == NY


@pytest.mark.parametrize("name", ["", "Mars/Olympus", "../etc/passwd", "/etc/localtime", "America", "Etc"])
def test_zone_is_none_for_a_name_that_is_not_a_zone(name):
    assert pacing.zone(SimpleNamespace(goals_pacing_enabled=True, goals_timezone=name)) is None


# ---- the owed day -------------------------------------------------------------------------------------------------
# A day whose time passed while the goal was active, and on which no session started, is owed: the next local day may
# start one session before its own time, and that session is the new day's.

EIGHT_PM = 20 * 60


def test_previous_day_start_on_a_normal_day_and_on_both_clock_changes():
    assert pacing.previous_day_start(_utc(2026, 11, 18, 15, 0), NY) == _utc(2026, 11, 17, 5, 0)
    # 00:30 EDT on 2026-03-09: the previous day (2026-03-08, 23 hours) began at 05:00Z
    assert pacing.previous_day_start(_utc(2026, 3, 9, 4, 30), NY) == _utc(2026, 3, 8, 5, 0)
    # 00:30 EST on 2026-11-02: the previous day (2026-11-01, 25 hours) began at 04:00Z
    assert pacing.previous_day_start(_utc(2026, 11, 2, 5, 30), NY) == _utc(2026, 11, 1, 4, 0)


def test_the_previous_days_time_owed_when_active_through_it_and_no_session_since_its_midnight():
    now = _utc(2026, 11, 18, 5, 30)  # 00:30 on the 18th in New York; the 17th's 20:00 was 01:00Z on the 18th
    yesterdays_time = _utc(2026, 11, 18, 1, 0)
    owed = lambda **kw: pacing.owed(now, NY, EIGHT_PM, **{"started_since_previous_day": 0, **kw})  # noqa: E731
    assert owed(active_since=yesterdays_time - timedelta(days=3)) is True
    assert owed(active_since=yesterdays_time) is True  # active at that very instant
    assert owed(active_since=yesterdays_time + timedelta(seconds=1)) is False  # approved or resumed after it
    assert owed(active_since=now) is False  # approved for the first time today
    assert owed(active_since=None) is False  # never approved
    assert owed(active_since=yesterdays_time - timedelta(days=3), started_since_previous_day=1) is False


def test_the_owed_day_is_the_previous_local_days_on_both_clock_changes():
    # 00:30 EDT on 2026-03-09: the 8th (23 hours) had its 20:00 EDT at 00:00Z on the 9th
    now = _utc(2026, 3, 9, 4, 30)
    assert pacing.owed(now, NY, EIGHT_PM, started_since_previous_day=0, active_since=_utc(2026, 3, 9, 0, 0)) is True
    assert pacing.owed(now, NY, EIGHT_PM, started_since_previous_day=0, active_since=_utc(2026, 3, 9, 0, 1)) is False
    # 00:30 EST on 2026-11-02: the 1st (25 hours) had its 20:00 EST at 01:00Z on the 2nd
    now = _utc(2026, 11, 2, 5, 30)
    assert pacing.owed(now, NY, EIGHT_PM, started_since_previous_day=0, active_since=_utc(2026, 11, 2, 1, 0)) is True
    assert pacing.owed(now, NY, EIGHT_PM, started_since_previous_day=0, active_since=_utc(2026, 11, 2, 1, 1)) is False


def test_an_owed_day_starts_before_todays_time_and_is_todays_session():
    before = _utc(2026, 11, 18, 5, 30)  # 00:30 local, the time is 20:00
    assert pacing.hold(before, NY, EIGHT_PM, 0) == "paced"
    assert pacing.hold(before, NY, EIGHT_PM, 0, owed=True) is None
    # once it started it is the day's one session: today's time starts nothing more
    assert pacing.hold(_utc(2026, 11, 19, 1, 0), NY, EIGHT_PM, 1, owed=True) == "day_sessions"
    # and its close wakes the goal at the following day's time (the 19th's 20:00), not this day's
    assert pacing.wake_after_close(before, NY, EIGHT_PM) == _utc(2026, 11, 20, 1, 0)


def test_an_owed_day_is_one_day_never_a_backlog():
    """After an owed session, the next day owes nothing: a session started on the day before it."""
    owed_session_day = _utc(2026, 11, 18, 9, 0)  # 04:00 on the 18th, when quiet hours ended
    next_day = _utc(2026, 11, 19, 5, 30)  # 00:30 on the 19th
    since = pacing.previous_day_start(next_day, NY)
    started_since = 1 if owed_session_day >= since else 0
    assert started_since == 1
    active = _utc(2026, 11, 1)
    assert pacing.owed(next_day, NY, EIGHT_PM, started_since_previous_day=started_since, active_since=active) is False
