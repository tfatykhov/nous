"""Unit tests for UsageTracker — in-memory feedback tracking, no DB needed."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from nous.cognitive.usage_tracker import UsageTracker


# ---------------------------------------------------------------------------
# record + score tests
# ---------------------------------------------------------------------------


def test_record_and_score():
    """Recording a referenced retrieval produces a positive usage score."""
    tracker = UsageTracker()
    tracker.record_retrieval("mem-1", "decision", was_referenced=True)

    score = tracker.get_usage_score("mem-1")
    assert score > 0


def test_decay_over_time():
    """Usage score decays to ~50% after 7 days (one half-life)."""
    tracker = UsageTracker()

    # Record at a known time
    base_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with patch("nous.cognitive.usage_tracker.datetime") as mock_dt:
        mock_dt.now.return_value = base_time
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        tracker.record_retrieval("mem-1", "decision", was_referenced=True)

    # Check score 7 days later
    future_time = base_time + timedelta(days=7)
    with patch("nous.cognitive.usage_tracker.datetime") as mock_dt:
        mock_dt.now.return_value = future_time
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        score = tracker.get_usage_score("mem-1")

    # Half-life = 7 days, so score should be ~0.5
    assert 0.4 <= score <= 0.6, f"Expected ~0.5, got {score}"


# ---------------------------------------------------------------------------
# boost factor tests
# ---------------------------------------------------------------------------


def test_boost_factor_referenced():
    """>50% reference rate gives boost > 1.0."""
    tracker = UsageTracker()
    tracker.record_retrieval("mem-1", "decision", was_referenced=True)
    tracker.record_retrieval("mem-1", "decision", was_referenced=True)
    tracker.record_retrieval("mem-1", "decision", was_referenced=False)

    boost = tracker.get_boost_factor("mem-1")
    # ref_rate = 2/3 = 0.667; boost = 0.5 + 0.667 = 1.167
    assert boost > 1.0


def test_boost_factor_ignored():
    """0% reference rate gives 0.3x penalty."""
    tracker = UsageTracker()
    tracker.record_retrieval("mem-1", "fact", was_referenced=False)
    tracker.record_retrieval("mem-1", "fact", was_referenced=False)
    tracker.record_retrieval("mem-1", "fact", was_referenced=False)

    boost = tracker.get_boost_factor("mem-1")
    # ref_rate = 0/3 = 0; boost = 0.3 + 0 * 1.7 = 0.3
    assert boost == 0.3


def test_unknown_memory_neutral():
    """Unknown memory ID returns neutral boost of 1.0."""
    tracker = UsageTracker()
    boost = tracker.get_boost_factor("never-seen-id")
    assert boost == 1.0


# ---------------------------------------------------------------------------
# overlap computation test
# ---------------------------------------------------------------------------


def test_overlap_computation():
    """Containment coefficient (NOT Jaccard) on known inputs.

    F9: |A intersection B| / min(|A|, |B|)
    Words >= 3 chars:
      context: {the, quick, brown, fox}  (4 words)
      response: {brown, fox, jumps}      (3 words)
      intersection: {brown, fox}         (2 words)
      containment = 2 / min(4, 3) = 2/3 ~= 0.667
    """
    overlap = UsageTracker.compute_overlap(
        "the quick brown fox",
        "brown fox jumps",
    )
    assert abs(overlap - 2 / 3) < 0.01
