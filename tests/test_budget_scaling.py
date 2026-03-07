"""Tests for model-aware budget scaling (F017 Phase 3)."""

from unittest.mock import AsyncMock

from nous.cognitive.context import ContextEngine
from nous.config import Settings


class TestBudgetScaling:
    def _make_engine(self, model="claude-sonnet-4-6-20250514", enabled=True):
        s = Settings(model=model, budget_scale_enabled=enabled)
        return ContextEngine(brain=AsyncMock(), heart=AsyncMock(), settings=s)

    def test_1m_model_scales_2_5x(self):
        engine = self._make_engine(model="claude-sonnet-4-6-20250514")
        assert engine._scaled_budget(1000) == 2500

    def test_200k_model_scales_1_5x(self):
        engine = self._make_engine(model="claude-sonnet-4-5-20250514")
        assert engine._scaled_budget(1000) == 1500

    def test_disabled_no_scaling(self):
        engine = self._make_engine(enabled=False)
        assert engine._scaled_budget(1000) == 1000

    def test_unknown_model_defaults_to_200k_scaling(self):
        # _get_context_window returns 200_000 for unknown models, so 1.5x applies
        engine = self._make_engine(model="unknown-model")
        assert engine._scaled_budget(1000) == 1500

    def test_small_window_model_no_scaling(self):
        # gpt-4-turbo has 128K window — below 200K threshold, no scaling
        engine = self._make_engine(model="gpt-4-turbo")
        assert engine._scaled_budget(1000) == 1000
