"""F030.2 — Per-consumer MMR override on `Heart.recall`.

The `apply_mmr` parameter overrides the global MMR gate so individual
callers (eval scripts now, ContextEngine packing later) can force MMR
on or off regardless of `mmr_enabled` and `mmr_skip_after_ce`.

  - `apply_mmr=None`  → settings-driven (current default behavior).
  - `apply_mmr=True`  → MMR runs, bypassing skip-after-CE.
  - `apply_mmr=False` → MMR does NOT run.

Test pattern mirrors tests/test_f030_1_mmr_skip.py.
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
    mmr_enabled: bool = False,
    mmr_skip_after_ce: bool = True,
    cross_encoder_enabled: bool = True,
) -> Heart:
    db = MagicMock()
    db.session = MagicMock()
    settings = Settings(_env_file=None)
    object.__setattr__(settings, "mmr_enabled", mmr_enabled)
    object.__setattr__(settings, "mmr_skip_after_ce", mmr_skip_after_ce)
    object.__setattr__(settings, "cross_encoder_enabled", cross_encoder_enabled)
    embeddings = AsyncMock()
    embeddings.embed = AsyncMock(return_value=[1.0, 0.0, 0.0])
    return Heart(db, settings, embeddings)


def _two_facts():
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
    RuntimeConfig.reset()
    yield
    RuntimeConfig.reset()


class TestF030_2MMROverride:
    @pytest.mark.asyncio
    async def test_apply_mmr_true_forces_mmr_when_disabled(self):
        """apply_mmr=True runs MMR even when settings.mmr_enabled=False."""
        heart = _make_heart(
            mmr_enabled=False,  # global off
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
                   })):
            mock_mmr.return_value = [
                RecallResult(type="fact", id=r1.id, summary="fact1", score=0.9),
                RecallResult(type="fact", id=r2.id, summary="fact2", score=0.5),
            ]
            await heart._recall(
                "q", 10, None, session, apply_mmr=True,
            )
            mock_ce.assert_not_called()
            mock_mmr.assert_called_once()

    @pytest.mark.asyncio
    async def test_apply_mmr_true_bypasses_skip_after_ce(self):
        """apply_mmr=True runs MMR even when CE reordered + skip_after_ce=True."""
        heart = _make_heart(
            mmr_enabled=False,
            mmr_skip_after_ce=True,
            cross_encoder_enabled=True,
        )
        session = AsyncMock()
        r1, r2 = _two_facts()
        heart.facts.search = AsyncMock(return_value=[r1, r2])
        heart.episodes.search = AsyncMock(return_value=[])
        heart.procedures.search = AsyncMock(return_value=[])
        heart.censors.search = AsyncMock(return_value=[])

        async def fake_ce(query, candidates, text_fn, **kw):
            reordered = list(reversed(candidates))
            for c in reordered:
                c.score = 0.42
            return reordered

        with patch("nous.heart.heart.CROSS_ENCODER_AVAILABLE", True), \
             patch("nous.heart.heart.cross_encoder_rerank",
                   new=AsyncMock(side_effect=fake_ce)) as mock_ce, \
             patch("nous.heart.heart.mmr_rerank") as mock_mmr, \
             patch("nous.heart.heart.batch_fetch_embeddings",
                   new=AsyncMock(return_value={
                       r1.id: [1.0, 0.0, 0.0],
                       r2.id: [0.0, 1.0, 0.0],
                   })):
            mock_mmr.return_value = [
                RecallResult(type="fact", id=r1.id, summary="fact1", score=0.9),
                RecallResult(type="fact", id=r2.id, summary="fact2", score=0.5),
            ]
            await heart._recall(
                "q", 10, None, session, apply_mmr=True,
            )
            mock_ce.assert_awaited_once()
            mock_mmr.assert_called_once()  # bypasses skip_after_ce

    @pytest.mark.asyncio
    async def test_apply_mmr_false_forces_mmr_off_when_enabled(self):
        """apply_mmr=False suppresses MMR even when settings.mmr_enabled=True."""
        heart = _make_heart(
            mmr_enabled=True,  # global on
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
             patch("nous.heart.heart.cross_encoder_rerank"), \
             patch("nous.heart.heart.mmr_rerank") as mock_mmr, \
             patch("nous.heart.heart.batch_fetch_embeddings") as mock_fetch:
            await heart._recall(
                "q", 10, None, session, apply_mmr=False,
            )
            mock_mmr.assert_not_called()
            mock_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_apply_mmr_none_uses_settings(self):
        """apply_mmr=None falls through to existing settings-driven gate."""
        heart = _make_heart(
            mmr_enabled=False,  # off → MMR should NOT run
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
             patch("nous.heart.heart.cross_encoder_rerank"), \
             patch("nous.heart.heart.mmr_rerank") as mock_mmr, \
             patch("nous.heart.heart.batch_fetch_embeddings") as mock_fetch:
            await heart._recall(
                "q", 10, None, session, apply_mmr=None,
            )
            mock_mmr.assert_not_called()
            mock_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_public_recall_threads_apply_mmr(self):
        """The public Heart.recall passes apply_mmr through to _recall."""
        heart = _make_heart(
            mmr_enabled=False,
            cross_encoder_enabled=False,
        )
        session = AsyncMock()
        r1, r2 = _two_facts()
        heart.facts.search = AsyncMock(return_value=[r1, r2])
        heart.episodes.search = AsyncMock(return_value=[])
        heart.procedures.search = AsyncMock(return_value=[])
        heart.censors.search = AsyncMock(return_value=[])

        with patch("nous.heart.heart.CROSS_ENCODER_AVAILABLE", True), \
             patch("nous.heart.heart.cross_encoder_rerank"), \
             patch("nous.heart.heart.mmr_rerank") as mock_mmr, \
             patch("nous.heart.heart.batch_fetch_embeddings",
                   new=AsyncMock(return_value={
                       r1.id: [1.0, 0.0, 0.0],
                       r2.id: [0.0, 1.0, 0.0],
                   })):
            mock_mmr.return_value = [
                RecallResult(type="fact", id=r1.id, summary="fact1", score=0.9),
            ]
            await heart.recall(
                "q", limit=10, session=session, apply_mmr=True,
            )
            mock_mmr.assert_called_once()
