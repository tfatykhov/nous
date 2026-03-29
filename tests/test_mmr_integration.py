"""Integration tests for MMR in Heart._recall pipeline (F030)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from nous.config import Settings
from nous.heart.heart import Heart
from nous.heart.schemas import FactSummary, RecallResult


def _make_heart(mmr_enabled: bool = True, mmr_weight: float = 0.7) -> Heart:
    """Create a Heart instance with mocked dependencies."""
    db = MagicMock()
    db.session = MagicMock()
    settings = Settings()
    object.__setattr__(settings, "mmr_enabled", mmr_enabled)
    object.__setattr__(settings, "mmr_diversity_weight", mmr_weight)
    embeddings = AsyncMock()
    embeddings.embed = AsyncMock(return_value=[1.0, 0.0, 0.0])
    heart = Heart(db, settings, embeddings)
    return heart


class TestRecallWithMMR:
    @pytest.mark.asyncio
    async def test_mmr_disabled_uses_score_sort(self):
        """When MMR is disabled, _recall returns score-sorted results."""
        heart = _make_heart(mmr_enabled=False)
        session = AsyncMock()

        r1 = FactSummary(id=uuid4(), content="fact1", subject="s", category="c",
                         confidence=0.8, active=True, score=0.5)
        r2 = FactSummary(id=uuid4(), content="fact2", subject="s2", category="c2",
                         confidence=0.7, active=True, score=0.9)

        heart.facts.search = AsyncMock(return_value=[r1, r2])
        heart.episodes.search = AsyncMock(return_value=[])
        heart.procedures.search = AsyncMock(return_value=[])
        heart.censors.search = AsyncMock(return_value=[])

        results = await heart._recall("test query", 10, None, session)
        assert len(results) == 2
        assert results[0].score >= results[1].score

    @pytest.mark.asyncio
    async def test_mmr_enabled_calls_rerank(self):
        """When MMR is enabled, _recall calls mmr_rerank."""
        heart = _make_heart(mmr_enabled=True)
        session = AsyncMock()

        r1 = FactSummary(id=uuid4(), content="fact1", subject="s", category="c",
                         confidence=0.8, active=True, score=0.9)
        r2 = FactSummary(id=uuid4(), content="fact2", subject="s2", category="c2",
                         confidence=0.7, active=True, score=0.5)

        heart.facts.search = AsyncMock(return_value=[r1, r2])
        heart.episodes.search = AsyncMock(return_value=[])
        heart.procedures.search = AsyncMock(return_value=[])
        heart.censors.search = AsyncMock(return_value=[])

        with patch("nous.heart.heart.batch_fetch_embeddings") as mock_fetch, \
             patch("nous.heart.heart.mmr_rerank") as mock_mmr:
            mock_fetch.return_value = {
                r1.id: [1.0, 0.0, 0.0],
                r2.id: [0.0, 1.0, 0.0],
            }
            mock_mmr.return_value = [
                RecallResult(type="fact", id=r2.id, summary="fact2", score=0.5),
                RecallResult(type="fact", id=r1.id, summary="fact1", score=0.9),
            ]
            results = await heart._recall("test query", 10, None, session)

            mock_fetch.assert_called_once()
            mock_mmr.assert_called_once()
            assert len(results) == 2

    @pytest.mark.asyncio
    async def test_mmr_skipped_with_single_result(self):
        """MMR should not run with 0 or 1 candidates."""
        heart = _make_heart(mmr_enabled=True)
        session = AsyncMock()

        r1 = FactSummary(id=uuid4(), content="only fact", subject="s", category="c",
                         confidence=0.8, active=True, score=0.9)

        heart.facts.search = AsyncMock(return_value=[r1])
        heart.episodes.search = AsyncMock(return_value=[])
        heart.procedures.search = AsyncMock(return_value=[])
        heart.censors.search = AsyncMock(return_value=[])

        with patch("nous.heart.heart.batch_fetch_embeddings") as mock_fetch:
            results = await heart._recall("test query", 10, None, session)
            mock_fetch.assert_not_called()
            assert len(results) == 1

    @pytest.mark.asyncio
    async def test_mmr_graceful_on_embedding_failure(self):
        """If embedding provider fails, fall back to score sort."""
        heart = _make_heart(mmr_enabled=True)
        heart._embeddings.embed = AsyncMock(side_effect=Exception("API down"))
        session = AsyncMock()

        r1 = FactSummary(id=uuid4(), content="f1", subject="s", category="c",
                         confidence=0.8, active=True, score=0.9)
        r2 = FactSummary(id=uuid4(), content="f2", subject="s", category="c",
                         confidence=0.8, active=True, score=0.7)

        heart.facts.search = AsyncMock(return_value=[r1, r2])
        heart.episodes.search = AsyncMock(return_value=[])
        heart.procedures.search = AsyncMock(return_value=[])
        heart.censors.search = AsyncMock(return_value=[])

        with patch("nous.heart.heart.batch_fetch_embeddings") as mock_fetch:
            mock_fetch.return_value = {}
            results = await heart._recall("test query", 10, None, session)
            # Should still return results (fallback to score sort)
            assert len(results) == 2
            assert results[0].score >= results[1].score

    @pytest.mark.asyncio
    async def test_mmr_skipped_without_embedding_provider(self):
        """If no embedding provider, skip MMR even if enabled."""
        heart = _make_heart(mmr_enabled=True)
        heart._embeddings = None
        session = AsyncMock()

        r1 = FactSummary(id=uuid4(), content="f1", subject="s", category="c",
                         confidence=0.8, active=True, score=0.9)
        r2 = FactSummary(id=uuid4(), content="f2", subject="s", category="c",
                         confidence=0.8, active=True, score=0.7)

        heart.facts.search = AsyncMock(return_value=[r1, r2])
        heart.episodes.search = AsyncMock(return_value=[])
        heart.procedures.search = AsyncMock(return_value=[])
        heart.censors.search = AsyncMock(return_value=[])

        results = await heart._recall("test query", 10, None, session)
        assert len(results) == 2
        assert results[0].score >= results[1].score
