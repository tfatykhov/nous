"""F114 goals: the settings, the flag's dependencies, the compose lines and the environment reference rows."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from nous.config import Settings

ROOT = Path(__file__).resolve().parents[1]
ON = {"result_inbox_enabled": True, "intentions_enabled": True, "continuation_enabled": True}
DEFAULTS = {
    "NOUS_GOALS_ENABLED": "false",
    "NOUS_GOALS_TICK_SECONDS": "60",
    "NOUS_GOALS_MAX_ACTIVE_SESSIONS": "1",
    "NOUS_GOALS_RESERVED_SLOTS": "1",
    "NOUS_GOALS_QUEUE_HEADROOM": "2",
    "NOUS_GOALS_PACING_ENABLED": "false",
    "NOUS_GOALS_TIMEZONE": "",
    "NOUS_GOAL_ACTIVITY_ENABLED": "false",
}


def test_defaults_off():
    settings = Settings(_env_file=None)
    assert settings.goals_enabled is False
    assert settings.goals_tick_seconds == 60
    assert settings.goals_max_active_sessions == 1
    assert settings.goals_reserved_slots == 1
    assert settings.goals_queue_headroom == 2


def test_the_flag_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("NOUS_GOALS_ENABLED", "true")
    assert Settings(_env_file=None, **ON).goals_enabled is True


@pytest.mark.parametrize(
    ("off", "named"),
    [
        ({"continuation_enabled": False}, "NOUS_CONTINUATION_ENABLED"),
        ({"result_inbox_enabled": False}, "NOUS_RESULT_INBOX_ENABLED"),
        ({"subtask_enabled": False}, "NOUS_SUBTASK_ENABLED"),
    ],
)
def test_the_flag_needs_its_dependencies_and_says_so(caplog, off, named):
    with caplog.at_level(logging.WARNING):
        settings = Settings(_env_file=None, goals_enabled=True, **{**ON, **off})
    assert settings.goals_enabled is False
    assert "NOUS_GOALS_ENABLED=true needs" in caplog.text
    assert named in caplog.text


def test_the_bounds_of_the_numbers_are_enforced():
    with pytest.raises(ValueError):
        Settings(_env_file=None, goals_tick_seconds=4)
    with pytest.raises(ValueError):
        Settings(_env_file=None, goals_max_active_sessions=0)
    with pytest.raises(ValueError):
        Settings(_env_file=None, goals_reserved_slots=-1)
    with pytest.raises(ValueError):
        Settings(_env_file=None, goals_queue_headroom=5)


def test_compose_passes_every_goals_setting_with_a_real_default():
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for name, default in DEFAULTS.items():
        assert f"- {name}=${{{name}:-{default}}}" in text, name


def _reference_row(name: str, default: str) -> str:
    """How the environment reference starts a setting's row: an empty default is written ``(empty)``."""
    return f"| `{name}` | {f'`{default}`' if default else '(empty)'} |"


def test_every_goals_setting_has_a_row_in_the_environment_reference():
    text = (ROOT / "docs" / "reference" / "environment-variables.md").read_text(encoding="utf-8")
    for name, default in DEFAULTS.items():
        assert _reference_row(name, default) in text, name


# ---- goal pacing: once-a-day goals and the session settings form --------------------------------------------------

GOALS_ON = {**ON, "subtask_enabled": True, "goals_enabled": True}


def test_pacing_is_off_by_default_with_no_zone():
    settings = Settings(_env_file=None)
    assert settings.goals_pacing_enabled is False
    assert settings.goals_timezone == ""


def test_pacing_and_its_zone_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("NOUS_GOALS_PACING_ENABLED", "true")
    monkeypatch.setenv("NOUS_GOALS_TIMEZONE", "America/New_York")
    settings = Settings(_env_file=None, **GOALS_ON)
    assert (settings.goals_pacing_enabled, settings.goals_timezone) == (True, "America/New_York")


def test_pacing_with_a_known_zone_stays_on():
    settings = Settings(_env_file=None, goals_pacing_enabled=True, goals_timezone="Asia/Kolkata", **GOALS_ON)
    assert settings.goals_pacing_enabled is True


@pytest.mark.parametrize(
    ("values", "named"),
    [
        ({"goals_timezone": "America/New_York", **ON}, "NOUS_GOALS_ENABLED=true"),
        ({"goals_timezone": "", **GOALS_ON}, "NOUS_GOALS_TIMEZONE (a time zone such as America/New_York)"),
        ({"goals_timezone": "Mars/Olympus", **GOALS_ON}, "NOUS_GOALS_TIMEZONE (a time zone such as America/New_York)"),
        ({"goals_timezone": "../etc/passwd", **GOALS_ON}, "NOUS_GOALS_TIMEZONE (a time zone such as America/New_York)"),
        ({"goals_timezone": "America", **GOALS_ON}, "NOUS_GOALS_TIMEZONE (a time zone such as America/New_York)"),
    ],
)
def test_pacing_needs_goals_and_a_known_zone_and_says_so(caplog, values, named):
    with caplog.at_level(logging.WARNING):
        settings = Settings(_env_file=None, goals_pacing_enabled=True, **values)
    assert settings.goals_pacing_enabled is False
    assert "NOUS_GOALS_PACING_ENABLED=true needs" in caplog.text
    assert named in caplog.text


def test_the_zone_is_never_read_while_pacing_is_off(caplog):
    with caplog.at_level(logging.WARNING):
        settings = Settings(_env_file=None, goals_timezone="Mars/Olympus", **GOALS_ON)
    assert settings.goals_pacing_enabled is False
    assert "NOUS_GOALS_TIMEZONE" not in caplog.text


def test_a_zone_name_is_at_most_64_characters():
    with pytest.raises(ValueError):
        Settings(_env_file=None, goals_timezone="A" * 65)


# ---- what a goal is doing now, and the folding Goals page ---------------------------------------------------------


def test_the_activity_panel_is_off_by_default():
    assert Settings(_env_file=None).goal_activity_enabled is False


def test_the_activity_panel_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("NOUS_GOAL_ACTIVITY_ENABLED", "true")
    assert Settings(_env_file=None).goal_activity_enabled is True
