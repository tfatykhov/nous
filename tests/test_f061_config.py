"""F061 PR-1: tests for the new NOUS_SUBTASK_* settings.

Convention check: F061 settings use plain Field defaults with NO
validation_alias. The env_prefix="NOUS_" on Settings reads NOUS_SUBTASK_*
into them automatically. F046's PR #318 lesson is that validation_alias is
redundant under env_prefix and breaks `Settings(field=value)` construction.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nous.config import Settings


def _clear_f061_env(monkeypatch) -> None:
    """Strip any ambient NOUS_SUBTASK_* vars so defaults aren't shadowed."""
    for var in (
        "NOUS_SUBTASK_HARDENING_ENABLED",
        "NOUS_SUBTASK_MAX_ATTEMPTS",
        "NOUS_SUBTASK_REPORT_MIN_SUMMARY_CHARS",
        "NOUS_SUBTASK_BOOTSTRAP_TIMEOUT",
        "NOUS_SUBTASK_WORK_TIMEOUT",
        "NOUS_SUBTASK_OUTCOME_PERSISTENCE_ENABLED",
        "NOUS_SUBTASK_FORCE_TOOL_ON_PENULTIMATE",
    ):
        monkeypatch.delenv(var, raising=False)


class TestF061SettingsDefaults:
    """Defaults match the spec — flag off, 1 retry total of 2 attempts."""

    def test_defaults(self, monkeypatch):
        # Hermetic against BOTH process env AND repo .env. pydantic-settings
        # reads from .env via env_file= config; monkeypatch.delenv only clears
        # os.environ. Pass _env_file=None to short-circuit .env loading too.
        _clear_f061_env(monkeypatch)
        s = Settings(_env_file=None)
        assert s.subtask_hardening_enabled is False
        assert s.subtask_max_attempts == 2
        assert s.subtask_report_min_summary_chars == 50
        assert s.subtask_bootstrap_timeout == 30
        assert s.subtask_work_timeout == 570
        assert s.subtask_outcome_persistence_enabled is True
        assert s.subtask_force_tool_on_penultimate is True


class TestF061SettingsEnvOverride:
    """env_prefix='NOUS_' picks up plain field names."""

    def test_hardening_flag_via_env(self, monkeypatch):
        _clear_f061_env(monkeypatch)
        monkeypatch.setenv("NOUS_SUBTASK_HARDENING_ENABLED", "true")
        assert Settings().subtask_hardening_enabled is True

    def test_max_attempts_via_env(self, monkeypatch):
        _clear_f061_env(monkeypatch)
        monkeypatch.setenv("NOUS_SUBTASK_MAX_ATTEMPTS", "3")
        assert Settings().subtask_max_attempts == 3

    def test_min_summary_chars_via_env(self, monkeypatch):
        _clear_f061_env(monkeypatch)
        monkeypatch.setenv("NOUS_SUBTASK_REPORT_MIN_SUMMARY_CHARS", "100")
        assert Settings().subtask_report_min_summary_chars == 100


class TestF061SettingsValidation:
    """Range constraints from Field(ge=..., le=...)."""

    def test_max_attempts_below_min_rejects(self, monkeypatch):
        _clear_f061_env(monkeypatch)
        monkeypatch.setenv("NOUS_SUBTASK_MAX_ATTEMPTS", "0")
        with pytest.raises(ValidationError):
            Settings()

    def test_max_attempts_above_max_rejects(self, monkeypatch):
        _clear_f061_env(monkeypatch)
        monkeypatch.setenv("NOUS_SUBTASK_MAX_ATTEMPTS", "4")
        with pytest.raises(ValidationError):
            Settings()

    def test_min_summary_chars_zero_rejects(self, monkeypatch):
        _clear_f061_env(monkeypatch)
        monkeypatch.setenv("NOUS_SUBTASK_REPORT_MIN_SUMMARY_CHARS", "0")
        with pytest.raises(ValidationError):
            Settings()

    def test_bootstrap_timeout_zero_rejects(self, monkeypatch):
        _clear_f061_env(monkeypatch)
        monkeypatch.setenv("NOUS_SUBTASK_BOOTSTRAP_TIMEOUT", "0")
        with pytest.raises(ValidationError):
            Settings()


class TestF061SettingsConstructorOverride:
    """Settings(field=value) construction must work — F046 PR #318 regression guard.

    validation_alias breaks `Settings(subtask_max_attempts=3)` because the
    field then has no name pydantic will accept; only the alias works. F061
    uses plain field names, so this MUST work.
    """

    def test_constructor_accepts_plain_field_names(self, monkeypatch):
        _clear_f061_env(monkeypatch)
        s = Settings(
            subtask_hardening_enabled=True,
            subtask_max_attempts=3,
            subtask_report_min_summary_chars=80,
        )
        assert s.subtask_hardening_enabled is True
        assert s.subtask_max_attempts == 3
        assert s.subtask_report_min_summary_chars == 80
