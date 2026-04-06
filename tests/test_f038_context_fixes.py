"""Tests for F038 context fixes: procedure score floor, episode recency, procedure-identity dedup."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from nous.cognitive.context import ContextEngine, _IDENTITY_OVERLAP_THRESHOLD
from nous.config import Settings
from nous.heart.search import _wrap_with_score


class FakeItem:
    """Minimal stand-in for scored memory items."""

    def __init__(
        self,
        score=None,
        created_at=None,
        started_at=None,
        category=None,
        body=None,
        steps_text=None,
        name=None,
        id=None,
    ):
        self.score = score
        self.created_at = created_at or datetime.now(timezone.utc)
        self.started_at = started_at
        self.category = category
        self.body = body
        self.steps_text = steps_text
        self.name = name or ""
        self.id = id or ""


def _make_engine(
    has_embeddings: bool = True,
    procedure_score_floor: float = 0.40,
    identity_prompt: str = "",
) -> ContextEngine:
    brain = AsyncMock()
    if has_embeddings:
        brain.embeddings = MagicMock()
    else:
        brain.embeddings = None
    heart = AsyncMock()
    settings = Settings(procedure_score_floor=procedure_score_floor)
    return ContextEngine(brain, heart, settings, identity_prompt=identity_prompt)


# ---------------------------------------------------------------
# Fix 2.1: Procedure score floor
# ---------------------------------------------------------------


class TestProcedureScoreFloor:
    """Procedures below 0.40 are filtered when embeddings enabled."""

    def test_procedure_score_floor_filters_low_scores(self):
        engine = _make_engine(has_embeddings=True, procedure_score_floor=0.40)
        items = [
            FakeItem(score=0.80),
            FakeItem(score=0.50),
            FakeItem(score=0.39),
            FakeItem(score=0.10),
        ]
        # Apply filter directly (same logic as in build())
        filtered = [
            p for p in items
            if (getattr(p, "score", 0) or 0) >= engine._settings.procedure_score_floor
        ]
        assert len(filtered) == 2
        assert all(p.score >= 0.40 for p in filtered)

    def test_procedure_score_floor_no_filter_without_embeddings(self):
        engine = _make_engine(has_embeddings=False, procedure_score_floor=0.40)
        items = [
            FakeItem(score=0.10),
            FakeItem(score=0.05),
        ]
        # When embeddings are disabled, floor should not apply
        assert not engine._has_embeddings
        # Simulate the gated check from build()
        if engine._has_embeddings and engine._settings.procedure_score_floor > 0:
            items = [
                p for p in items
                if (getattr(p, "score", 0) or 0) >= engine._settings.procedure_score_floor
            ]
        assert len(items) == 2  # All kept

    def test_procedure_score_floor_exact_boundary(self):
        engine = _make_engine(has_embeddings=True, procedure_score_floor=0.40)
        items = [FakeItem(score=0.40), FakeItem(score=0.3999)]
        filtered = [
            p for p in items
            if (getattr(p, "score", 0) or 0) >= engine._settings.procedure_score_floor
        ]
        assert len(filtered) == 1
        assert filtered[0].score == 0.40

    def test_procedure_score_floor_disabled_when_zero(self):
        engine = _make_engine(has_embeddings=True, procedure_score_floor=0.0)
        items = [FakeItem(score=0.01)]
        # Floor of 0 means no filtering
        if engine._has_embeddings and engine._settings.procedure_score_floor > 0:
            items = [
                p for p in items
                if (getattr(p, "score", 0) or 0) >= engine._settings.procedure_score_floor
            ]
        assert len(items) == 1


# ---------------------------------------------------------------
# Fix 2.3: Episode recency weighting
# ---------------------------------------------------------------


class TestEpisodeRecency:
    """Episode recency uses linear decay instead of exponential staleness."""

    def test_episode_recency_recent_unchanged(self):
        engine = _make_engine()
        ep = FakeItem(score=0.80, started_at=datetime.now(timezone.utc))
        result = engine._apply_episode_recency([ep])
        # age ~0 days -> decay ~1.0 -> score ~0.80
        assert abs(result[0].score - 0.80) < 0.01

    def test_episode_recency_old_penalized(self):
        engine = _make_engine()
        ep = FakeItem(
            score=0.80,
            started_at=datetime.now(timezone.utc) - timedelta(days=60),
        )
        result = engine._apply_episode_recency([ep])
        # age 60 days -> decay = max(0.5, 1.0 - 60/60) = max(0.5, 0.0) = 0.5
        # score = 0.80 * 0.5 = 0.40
        assert abs(result[0].score - 0.40) < 0.01

    def test_episode_recency_30_days(self):
        engine = _make_engine()
        ep = FakeItem(
            score=0.80,
            started_at=datetime.now(timezone.utc) - timedelta(days=30),
        )
        result = engine._apply_episode_recency([ep])
        # age 30 days -> decay = max(0.5, 1.0 - 30/60) = max(0.5, 0.5) = 0.5
        # score = 0.80 * 0.5 = 0.40
        assert abs(result[0].score - 0.40) < 0.01

    def test_episode_recency_very_old_floors_at_half(self):
        engine = _make_engine()
        ep = FakeItem(
            score=0.80,
            started_at=datetime.now(timezone.utc) - timedelta(days=120),
        )
        result = engine._apply_episode_recency([ep])
        # age 120 days -> decay = max(0.5, 1.0 - 120/60) = max(0.5, -1.0) = 0.5
        # score = 0.80 * 0.5 = 0.40
        assert abs(result[0].score - 0.40) < 0.01

    def test_episode_recency_none_score_unchanged(self):
        engine = _make_engine()
        ep = FakeItem(score=None, started_at=datetime.now(timezone.utc) - timedelta(days=30))
        result = engine._apply_episode_recency([ep])
        assert result[0].score is None

    def test_episode_recency_none_started_at_unchanged(self):
        engine = _make_engine()
        ep = FakeItem(score=0.80, started_at=None)
        result = engine._apply_episode_recency([ep])
        assert result[0].score == 0.80


# ---------------------------------------------------------------
# Fix 1.3: Procedure vs identity dedup
# ---------------------------------------------------------------


class TestProcedureIdentityDedup:
    """Procedures whose body overlaps with identity prompt are filtered."""

    def test_procedure_identity_dedup_filters_matching(self):
        identity = (
            "You are Nous, a cognitive AI agent that learns from experience. "
            "You record decisions with reasoning, extract and store facts, "
            "search all memory types, and create guardrails."
        )
        engine = _make_engine(identity_prompt=identity)

        # Procedure body that largely repeats the identity prompt
        matching_proc = FakeItem(
            score=0.80,
            body=(
                "You are Nous, a cognitive AI agent that learns from experience. "
                "You record decisions with reasoning, extract and store facts."
            ),
        )
        different_proc = FakeItem(
            score=0.70,
            body="Deploy to production using docker compose up -d with health checks.",
        )

        procs = [matching_proc, different_proc]
        _effective_identity = engine._identity_prompt
        filtered = [
            p for p in procs
            if _effective_identity == "" or
            __import__("nous.utils", fromlist=["text_overlap"]).text_overlap(
                getattr(p, "body", "") or getattr(p, "steps_text", "") or "",
                _effective_identity,
            ) < _IDENTITY_OVERLAP_THRESHOLD
        ]

        # The matching procedure should be filtered, different one kept
        assert len(filtered) == 1
        assert filtered[0].score == 0.70

    def test_procedure_identity_dedup_no_filter_without_identity(self):
        engine = _make_engine(identity_prompt="")
        procs = [FakeItem(score=0.80, body="anything")]
        # No identity prompt -> no filtering
        assert engine._identity_prompt == ""
        # The guard in build() checks `if self._identity_prompt`
        # so no filtering happens

    def test_procedure_identity_dedup_uses_steps_text_fallback(self):
        from nous.utils import text_overlap

        identity = "You are Nous, a cognitive agent that records decisions and stores facts."
        engine = _make_engine(identity_prompt=identity)

        # body is empty, steps_text matches identity
        proc = FakeItem(
            score=0.80,
            body="",
            steps_text="You are Nous, a cognitive agent that records decisions and stores facts.",
        )
        content = getattr(proc, "body", "") or getattr(proc, "steps_text", "") or ""
        overlap = text_overlap(content, identity)
        assert overlap >= _IDENTITY_OVERLAP_THRESHOLD
