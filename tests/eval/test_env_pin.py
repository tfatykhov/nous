"""`pinned_settings` must ignore BOTH settings sources, not just dotenv.

The bug these lock down shipped once: `Settings(_env_file=None)` looks pinned,
disables only the dotenv source, and lets every exported `NOUS_*` variable
through. A probe built on it silently measured a different system depending on
which shell it ran from.
"""

from __future__ import annotations

import os

import pytest

from nous.config import Settings
from nous_eval.env_pin import hidden_env, pinned_runtime, pinned_settings


@pytest.fixture
def _decay_env(monkeypatch):
    monkeypatch.setenv("NOUS_SPREADING_ACTIVATION_DECAY", "0.99")


def test_env_file_none_alone_is_NOT_enough(_decay_env):
    """Characterises the leak — if this ever fails, pydantic changed and the
    module docstring's worked example is stale."""
    assert Settings(_env_file=None).spreading_activation_decay == 0.99


def test_pinned_settings_ignores_process_env(_decay_env):
    assert pinned_settings().spreading_activation_decay == 0.5


def test_pinned_settings_ignores_unprefixed_db_vars(monkeypatch):
    monkeypatch.setenv("DB_NAME", "leaked_db")
    assert pinned_settings().db_name != "leaked_db"


def test_overrides_still_apply(_decay_env):
    s = pinned_settings(spreading_activation_decay=0.25, db_name="pinned")
    assert s.spreading_activation_decay == 0.25
    assert s.db_name == "pinned"


def test_non_nous_env_survives(monkeypatch):
    """API keys are unprefixed and must be readable by the caller."""
    monkeypatch.setenv("EVAL_DB_PASSWORD", "secret")
    with hidden_env():
        assert os.environ["EVAL_DB_PASSWORD"] == "secret"


def test_hidden_env_restores_on_exit(_decay_env):
    with hidden_env():
        assert "NOUS_SPREADING_ACTIVATION_DECAY" not in os.environ
    assert os.environ["NOUS_SPREADING_ACTIVATION_DECAY"] == "0.99"


def test_hidden_env_restores_on_exception(_decay_env):
    with pytest.raises(RuntimeError):
        with hidden_env():
            raise RuntimeError("boom")
    assert os.environ["NOUS_SPREADING_ACTIVATION_DECAY"] == "0.99"


def test_lowercase_env_is_also_hidden(monkeypatch):
    """pydantic-settings matches env names case-insensitively, so a
    case-sensitive filter leaves a live override in place."""
    monkeypatch.setenv("nous_spreading_activation_decay", "0.99")
    assert pinned_settings().spreading_activation_decay == 0.5


def test_lowercase_env_restored(monkeypatch):
    monkeypatch.setenv("nous_spreading_activation_decay", "0.99")
    with hidden_env():
        pass
    assert os.environ["nous_spreading_activation_decay"] == "0.99"


def test_hides_the_search_resolver_fingerprint_vars(monkeypatch):
    """`nous.heart.search._resolver_settings` reads these live from os.environ
    at QUERY time, so pinning the Settings object alone does not pin fusion."""
    for name in ("NOUS_RRF_K", "NOUS_VECTOR_WEIGHT", "NOUS_HYBRID_SEARCH_KEYWORD_ENABLED"):
        monkeypatch.setenv(name, "999")
    with hidden_env():
        for name in ("NOUS_RRF_K", "NOUS_VECTOR_WEIGHT", "NOUS_HYBRID_SEARCH_KEYWORD_ENABLED"):
            assert name not in os.environ


def test_pinned_runtime_publishes_fusion_params(monkeypatch):
    """Hiding alone is NOT enough: `_resolver_settings` builds its own Settings
    from these env vars, so removing them substitutes code defaults instead of
    the pinned shape."""
    monkeypatch.setenv("NOUS_RRF_K", "999")
    s = pinned_settings(rrf_k=30, vector_weight=0.5, hybrid_search_keyword_enabled=False)
    with pinned_runtime(s):
        assert os.environ["NOUS_RRF_K"] == "30"
        assert os.environ["NOUS_VECTOR_WEIGHT"] == "0.5"
        # Booleans must be env-parseable, not Python's "False".
        assert os.environ["NOUS_HYBRID_SEARCH_KEYWORD_ENABLED"] == "false"
    assert os.environ["NOUS_RRF_K"] == "999"


def test_pinned_runtime_reaches_the_real_resolver(monkeypatch):
    """End-to-end: the value the search layer actually resolves."""
    from nous.heart import search

    monkeypatch.setenv("NOUS_RRF_K", "999")
    s = pinned_settings(rrf_k=30)
    with pinned_runtime(s):
        assert search._resolver_settings().rrf_k == 30


def test_published_vars_do_not_escape_the_context(monkeypatch):
    """The regression that turned four unrelated suites red: `pinned_runtime`
    sets NOUS_* inside `hidden_env`, whose finally only restored what it had
    saved — so the published values escaped into the pytest process."""
    monkeypatch.delenv("NOUS_HYBRID_SEARCH_KEYWORD_ENABLED", raising=False)
    monkeypatch.delenv("NOUS_RRF_K", raising=False)
    with pinned_runtime(pinned_settings(hybrid_search_keyword_enabled=False)):
        assert os.environ["NOUS_HYBRID_SEARCH_KEYWORD_ENABLED"] == "false"
    assert "NOUS_HYBRID_SEARCH_KEYWORD_ENABLED" not in os.environ
    assert "NOUS_RRF_K" not in os.environ


def test_environment_restored_exactly(monkeypatch):
    monkeypatch.setenv("NOUS_RRF_K", "77")
    before = {k: v for k, v in os.environ.items() if k.startswith("NOUS_")}
    with pinned_runtime(pinned_settings(rrf_k=30)):
        pass
    after = {k: v for k, v in os.environ.items() if k.startswith("NOUS_")}
    assert before == after
