"""Tests for relevance floor filtering (F017 Phase 1)."""

from unittest.mock import AsyncMock

from nous.cognitive.context import ContextEngine
from nous.config import Settings


class FakeItem:
    def __init__(self, score, source=None, id="item"):
        self.score = score
        self.source = source
        self.id = id


class TestRelevanceFloor:
    def _make_engine(self, enabled=True):
        s = Settings(relevance_floor_enabled=enabled)
        return ContextEngine(AsyncMock(), AsyncMock(), s)

    def test_filters_below_floor(self):
        engine = self._make_engine()
        items = [FakeItem(0.6), FakeItem(0.3), FakeItem(0.5)]
        result = engine._apply_relevance_floor(items, "fact")
        assert len(result) == 2
        assert all(r.score >= 0.45 for r in result)

    def test_exempt_source_bypasses_floor(self):
        engine = self._make_engine()
        items = [FakeItem(0.2, source="pre_prune_extraction")]
        result = engine._apply_relevance_floor(items, "fact")
        assert len(result) == 1

    def test_disabled_passes_all(self):
        engine = self._make_engine(enabled=False)
        items = [FakeItem(0.1), FakeItem(0.05)]
        result = engine._apply_relevance_floor(items, "fact")
        assert len(result) == 2

    def test_per_type_floors(self):
        engine = self._make_engine()
        items = [FakeItem(0.48), FakeItem(0.55)]
        result = engine._apply_relevance_floor(items, "procedure")
        assert len(result) == 1
        assert result[0].score == 0.55

    def test_empty_input(self):
        engine = self._make_engine()
        assert engine._apply_relevance_floor([], "fact") == []
