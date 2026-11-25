"""Entity profile settings (F119 PR 1): defaults (the writer on), environment names, bounds, the owner keys list,
compose lines and doc rows."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from nous.config import Settings
from nous.heart.entity_profile import owner_keys

ROOT = Path(__file__).resolve().parents[1]

DEFAULTS = {
    "entity_profile_writer_enabled": True,
    "entity_profile_min_facts": 10,
    "entity_profile_max_profiles": 150,
    "entity_profile_max_calls_per_day": 40,
    "entity_profile_max_lines": 8,
    "entity_profile_max_tokens": 300,
    "entity_profile_source_limit": 40,
    "entity_profile_owner_keys": [],
}
DOC_DEFAULTS = {"False": "false", "True": "true", "[]": ""}


def test_the_defaults():
    s = Settings(_env_file=None)
    assert {k: getattr(s, k) for k in DEFAULTS} == DEFAULTS


def test_the_writer_is_on_by_default_and_false_switches_it_off(monkeypatch):
    assert Settings.model_fields["entity_profile_writer_enabled"].default is True
    monkeypatch.setenv("NOUS_ENTITY_PROFILE_WRITER_ENABLED", "false")
    assert Settings(_env_file=None).entity_profile_writer_enabled is False


def test_the_environment_names(monkeypatch):
    monkeypatch.setenv("NOUS_ENTITY_PROFILE_MAX_CALLS_PER_DAY", "5")
    monkeypatch.setenv("NOUS_ENTITY_PROFILE_OWNER_KEYS", " Pat , ,Quinn Avery's")
    s = Settings(_env_file=None)
    assert s.entity_profile_max_calls_per_day == 5
    assert s.entity_profile_owner_keys == ["Pat", "Quinn Avery's"]
    assert owner_keys(s) == {"user", "assistant", "pat", "quinn avery"}


def test_an_empty_owner_keys_value_is_an_empty_list(monkeypatch):
    monkeypatch.setenv("NOUS_ENTITY_PROFILE_OWNER_KEYS", "")
    s = Settings(_env_file=None)
    assert s.entity_profile_owner_keys == [] and owner_keys(s) == {"user", "assistant"}


@pytest.mark.parametrize(
    "field",
    [
        "entity_profile_min_facts",
        "entity_profile_max_profiles",
        "entity_profile_max_calls_per_day",
        "entity_profile_max_lines",
        "entity_profile_max_tokens",
        "entity_profile_source_limit",
    ],
)
def test_a_bound_below_one_is_rejected(field):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: 0})


def test_compose_and_the_docs_carry_every_setting():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    doc = (ROOT / "docs" / "reference" / "environment-variables.md").read_text(encoding="utf-8")
    for field, default in DEFAULTS.items():
        name = f"NOUS_{field.upper()}"
        value = DOC_DEFAULTS.get(str(default), str(default))
        assert f"- {name}=${{{name}:-{value}}}" in compose, name
        assert (f"| `{name}` | `{value}` |" if value else f"| `{name}` | (empty) |") in doc, name
    rest = (ROOT / "docs" / "reference" / "rest-api.md").read_text(encoding="utf-8")
    for route in (
        "| GET | `/entity-profiles` |",
        "| GET | `/entity-profiles/{key}` |",
        "| POST | `/entity-profiles/rebuild` |",
    ):
        assert route in rest, route
    assert "entity_profile.py" in (ROOT / "docs" / "reference" / "project-structure.md").read_text(encoding="utf-8")
