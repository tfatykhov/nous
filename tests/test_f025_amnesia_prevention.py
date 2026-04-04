"""Tests for F025 Amnesia Prevention Phase 2+3."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from nous.cognitive.context import ContextEngine
from nous.config import Settings


@dataclass
class MockMemoryItem:
    """Minimal mock for memory items passed through staleness pipeline."""

    score: float | None = 0.8
    created_at: datetime | None = None
    category: str = ""


def _make_engine(
    staleness_penalty_enabled: bool = True,
    staleness_half_life_days: int = 20,
    budget_scale_enabled: bool = True,
    model: str = "claude-sonnet-4-6",
    context_window: int = 0,
) -> ContextEngine:
    """Create a minimal ContextEngine with settings.

    Uses object.__setattr__ to override context_window after construction
    because pydantic-settings validation_alias fields cannot be passed as
    keyword args with the Python field name.
    """
    settings = Settings(
        staleness_penalty_enabled=staleness_penalty_enabled,
        staleness_half_life_days=staleness_half_life_days,
        budget_scale_enabled=budget_scale_enabled,
        model=model,
    )
    if context_window > 0:
        object.__setattr__(settings, "context_window", context_window)
    return ContextEngine(brain=AsyncMock(), heart=AsyncMock(), settings=settings)


class TestStalenessPersonExemption:
    """P2-A: Person facts should be exempt from staleness penalty."""

    def test_person_category_exempt(self):
        """Old person facts keep original score."""
        engine = _make_engine()
        old_date = datetime.now(timezone.utc) - timedelta(days=60)
        items = [MockMemoryItem(score=0.8, created_at=old_date, category="person")]
        result = engine._apply_staleness_penalty(items)
        assert result[0].score == 0.8

    def test_rule_still_exempt(self):
        """Existing rule exemption unchanged."""
        engine = _make_engine()
        old_date = datetime.now(timezone.utc) - timedelta(days=60)
        items = [MockMemoryItem(score=0.8, created_at=old_date, category="rule")]
        result = engine._apply_staleness_penalty(items)
        assert result[0].score == 0.8

    def test_preference_still_exempt(self):
        """Existing preference exemption unchanged."""
        engine = _make_engine()
        old_date = datetime.now(timezone.utc) - timedelta(days=60)
        items = [MockMemoryItem(score=0.8, created_at=old_date, category="preference")]
        result = engine._apply_staleness_penalty(items)
        assert result[0].score == 0.8

    def test_technical_still_exempt(self):
        """Existing technical exemption unchanged."""
        engine = _make_engine()
        old_date = datetime.now(timezone.utc) - timedelta(days=60)
        items = [MockMemoryItem(score=0.8, created_at=old_date, category="technical")]
        result = engine._apply_staleness_penalty(items)
        assert result[0].score == 0.8

    def test_uncategorized_still_decayed(self):
        """Items without exempt category still decay."""
        engine = _make_engine()
        old_date = datetime.now(timezone.utc) - timedelta(days=60)
        items = [MockMemoryItem(score=0.8, created_at=old_date, category="other")]
        result = engine._apply_staleness_penalty(items)
        assert result[0].score < 0.8

    def test_tool_category_still_decayed(self):
        """Tool facts should still decay (they genuinely go stale)."""
        engine = _make_engine()
        old_date = datetime.now(timezone.utc) - timedelta(days=60)
        items = [MockMemoryItem(score=0.8, created_at=old_date, category="tool")]
        result = engine._apply_staleness_penalty(items)
        assert result[0].score < 0.8

    def test_staleness_disabled_skips_all(self):
        """When staleness_penalty_enabled=False, no decay applied."""
        engine = _make_engine(staleness_penalty_enabled=False)
        old_date = datetime.now(timezone.utc) - timedelta(days=100)
        items = [MockMemoryItem(score=0.8, created_at=old_date, category="other")]
        result = engine._apply_staleness_penalty(items)
        assert result[0].score == 0.8

    def test_staleness_minimum_floor_030(self):
        """Decay should never go below 0.3 floor."""
        engine = _make_engine()
        very_old = datetime.now(timezone.utc) - timedelta(days=365)
        items = [MockMemoryItem(score=1.0, created_at=very_old, category="other")]
        result = engine._apply_staleness_penalty(items)
        assert result[0].score == pytest.approx(0.3, abs=0.01)

    def test_no_score_items_pass_through(self):
        """Items without score pass through unchanged."""
        engine = _make_engine()
        items = [MockMemoryItem(score=None, created_at=datetime.now(timezone.utc))]
        result = engine._apply_staleness_penalty(items)
        assert result[0].score is None


class TestUserProfileBudgetScaling:
    """P2-B: user_profile budget should pass through _scaled_budget."""

    def test_scaled_budget_at_1m_window(self):
        """200 tokens -> 500 at 1M context window."""
        engine = _make_engine(budget_scale_enabled=True, context_window=1_000_000)
        assert engine._scaled_budget(200) == 500

    def test_scaled_budget_at_200k_window(self):
        """200 tokens -> 300 at 200K context window."""
        engine = _make_engine(budget_scale_enabled=True, context_window=200_000)
        assert engine._scaled_budget(200) == 300

    def test_scaled_budget_disabled(self):
        """When disabled, returns base budget."""
        engine = _make_engine(budget_scale_enabled=False)
        assert engine._scaled_budget(200) == 200

    def test_user_profile_scaling_in_source(self):
        """Verify context.py applies _scaled_budget to user_profile."""
        source = inspect.getsource(ContextEngine.build)
        assert "_scaled_budget(budget.user_profile)" in source


# ---------------------------------------------------------------------------
# P2-E: Source Text Passthrough for Admission Grounding
# ---------------------------------------------------------------------------


class TestSourceTextPassthrough:
    """P2-E: Fact admission should ground against transcript, not summary."""

    def test_fact_input_has_source_text_field(self):
        from nous.heart.schemas import FactInput
        inp = FactInput(
            content="Tim uses Python",
            subject="Tim",
            source_text="User: I mainly use Python\n\nAssistant: Got it.",
        )
        assert inp.source_text == "User: I mainly use Python\n\nAssistant: Got it."

    def test_fact_input_source_text_defaults_none(self):
        from nous.heart.schemas import FactInput
        inp = FactInput(content="test", subject="test")
        assert inp.source_text is None

    @pytest.mark.asyncio
    async def test_get_source_text_prefers_source_text_field(self):
        from nous.heart.facts import FactManager
        manager = FactManager.__new__(FactManager)

        inp = MagicMock()
        inp.source_text = "the original transcript text"
        inp.source_episode_id = None

        session = AsyncMock()
        result = await manager._get_source_text(inp, session)
        assert result == "the original transcript text"
        session.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_source_text_falls_back_to_episode_summary(self):
        from nous.heart.facts import FactManager

        manager = FactManager.__new__(FactManager)

        inp = MagicMock()
        inp.source_text = None
        inp.source_episode_id = uuid4()

        mock_episode = MagicMock()
        mock_episode.transcript = None  # F025 P3-C: no transcript available
        mock_episode.summary = "episode summary text"

        session = AsyncMock()
        session.get = AsyncMock(return_value=mock_episode)

        result = await manager._get_source_text(inp, session)
        assert result == "episode summary text"

    @pytest.mark.asyncio
    async def test_get_source_text_returns_none_when_nothing_available(self):
        from nous.heart.facts import FactManager

        manager = FactManager.__new__(FactManager)

        inp = MagicMock()
        inp.source_text = None
        inp.source_episode_id = None

        session = AsyncMock()
        result = await manager._get_source_text(inp, session)
        assert result is None


class TestTranscriptPersistence:
    """P3-C: Episode model should have transcript column."""

    def test_episode_model_has_transcript(self):
        from nous.storage.models import Episode

        assert hasattr(Episode, "transcript")

    def test_episode_transcript_nullable(self):
        from nous.storage.models import Episode

        col = Episode.__table__.columns["transcript"]
        assert col.nullable is True

    @pytest.mark.asyncio
    async def test_get_source_text_prefers_transcript_over_summary(self):
        """When no source_text, prefer episode.transcript over episode.summary."""
        from nous.heart.facts import FactManager

        manager = FactManager.__new__(FactManager)

        inp = MagicMock()
        inp.source_text = None
        inp.source_episode_id = uuid4()

        mock_episode = MagicMock()
        mock_episode.transcript = "full transcript text here"
        mock_episode.summary = "short summary"

        session = AsyncMock()
        session.get = AsyncMock(return_value=mock_episode)

        result = await manager._get_source_text(inp, session)
        assert result == "full transcript text here"

    @pytest.mark.asyncio
    async def test_get_source_text_falls_back_to_summary_when_no_transcript(self):
        """When transcript is None, fall back to summary."""
        from nous.heart.facts import FactManager

        manager = FactManager.__new__(FactManager)

        inp = MagicMock()
        inp.source_text = None
        inp.source_episode_id = uuid4()

        mock_episode = MagicMock()
        mock_episode.transcript = None
        mock_episode.summary = "summary text"

        session = AsyncMock()
        session.get = AsyncMock(return_value=mock_episode)

        result = await manager._get_source_text(inp, session)
        assert result == "summary text"

    @pytest.mark.asyncio
    async def test_source_text_still_takes_priority(self):
        """source_text > transcript > summary."""
        from nous.heart.facts import FactManager

        manager = FactManager.__new__(FactManager)

        inp = MagicMock()
        inp.source_text = "from FactInput directly"
        inp.source_episode_id = None

        session = AsyncMock()
        result = await manager._get_source_text(inp, session)
        assert result == "from FactInput directly"
