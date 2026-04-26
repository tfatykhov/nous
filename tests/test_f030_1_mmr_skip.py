"""F030.1 — Skip MMR when cross-encoder rerank just reordered the head.

The F051 retrieval-eval harness measured that chaining MMR after CE neutralizes
CE's relevance gains (MMR's diversity selection re-picks the top-K from CE's
reordered top-20 by diversity, blowing away the CE order). This test module
verifies the gate logic in `Heart._recall`:

  - CE reordered + flag on  -> MMR is NOT invoked.
  - CE off                  -> MMR IS invoked (cold-path diversity preserved).
  - CE reordered + flag off -> MMR IS invoked (legacy chained behavior).
  - Default Settings has    -> mmr_skip_after_ce = True.

Test pattern mirrors tests/test_mmr_integration.py: mock the four sub-searches,
patch `cross_encoder_rerank`, `CROSS_ENCODER_AVAILABLE`, `mmr_rerank`, and
`batch_fetch_embeddings` at the `nous.heart.heart` module path.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from nous.config import Settings
from nous.heart.heart import Heart
from nous.heart.schemas import FactSummary, RecallResult
from nous.runtime_config import RuntimeConfig


def _make_heart(
    *,
    mmr_enabled: bool = True,
    mmr_skip_after_ce: bool = True,
    cross_encoder_enabled: bool = True,
) -> Heart:
    """Construct a Heart with mocked DB + embeddings and overridden flags."""
    db = MagicMock()
    db.session = MagicMock()
    # _env_file=None bypasses the dev .env (which carries unrelated keys).
    settings = Settings(_env_file=None)
    object.__setattr__(settings, "mmr_enabled", mmr_enabled)
    object.__setattr__(settings, "mmr_skip_after_ce", mmr_skip_after_ce)
    object.__setattr__(settings, "cross_encoder_enabled", cross_encoder_enabled)
    embeddings = AsyncMock()
    embeddings.embed = AsyncMock(return_value=[1.0, 0.0, 0.0])
    return Heart(db, settings, embeddings)


def _two_facts():
    """Two FactSummary fixtures with distinct IDs and scores."""
    r1 = FactSummary(
        id=uuid4(), content="fact1", subject="s1", category="c1",
        confidence=0.8, active=True, score=0.9,
    )
    r2 = FactSummary(
        id=uuid4(), content="fact2", subject="s2", category="c2",
        confidence=0.7, active=True, score=0.5,
    )
    return r1, r2


@pytest.fixture(autouse=True)
def _reset_runtime_config():
    """Ensure RuntimeConfig overrides don't leak between tests."""
    RuntimeConfig.reset()
    yield
    RuntimeConfig.reset()


