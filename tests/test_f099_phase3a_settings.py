"""F099 Phase 3a: NOUS_INTENTION_CARDS_ENABLED, its validator, the helper, the compose line and the doc row."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

from nous.brain import continuation
from nous.config import Settings

BASE = {"result_inbox_enabled": True, "intentions_enabled": True, "continuation_enabled": True}
ROOT = Path(__file__).resolve().parents[1]


def test_the_default_is_off():
    s = Settings(_env_file=None, **BASE)
    assert s.intention_cards_enabled is False
    assert continuation.cards_enabled(s) is False


def test_cards_need_continuation_and_the_companion(caplog):
    for over in ({"continuation_enabled": False}, {"a2ui_enabled": False}):
        with caplog.at_level(logging.WARNING, logger="nous.config"):
            s = Settings(_env_file=None, **{**BASE, "intention_cards_enabled": True, **over})
        assert s.intention_cards_enabled is False, over
        assert "NOUS_INTENTION_CARDS_ENABLED=true needs" in caplog.text
        caplog.clear()


def test_cards_stay_on_with_both_prerequisites():
    s = Settings(_env_file=None, **BASE, intention_cards_enabled=True)
    assert s.intention_cards_enabled is True
    assert continuation.cards_enabled(s) is True


def test_the_helper_rereads_the_prerequisites():
    """A settings object that bypassed the validator (``model_copy`` skips validators, as the main.py flag gate
    does when it forces ``continuation_enabled`` off) still counts as off."""
    on = Settings(_env_file=None, **BASE, intention_cards_enabled=True)
    assert continuation.cards_enabled(on.model_copy(update={"a2ui_enabled": False})) is False
    assert continuation.cards_enabled(on.model_copy(update={"continuation_enabled": False})) is False
    assert continuation.cards_enabled(SimpleNamespace(intention_cards_enabled=True)) is False  # a mock is off


def test_the_environment_name_is_the_contract_name(monkeypatch):
    monkeypatch.setenv("NOUS_INTENTION_CARDS_ENABLED", "true")
    assert Settings(_env_file=None, **BASE).intention_cards_enabled is True


def test_compose_passes_the_flag_with_the_settings_default(monkeypatch):
    monkeypatch.delenv("NOUS_INTENTION_CARDS_ENABLED", raising=False)  # the default is read, not the developer's shell
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    line = "- NOUS_INTENTION_CARDS_ENABLED=${NOUS_INTENTION_CARDS_ENABLED:-false}"
    assert line in compose
    assert compose.index("NOUS_INTENTION_PROPOSAL_TTL_HOURS") < compose.index(line)  # with the F099 lines
    assert Settings(_env_file=None).intention_cards_enabled is False


def test_the_environment_variables_doc_has_a_row():
    doc = (ROOT / "docs" / "reference" / "environment-variables.md").read_text(encoding="utf-8")
    assert "| `NOUS_INTENTION_CARDS_ENABLED` | `false` |" in doc
