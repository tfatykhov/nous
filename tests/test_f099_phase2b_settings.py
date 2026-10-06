"""F099 Phase 2b: the continuation settings, their validators, and the flag gate."""

from __future__ import annotations

import logging

import pytest

import nous.main as main
from nous.brain import continuation
from nous.config import Settings

BASE = {"result_inbox_enabled": True, "intentions_enabled": True}


def test_defaults_are_the_contract_values():
    s = Settings(_env_file=None)
    assert s.continuation_enabled is False
    assert (s.continuation_max_depth, s.continuation_max_spawns_per_root) == (3, 12)
    assert (s.continuation_max_turns_per_root, s.continuation_max_tokens_per_root) == (8, 400000)
    assert (s.continuation_stall_limit, s.intention_root_ttl_hours) == (2, 72)
    assert (s.continuation_max_concurrent, s.continuation_debounce_seconds) == (2, 20)
    assert (s.continuation_max_wait_seconds, s.continuation_lease_seconds) == (120, 900)
    assert (s.continuation_turn_timeout_seconds, s.continuation_max_attempts) == (780, 3)
    assert s.intention_proposal_ttl_hours == 24


@pytest.mark.parametrize(
    "name,bad",
    [
        ("continuation_max_depth", 0),
        ("continuation_max_spawns_per_root", 0),
        ("continuation_max_turns_per_root", 0),
        ("continuation_max_tokens_per_root", 999),
        ("continuation_stall_limit", 0),
        ("intention_root_ttl_hours", 0),
        ("continuation_max_concurrent", 0),
        ("continuation_debounce_seconds", -1),
        ("continuation_max_wait_seconds", -1),
        ("continuation_lease_seconds", 119),
        ("continuation_turn_timeout_seconds", 59),
        ("continuation_max_attempts", 0),
        ("intention_proposal_ttl_hours", 0),
    ],
)
def test_bounds_are_enforced(name, bad):
    with pytest.raises(ValueError):
        Settings(_env_file=None, **{name: bad})


def test_the_environment_names_are_the_contract_names(monkeypatch):
    monkeypatch.setenv("NOUS_CONTINUATION_MAX_DEPTH", "5")
    monkeypatch.setenv("NOUS_INTENTION_ROOT_TTL_HOURS", "36")
    s = Settings(_env_file=None)
    assert (s.continuation_max_depth, s.intention_root_ttl_hours) == (5, 36)


def test_continuation_without_intentions_is_forced_off_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="nous.config"):
        s = Settings(_env_file=None, result_inbox_enabled=True, continuation_enabled=True)
    assert s.continuation_enabled is False
    assert "NOUS_CONTINUATION_ENABLED=true needs NOUS_INTENTIONS_ENABLED=true" in caplog.text


def test_a_missing_inbox_forces_intentions_and_then_continuation_off(caplog):
    """The two validators run in declaration order: intentions first."""
    with caplog.at_level(logging.WARNING, logger="nous.config"):
        s = Settings(_env_file=None, intentions_enabled=True, continuation_enabled=True)
    assert (s.intentions_enabled, s.continuation_enabled) == (False, False)
    assert "NOUS_INTENTIONS_ENABLED=true needs NOUS_RESULT_INBOX_ENABLED=true" in caplog.text
    assert "NOUS_CONTINUATION_ENABLED=true needs NOUS_INTENTIONS_ENABLED=true" in caplog.text


def test_continuation_stays_on_with_both_prerequisites():
    assert Settings(_env_file=None, continuation_enabled=True, **BASE).continuation_enabled is True


def test_the_turn_timeout_must_sit_at_least_60s_below_the_lease():
    Settings(_env_file=None, continuation_lease_seconds=840, continuation_turn_timeout_seconds=780)  # exactly 60
    with pytest.raises(ValueError, match="at least 60 s below NOUS_CONTINUATION_LEASE_SECONDS"):
        Settings(_env_file=None, continuation_lease_seconds=840, continuation_turn_timeout_seconds=781)
    with pytest.raises(ValueError, match="at least 60 s below NOUS_CONTINUATION_LEASE_SECONDS"):
        Settings(_env_file=None, continuation_lease_seconds=120)  # the default 780 s timeout no longer fits


