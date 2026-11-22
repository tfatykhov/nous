"""F116 PR 2: the settings of the self-initiated check's filters, their defaults, bounds and doc rows."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from nous.config import Settings

ROOT = Path(__file__).resolve().parents[1]

# Every setting of this PR and its code default as compose would write it.
F116_DEFAULTS: dict[str, str] = {
    "NOUS_HEARTBEAT_PENDING_KIND_MODE": "off",
    "NOUS_HEARTBEAT_PENDING_AGE_MODE": "off",
    "NOUS_HEARTBEAT_PENDING_SUPERSEDE_MODE": "off",
    "NOUS_HEARTBEAT_PENDING_SNAPSHOT_TTL_HOURS": "336",
    "NOUS_HEARTBEAT_PENDING_FACT_MAX_AGE_HOURS": "720",
    "NOUS_HEARTBEAT_PENDING_EPISODE_MAX_AGE_HOURS": "720",
}
MODE_ENVS = [k for k in F116_DEFAULTS if k.endswith("_MODE")]


def _field(env: str) -> str:
    return env.removeprefix("NOUS_").lower()


@pytest.fixture
def clean_env(monkeypatch):
    """No value from the shell may leak into a default."""
    for name in F116_DEFAULTS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_every_setting_ships_with_its_default(clean_env):
    s = Settings(_env_file=None)
    assert s.heartbeat_pending_kind_mode == "off"
    assert s.heartbeat_pending_age_mode == "off"
    assert s.heartbeat_pending_supersede_mode == "off"
    assert s.heartbeat_pending_snapshot_ttl_hours == 336
    assert s.heartbeat_pending_fact_max_age_hours == 720
    assert s.heartbeat_pending_episode_max_age_hours == 720


@pytest.mark.parametrize("env", MODE_ENVS)
@pytest.mark.parametrize("value", ["banana", "", "  ", "ON"])
def test_a_mode_value_of_any_text_loads(clean_env, env, value):
    """A mode is plain text: the filter reads anything but off, shadow or on as off, and the process still starts."""
    clean_env.setenv(env, value)
    assert getattr(Settings(_env_file=None), _field(env)) == value


@pytest.mark.parametrize("env", MODE_ENVS)
def test_a_mode_value_is_read(clean_env, env):
    clean_env.setenv(env, "shadow")
    assert getattr(Settings(_env_file=None), _field(env)) == "shadow"


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("heartbeat_pending_snapshot_ttl_hours", 23),
        ("heartbeat_pending_snapshot_ttl_hours", 2161),
        ("heartbeat_pending_fact_max_age_hours", 71),
        ("heartbeat_pending_fact_max_age_hours", 8761),
        ("heartbeat_pending_episode_max_age_hours", 71),
        ("heartbeat_pending_episode_max_age_hours", 8761),
    ],
)
def test_the_bounds_are_enforced(clean_env, field, bad):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: bad})


@pytest.mark.parametrize(
    ("field", "edge"),
    [
        ("heartbeat_pending_snapshot_ttl_hours", 24),
        ("heartbeat_pending_snapshot_ttl_hours", 2160),
        ("heartbeat_pending_fact_max_age_hours", 72),
        ("heartbeat_pending_fact_max_age_hours", 8760),
        ("heartbeat_pending_episode_max_age_hours", 72),
        ("heartbeat_pending_episode_max_age_hours", 8760),
    ],
)
def test_the_bounds_are_inclusive(clean_env, field, edge):
    assert getattr(Settings(_env_file=None, **{field: edge}), field) == edge
