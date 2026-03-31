"""Tests for F033 multi-tier search config settings."""

from nous.config import Settings


def test_tavily_api_key_from_env(monkeypatch):
    """TAVILY_API_KEY env var maps to tavily_api_key."""
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test123")
    s = Settings(_env_file=None)
    assert s.tavily_api_key == "tvly-test123"


def test_exa_api_key_from_env(monkeypatch):
    """EXA_API_KEY env var maps to exa_api_key."""
    monkeypatch.setenv("EXA_API_KEY", "exa-test456")
    s = Settings(_env_file=None)
    assert s.exa_api_key == "exa-test456"


def test_search_provider_default():
    """Default search_provider is 'auto'."""
    s = Settings(_env_file=None)
    assert s.search_provider == "auto"


def test_search_provider_override(monkeypatch):
    """NOUS_SEARCH_PROVIDER overrides search_provider."""
    monkeypatch.setenv("NOUS_SEARCH_PROVIDER", "brave")
    s = Settings(_env_file=None)
    assert s.search_provider == "brave"
