"""Tests for Reciprocal Rank Fusion (RRF) hybrid search (F025)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from nous.config import Settings
from nous.heart.search import _rrf_merge
from nous.runtime_config import RuntimeConfig


class TestRRFConfig:
    def test_rrf_k_default(self):
        s = Settings()
        assert s.rrf_k == 60

    def test_rrf_k_from_env(self, monkeypatch):
        monkeypatch.setenv("NOUS_RRF_K", "40")
        s = Settings()
        assert s.rrf_k == 40


class TestRRFRuntimeConfig:
    def setup_method(self):
        RuntimeConfig.reset()

    def teardown_method(self):
        RuntimeConfig.reset()

    def test_get_rrf_k_default(self):
        rc = RuntimeConfig.get()
        s = Settings()
        assert rc.get_rrf_k(s) == 60

    def test_set_and_get_rrf_k(self):
        rc = RuntimeConfig.get()
        rc.set_rrf_k(40)
        s = Settings()
        assert rc.get_rrf_k(s) == 40
        assert rc.get_rrf_k_source(s) == "runtime_override"

    def test_clear_rrf_k(self):
        rc = RuntimeConfig.get()
        rc.set_rrf_k(40)
        rc.clear_rrf_k()
        s = Settings()
        assert rc.get_rrf_k(s) == 60
        assert rc.get_rrf_k_source(s) == "default"


class TestRRFMerge:
    """Test the pure RRF merge function (no DB needed)."""

    def test_both_lists_same_doc(self):
        """Doc appearing in both lists gets scores from both."""
        from nous.heart.search import _rrf_merge

        doc_a = uuid4()
        vector_ranked = [(doc_a, 0.95)]
        keyword_ranked = [(doc_a, 0.08)]
        result = _rrf_merge(vector_ranked, keyword_ranked, k=60, vector_weight=0.5, limit=10)
        assert len(result) == 1
        assert result[0][0] == doc_a
        # Normalized: (0.5/60 + 0.5/60) / (1/60) = 1.0
        assert abs(result[0][1] - 1.0) < 1e-9

    def test_disjoint_lists(self):
        """Docs in only one list get penalty rank for the other."""
        from nous.heart.search import _rrf_merge

        doc_v = uuid4()
        doc_k = uuid4()
        vector_ranked = [(doc_v, 0.9)]
        keyword_ranked = [(doc_k, 0.08)]
        result = _rrf_merge(vector_ranked, keyword_ranked, k=60, vector_weight=0.5, limit=10)
        assert len(result) == 2
        # penalty rank = limit + 1 = 11
        # Normalized: raw / (1/k) = raw * k
        raw_v = 0.5 / 60 + 0.5 / 71
        raw_k = 0.5 / 71 + 0.5 / 60
        max_score = 1.0 / 60
        assert abs(result[0][1] - raw_v / max_score) < 1e-9
        assert abs(result[1][1] - raw_k / max_score) < 1e-9

    def test_vector_only(self):
        """Empty keyword list — all docs use penalty rank for keyword."""
        from nous.heart.search import _rrf_merge

        doc = uuid4()
        result = _rrf_merge([(doc, 0.9)], [], k=60, vector_weight=0.7, limit=5)
        assert len(result) == 1
        raw = 0.7 / 60 + 0.3 / (60 + 6)
        expected = raw / (1.0 / 60)
        assert abs(result[0][1] - expected) < 1e-9

    def test_keyword_only(self):
        """Empty vector list — all docs use penalty rank for vector."""
        from nous.heart.search import _rrf_merge

        doc = uuid4()
        result = _rrf_merge([], [(doc, 0.05)], k=60, vector_weight=0.7, limit=5)
        assert len(result) == 1
        raw = 0.7 / (60 + 6) + 0.3 / 60
        expected = raw / (1.0 / 60)
        assert abs(result[0][1] - expected) < 1e-9

    def test_both_empty(self):
        """Both lists empty — returns empty."""
        from nous.heart.search import _rrf_merge

        result = _rrf_merge([], [], k=60, vector_weight=0.5, limit=10)
        assert result == []

    def test_limit_respected(self):
        """Result count capped at limit."""
        from nous.heart.search import _rrf_merge

        docs = [(uuid4(), 0.9 - i * 0.01) for i in range(20)]
        result = _rrf_merge(docs, [], k=60, vector_weight=0.7, limit=5)
        assert len(result) == 5

    def test_ordering_by_rrf_score(self):
        """Results sorted by RRF score descending."""
        from nous.heart.search import _rrf_merge

        doc_a, doc_b, doc_c = uuid4(), uuid4(), uuid4()
        vector = [(doc_a, 0.9), (doc_b, 0.8), (doc_c, 0.7)]
        keyword = [(doc_b, 0.08), (doc_c, 0.06), (doc_a, 0.04)]
        result = _rrf_merge(vector, keyword, k=60, vector_weight=0.5, limit=10)
        scores = [s for _, s in result]
        assert scores == sorted(scores, reverse=True)

    def test_normalization_rank1_both_is_1(self):
        """Doc ranked #1 in both lists normalizes to 1.0."""
        from nous.heart.search import _rrf_merge

        doc = uuid4()
        result = _rrf_merge([(doc, 0.9)], [(doc, 0.1)], k=60, vector_weight=0.7, limit=10)
        assert abs(result[0][1] - 1.0) < 1e-9

    def test_normalization_preserves_order(self):
        """Normalization doesn't change relative ranking."""
        from nous.heart.search import _rrf_merge

        docs = [uuid4() for _ in range(5)]
        vector = [(docs[i], 0.9 - i * 0.1) for i in range(5)]
        keyword = [(docs[i], 0.05 - i * 0.01) for i in range(5)]
        result = _rrf_merge(vector, keyword, k=60, vector_weight=0.7, limit=10)
        # Order should match input order since both lists agree
        assert [doc_id for doc_id, _ in result] == docs

    def test_normalization_scores_in_range(self):
        """All normalized scores are between 0 and 1 (before downstream boosts)."""
        from nous.heart.search import _rrf_merge

        docs = [uuid4() for _ in range(10)]
        vector = [(docs[i], 0.9 - i * 0.05) for i in range(10)]
        keyword = [(docs[i], 0.1 - i * 0.005) for i in range(8)]  # fewer keyword results
        result = _rrf_merge(vector, keyword, k=60, vector_weight=0.7, limit=10)
        for _, score in result:
            assert 0.0 <= score <= 1.0, f"Score {score} out of [0,1] range"


