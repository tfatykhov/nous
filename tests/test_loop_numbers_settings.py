"""The loop numbers and the status line: both settings default off, compose passes both, and both have a row in the
environment reference."""

from __future__ import annotations

from pathlib import Path

from nous.config import Settings

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = {"NOUS_LOOP_NUMBERS_ENABLED": "false", "NOUS_STATUS_LINE_ENABLED": "false"}


def test_both_settings_default_off():
    settings = Settings(_env_file=None)
    assert settings.loop_numbers_enabled is False
    assert settings.status_line_enabled is False


def test_both_settings_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("NOUS_LOOP_NUMBERS_ENABLED", "true")
    monkeypatch.setenv("NOUS_STATUS_LINE_ENABLED", "true")
    settings = Settings(_env_file=None)
    assert settings.loop_numbers_enabled is True and settings.status_line_enabled is True


def test_compose_passes_both_settings_with_a_real_default():
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for name, default in DEFAULTS.items():
        assert f"- {name}=${{{name}:-{default}}}" in text, name


def test_both_settings_have_a_row_in_the_environment_reference():
    text = (ROOT / "docs" / "reference" / "environment-variables.md").read_text(encoding="utf-8")
    for name, default in DEFAULTS.items():
        assert f"| `{name}` | `{default}` |" in text, name
