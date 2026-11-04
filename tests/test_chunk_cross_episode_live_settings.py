"""J-3: NOUS_CHUNK_CROSS_EPISODE_LIVE_ENABLED / _MAX, the compose lines and the doc rows."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from nous.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def test_defaults_are_off_and_200():
    s = Settings(_env_file=None)
    assert s.chunk_cross_episode_live_enabled is False
    assert s.chunk_cross_episode_live_max == 200


def test_the_environment_names_are_the_contract_names(monkeypatch):
    monkeypatch.setenv("NOUS_CHUNK_CROSS_EPISODE_LIVE_ENABLED", "true")
    monkeypatch.setenv("NOUS_CHUNK_CROSS_EPISODE_LIVE_MAX", "50")
    s = Settings(_env_file=None)
    assert s.chunk_cross_episode_live_enabled is True
    assert s.chunk_cross_episode_live_max == 50


def test_compose_passes_both_with_the_settings_defaults():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert (
        "- NOUS_CHUNK_CROSS_EPISODE_LIVE_ENABLED=${NOUS_CHUNK_CROSS_EPISODE_LIVE_ENABLED:-false}"
        in compose
    )
    assert "- NOUS_CHUNK_CROSS_EPISODE_LIVE_MAX=${NOUS_CHUNK_CROSS_EPISODE_LIVE_MAX:-200}" in compose


def test_the_docs_have_both_rows():
    doc = (ROOT / "docs" / "reference" / "environment-variables.md").read_text(encoding="utf-8")
    rows = doc.splitlines()
    assert any(r.startswith("| `NOUS_CHUNK_CROSS_EPISODE_LIVE_ENABLED` | `false` |") for r in rows)
    assert any(r.startswith("| `NOUS_CHUNK_CROSS_EPISODE_LIVE_MAX` | `200` |") for r in rows)


def test_top_k_must_be_positive(monkeypatch):
    """The neighbour queries render top_k as a literal LIMIT, so a value
    below 1 must fail at startup, not on every chunk."""
    monkeypatch.setenv("NOUS_CHUNK_CROSS_EPISODE_TOP_K", "0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
