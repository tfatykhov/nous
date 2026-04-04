"""Tests for F025 Amnesia Prevention Phase 2+3."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from nous.cognitive.context import ContextEngine
from nous.config import Settings


@dataclass
class MockMemoryItem:
    """Minimal mock for memory items passed through staleness pipeline."""

    score: float | None = 0.8
    created_at: datetime | None = None
    category: str = ""


def _make_engine(
    staleness_penalty_enabled: bool = True,
    staleness_half_life_days: int = 20,
    budget_scale_enabled: bool = True,
    model: str = "claude-sonnet-4-6",
    context_window: int = 0,
) -> ContextEngine:
    """Create a minimal ContextEngine with settings.

    Uses object.__setattr__ to override context_window after construction
    because pydantic-settings validation_alias fields cannot be passed as
    keyword args with the Python field name.
    """
    settings = Settings(
        staleness_penalty_enabled=staleness_penalty_enabled,
        staleness_half_life_days=staleness_half_life_days,
        budget_scale_enabled=budget_scale_enabled,
        model=model,
    )
    if context_window > 0:
        object.__setattr__(settings, "context_window", context_window)
    return ContextEngine(brain=AsyncMock(), heart=AsyncMock(), settings=settings)


class TestStalenessPersonExemption:
    """P2-A: Person facts should be exempt from staleness penalty."""

    def test_person_category_exempt(self):
        """Old person facts keep original score."""
        engine = _make_engine()
        old_date = datetime.now(timezone.utc) - timedelta(days=60)
        items = [MockMemoryItem(score=0.8, created_at=old_date, category="person")]
        result = engine._apply_staleness_penalty(items)
        assert result[0].score == 0.8

    def test_rule_still_exempt(self):
        """Existing rule exemption unchanged."""
        engine = _make_engine()
        old_date = datetime.now(timezone.utc) - timedelta(days=60)
        items = [MockMemoryItem(score=0.8, created_at=old_date, category="rule")]
        result = engine._apply_staleness_penalty(items)
        assert result[0].score == 0.8

    def test_preference_still_exempt(self):
        """Existing preference exemption unchanged."""
        engine = _make_engine()
        old_date = datetime.now(timezone.utc) - timedelta(days=60)
        items = [MockMemoryItem(score=0.8, created_at=old_date, category="preference")]
        result = engine._apply_staleness_penalty(items)
        assert result[0].score == 0.8

    def test_technical_still_exempt(self):
        """Existing technical exemption unchanged."""
        engine = _make_engine()
        old_date = datetime.now(timezone.utc) - timedelta(days=60)
        items = [MockMemoryItem(score=0.8, created_at=old_date, category="technical")]
        result = engine._apply_staleness_penalty(items)
        assert result[0].score == 0.8

    def test_uncategorized_still_decayed(self):
        """Items without exempt category still decay."""
        engine = _make_engine()
        old_date = datetime.now(timezone.utc) - timedelta(days=60)
        items = [MockMemoryItem(score=0.8, created_at=old_date, category="other")]
        result = engine._apply_staleness_penalty(items)
        assert result[0].score < 0.8

    def test_tool_category_still_decayed(self):
        """Tool facts should still decay (they genuinely go stale)."""
        engine = _make_engine()
        old_date = datetime.now(timezone.utc) - timedelta(days=60)
        items = [MockMemoryItem(score=0.8, created_at=old_date, category="tool")]
        result = engine._apply_staleness_penalty(items)
        assert result[0].score < 0.8

    def test_staleness_disabled_skips_all(self):
        """When staleness_penalty_enabled=False, no decay applied."""
        engine = _make_engine(staleness_penalty_enabled=False)
        old_date = datetime.now(timezone.utc) - timedelta(days=100)
        items = [MockMemoryItem(score=0.8, created_at=old_date, category="other")]
        result = engine._apply_staleness_penalty(items)
        assert result[0].score == 0.8

    def test_staleness_minimum_floor_030(self):
        """Decay should never go below 0.3 floor."""
        engine = _make_engine()
        very_old = datetime.now(timezone.utc) - timedelta(days=365)
        items = [MockMemoryItem(score=1.0, created_at=very_old, category="other")]
        result = engine._apply_staleness_penalty(items)
        assert result[0].score == pytest.approx(0.3, abs=0.01)

    def test_no_score_items_pass_through(self):
        """Items without score pass through unchanged."""
        engine = _make_engine()
        items = [MockMemoryItem(score=None, created_at=datetime.now(timezone.utc))]
        result = engine._apply_staleness_penalty(items)
        assert result[0].score is None


class TestUserProfileBudgetScaling:
    """P2-B: user_profile budget should pass through _scaled_budget."""

    def test_scaled_budget_at_1m_window(self):
        """200 tokens -> 500 at 1M context window."""
        engine = _make_engine(budget_scale_enabled=True, context_window=1_000_000)
        assert engine._scaled_budget(200) == 500

    def test_scaled_budget_at_200k_window(self):
        """200 tokens -> 300 at 200K context window."""
        engine = _make_engine(budget_scale_enabled=True, context_window=200_000)
        assert engine._scaled_budget(200) == 300

    def test_scaled_budget_disabled(self):
        """When disabled, returns base budget."""
        engine = _make_engine(budget_scale_enabled=False)
        assert engine._scaled_budget(200) == 200

    def test_user_profile_scaling_in_source(self):
        """Verify context.py applies _scaled_budget to user_profile."""
        source = inspect.getsource(ContextEngine.build)
        assert "_scaled_budget(budget.user_profile)" in source
