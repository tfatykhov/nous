"""F050 — ``_rrf_merge_n`` and ``hybrid_search_multi`` tests.

The MANDATORY regression from plan v2 P1-1 (convergent arch + devil P1) is
``test_rrf_merge_n_n1_byte_identical_to_rrf_merge``. Without byte-identity
between the single-query path (``_rrf_merge`` inside ``hybrid_search``) and
the multi-query N=1 path (``_rrf_merge_n`` inside ``hybrid_search_multi``),
downstream consumers — frame boost (heart.search.apply_frame_boost), MMR
(F030), CE rerank (F042/F045), and the F017 relevance floor — read different
score magnitudes for single- vs multi-query callers and the eval gate decision
becomes incomparable across configs.

These tests will fail with ImportError until the Core agent lands the
multi-query helpers. Until then, the module is skipped at collection time.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

# ---------------------------------------------------------------------------
# Import gate — skip module cleanly if Core hasn't landed yet
# ---------------------------------------------------------------------------

try:
    from nous.heart.search import (  # noqa: F401
        _rrf_merge,
        _rrf_merge_n,
        hybrid_search,
        hybrid_search_multi,
    )
except ImportError:
    pytest.skip(
        "F050 multi-query helpers (_rrf_merge_n / hybrid_search_multi) "
        "not yet landed — Core agent in flight",
        allow_module_level=True,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_ranked(ids: list[UUID]) -> list[tuple[UUID, float]]:
    """Build a (id, score) ranked list with strictly-decreasing dummy scores."""
    return [(uid, 1.0 - 0.1 * i) for i, uid in enumerate(ids)]


# ---------------------------------------------------------------------------
# P1-1: byte-identical regression — _rrf_merge_n vs _rrf_merge
# ---------------------------------------------------------------------------


class TestRrfMergeNByteIdenticalRegression:
    """Plan v2 P1-1 (convergent arch + devil P1).

    When the multi-query path is invoked with N=1 (a single variant), the
    per-id scores produced by ``_rrf_merge_n`` MUST match the per-id scores
    produced by single-query ``_rrf_merge`` to within float tolerance (1e-9).

    Without this guarantee, frame boost / MMR / CE rerank / F017 floor see
    different magnitudes for single- vs multi-query callers — gate decisions
    against the F051 baseline become incomparable.
    """

    def test_rrf_merge_n_n1_byte_identical_to_rrf_merge(self) -> None:
        """N=1 ``_rrf_merge_n`` ≡ single-query ``_rrf_merge`` (per-id score)."""
        # Five docs, ranks 1..5 in BOTH vector and keyword lists (worst case
        # for divergence — every doc lives in both lists at the same rank).
        ids = [uuid4() for _ in range(5)]
        vector_ranked = _make_ranked(ids)
        keyword_ranked = _make_ranked(ids)

        # Single-query path: full _rrf_merge with default vector_weight.
        # _rrf_merge signature: (vector_ranked, keyword_ranked, k, vector_weight, limit)
        single = _rrf_merge(
            vector_ranked, keyword_ranked, k=60, vector_weight=0.7, limit=10
        )

        # Multi-query N=1 path: feed the SAME ranked list as the per-variant
        # output of hybrid_search. Plan §"hybrid_search_multi" specifies that
        # _rrf_merge_n's score is normalized to match _rrf_merge's [0,1] range.
        multi = _rrf_merge_n([single], k=60, limit=10)

        single_dict = dict(single)
        multi_dict = dict(multi)

        # Same set of doc IDs surfaced.
        assert single_dict.keys() == multi_dict.keys(), (
            "N=1 multi must surface the same doc IDs as single-query"
        )

        # Per-id scores identical to within float tolerance.
        for id_, single_score in single_dict.items():
            multi_score = multi_dict[id_]
            assert abs(single_score - multi_score) < 1e-9, (
                f"Score drift on {id_}: single={single_score}, "
                f"multi={multi_score}, delta={single_score - multi_score}"
            )

    def test_rrf_merge_n_n1_score_in_unit_interval(self) -> None:
        """N=1 path must produce scores in [0, 1] — same as _rrf_merge."""
        ids = [uuid4() for _ in range(3)]
        ranked = _make_ranked(ids)
        result = _rrf_merge_n([ranked], k=60, limit=10)
        for _id, score in result:
            assert 0.0 <= score <= 1.0 + 1e-9, (
                f"N=1 score {score} outside [0, 1] — normalization broken"
            )


# ---------------------------------------------------------------------------
# RRF properties (multi-variant)
# ---------------------------------------------------------------------------


class TestRrfMergeNProperties:
    def test_rrf_merge_n_two_variants_higher_score_for_co_occurring_docs(
        self,
    ) -> None:
        """A doc appearing in BOTH variant lists at rank 1 must outrank a
        doc appearing in only ONE variant list at rank 1."""
        co_occurring = uuid4()
        only_in_v1 = uuid4()
        only_in_v2 = uuid4()

        v1 = [(co_occurring, 0.9), (only_in_v1, 0.8)]
        v2 = [(co_occurring, 0.9), (only_in_v2, 0.8)]

        result = _rrf_merge_n([v1, v2], k=60, limit=10)
        score_map = dict(result)

        assert score_map[co_occurring] > score_map[only_in_v1], (
            "Co-occurring doc should outrank single-list doc"
        )
        assert score_map[co_occurring] > score_map[only_in_v2]

    def test_rrf_merge_n_empty_lists_handled(self) -> None:
        """All-empty input must return [] without raising."""
        assert _rrf_merge_n([], k=60, limit=10) == []
        assert _rrf_merge_n([[], [], []], k=60, limit=10) == []

    def test_rrf_merge_n_one_empty_one_populated(self) -> None:
        """Empty lists do not affect ranking of populated lists."""
        ids = [uuid4(), uuid4()]
        populated = _make_ranked(ids)
        with_empty = _rrf_merge_n([populated, []], k=60, limit=10)
        # Same IDs surface regardless of the empty companion list.
        assert {uid for uid, _ in with_empty} == set(ids)

    def test_rrf_merge_n_respects_limit(self) -> None:
        """Result length never exceeds the limit param."""
        ids = [uuid4() for _ in range(20)]
        ranked = _make_ranked(ids)
        result = _rrf_merge_n([ranked], k=60, limit=5)
        assert len(result) == 5

    def test_rrf_merge_n_deterministic(self) -> None:
        """Same input → same output across repeated calls."""
        ids = [uuid4() for _ in range(4)]
        v1 = _make_ranked(ids)
        v2 = _make_ranked(list(reversed(ids)))
        first = _rrf_merge_n([v1, v2], k=60, limit=10)
        for _ in range(3):
            assert _rrf_merge_n([v1, v2], k=60, limit=10) == first


# ---------------------------------------------------------------------------
# hybrid_search_multi — routing
# ---------------------------------------------------------------------------


class TestHybridSearchMultiRouting:
    """``hybrid_search_multi`` must (a) delegate single-element to
    ``hybrid_search`` (avoiding RRF overhead) and (b) union per-variant
    results when N > 1."""

    @pytest.mark.asyncio
    async def test_hybrid_search_multi_single_element_delegates_to_hybrid_search(
        self,
    ) -> None:
        """Single-flight fast path: queries=[(text, emb)] with len=1
        delegates to hybrid_search and returns its result verbatim."""
        ids = [uuid4(), uuid4()]
        delegate_result = _make_ranked(ids)

        with patch(
            "nous.heart.search.hybrid_search",
            new=AsyncMock(return_value=delegate_result),
        ) as mock_single:
            result = await hybrid_search_multi(
                session=AsyncMock(),
                table="heart.facts",
                queries=[("only-query", [0.1] * 4)],
                agent_id="nous-test",
                limit=10,
            )

        # Delegate called exactly once with the single-element query.
        assert mock_single.await_count == 1
        kwargs = mock_single.await_args.kwargs
        if kwargs:
            assert kwargs.get("query_text") == "only-query"
        # Returned result equals the delegated result (no extra processing).
        assert list(result) == delegate_result

    @pytest.mark.asyncio
    async def test_hybrid_search_multi_three_variants_unioned(self) -> None:
        """N=3 path: hybrid_search called once per variant, results unioned."""
        v1_ids = [uuid4(), uuid4()]
        v2_ids = [uuid4(), uuid4()]
        v3_ids = [uuid4(), uuid4()]

        async def fake_hybrid_search(*args, **kwargs):
            text = kwargs.get("query_text") or (args[3] if len(args) > 3 else "")
            return {
                "v1": _make_ranked(v1_ids),
                "v2": _make_ranked(v2_ids),
                "v3": _make_ranked(v3_ids),
            }.get(text, [])

        with patch(
            "nous.heart.search.hybrid_search",
            new=AsyncMock(side_effect=fake_hybrid_search),
        ) as mock_single:
            result = await hybrid_search_multi(
                session=AsyncMock(),
                table="heart.facts",
                queries=[
                    ("v1", [0.1] * 4),
                    ("v2", [0.2] * 4),
                    ("v3", [0.3] * 4),
                ],
                agent_id="nous-test",
                limit=20,
            )

        # One hybrid_search call per variant.
        assert mock_single.await_count == 3
        # All six unique IDs surface (fused, deduped).
        result_ids = {uid for uid, _ in result}
        expected = set(v1_ids) | set(v2_ids) | set(v3_ids)
        assert result_ids == expected

    @pytest.mark.asyncio
    async def test_hybrid_search_multi_empty_queries_returns_empty(self) -> None:
        """Empty queries list → no hybrid_search calls, empty result."""
        with patch(
            "nous.heart.search.hybrid_search",
            new=AsyncMock(return_value=[]),
        ) as mock_single:
            result = await hybrid_search_multi(
                session=AsyncMock(),
                table="heart.facts",
                queries=[],
                agent_id="nous-test",
                limit=10,
            )
        assert mock_single.await_count == 0
        assert list(result) == []
