"""Profile card settings: defaults (both flags off), environment names, bounds, compose lines and doc rows."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from nous.config import Settings

ROOT = Path(__file__).resolve().parents[1]

DEFAULTS = {
    "profile_card_writer_enabled": False,
    "profile_card_enabled": False,
    "profile_card_max_tokens": 400,
    "profile_card_max_entries": 25,
    "profile_card_source_limit": 80,
    "profile_card_episodes": 5,
}
DOC_DEFAULTS = {"False": "false", "True": "true"}


def test_the_defaults():
    s = Settings(_env_file=None)
    assert {k: getattr(s, k) for k in DEFAULTS} == DEFAULTS


def test_the_environment_names(monkeypatch):
    monkeypatch.setenv("NOUS_PROFILE_CARD_WRITER_ENABLED", "true")
    monkeypatch.setenv("NOUS_PROFILE_CARD_MAX_ENTRIES", "10")
    monkeypatch.setenv("NOUS_PROFILE_CARD_EPISODES", "0")
    s = Settings(_env_file=None)
    assert (s.profile_card_writer_enabled, s.profile_card_max_entries, s.profile_card_episodes) == (True, 10, 0)


@pytest.mark.parametrize(
    "field",
    ["profile_card_max_tokens", "profile_card_max_entries", "profile_card_source_limit"],
)
def test_a_bound_below_one_is_rejected(field):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: 0})


def test_a_negative_episode_count_is_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, profile_card_episodes=-1)


def test_compose_and_the_docs_carry_every_setting():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    doc = (ROOT / "docs" / "reference" / "environment-variables.md").read_text(encoding="utf-8")
    for field, default in DEFAULTS.items():
        name = f"NOUS_{field.upper()}"
        value = DOC_DEFAULTS.get(str(default), str(default))
        assert f"- {name}=${{{name}:-{value}}}" in compose, name
        assert f"| `{name}` | `{value}` |" in doc, name
    rest = (ROOT / "docs" / "reference" / "rest-api.md").read_text(encoding="utf-8")
    for route in (
        "| GET | `/profile-card` |",
        "| DELETE | `/profile-card/entries/{entry_id}` |",
        "| POST | `/profile-card/rebuild` |",
    ):
        assert route in rest, route
    assert "profile_card.py" in (ROOT / "docs" / "reference" / "project-structure.md").read_text(encoding="utf-8")