def test_max_wait_may_not_be_below_the_debounce():
    Settings(_env_file=None, continuation_debounce_seconds=30, continuation_max_wait_seconds=30)
    with pytest.raises(ValueError, match="MAX_WAIT"):
        Settings(_env_file=None, continuation_debounce_seconds=30, continuation_max_wait_seconds=29)


PHASE2_ENV = {
    "NOUS_CONTINUATION_ENABLED": "false",
    "NOUS_CONTINUATION_MAX_DEPTH": "3",
    "NOUS_CONTINUATION_MAX_SPAWNS_PER_ROOT": "12",
    "NOUS_CONTINUATION_MAX_TURNS_PER_ROOT": "8",
    "NOUS_CONTINUATION_MAX_TOKENS_PER_ROOT": "400000",
    "NOUS_CONTINUATION_STALL_LIMIT": "2",
    "NOUS_INTENTION_ROOT_TTL_HOURS": "72",
    "NOUS_CONTINUATION_MAX_CONCURRENT": "2",
    "NOUS_CONTINUATION_DEBOUNCE_SECONDS": "20",
    "NOUS_CONTINUATION_MAX_WAIT_SECONDS": "120",
    "NOUS_CONTINUATION_LEASE_SECONDS": "900",
    "NOUS_CONTINUATION_TURN_TIMEOUT_SECONDS": "780",
    "NOUS_CONTINUATION_MAX_ATTEMPTS": "3",
    "NOUS_INTENTION_PROPOSAL_TTL_HOURS": "24",
    # F098 lines the repo compose never had.
    "NOUS_RESULT_INBOX_DAG_SCHEDULED": "false",
    "NOUS_RESULT_MEMORY_ENABLED": "false",
    "NOUS_RESULT_MEMORY_SCHEDULED": "false",
}


def test_compose_passes_every_phase2_setting_with_the_settings_default():
    """A setting prod's compose does not pass silently keeps its default and cannot be turned on."""
    from pathlib import Path

    compose = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text(encoding="utf-8")
    for name, default in PHASE2_ENV.items():
        assert f"- {name}=${{{name}:-{default}}}" in compose, name
    s = Settings(_env_file=None)
    assert str(s.continuation_max_depth) == PHASE2_ENV["NOUS_CONTINUATION_MAX_DEPTH"]
    assert str(int(s.intention_root_ttl_hours)) == PHASE2_ENV["NOUS_INTENTION_ROOT_TTL_HOURS"]
    assert str(int(s.intention_proposal_ttl_hours)) == PHASE2_ENV["NOUS_INTENTION_PROPOSAL_TTL_HOURS"]


def test_the_runner_is_not_ready_in_this_build():
    """2e flips this assertion together with the gate test below."""
    assert continuation.CONTINUATION_RUNNER_READY is False


def test_the_gate_forces_a_requested_flag_off(caplog):
    settings = Settings(_env_file=None, continuation_enabled=True, **BASE)
    assert settings.continuation_enabled is True  # the validators alone do not gate it
    with caplog.at_level(logging.WARNING, logger="nous.main"):
        main._gate_continuation_flag(settings)
    assert settings.continuation_enabled is False
    assert "continuation runner is not shipped in this build" in caplog.text


def test_the_gate_is_silent_when_the_flag_is_off(caplog):
    settings = Settings(_env_file=None, **BASE)
    with caplog.at_level(logging.WARNING, logger="nous.main"):
        main._gate_continuation_flag(settings)
    assert settings.continuation_enabled is False
    assert "continuation runner" not in caplog.text


def test_the_gate_lets_a_ready_runner_through(monkeypatch):
    monkeypatch.setattr(continuation, "CONTINUATION_RUNNER_READY", True)
    settings = Settings(_env_file=None, continuation_enabled=True, **BASE)
    main._gate_continuation_flag(settings)
    assert settings.continuation_enabled is True


async def test_create_components_gates_the_flag_before_anything_reads_it(monkeypatch):
    """create_components must gate before it builds a single component. The
    first thing it builds is the Database, so a stand-in that stops there sees
    the flag already off."""
    settings = Settings(_env_file=None, continuation_enabled=True, **BASE)
    seen: dict[str, bool] = {}

    class _Stop(Exception):
        pass

    def _database(settings_arg, **kwargs):
        seen["flag_when_built"] = settings_arg.continuation_enabled
        raise _Stop

    monkeypatch.setattr(main, "Database", _database)
    with pytest.raises(_Stop):
        await main.create_components(settings)
    assert seen == {"flag_when_built": False}
