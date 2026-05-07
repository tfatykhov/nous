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
