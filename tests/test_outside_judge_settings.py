"""F118 outside judge: the three settings, their bounds, the compose lines and the environment reference rows."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from nous.config import Settings

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = {
    "NOUS_OUTSIDE_JUDGE_MODE": "off",
    "NOUS_OUTSIDE_JUDGE_MODEL": "",
    "NOUS_OUTSIDE_JUDGE_DAILY_CALLS": "100",
}


def test_defaults_are_off_with_the_background_model_and_a_cap_of_100():
    settings = Settings(_env_file=None)
    assert settings.outside_judge_mode == "off"
    assert settings.outside_judge_model == ""
    assert settings.outside_judge_daily_calls == 100


def test_the_settings_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("NOUS_OUTSIDE_JUDGE_MODE", "shadow")
    monkeypatch.setenv("NOUS_OUTSIDE_JUDGE_MODEL", "claude-haiku-5-5")
    monkeypatch.setenv("NOUS_OUTSIDE_JUDGE_DAILY_CALLS", "0")
    settings = Settings(_env_file=None)
    assert (settings.outside_judge_mode, settings.outside_judge_model, settings.outside_judge_daily_calls) == (
        "shadow",
        "claude-haiku-5-5",
        0,
    )


@pytest.mark.parametrize("mode", ["on", "enforce", "Shadow", "true"])
def test_a_mode_other_than_off_or_shadow_is_refused(mode):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, outside_judge_mode=mode)


@pytest.mark.parametrize("model", ["gpt-5", "gemini-3", "haiku", " sonnet-claude-"])
def test_a_model_that_is_not_a_claude_model_is_refused(model):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, outside_judge_model=model)


def test_a_claude_model_is_kept_without_surrounding_spaces():
    assert Settings(_env_file=None, outside_judge_model=" claude-opus-5-5 ").outside_judge_model == "claude-opus-5-5"


@pytest.mark.parametrize("calls", [-1, 5001])
def test_the_daily_cap_is_bounded(calls):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, outside_judge_daily_calls=calls)


@pytest.mark.parametrize("calls", [0, 1, 5000])
def test_the_daily_cap_bounds_are_inclusive(calls):
    assert Settings(_env_file=None, outside_judge_daily_calls=calls).outside_judge_daily_calls == calls


def test_empty_values_are_the_defaults(monkeypatch):
    """An empty value (an empty compose passthrough or .env line) is the default, never a failed start."""
    for name in DEFAULTS:
        monkeypatch.setenv(name, "")
    settings = Settings(_env_file=None)
    assert (settings.outside_judge_mode, settings.outside_judge_model, settings.outside_judge_daily_calls) == (
        "off",
        "",
        100,
    )


def test_compose_passes_every_outside_judge_setting_with_its_default():
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for name, default in DEFAULTS.items():
        assert f"- {name}=${{{name}:-{default}}}" in text, name


def _reference_row(name: str, default: str) -> str:
    """How the environment reference starts a setting's row: an empty default is written ``(empty)``."""
    return f"| `{name}` | {f'`{default}`' if default else '(empty)'} |"


def test_every_outside_judge_setting_has_a_row_in_the_environment_reference():
    text = (ROOT / "docs" / "reference" / "environment-variables.md").read_text(encoding="utf-8")
    for name, default in DEFAULTS.items():
        assert _reference_row(name, default) in text, name
