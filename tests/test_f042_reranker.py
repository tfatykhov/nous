"""F042 Cross-Encoder Reranking — unit tests.

Pure unit tests with no database, no real sentence-transformers model.
Monkeypatch `_load_cross_encoder` and `CROSS_ENCODER_AVAILABLE` on the
reranker module to inject a deterministic fake model.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest

from nous.heart import reranker as reranker_mod
from nous.heart.reranker import cross_encoder_rerank
from nous.heart.schemas import RecallResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_result(id_int: int, summary: str, score: float = 0.1, type_: str = "fact") -> RecallResult:
    """Build a RecallResult fixture with a deterministic UUID."""
    return RecallResult(
        type=type_,
        id=UUID(int=id_int),
        summary=summary,
        score=score,
        metadata={},
    )


class FakeModel:
    """Fake CrossEncoder with a predict(pairs) method returning deterministic floats."""

    def __init__(self, score_fn=None, raises: Exception | None = None):
        self.score_fn = score_fn or (lambda q, d: 0.0)
        self.raises = raises
        self.pairs_seen: list[tuple[str, str]] | None = None
        self.call_count = 0

    def predict(self, pairs):
        self.call_count += 1
        self.pairs_seen = list(pairs)
        if self.raises is not None:
            raise self.raises
        return [float(self.score_fn(q, d)) for (q, d) in pairs]


def install_fake_model(monkeypatch, fake: FakeModel) -> None:
    """Monkeypatch CROSS_ENCODER_AVAILABLE=True and _load_cross_encoder -> fake."""
    monkeypatch.setattr(reranker_mod, "CROSS_ENCODER_AVAILABLE", True)
    monkeypatch.setattr(reranker_mod, "_load_cross_encoder", lambda model_name: fake)


# Default rerank kwargs so each test doesn't repeat boilerplate.
DEFAULT_KWARGS = dict(
    model_name="test-model",
    max_candidates=10,
    text_limit=512,
)


# ---------------------------------------------------------------------------
# 1. Unavailable passthrough
# ---------------------------------------------------------------------------


async def test_reranker_unavailable_passthrough(monkeypatch):
    """When CROSS_ENCODER_AVAILABLE=False, returns the input list unchanged (same object)."""
    monkeypatch.setattr(reranker_mod, "CROSS_ENCODER_AVAILABLE", False)

    candidates = [
        make_result(1, "apple", score=0.3),
        make_result(2, "banana", score=0.5),
    ]
    original_scores = [c.score for c in candidates]

    out = await cross_encoder_rerank(
        query="fruit",
        candidates=candidates,
        text_fn=lambda r: r.summary,
        **DEFAULT_KWARGS,
    )

    assert out is candidates  # object identity preserved
    assert [c.score for c in out] == original_scores  # untouched


# ---------------------------------------------------------------------------
# 2. Empty list
# ---------------------------------------------------------------------------


async def test_reranker_empty_list(monkeypatch):
    """Empty candidates → returns []."""
    fake = FakeModel()
    install_fake_model(monkeypatch, fake)

    out = await cross_encoder_rerank(
        query="anything",
        candidates=[],
        text_fn=lambda r: r.summary,
        **DEFAULT_KWARGS,
    )
    assert out == []
    assert fake.call_count == 0  # short-circuited, model never invoked


# ---------------------------------------------------------------------------
# 3. Single item
# ---------------------------------------------------------------------------


async def test_reranker_single_item(monkeypatch):
    """One candidate → returns it unchanged (no rerank)."""
    fake = FakeModel()
    install_fake_model(monkeypatch, fake)

    lonely = make_result(1, "solo", score=0.42)
    out = await cross_encoder_rerank(
        query="anything",
        candidates=[lonely],
        text_fn=lambda r: r.summary,
        **DEFAULT_KWARGS,
    )

    assert len(out) == 1
    assert out[0] is lonely
    assert out[0].score == 0.42
    assert fake.call_count == 0


# ---------------------------------------------------------------------------
# 4. Empty query passthrough
# ---------------------------------------------------------------------------


async def test_reranker_empty_query(monkeypatch):
    """query='' → passthrough, model never invoked."""
    fake = FakeModel()
    install_fake_model(monkeypatch, fake)

    candidates = [
        make_result(1, "apple", score=0.3),
        make_result(2, "banana", score=0.5),
    ]
    out = await cross_encoder_rerank(
        query="",
        candidates=candidates,
        text_fn=lambda r: r.summary,
        **DEFAULT_KWARGS,
    )
    assert out is candidates
    assert [c.score for c in out] == [0.3, 0.5]
    assert fake.call_count == 0


# ---------------------------------------------------------------------------
# 5. Reorders by CE score
# ---------------------------------------------------------------------------


async def test_reranker_reorders_by_score(monkeypatch):
    """Fake model returns high logit when doc contains query substring → that item moves first."""

    def score_fn(query: str, doc: str) -> float:
        return 5.0 if query in doc else -5.0

    fake = FakeModel(score_fn=score_fn)
    install_fake_model(monkeypatch, fake)

    candidates = [
        make_result(1, "the cat sat on the mat", score=0.1),
        make_result(2, "a dog near a rocket ship", score=0.2),
        make_result(3, "another rocket launch event", score=0.3),
    ]

    out = await cross_encoder_rerank(
        query="rocket",
        candidates=candidates,
        text_fn=lambda r: r.summary,
        **DEFAULT_KWARGS,
    )

    # The two rocket-matching items should be head-sorted before the cat item.
    assert out[0].summary in {"a dog near a rocket ship", "another rocket launch event"}
    assert out[1].summary in {"a dog near a rocket ship", "another rocket launch event"}
    assert out[2].summary == "the cat sat on the mat"
    # Scores are sorted DESC
    assert out[0].score >= out[1].score >= out[2].score


# ---------------------------------------------------------------------------
# 6. Sigmoid scores written in place
# ---------------------------------------------------------------------------


async def test_reranker_writes_sigmoid_scores_in_place(monkeypatch):
    """Every reranked item ends in (0,1); list object identity preserved (same instances)."""

    fake = FakeModel(score_fn=lambda q, d: 2.5)
    install_fake_model(monkeypatch, fake)

    c1 = make_result(1, "alpha", score=0.0)
    c2 = make_result(2, "beta", score=0.0)
    c3 = make_result(3, "gamma", score=0.0)
    candidates = [c1, c2, c3]

    out = await cross_encoder_rerank(
        query="anything",
        candidates=candidates,
        text_fn=lambda r: r.summary,
        **DEFAULT_KWARGS,
    )

    # Same instances returned (mutation)
    assert set(id(c) for c in out) == {id(c1), id(c2), id(c3)}

    for c in out:
        assert 0.0 < c.score < 1.0
    # Mutation propagates to original references
    assert c1.score > 0.0
    assert c2.score > 0.0
    assert c3.score > 0.0


# ---------------------------------------------------------------------------
# 7. Text truncation honored
# ---------------------------------------------------------------------------


async def test_reranker_text_truncation_honored(monkeypatch):
    """Candidate summary longer than text_limit → pair text is truncated before predict()."""
    fake = FakeModel(score_fn=lambda q, d: 1.0)
    install_fake_model(monkeypatch, fake)

    long_summary = "x" * 2000
    candidates = [
        make_result(1, long_summary, score=0.1),
        make_result(2, "short", score=0.2),
    ]

    text_limit = 256
    await cross_encoder_rerank(
        query="q",
        candidates=candidates,
        text_fn=lambda r: r.summary,
        model_name="test-model",
        max_candidates=10,
        text_limit=text_limit,
    )

    assert fake.pairs_seen is not None
    assert len(fake.pairs_seen) == 2
    for _, text in fake.pairs_seen:
        assert len(text) <= text_limit


# ---------------------------------------------------------------------------
# 8. Empty summary sinks to tail of head
# ---------------------------------------------------------------------------


async def test_reranker_empty_summary_sinks_to_tail(monkeypatch):
    """Candidate with summary='' → score=-inf → sinks to last position of head slice."""
    fake = FakeModel(score_fn=lambda q, d: 1.0)  # all non-empty get positive
    install_fake_model(monkeypatch, fake)

    c1 = make_result(1, "alpha", score=0.1)
    c_empty = make_result(2, "", score=0.5)
    c3 = make_result(3, "gamma", score=0.1)
    c4 = make_result(4, "delta", score=0.1)
    candidates = [c1, c_empty, c3, c4]  # 4 items, all in head

    out = await cross_encoder_rerank(
        query="q",
        candidates=candidates,
        text_fn=lambda r: r.summary,
        model_name="test-model",
        max_candidates=10,
        text_limit=512,
    )

    # Empty summary ends up last in the head
    assert out[-1] is c_empty
    assert out[-1].score == float("-inf")
    # The other three got finite sigmoid scores in (0,1)
    for c in out[:-1]:
        assert 0.0 < c.score < 1.0


# ---------------------------------------------------------------------------
# 9. max_candidates head split — tail untouched
# ---------------------------------------------------------------------------


async def test_reranker_max_candidates_head_split(monkeypatch):
    """With max_candidates=3 and 10 items, only head is reranked; tail retains order + scores."""
    # Fake returns inverse scores so reranking definitely reorders the head.
    def score_fn(q, d):
        return float(-ord(d[0]))  # reverse-alphabetical ordering by first char

    fake = FakeModel(score_fn=score_fn)
    install_fake_model(monkeypatch, fake)

    candidates = [make_result(i + 1, chr(ord("a") + i), score=0.01 * (i + 1)) for i in range(10)]
    tail_snapshot = [(c.id, c.summary, c.score) for c in candidates[3:]]

    out = await cross_encoder_rerank(
        query="q",
        candidates=candidates,
        text_fn=lambda r: r.summary,
        model_name="test-model",
        max_candidates=3,
        text_limit=512,
    )

    assert len(out) == 10
    # Head (first 3) got sigmoid-in-place scores
    for c in out[:3]:
        assert 0.0 < c.score < 1.0
    # Tail [3:10] is unchanged in order AND score
    assert [(c.id, c.summary, c.score) for c in out[3:]] == tail_snapshot
    # Fake only saw 3 pairs
    assert fake.pairs_seen is not None
    assert len(fake.pairs_seen) == 3


# ---------------------------------------------------------------------------
# 10. Tie scores stable
# ---------------------------------------------------------------------------


async def test_reranker_tie_scores_stable(monkeypatch):
    """Identical fake scores → Python's stable sort preserves input order."""
    fake = FakeModel(score_fn=lambda q, d: 1.0)
    install_fake_model(monkeypatch, fake)

    c1 = make_result(1, "first", score=0.1)
    c2 = make_result(2, "second", score=0.1)
    c3 = make_result(3, "third", score=0.1)
    candidates = [c1, c2, c3]

    out = await cross_encoder_rerank(
        query="q",
        candidates=candidates,
        text_fn=lambda r: r.summary,
        **DEFAULT_KWARGS,
    )

    # All scores identical → stable sort keeps input order
    assert out[0] is c1
    assert out[1] is c2
    assert out[2] is c3