class TestF030_1MMRSkipAfterCE:
    @pytest.mark.asyncio
    async def test_mmr_skipped_when_ce_reordered(self):
        """CE on + reorders head + skip flag on -> MMR is NOT invoked."""
        heart = _make_heart(
            mmr_enabled=True,
            mmr_skip_after_ce=True,
            cross_encoder_enabled=True,
        )
        session = AsyncMock()
        r1, r2 = _two_facts()

        heart.facts.search = AsyncMock(return_value=[r1, r2])
        heart.episodes.search = AsyncMock(return_value=[])
        heart.procedures.search = AsyncMock(return_value=[])
        heart.censors.search = AsyncMock(return_value=[])

        # Fake CE: swap order so pre_ce_head != post_ce_head -> ce_reordered=True.
        async def fake_ce_rerank(query, candidates, text_fn, **kw):
            reordered = list(reversed(candidates))
            for c in reordered:
                c.score = 0.42  # mutate to simulate sigmoid score
            return reordered

        with patch("nous.heart.heart.CROSS_ENCODER_AVAILABLE", True), \
             patch("nous.heart.heart.cross_encoder_rerank",
                   new=AsyncMock(side_effect=fake_ce_rerank)) as mock_ce, \
             patch("nous.heart.heart.mmr_rerank") as mock_mmr, \
             patch("nous.heart.heart.batch_fetch_embeddings") as mock_fetch:
            results = await heart._recall("test query", 10, None, session)

            mock_ce.assert_awaited_once()
            mock_mmr.assert_not_called()
            mock_fetch.assert_not_called()
            assert len(results) == 2

    @pytest.mark.asyncio
    async def test_mmr_runs_when_ce_disabled(self):
        """CE off + MMR on -> MMR IS invoked (cold-path diversity preserved)."""
        heart = _make_heart(
            mmr_enabled=True,
            mmr_skip_after_ce=True,
            cross_encoder_enabled=False,
        )
        session = AsyncMock()
        r1, r2 = _two_facts()

        heart.facts.search = AsyncMock(return_value=[r1, r2])
        heart.episodes.search = AsyncMock(return_value=[])
        heart.procedures.search = AsyncMock(return_value=[])
        heart.censors.search = AsyncMock(return_value=[])

        with patch("nous.heart.heart.CROSS_ENCODER_AVAILABLE", True), \
             patch("nous.heart.heart.cross_encoder_rerank") as mock_ce, \
             patch("nous.heart.heart.mmr_rerank") as mock_mmr, \
             patch("nous.heart.heart.batch_fetch_embeddings",
                   new=AsyncMock(return_value={
                       r1.id: [1.0, 0.0, 0.0],
                       r2.id: [0.0, 1.0, 0.0],
                   })) as mock_fetch:
            mock_mmr.return_value = [
                RecallResult(type="fact", id=r1.id, summary="fact1", score=0.9),
                RecallResult(type="fact", id=r2.id, summary="fact2", score=0.5),
            ]
            await heart._recall("test query", 10, None, session)

            mock_ce.assert_not_called()
            mock_mmr.assert_called_once()
            mock_fetch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_mmr_runs_when_skip_flag_off(self):
        """CE on + reorders + skip flag OFF -> legacy chained behavior, MMR runs."""
        heart = _make_heart(
            mmr_enabled=True,
            mmr_skip_after_ce=False,  # opt out of F030.1
            cross_encoder_enabled=True,
        )
        session = AsyncMock()
        r1, r2 = _two_facts()

        heart.facts.search = AsyncMock(return_value=[r1, r2])
        heart.episodes.search = AsyncMock(return_value=[])
        heart.procedures.search = AsyncMock(return_value=[])
        heart.censors.search = AsyncMock(return_value=[])

        async def fake_ce_rerank(query, candidates, text_fn, **kw):
            reordered = list(reversed(candidates))
            for c in reordered:
                c.score = 0.42
            return reordered

        with patch("nous.heart.heart.CROSS_ENCODER_AVAILABLE", True), \
             patch("nous.heart.heart.cross_encoder_rerank",
                   new=AsyncMock(side_effect=fake_ce_rerank)) as mock_ce, \
             patch("nous.heart.heart.mmr_rerank") as mock_mmr, \
             patch("nous.heart.heart.batch_fetch_embeddings",
                   new=AsyncMock(return_value={
                       r1.id: [1.0, 0.0, 0.0],
                       r2.id: [0.0, 1.0, 0.0],
                   })) as mock_fetch:
            mock_mmr.return_value = [
                RecallResult(type="fact", id=r2.id, summary="fact2", score=0.42),
                RecallResult(type="fact", id=r1.id, summary="fact1", score=0.42),
            ]
            await heart._recall("test query", 10, None, session)

            mock_ce.assert_awaited_once()
            mock_mmr.assert_called_once()  # legacy chain preserved
            mock_fetch.assert_awaited_once()

    def test_mmr_skip_default_is_true(self):
        """Default Settings ships with mmr_skip_after_ce=True (F051 finding)."""
        s = Settings(_env_file=None)
        assert s.mmr_skip_after_ce is True
