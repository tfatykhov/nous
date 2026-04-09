"""Tests for staleness penalty (F017 Phase 5)."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from nous.cognitive.context import ContextEngine
from nous.config import Settings


class FakeItem:
    def __init__(self, score, created_at=None, category=None):
        self.score = score
        self.created_at = created_at or datetime.now(UTC)
        self.category = category


class TestStalenessPenalty:
    def _make_engine(self, enabled=True, half_life=14):
        s = Settings(
            staleness_penalty_enabled=enabled,
            staleness_half_life_days=half_life,
        )
        return ContextEngine(AsyncMock(), AsyncMock(), s)

    def test_fresh_content_unpenalized(self):
        engine = self._make_engine()
        item = FakeItem(score=0.8)
        result = engine._apply_staleness_penalty([item])
        assert result[0].score == 0.8

    def test_14_day_old_halved(self):
        # Uses explicit half_life=14; default changed to 30 in issue #203
        engine = self._make_engine(half_life=14)
        item = FakeItem(score=0.8, created_at=datetime.now(UTC) - timedelta(days=14))
        result = engine._apply_staleness_penalty([item])
        assert abs(result[0].score - 0.4) < 0.05

    def test_rule_category_exempt(self):
        engine = self._make_engine()
        item = FakeItem(score=0.8, created_at=datetime.now(UTC) - timedelta(days=60), category="rule")
        result = engine._apply_staleness_penalty([item])
        assert result[0].score == 0.8

    def test_preference_category_exempt(self):
        engine = self._make_engine()
        item = FakeItem(score=0.8, created_at=datetime.now(UTC) - timedelta(days=60), category="preference")
        result = engine._apply_staleness_penalty([item])
        assert result[0].score == 0.8

    def test_technical_category_exempt(self):
        engine = self._make_engine()
        item = FakeItem(score=0.8, created_at=datetime.now(UTC) - timedelta(days=60), category="technical")
        result = engine._apply_staleness_penalty([item])
        assert result[0].score == 0.8

    def test_concept_category_exempt(self):
        engine = self._make_engine()
        item = FakeItem(score=0.8, created_at=datetime.now(UTC) - timedelta(days=60), category="concept")
        result = engine._apply_staleness_penalty([item])
        assert result[0].score == 0.8

    def test_30_percent_floor(self):
        engine = self._make_engine()
        item = FakeItem(score=0.8, created_at=datetime.now(UTC) - timedelta(days=365))
        result = engine._apply_staleness_penalty([item])
        assert result[0].score >= 0.24 - 0.01  # 0.8 * 0.3

    def test_disabled_no_penalty(self):
        engine = self._make_engine(enabled=False)
        item = FakeItem(score=0.8, created_at=datetime.now(UTC) - timedelta(days=60))
        result = engine._apply_staleness_penalty([item])
        assert result[0].score == 0.8

    def test_none_score_unchanged(self):
        engine = self._make_engine()
        item = FakeItem(score=None, created_at=datetime.now(UTC) - timedelta(days=30))
        result = engine._apply_staleness_penalty([item])
        assert result[0].score is None
