"""Tests for model-aware dynamic compaction thresholds (F016 Phase 2)."""

from nous.cognitive.schemas import MODEL_CONTEXT_WINDOWS
from nous.config import Settings

# Env vars that override the dynamic computation under test. _env_file=None
# shields against the repo .env, but NOT against process-level exports
# (codex P2 on PR #508) — and compaction_threshold/keep_recent_tokens are
# explicit-set-tracked via model_fields_set, so they cannot be neutralized
# by passing defaults; they must be absent from the environment entirely.
_OVERRIDE_VARS = (
    "NOUS_CONTEXT_WINDOW",
    "NOUS_COMPACTION_THRESHOLD",
    "NOUS_KEEP_RECENT_TOKENS",
)


def _clean_settings(monkeypatch, **kwargs) -> Settings:
    for var in _OVERRIDE_VARS:
        monkeypatch.delenv(var, raising=False)
    return Settings(_env_file=None, **kwargs)


class TestModelContextWindows:
    def test_1m_models(self):
        assert MODEL_CONTEXT_WINDOWS["claude-sonnet-4-6"] == 1_000_000
        assert MODEL_CONTEXT_WINDOWS["claude-opus-4-6"] == 1_000_000

    def test_200k_models(self):
        assert MODEL_CONTEXT_WINDOWS["claude-sonnet-4-5"] == 200_000


class TestEffectiveThresholds:
    def test_dynamic_threshold_1m_model(self, monkeypatch):
        s = _clean_settings(monkeypatch, model="claude-sonnet-4-6-20250514")
        assert s.effective_compaction_threshold == 600_000
        assert s.effective_keep_recent == 200_000

    def test_dynamic_threshold_200k_model(self, monkeypatch):
        s = _clean_settings(monkeypatch, model="claude-sonnet-4-5-20250514")
        assert s.effective_compaction_threshold == 120_000
        assert s.effective_keep_recent == 40_000

    def test_explicit_override_takes_priority(self, monkeypatch):
        s = _clean_settings(
            monkeypatch,
            model="claude-sonnet-4-6-20250514",
            NOUS_COMPACTION_THRESHOLD="50000",
        )
        assert s.effective_compaction_threshold == 50_000

    def test_unknown_model_defaults_200k(self, monkeypatch):
        s = _clean_settings(monkeypatch, model="some-unknown-model")
        assert s.effective_compaction_threshold == 120_000

    def test_longest_key_first_matching(self, monkeypatch):
        """Ensure 'claude-sonnet-4-5' matches before 'claude-sonnet-4-6' for model claude-sonnet-4-5-20250514."""
        s = _clean_settings(monkeypatch, model="claude-sonnet-4-5-20250514")
        assert s.effective_compaction_threshold == 120_000  # 200K * 0.6, not 1M * 0.6


class TestShouldCompactUsesEffective:
    def test_should_compact_uses_effective_threshold(self, monkeypatch):
        from nous.api.compaction import ConversationCompactor
        s = _clean_settings(
            monkeypatch,
            model="claude-sonnet-4-6-20250514",
            NOUS_COMPACTION_ENABLED="true",
        )
        c = ConversationCompactor(s)
        # With 1M model, threshold is 600K
        assert not c.should_compact(10_000, 100_000)  # 110K < 600K
        assert c.should_compact(10_000, 600_000)  # 610K > 600K
