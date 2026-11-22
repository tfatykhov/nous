"""F113 fixed finish checks: the setting, its compose line and its environment reference row."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from nous.cognitive.finish_checks import finish_checks_mode_of
from nous.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def test_the_default_is_off():
    assert Settings(_env_file=None).finish_checks_mode == "off"


def test_shadow_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("NOUS_FINISH_CHECKS_MODE", "shadow")
    assert Settings(_env_file=None).finish_checks_mode == "shadow"


@pytest.mark.parametrize("value", ["on", "bogus", ""])
def test_other_values_are_refused(value):
    with pytest.raises(ValueError):
        Settings(_env_file=None, finish_checks_mode=value)


def test_settings_without_the_field_read_as_off():
    assert finish_checks_mode_of(SimpleNamespace()) == "off"
    assert finish_checks_mode_of(Settings(_env_file=None, finish_checks_mode="shadow")) == "shadow"


def test_compose_passes_the_setting_with_its_default():
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "- NOUS_FINISH_CHECKS_MODE=${NOUS_FINISH_CHECKS_MODE:-off}" in text


def test_the_setting_has_a_row_in_the_environment_reference():
    text = (ROOT / "docs" / "reference" / "environment-variables.md").read_text(encoding="utf-8")
    assert "| `NOUS_FINISH_CHECKS_MODE` | `off` |" in text
