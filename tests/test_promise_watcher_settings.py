"""F111 PR B1: the watcher's and the classifier's settings, their compose lines and their doc rows (plan §1, §9 J)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from nous.config import Settings

ROOT = Path(__file__).resolve().parents[1]

# Every F111 setting: its code default as compose writes it ("" = unset).
F111_DEFAULTS: dict[str, str] = {
    "NOUS_PROMISE_GATE_ENABLED": "true",
    "NOUS_PROMISE_GATE_MODE": "shadow",
    "NOUS_PROMISE_GATE_MAX_REENTRIES": "2",
    "NOUS_PROMISE_GATE_DEFAULT_DUE_MINUTES": "60",
    "NOUS_PROMISE_GATE_GRACE_SECONDS": "900",
    "NOUS_PROMISE_GATE_CLASSIFIER_ENABLED": "false",
    "NOUS_PROMISE_GATE_CLASSIFIER_MODEL": "claude-haiku-5-5",
    "NOUS_PROMISE_GATE_CLASSIFIER_TIMEOUT_SECONDS": "10",
    "NOUS_PROMISE_GATE_CLASSIFIER_MAX_CONCURRENCY": "2",
    "NOUS_PROMISE_GATE_LOG_RETENTION_DAYS": "30",
    "NOUS_PROMISE_WATCHER_ENABLED": "false",
    "NOUS_PROMISE_WATCHER_INTERVAL_SECONDS": "300",
    "NOUS_PROMISE_WATCHER_SINCE": "",
    "NOUS_PROMISE_WATCHER_MAX_ITEMS": "10",
    "NOUS_OWNER_ROUTE_SECRET_SHA256": "",
}


def _field(env: str) -> str:
    return env.removeprefix("NOUS_").lower()


@pytest.fixture
def clean_env(monkeypatch):
    """The job shell exports production NOUS_* values: none of the F111 ones may leak into a default."""
    for name in F111_DEFAULTS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _compose() -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def _env_of(service: dict) -> dict[str, str]:
    env = service.get("environment") or {}
    if isinstance(env, list):
        return dict(item.split("=", 1) if "=" in item else (item, "") for item in env)
    return {k: str(v) for k, v in env.items()}


def test_every_new_setting_ships_with_its_default(clean_env):
    s = Settings(_env_file=None)
    assert s.promise_watcher_enabled is False
    assert s.promise_watcher_interval_seconds == 300
    assert s.promise_watcher_since is None
    assert s.promise_watcher_max_items == 10
    assert s.promise_gate_classifier_enabled is False
    assert s.promise_gate_classifier_model == "claude-haiku-5-5"
    assert s.promise_gate_classifier_timeout_seconds == 10
    assert s.promise_gate_classifier_max_concurrency == 2
    assert s.promise_gate_log_retention_days == 30
    # PR A's gate defaults and the tool policy are not touched.
    assert (s.promise_gate_enabled, s.promise_gate_mode) == (True, "shadow")
    assert Settings.model_fields["tool_context_policy_mode"].default == "warn"


@pytest.mark.parametrize(
    "env", [k for k, v in F111_DEFAULTS.items() if v == ""] + ["NOUS_PROMISE_GATE_CLASSIFIER_MODEL"]
)
def test_an_empty_value_loads_as_unset(clean_env, env):
    """Compose passes an unset variable through as "": each setting reads it as unset (its default)."""
    clean_env.setenv(env, "")
    s = Settings(_env_file=None)
    expected = Settings.model_fields[_field(env)].default
    assert getattr(s, _field(env)) == expected
    if env != "NOUS_OWNER_ROUTE_SECRET_SHA256":  # PR A's digest: a blank one is refused at start like a bad one
        clean_env.setenv(env, "   ")
        assert getattr(Settings(_env_file=None), _field(env)) == expected


def test_the_watcher_cutoff_is_kept_as_given(clean_env):
    clean_env.setenv("NOUS_PROMISE_WATCHER_SINCE", "2026-11-20T00:00:00+00:00")
    assert Settings(_env_file=None).promise_watcher_since == "2026-11-20T00:00:00+00:00"
    # An unparsable value does not stop the process: the watcher refuses it at start (test_commitment_watcher).
    clean_env.setenv("NOUS_PROMISE_WATCHER_SINCE", "next tuesday")
    assert Settings(_env_file=None).promise_watcher_since == "next tuesday"


@pytest.mark.parametrize("model", ["gpt-5", "gemini-3-pro", "haiku", "typesafe-1"])
def test_a_classifier_model_that_is_not_claude_is_refused(clean_env, model):
    clean_env.setenv("NOUS_PROMISE_GATE_CLASSIFIER_MODEL", model)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_the_classifier_model_setting_is_read(clean_env):
    clean_env.setenv("NOUS_PROMISE_GATE_CLASSIFIER_MODEL", "claude-sonnet-5-5")
    assert Settings(_env_file=None).promise_gate_classifier_model == "claude-sonnet-5-5"


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("promise_watcher_interval_seconds", 59),
        ("promise_watcher_max_items", 0),
        ("promise_watcher_max_items", 16),
        ("promise_gate_classifier_max_concurrency", 0),
        ("promise_gate_classifier_timeout_seconds", 0),
        ("promise_gate_log_retention_days", 0),
    ],
)
def test_the_bounds_are_enforced(clean_env, field, bad):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: bad})


def test_compose_passes_every_f111_setting_with_the_code_default(clean_env):
    nous_env = _env_of(_compose()["services"]["nous"])
    for env, default in F111_DEFAULTS.items():
        assert nous_env.get(env) == f"${{{env}:-{default}}}", env
        field = Settings.model_fields[_field(env)]
        if field.annotation in (int, bool):
            assert default != "", f"{env}: an int or bool needs a real compose default"
        code = field.default
        if isinstance(code, bool):
            assert default == str(code).lower(), env
        elif code is None:
            assert default == "", env
        else:
            assert default == str(code), env


def test_the_owner_secret_is_under_the_telegram_service_only():
    services = _compose()["services"]
    holders = [name for name, svc in services.items() if "NOUS_OWNER_ROUTE_SECRET" in _env_of(svc)]
    assert holders == ["telegram"]
    assert _env_of(services["telegram"])["NOUS_OWNER_ROUTE_SECRET"] == "${NOUS_OWNER_ROUTE_SECRET:-}"
    assert "NOUS_OWNER_ROUTE_SECRET_SHA256" in _env_of(services["nous"])
    assert "NOUS_OWNER_ROUTE_SECRET_SHA256" not in _env_of(services["telegram"])


def test_the_docs_have_the_rows():
    doc = (ROOT / "docs" / "reference" / "environment-variables.md").read_text(encoding="utf-8")
    for env, default in F111_DEFAULTS.items():
        shown = f"`{default}`" if default else "(empty)"
        assert f"| `{env}` | {shown} |" in doc, env


@pytest.mark.postgres_only
def test_main_wires_no_watcher_and_no_classifier_with_the_flags_off(monkeypatch):
    """Review M10: create_components with the B1 flags at their defaults (watcher and classifier off, the gate in
    shadow): no scheduler and no watcher anywhere, the gate without a classifier, and what runs with the gate on
    whatever the new flags say (review M2): the gate itself and the promise_gate_log retention loop."""
    from tests.test_model_call_retention import _boot, main

    seen: dict = {}

    async def checks(components: dict, settings) -> None:
        try:
            runner = components["runner"]
            seen["classifier"] = runner._promise_classifier
            seen["watcher"] = runner._commitment_watcher
            seen["heartbeat_watcher"] = components["heartbeat_runner"]._commitment_watcher
            seen["gate_classifier"] = runner._promise_gate._classifier
            seen["retention_task"] = components["promise_gate_log_retention_task"]
        finally:
            await main.shutdown_components(components)

    _boot(monkeypatch, checks)
    assert seen["classifier"] is None and seen["watcher"] is None and seen["heartbeat_watcher"] is None
    assert seen["gate_classifier"] is None
    assert seen["retention_task"] is not None
