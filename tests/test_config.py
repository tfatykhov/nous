"""Core Settings validation tests."""

import pytest
from pydantic import ValidationError

from nous.config import Settings


class TestDAGTimeoutValidation:
    """F046: NOUS_DAG_NODE_DEFAULT_TIMEOUT must not exceed NOUS_DAG_NODE_MAX_TIMEOUT."""

    def test_default_must_not_exceed_max(self, monkeypatch):
        monkeypatch.setenv("NOUS_DAG_NODE_DEFAULT_TIMEOUT", "10000")
        monkeypatch.setenv("NOUS_DAG_NODE_MAX_TIMEOUT", "7200")
        with pytest.raises(ValidationError):
            Settings()

    def test_default_equals_max_is_ok(self, monkeypatch):
        monkeypatch.setenv("NOUS_DAG_NODE_DEFAULT_TIMEOUT", "900")
        monkeypatch.setenv("NOUS_DAG_NODE_MAX_TIMEOUT", "900")
        s = Settings()
        assert s.dag_node_default_timeout == 900

    def test_defaults(self, monkeypatch):
        # Guard against ambient env AND repo .env leaking into defaults test.
        # pydantic-settings reads from .env via env_file= config; monkeypatch.delenv
        # only clears os.environ. Pass _env_file=None to short-circuit .env loading.
        monkeypatch.delenv("NOUS_DAG_NODE_DEFAULT_TIMEOUT", raising=False)
        monkeypatch.delenv("NOUS_DAG_NODE_MAX_TIMEOUT", raising=False)
        s = Settings(_env_file=None)
        assert s.dag_node_default_timeout == 600
        assert s.dag_node_max_timeout == 7200


class TestContextBudgetOverridesParsing:
    """Audit ST-1: empty/blank NOUS_CONTEXT_BUDGET_OVERRIDES must not crash boot.

    docker-compose passes `NOUS_CONTEXT_BUDGET_OVERRIDES=${...:-}` (empty string)
    on a fresh install with no host .env. Without NoDecode + the before-validator,
    pydantic-settings' default complex-field decoder calls json.loads("") and
    raises SettingsError, crash-looping the container.
    """

    def test_empty_string_yields_empty_dict(self, monkeypatch):
        monkeypatch.setenv("NOUS_CONTEXT_BUDGET_OVERRIDES", "")
        s = Settings(_env_file=None)
        assert s.context_budget_overrides == {}

    def test_whitespace_string_yields_empty_dict(self, monkeypatch):
        monkeypatch.setenv("NOUS_CONTEXT_BUDGET_OVERRIDES", "   ")
        s = Settings(_env_file=None)
        assert s.context_budget_overrides == {}

    def test_valid_json_is_parsed(self, monkeypatch):
        monkeypatch.setenv(
            "NOUS_CONTEXT_BUDGET_OVERRIDES", '{"total": 13000, "facts": 3000}'
        )
        s = Settings(_env_file=None)
        assert s.context_budget_overrides == {"total": 13000, "facts": 3000}

    def test_unset_uses_default_empty_dict(self, monkeypatch):
        monkeypatch.delenv("NOUS_CONTEXT_BUDGET_OVERRIDES", raising=False)
        s = Settings(_env_file=None)
        assert s.context_budget_overrides == {}

    def test_programmatic_dict_passthrough(self, monkeypatch):
        monkeypatch.delenv("NOUS_CONTEXT_BUDGET_OVERRIDES", raising=False)
        s = Settings(_env_file=None, context_budget_overrides={"total": 9000})
        assert s.context_budget_overrides == {"total": 9000}
