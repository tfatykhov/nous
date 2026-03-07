"""Tests for _ScoredWrapper and boost writeback."""

from nous.heart.search import apply_frame_boost, _ScoredWrapper, _wrap_with_score


class FakeItem:
    """Minimal item with score and frame attributes."""
    def __init__(self, score, encoded_frame=None, encoded_censors=None, name="item"):
        self.score = score
        self.encoded_frame = encoded_frame
        self.encoded_censors = encoded_censors
        self.name = name
        self.id = name


class TestScoredWrapper:
    def test_score_override(self):
        item = FakeItem(score=0.5)
        wrapped = _ScoredWrapper(item, 0.8)
        assert wrapped.score == 0.8

    def test_delegates_other_attrs(self):
        item = FakeItem(score=0.5, name="test")
        wrapped = _ScoredWrapper(item, 0.8)
        assert wrapped.name == "test"
        assert wrapped.id == "test"

    def test_original_unchanged(self):
        item = FakeItem(score=0.5)
        _ScoredWrapper(item, 0.8)
        assert item.score == 0.5

    def test_wrap_with_score(self):
        item = FakeItem(score=0.5)
        wrapped = _wrap_with_score(item, 0.65)
        assert wrapped.score == 0.65


class TestFrameBoostWriteback:
    def test_boost_writes_score(self):
        items = [
            FakeItem(score=0.5, encoded_frame="task"),
            FakeItem(score=0.7, encoded_frame="debug"),
        ]
        result = apply_frame_boost(items, current_frame="task")
        # First item (task frame) gets 1.3x boost: 0.5 * 1.3 = 0.65
        boosted_item = [r for r in result if r.name == "item"][0]  # the task one
        assert abs(boosted_item.score - 0.65) < 0.01

    def test_no_frame_no_change(self):
        items = [FakeItem(score=0.5)]
        result = apply_frame_boost(items, current_frame=None)
        assert result[0].score == 0.5

    def test_none_score_handled(self):
        item = FakeItem(score=None, encoded_frame="task")
        result = apply_frame_boost([item], current_frame="task")
        assert result[0].score == 0.0