# ---------------------------------------------------------------------------
# 11. Runs in a thread (asyncio.to_thread is used)
# ---------------------------------------------------------------------------


async def test_reranker_runs_in_thread(monkeypatch):
    """Assert asyncio.to_thread is invoked by the reranker so predict() doesn't block the loop."""
    calls = {"count": 0}
    real_to_thread = asyncio.to_thread

    async def tracking_to_thread(fn, *args, **kwargs):
        calls["count"] += 1
        return await real_to_thread(fn, *args, **kwargs)

    fake = FakeModel(score_fn=lambda q, d: 1.0)
    install_fake_model(monkeypatch, fake)
    # Patch asyncio.to_thread on the reranker module's imported asyncio
    monkeypatch.setattr(reranker_mod.asyncio, "to_thread", tracking_to_thread)

    candidates = [
        make_result(1, "alpha", score=0.1),
        make_result(2, "beta", score=0.2),
    ]
    out = await cross_encoder_rerank(
        query="q",
        candidates=candidates,
        text_fn=lambda r: r.summary,
        **DEFAULT_KWARGS,
    )

    assert calls["count"] == 1
    assert len(out) == 2


# ---------------------------------------------------------------------------
# 12. Predict exception passthrough
# ---------------------------------------------------------------------------


async def test_reranker_predict_exception_passthrough(monkeypatch):
    """fake.predict raises RuntimeError → reranker returns original list, scores untouched."""
    fake = FakeModel(raises=RuntimeError("boom"))
    install_fake_model(monkeypatch, fake)

    c1 = make_result(1, "alpha", score=0.33)
    c2 = make_result(2, "beta", score=0.77)
    candidates = [c1, c2]
    original_scores = [c.score for c in candidates]

    out = await cross_encoder_rerank(
        query="q",
        candidates=candidates,
        text_fn=lambda r: r.summary,
        **DEFAULT_KWARGS,
    )

    assert out is candidates
    assert [c.score for c in out] == original_scores


# ---------------------------------------------------------------------------
# 13. Load failure passthrough
# ---------------------------------------------------------------------------


async def test_reranker_load_failure_passthrough(monkeypatch):
    """_load_cross_encoder raises → reranker returns original list unchanged."""
    monkeypatch.setattr(reranker_mod, "CROSS_ENCODER_AVAILABLE", True)

    def boom(model_name):
        raise RuntimeError("model load failed")

    monkeypatch.setattr(reranker_mod, "_load_cross_encoder", boom)

    c1 = make_result(1, "alpha", score=0.33)
    c2 = make_result(2, "beta", score=0.77)
    candidates = [c1, c2]
    original_scores = [c.score for c in candidates]

    out = await cross_encoder_rerank(
        query="q",
        candidates=candidates,
        text_fn=lambda r: r.summary,
        **DEFAULT_KWARGS,
    )

    assert out is candidates
    assert [c.score for c in out] == original_scores
