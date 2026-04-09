"""Tests for MMR diversity re-ranking (F030)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from nous.config import Settings
from nous.heart.schemas import RecallResult
from nous.heart.search import batch_fetch_embeddings, cosine_similarity, mmr_rerank

# --- Helpers ---


def _make_result(score: float = 0.5, mem_type: str = "fact") -> RecallResult:
    """Helper to create a RecallResult with a random ID."""
    return RecallResult(
        type=mem_type,
        id=uuid4(),
        summary=f"test result score={score}",
        score=score,
    )


# --- cosine_similarity ---


class TestCosineSimilarity:
    def test_identical_vectors(self):
        a = [1.0, 0.0, 0.0]
        b = [1.0, 0.0, 0.0]
        assert cosine_similarity(a, b) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        a = [1.0, 0.0, 0.0]
        b = [0.0, 1.0, 0.0]
        assert cosine_similarity(a, b) == pytest.approx(0.0)

    def test_opposite_vectors(self):
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        assert cosine_similarity(a, b) == pytest.approx(-1.0)

    def test_zero_vector_returns_zero(self):
        a = [0.0, 0.0, 0.0]
        b = [1.0, 2.0, 3.0]
        assert cosine_similarity(a, b) == 0.0

    def test_both_zero_vectors(self):
        a = [0.0, 0.0]
        b = [0.0, 0.0]
        assert cosine_similarity(a, b) == 0.0

    def test_high_dimensional(self):
        """1536-dim vectors (text-embedding-3-small size)."""
        a = [1.0] * 1536
        b = [1.0] * 1536
        assert cosine_similarity(a, b) == pytest.approx(1.0, abs=1e-6)

    def test_similar_vectors(self):
        a = [1.0, 2.0, 3.0]
        b = [1.1, 2.1, 3.1]
        result = cosine_similarity(a, b)
        assert 0.99 < result < 1.0


# --- mmr_rerank ---


class TestMMRRerank:
    def test_empty_candidates(self):
        result = mmr_rerank([], {}, [1.0, 0.0, 0.0])
        assert result == []

    def test_single_candidate(self):
        c = _make_result(0.9)
        emb = [1.0, 0.0, 0.0]
        result = mmr_rerank([c], {c.id: emb}, [1.0, 0.0, 0.0])
        assert len(result) == 1
        assert result[0].id == c.id

    def test_lambda_1_preserves_relevance_order(self):
        """λ=1.0 means pure relevance — order by cosine sim to query."""
        c1 = _make_result(0.9)
        c2 = _make_result(0.7)
        c3 = _make_result(0.5)
        embs = {
            c1.id: [1.0, 0.0, 0.0],
            c2.id: [0.9, 0.4, 0.0],
            c3.id: [0.5, 0.5, 0.5],
        }
        query_emb = [1.0, 0.0, 0.0]
        result = mmr_rerank([c1, c2, c3], embs, query_emb, lambda_=1.0, limit=3)
        assert result[0].id == c1.id  # cos=1.0

    def test_clustered_embeddings_get_diversified(self):
        """Two items with identical embeddings — MMR should pick one, then diversify."""
        c1 = _make_result(0.9)
        c2 = _make_result(0.85)
        c3 = _make_result(0.6)
        embs = {
            c1.id: [1.0, 0.0, 0.0],
            c2.id: [1.0, 0.0, 0.0],  # Identical to c1
            c3.id: [0.0, 1.0, 0.0],  # Orthogonal
        }
        query_emb = [0.7, 0.7, 0.0]
        result = mmr_rerank([c1, c2, c3], embs, query_emb, lambda_=0.5, limit=3)
        assert result[0].id == c1.id
        # c3 should be second — c2 is penalized for being identical to c1
        assert result[1].id == c3.id
        assert result[2].id == c2.id

    def test_limit_respected(self):
        candidates = [_make_result(0.9 - i * 0.1) for i in range(5)]
        embs = {c.id: [float(i), 0.0, 0.0] for i, c in enumerate(candidates)}
        query_emb = [1.0, 0.0, 0.0]
        result = mmr_rerank(candidates, embs, query_emb, limit=3)
        assert len(result) == 3

    def test_missing_embeddings_appended_by_score(self):
        """Items without embeddings are appended after MMR-selected items, sorted by score."""
        c1 = _make_result(0.9)
        c2 = _make_result(0.8)  # No embedding
        c3 = _make_result(0.7)
        c4 = _make_result(0.6)  # No embedding
        embs = {
            c1.id: [1.0, 0.0, 0.0],
            c3.id: [0.0, 1.0, 0.0],
        }
        query_emb = [1.0, 0.0, 0.0]
        result = mmr_rerank([c1, c2, c3, c4], embs, query_emb, limit=4)
        assert result[0].id == c1.id
        assert result[1].id == c3.id
        assert result[2].id == c2.id  # score 0.8 > 0.6
        assert result[3].id == c4.id

    def test_no_embeddings_falls_back_to_score_sort(self):
        """When no items have embeddings, fall back to score-sorted order."""
        c1 = _make_result(0.5)
        c2 = _make_result(0.9)
        c3 = _make_result(0.7)
        result = mmr_rerank([c1, c2, c3], {}, [1.0, 0.0, 0.0], limit=3)
        assert result[0].id == c2.id
        assert result[1].id == c3.id
        assert result[2].id == c1.id

    def test_identical_embeddings_no_infinite_loop(self):
        """All items have the same embedding — should not hang."""
        candidates = [_make_result(0.9 - i * 0.1) for i in range(5)]
        embs = {c.id: [1.0, 0.0, 0.0] for c in candidates}
        query_emb = [1.0, 0.0, 0.0]
        result = mmr_rerank(candidates, embs, query_emb, limit=5)
        assert len(result) == 5

    def test_default_lambda(self):
        """Default λ=0.7 should favor relevance but apply some diversity."""
        c1 = _make_result(0.9)
        c2 = _make_result(0.85)
        embs = {
            c1.id: [1.0, 0.0, 0.0],
            c2.id: [0.99, 0.1, 0.0],
        }
        query_emb = [1.0, 0.0, 0.0]
        result = mmr_rerank([c1, c2], embs, query_emb)
        assert len(result) == 2


# --- batch_fetch_embeddings ---


class TestBatchFetchEmbeddings:
    @pytest.mark.asyncio
    async def test_empty_type_ids(self):
        session = AsyncMock()
        result = await batch_fetch_embeddings(session, {}, "test-agent")
        assert result == {}
        session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_groups_queries_by_type(self):
        """Should issue one query per memory type."""
        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.all.return_value = []
        session.execute.return_value = mock_result

        id1, id2 = uuid4(), uuid4()
        type_ids = {"fact": [id1], "episode": [id2]}
        await batch_fetch_embeddings(session, type_ids, "test-agent")
        assert session.execute.call_count == 2

    @pytest.mark.asyncio
    async def test_returns_embedding_dict(self):
        """Should return {id: embedding} dict from DB results."""
        session = AsyncMock()
        id1 = uuid4()
        emb = [0.1] * 10

        mock_row = MagicMock()
        mock_row.id = id1
        mock_row.embedding = json.dumps(emb)

        mock_result = MagicMock()
        mock_result.all.return_value = [mock_row]
        session.execute.return_value = mock_result

        result = await batch_fetch_embeddings(session, {"fact": [id1]}, "test-agent")
        assert id1 in result
        assert result[id1] == emb

    @pytest.mark.asyncio
    async def test_skips_unknown_types(self):
        session = AsyncMock()
        result = await batch_fetch_embeddings(
            session, {"unknown_type": [uuid4()]}, "test-agent"
        )
        assert result == {}
        session.execute.assert_not_called()


# --- Config ---


class TestMMRConfig:
    def test_mmr_disabled_by_default(self):
        s = Settings()
        assert s.mmr_enabled is False

    def test_mmr_enabled_from_env(self, monkeypatch):
        monkeypatch.setenv("NOUS_MMR_ENABLED", "true")
        s = Settings()
        assert s.mmr_enabled is True

    def test_mmr_diversity_weight_default(self):
        s = Settings()
        assert s.mmr_diversity_weight == 0.7

    def test_mmr_diversity_weight_from_env(self, monkeypatch):
        monkeypatch.setenv("NOUS_MMR_DIVERSITY_WEIGHT", "0.5")
        s = Settings()
        assert s.mmr_diversity_weight == 0.5

    def test_mmr_diversity_weight_bounds(self):
        s = Settings()
        assert 0.0 <= s.mmr_diversity_weight <= 1.0
