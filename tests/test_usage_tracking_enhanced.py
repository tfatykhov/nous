"""Tests for enhanced usage tracking (F017 Phase 6)."""

from nous.cognitive.usage_tracker import UsageTracker


class TestExtendedBoostRange:
    def test_high_reference_rate_boost(self):
        tracker = UsageTracker()
        for _ in range(5):
            tracker.record_retrieval("m1", "fact", was_referenced=True, overlap_score=0.6)
        boost = tracker.get_boost_factor("m1")
        assert boost > 1.5  # Extended range

    def test_zero_reference_rate_penalty(self):
        tracker = UsageTracker()
        for _ in range(5):
            tracker.record_retrieval("m1", "fact", was_referenced=False, overlap_score=0.0)
        boost = tracker.get_boost_factor("m1")
        assert boost < 0.5  # Extended penalty

    def test_range_upper_bound(self):
        tracker = UsageTracker()
        for _ in range(10):
            tracker.record_retrieval("m1", "fact", was_referenced=True, overlap_score=0.8)
        assert tracker.get_boost_factor("m1") <= 2.0

    def test_range_lower_bound(self):
        tracker = UsageTracker()
        for _ in range(10):
            tracker.record_retrieval("m2", "fact", was_referenced=False, overlap_score=0.0)
        assert tracker.get_boost_factor("m2") >= 0.3

    def test_unknown_memory_returns_1(self):
        tracker = UsageTracker()
        assert tracker.get_boost_factor("unknown") == 1.0

    def test_insufficient_data_returns_1(self):
        tracker = UsageTracker()
        tracker.record_retrieval("m1", "fact", was_referenced=True, overlap_score=0.5)
        assert tracker.get_boost_factor("m1") == 1.0  # needs 2+ retrievals