class TestPenaltyRankDecoupling:
    """``_rrf_merge`` scores a doc missing from one leg at ``limit + 1``, so a
    row-count limit is also a scoring input. ``hybrid_search`` passed its own
    ``limit`` straight through, which made ``episode_chunk_recall_limit`` a
    scoring knob: raising it 20 -> 30 at NOUS_RRF_K=30 dropped every
    single-leg chunk by ~0.029 and DEMOTED chunks (measured over 60 queries:
    -0.83 chunks in top-10, 0/60 of the newly admitted chunks reaching
    top-10). ``penalty_limit`` pins the penalty base so the two are separable.
    """

    @staticmethod
    def _vector_only_candidates(n=40):
        """n vector hits, zero keyword hits -> every doc takes penalty_rank."""
        return [(uuid4(), 0.9) for _ in range(n)], []

    @pytest.mark.parametrize("limit", [10, 20, 30, 50])
    def test_score_is_invariant_across_limits_when_penalty_pinned(self, limit):
        """THE regression test. With the penalty base pinned, a document's
        score does not move when the row allotment changes."""
        vector, keyword = self._vector_only_candidates()
        target = vector[0][0]

        merged = _rrf_merge(vector, keyword, 30, 0.7, 10, return_limit=limit)

        score = dict(merged)[target]
        assert score == pytest.approx(0.9195, abs=1e-4), (
            "pinning the penalty base must hold the score steady regardless "
            f"of how many rows are returned (limit={limit})"
        )

    def test_score_varies_with_limit_when_not_pinned(self):
        """Negative case — pins the CURRENT coupled behaviour so it cannot be
        silently reintroduced, and proves the parametrized test above is
        actually exercising ``penalty_limit`` rather than passing trivially."""
        vector, keyword = self._vector_only_candidates()
        target = vector[0][0]

        scores = {limit: dict(_rrf_merge(vector, keyword, 30, 0.7, limit))[target] for limit in (10, 20, 30, 50)}

        assert len(set(scores.values())) == 4, (
            f"unpinned, each limit must yield a DIFFERENT score — that coupling is the defect being fixed; got {scores}"
        )
        # Monotonically decreasing: a bigger limit means a worse penalty rank.
        assert scores[10] > scores[20] > scores[30] > scores[50]
        # The specific prod-shaped delta that produced the measured regression.
        assert scores[20] - scores[30] == pytest.approx(0.029, abs=5e-4)

    def test_return_limit_still_controls_row_count(self):
        """Pinning the penalty base must not shrink the result set."""
        vector, keyword = self._vector_only_candidates(n=40)

        assert len(_rrf_merge(vector, keyword, 30, 0.7, 10, return_limit=30)) == 30
        assert len(_rrf_merge(vector, keyword, 30, 0.7, 10)) == 10

    @pytest.mark.asyncio
    async def test_hybrid_search_penalty_limit_defaults_to_coupled(self):
        """``penalty_limit=None`` must leave the other five hybrid_search call
        sites byte-identical — the merge is called with the row limit only."""
        import nous.heart.search as search_mod

        captured: dict = {}

        def _spy(vector, keyword, k, vw, limit, return_limit=None):
            captured["limit"] = limit
            captured["return_limit"] = return_limit
            return []

        session = MagicMock()
        session.execute = AsyncMock(return_value=MagicMock(all=lambda: []))

        with patch.object(search_mod, "_rrf_merge", _spy):
            await search_mod.hybrid_search(
                session, "heart.episode_chunks", [0.1, 0.2], "q", "a", limit=30, active_filter=False
            )

        assert captured["limit"] == 30
        assert captured["return_limit"] is None

    @pytest.mark.asyncio
    async def test_hybrid_search_penalty_limit_pins_merge_base(self):
        """With ``penalty_limit`` set, the merge gets the pinned base as its
        penalty ``limit`` and the row count moves to ``return_limit``."""
        import nous.heart.search as search_mod

        captured: dict = {}

        def _spy(vector, keyword, k, vw, limit, return_limit=None):
            captured["limit"] = limit
            captured["return_limit"] = return_limit
            return []

        session = MagicMock()
        session.execute = AsyncMock(return_value=MagicMock(all=lambda: []))

        with patch.object(search_mod, "_rrf_merge", _spy):
            await search_mod.hybrid_search(
                session, "heart.episode_chunks", [0.1, 0.2], "q", "a", limit=30, active_filter=False, penalty_limit=10
            )

        assert captured["limit"] == 10, "penalty base must be the pinned value"
        assert captured["return_limit"] == 30, "row count must survive pinning"

    @pytest.mark.asyncio
    async def test_chunk_leg_threads_setting_through(self):
        """Wiring test: the F067 chunk leg must actually pass
        ``chunk_rrf_penalty_limit`` down to hybrid_search. Without this the
        setting exists but does nothing — the failure mode that produced the
        original inert-knob bug in the first place."""
        import contextlib
        from types import SimpleNamespace

        import nous.heart.search as search_mod
        from nous.api.retrieval_pipeline import _search_episode_chunks

        captured: dict = {}

        async def _spy(*args, **kwargs):
            captured.update(kwargs)
            return []

        @contextlib.asynccontextmanager
        async def _session():
            yield MagicMock(execute=AsyncMock())

        heart = SimpleNamespace(
            db=SimpleNamespace(session=_session),
            _embeddings=SimpleNamespace(embed=AsyncMock(return_value=[0.1, 0.2])),
            agent_id="a",
        )

        with patch.object(search_mod, "hybrid_search", _spy):
            await _search_episode_chunks(
                heart=heart,
                query="q",
                agent_id="a",
                limit=30,
                settings=SimpleNamespace(
                    chunk_hybrid_search_enabled=True,
                    chunk_rrf_penalty_limit=10,
                ),
            )

        assert captured.get("penalty_limit") == 10, "chunk leg must thread the setting through, else the knob is inert"
        assert captured.get("limit") == 30, "row allotment must be unaffected"

    def test_negative_penalty_limit_is_rejected_at_startup(self):
        """Codex P2. ``penalty_limit`` becomes ``penalty_rank = limit + 1``,
        which is a divisor term in ``1 / (k + penalty_rank)``. At prod's
        NOUS_RRF_K=30 a value of -31 zeroes that denominator and takes the
        whole chunk-recall stage down with ZeroDivisionError; other negatives
        yield scores outside [0,1] or silently empty the result list via
        ``scored[:limit]``. Must fail closed at config load."""
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            Settings(chunk_rrf_penalty_limit=-31)
        with pytest.raises(pydantic.ValidationError):
            Settings(chunk_rrf_penalty_limit=0)

        assert Settings(chunk_rrf_penalty_limit=20).chunk_rrf_penalty_limit == 20
        assert Settings().chunk_rrf_penalty_limit is None

    def test_the_crash_that_bound_guards_against(self):
        """Pins WHY the bound exists — if someone widens it back, this shows
        the concrete failure rather than an abstract 'validation' rule."""
        vector, keyword = self._vector_only_candidates(n=1)

        with pytest.raises(ZeroDivisionError):
            _rrf_merge(vector, keyword, 30, 0.7, -31)
