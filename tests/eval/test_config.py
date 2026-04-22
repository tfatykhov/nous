"""Unit tests for nous_eval.config (F051 Phase 1).

Covers:
- Defaults match spec (port 5433, agent_id="nous-eval-corpus", fixture_version)
- env_prefix=NOUS_EVAL_ via SettingsConfigDict
- db_url property matches main Settings contract
- smoke_mode property when fixtures_dir is None or missing
- fixtures_dir validator: empty/None/"None" string -> None
- warn_if_default_password emits a UserWarning
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

try:
    from nous_eval.config import EvalSettings
except ImportError:
    pytest.skip("nous_eval.config not yet available", allow_module_level=True)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_defaults_match_spec() -> None:
    s = EvalSettings()
    assert s.db_port == 5433
    assert s.db_user == "nous"
    assert s.db_name == "nous_eval"
    assert s.agent_id == "nous-eval-corpus"
    assert s.fixture_version != "latest"  # spec changed default away from "latest"
    assert s.top_k == 10
    assert s.f050_gate_threshold == 0.07
    assert s.f050_gate_max_single_regression == 0.03
    assert s.f050_gate_require_majority_positive is True


def test_env_prefix_is_nous_eval(monkeypatch: pytest.MonkeyPatch) -> None:
    """NOUS_EVAL_DB_PORT=9999 -> EvalSettings.db_port=9999. Confirms env_prefix wiring."""
    monkeypatch.setenv("NOUS_EVAL_DB_PORT", "9999")
    monkeypatch.setenv("NOUS_EVAL_AGENT_ID", "test-agent")
    s = EvalSettings()
    assert s.db_port == 9999
    assert s.agent_id == "test-agent"


# ---------------------------------------------------------------------------
# db_url property
# ---------------------------------------------------------------------------


def test_db_url_format() -> None:
    s = EvalSettings()
    url = s.db_url
    assert url.startswith("postgresql+asyncpg://")
    assert s.db_user in url
    assert s.db_name in url
    assert str(s.db_port) in url


# ---------------------------------------------------------------------------
# smoke_mode property
# ---------------------------------------------------------------------------


def test_smoke_mode_when_fixtures_dir_unset() -> None:
    s = EvalSettings(fixtures_dir=None)
    assert s.smoke_mode is True


def test_smoke_mode_when_fixtures_dir_missing(tmp_path: Path) -> None:
    """A path that doesn't exist on disk -> smoke_mode True."""
    s = EvalSettings(fixtures_dir=tmp_path / "does_not_exist")
    assert s.smoke_mode is True


def test_smoke_mode_false_when_fixtures_dir_exists(tmp_path: Path) -> None:
    s = EvalSettings(fixtures_dir=tmp_path)
    assert s.smoke_mode is False


# ---------------------------------------------------------------------------
# fixtures_dir validator
# ---------------------------------------------------------------------------


def test_fixtures_dir_validator_empty_string_to_none() -> None:
    s = EvalSettings(fixtures_dir="")
    assert s.fixtures_dir is None


def test_fixtures_dir_validator_none_string_to_none() -> None:
    """The literal string "None" (a common env-var quirk) is treated as unset."""
    s = EvalSettings(fixtures_dir="None")
    assert s.fixtures_dir is None


# ---------------------------------------------------------------------------
# warn_if_default_password
# ---------------------------------------------------------------------------


def test_warn_if_default_password_emitted() -> None:
    """Default password 'nous_eval' must trigger a UserWarning when the helper is called."""
    s = EvalSettings()
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        s.warn_if_default_password()
    matches = [x for x in w if issubclass(x.category, UserWarning)]
    assert any("password" in str(m.message).lower() for m in matches)


def test_warn_if_custom_password_silent() -> None:
    s = EvalSettings(db_password="a-stronger-password-2026")
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        s.warn_if_default_password()
    matches = [x for x in w if issubclass(x.category, UserWarning)]
    assert not matches
