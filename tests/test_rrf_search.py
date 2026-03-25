"""Tests for Reciprocal Rank Fusion (RRF) hybrid search (F025)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from nous.config import Settings
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
