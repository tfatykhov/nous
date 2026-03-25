"""Tests for adaptive relevance filtering (replaces F017 floor + diminishing returns)."""

from unittest.mock import AsyncMock

from nous.cognitive.context import ContextEngine, RELEVANCE_MIN_RESULTS, RELEVANCE_MAX_RESULTS
from nous.config import Settings


class FakeItem:
    def __init__(self, score, source=None, id="item"):
        self.score = score
        self.source = source
        self.id = id


class TestRelevanceFilter:
    def _make_engine(self, enabled=True, drop_ratio=0.6, min_overrides=None, max_overrides=None):
        s = Settings(
            relevance_floor_enabled=enabled,
            relevance_drop_ratio=drop_ratio,
            relevance_min_results=min_overrides or {},
            relevance_max_results=max_overrides or {},
        )
        return ContextEngine(AsyncMock(), AsyncMock(), s)

    def test_empty_input(self):
        engine = self._make_engine()
        assert engine._apply_relevance_filter([], "fact") == []

    def test_min_k_guarantee(self):
        """Items <= min_k are always kept regardless of score."""
        engine = self._make_engine()
        min_k = RELEVANCE_MIN_RESULTS["fact"]  # 3
        items = [FakeItem(0.01) for _ in range(min_k)]
        result = engine._apply_relevance_filter(items, "fact")
        assert len(result) == min_k

    def test_max_k_cap(self):
        """Items > max_k are always trimmed."""
        engine = self._make_engine()
        max_k = RELEVANCE_MAX_RESULTS["fact"]  # 8
        items = [FakeItem(0.9 - i * 0.01) for i in range(max_k + 5)]
        result = engine._apply_relevance_filter(items, "fact")
        assert len(result) <= max_k

    def test_gap_detection_within_range(self):
        """Sharp score drop between min_k and max_k triggers cutoff."""
        engine = self._make_engine()
        # For decisions: min_k=2, max_k=5
        items = [
            FakeItem(0.95),
            FakeItem(0.92),
            FakeItem(0.30),  # Sharp drop: 0.30 < 0.92 * 0.6 = 0.552
            FakeItem(0.28),
            FakeItem(0.25),
        ]
        result = engine._apply_relevance_filter(items, "decision")
        assert len(result) == 2  # Keeps min_k items before the gap

    def test_no_gap_keeps_all_up_to_max(self):
        """Gradual score decline keeps all items up to max_k."""
        engine = self._make_engine()
        # Scores decline gradually (each >= 60% of previous)
        items = [FakeItem(0.9 - i * 0.05) for i in range(5)]
        result = engine._apply_relevance_filter(items, "decision")
        assert len(result) == 5

    def test_disabled_passes_all(self):
        """relevance_floor_enabled=False disables filtering entirely."""
        engine = self._make_engine(enabled=False)
        items = [FakeItem(0.01) for _ in range(20)]
        result = engine._apply_relevance_filter(items, "fact")
        assert len(result) == 20

    def test_exempt_source_survives_gap(self):
        """Items from exempt sources are preserved even beyond a gap cut."""
        engine = self._make_engine()
        items = [
            FakeItem(0.95),
            FakeItem(0.92),
            FakeItem(0.30),  # Gap triggers cut here
            FakeItem(0.10, source="pre_prune_extraction"),  # Exempt — should survive
        ]
        result = engine._apply_relevance_filter(items, "decision")
        # Should keep 2 items before gap + 1 exempt item
        assert len(result) == 3
        assert result[2].source == "pre_prune_extraction"

    def test_exempt_source_skips_gap_check(self):
        """Exempt source items at gap position don't trigger the cut."""
        engine = self._make_engine()
        items = [
            FakeItem(0.95),
            FakeItem(0.92),
            FakeItem(0.10, source="pre_prune_extraction"),  # Low score but exempt
            FakeItem(0.88),  # Should still be kept (gap check resumes)
        ]
        result = engine._apply_relevance_filter(items, "decision")
        assert len(result) == 4

    def test_config_overrides_defaults(self):
        """Settings min/max override module-level defaults."""
        engine = self._make_engine(min_overrides={"fact": 1}, max_overrides={"fact": 2})
        items = [
            FakeItem(0.95),
            FakeItem(0.20),  # Gap: 0.20 < 0.95 * 0.6 = 0.57
            FakeItem(0.18),
        ]
        result = engine._apply_relevance_filter(items, "fact")
        # min_k=1, so gap detection starts at index 1
        assert len(result) == 1

    def test_unknown_type_uses_defaults(self):
        """Unknown memory type uses fallback min=2, max=5."""
        engine = self._make_engine()
        items = [FakeItem(0.9 - i * 0.01) for i in range(3)]
        result = engine._apply_relevance_filter(items, "unknown_type")
        # min_k=2, max_k=5, 3 items with gradual decline → all kept
        assert len(result) == 3

    def test_single_item(self):
        """Single item is always kept."""
        engine = self._make_engine()
        result = engine._apply_relevance_filter([FakeItem(0.5)], "fact")
        assert len(result) == 1
