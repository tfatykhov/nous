"""F099 Phase 3d: the calibration flag, its dependency and the two helpers (contract 4.1)."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from nous.brain import continuation
from nous.config import Settings

ROOT = Path(__file__).resolve().parents[1]
ON = {"result_inbox_enabled": True, "intentions_enabled": True, "continuation_enabled": True}


def test_the_flag_defaults_off():
    assert Settings(_env_file=None).intention_calibration_enabled is False


def test_the_flag_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("NOUS_INTENTION_CALIBRATION_ENABLED", "true")
    assert Settings(_env_file=None, **ON).intention_calibration_enabled is True


def test_the_flag_needs_continuation_and_says_so(caplog):
    with caplog.at_level(logging.WARNING):
        settings = Settings(_env_file=None, intention_calibration_enabled=True)  # continuation is off
    assert settings.intention_calibration_enabled is False
    assert "NOUS_INTENTION_CALIBRATION_ENABLED=true needs NOUS_CONTINUATION_ENABLED=true" in caplog.text


def test_calibration_enabled_truth_table():
    assert continuation.calibration_enabled(Settings(_env_file=None, intention_calibration_enabled=True, **ON))
    assert not continuation.calibration_enabled(Settings(_env_file=None, **ON))
    # a settings object that bypassed the validator, and a mock, both count as off
    assert not continuation.calibration_enabled(SimpleNamespace(intention_calibration_enabled=True))
    assert not continuation.calibration_enabled(MagicMock())
    assert not continuation.calibration_enabled(None)


def test_arrival_exclusion_is_empty_with_the_flag_off_and_the_keyword_with_it_on():
    on = Settings(_env_file=None, intention_calibration_enabled=True, **ON)
    assert continuation.arrival_exclusion(on) == {"exclude_intention_arrivals": True}
    assert continuation.arrival_exclusion(Settings(_env_file=None, **ON)) == {}
    assert continuation.arrival_exclusion(None) == {}


def test_compose_passes_the_flag_with_a_real_default():
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "- NOUS_INTENTION_CALIBRATION_ENABLED=${NOUS_INTENTION_CALIBRATION_ENABLED:-false}" in text


def test_the_flag_has_a_row_in_the_environment_reference():
    text = (ROOT / "docs" / "reference" / "environment-variables.md").read_text(encoding="utf-8")
    assert "| `NOUS_INTENTION_CALIBRATION_ENABLED` | `false` |" in text
