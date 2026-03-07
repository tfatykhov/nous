"""Tests for diminishing returns cutoff (F017 Phase 2)."""

from unittest.mock import AsyncMock

from nous.cognitive.context import ContextEngine
from nous.config import Settings


class FakeItem:
    def __init__(self, score):
        self.score = score


class TestDiminishingReturnsCutoff:
    def _make_engine(self):
        s = Settings(relevance_drop_ratio=0.6)
        return ContextEngine(AsyncMock(), AsyncMock(), s)

    def test_cuts_at_sharp_drop(self):
        engine = self._make_engine()
        items = [FakeItem(0.82), FakeItem(0.79), FakeItem(0.75), FakeItem(0.31), FakeItem(0.28)]
        result = engine._apply_diminishing_returns_cutoff(items)
        assert len(result) == 3

    def test_no_sharp_drop(self):
        engine = self._make_engine()
        items = [FakeItem(0.8), FakeItem(0.7), FakeItem(0.65), FakeItem(0.6)]
        result = engine._apply_diminishing_returns_cutoff(items)
        assert len(result) == 4

    def test_single_item(self):
        engine = self._make_engine()
        assert len(engine._apply_diminishing_returns_cutoff([FakeItem(0.5)])) == 1

    def test_empty_input(self):
        engine = self._make_engine()
        assert engine._apply_diminishing_returns_cutoff([]) == []

    def test_zero_prev_score_skipped(self):
        engine = self._make_engine()
        items = [FakeItem(0.0), FakeItem(0.5)]
        result = engine._apply_diminishing_returns_cutoff(items)
        assert len(result) == 2  # prev=0 so condition skipped
