"""Tests for F023 Memory Admission Control (A-MAC).

Unit tests for the AdmissionController scoring dimensions,
ROUGE-L grounding, bypass logic, and shadow mode.
"""

import math
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from nous.heart.admission import (
    DEFAULT_WEIGHTS,
    AdmissionConfig,
    AdmissionController,
)
from nous.heart.schemas import FactInput

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fact(
    content: str = "Tim prefers dark mode in his IDE",
    category: str | None = "preference",
    subject: str | None = "Tim",
    confidence: float = 0.9,
    source: str | None = "fact_extractor",
    **kwargs,
) -> FactInput:
    return FactInput(
        content=content,
        category=category,
        subject=subject,
        confidence=confidence,
        source=source,
        **kwargs,
    )


def _controller(**overrides) -> AdmissionController:
    config = AdmissionConfig(**overrides)
    return AdmissionController(config=config)


# ---------------------------------------------------------------------------
# ROUGE-L / LCS
# ---------------------------------------------------------------------------


class TestRougeL:
    def test_exact_match(self):
        ctrl = _controller()
        score = ctrl._rouge_l_score("hello world", "hello world")
        assert score == 1.0

    def test_no_overlap(self):
        ctrl = _controller()
        score = ctrl._rouge_l_score("hello world", "foo bar baz")
        assert score == 0.0

    def test_partial_overlap(self):
        ctrl = _controller()
        # "Tim prefers dark mode" vs source containing those words
        fact_text = "Tim prefers dark mode"
        source_text = "In our conversation Tim mentioned he prefers dark mode for coding"
        score = ctrl._rouge_l_score(fact_text, source_text)
        # LCS should find "Tim prefers dark mode" (4 tokens) in source
        assert score > 0.5

    def test_empty_fact(self):
        ctrl = _controller()
        score = ctrl._rouge_l_score("", "hello world")
        assert score == 0.5  # Neutral fallback

    def test_empty_source(self):
        ctrl = _controller()
        score = ctrl._rouge_l_score("hello world", "")
        assert score == 0.5  # Neutral fallback


class TestLCS:
    def test_empty(self):
        ctrl = _controller()
        assert ctrl._lcs_length([], []) == 0

    def test_single_match(self):
        ctrl = _controller()
        assert ctrl._lcs_length(["a"], ["a"]) == 1

    def test_no_match(self):
        ctrl = _controller()
        assert ctrl._lcs_length(["a"], ["b"]) == 0

    def test_subsequence(self):
        ctrl = _controller()
        assert ctrl._lcs_length(["a", "b", "c"], ["a", "x", "b", "y", "c"]) == 3


# ---------------------------------------------------------------------------
# Type Prior
# ---------------------------------------------------------------------------


class TestTypePrior:
    def test_known_categories(self):
        ctrl = _controller()
        assert ctrl._score_type_prior(_fact(category="rule")) == 0.95
        assert ctrl._score_type_prior(_fact(category="preference")) == 0.90
        assert ctrl._score_type_prior(_fact(category="person")) == 0.85
        assert ctrl._score_type_prior(_fact(category="technical")) == 0.70
        assert ctrl._score_type_prior(_fact(category="tool")) == 0.65
        assert ctrl._score_type_prior(_fact(category="concept")) == 0.60

    def test_unknown_category(self):
        ctrl = _controller()
        assert ctrl._score_type_prior(_fact(category="other")) == 0.50

    def test_none_category(self):
        ctrl = _controller()
        assert ctrl._score_type_prior(_fact(category=None)) == 0.50


# ---------------------------------------------------------------------------
# Recency
# ---------------------------------------------------------------------------


class TestRecency:
    def test_current_conversation(self):
        """No source_timestamp -> hours=0 -> score ~ 1.0."""
        ctrl = _controller()
        score = ctrl._score_recency(_fact())
        assert score == pytest.approx(1.0, abs=0.01)

    def test_three_days_ago(self):
        """~69 hours -> half-life -> score ~ 0.5."""
        ctrl = _controller()
        ts = datetime.now(UTC) - timedelta(hours=69)
        score = ctrl._score_recency(_fact(source_timestamp=ts))
        assert score == pytest.approx(0.5, abs=0.05)

    def test_one_week_ago(self):
        """168 hours -> score ~ 0.19."""
        ctrl = _controller()
        ts = datetime.now(UTC) - timedelta(hours=168)
        score = ctrl._score_recency(_fact(source_timestamp=ts))
        assert score == pytest.approx(0.19, abs=0.05)

    def test_custom_lambda(self):
        """Faster decay with higher lambda."""
        ctrl = _controller(recency_lambda=0.1)
        ts = datetime.now(UTC) - timedelta(hours=10)
        score = ctrl._score_recency(_fact(source_timestamp=ts))
        expected = math.exp(-0.1 * 10)
        assert score == pytest.approx(expected, abs=0.01)


# ---------------------------------------------------------------------------
# Novelty
# ---------------------------------------------------------------------------


class TestNovelty:
    def test_no_existing_facts(self):
        """No similarity data -> neutral 0.5."""
        ctrl = _controller()
        assert ctrl._score_novelty(None) == 0.5

    def test_highly_similar(self):
        """0.90 similarity -> 0.10 novelty."""
        ctrl = _controller()
        assert ctrl._score_novelty(0.90) == pytest.approx(0.10, abs=0.01)

    def test_moderately_similar(self):
        """0.50 similarity -> 0.50 novelty."""
        ctrl = _controller()
        assert ctrl._score_novelty(0.50) == pytest.approx(0.50, abs=0.01)

    def test_completely_novel(self):
        """0.0 similarity -> 1.0 novelty."""
        ctrl = _controller()
        assert ctrl._score_novelty(0.0) == pytest.approx(1.0, abs=0.01)


# ---------------------------------------------------------------------------
# Confidence (ROUGE-L grounding)
# ---------------------------------------------------------------------------


class TestConfidence:
    def test_with_source_text(self):
        """Source text available -> ROUGE-L score."""
        ctrl = _controller()
        fact = _fact(content="Tim prefers dark mode")
        source = "Tim mentioned he prefers dark mode for coding"
        score = ctrl._score_confidence(fact, source)
        assert score > 0.5  # Good grounding

    def test_no_source_text_knowledge_extractor(self):
        """No source -> fallback penalty for knowledge_extractor."""
        ctrl = _controller()
        fact = _fact(source="knowledge_extractor", confidence=0.8)
        score = ctrl._score_confidence(fact, None)
        assert score == pytest.approx(0.70, abs=0.01)  # 0.8 - 0.10

    def test_no_source_text_sleep_reflection(self):
        """Sleep reflection gets highest penalty."""
        ctrl = _controller()
        fact = _fact(source="sleep_reflection", confidence=0.8)
        score = ctrl._score_confidence(fact, None)
        assert score == pytest.approx(0.65, abs=0.01)  # 0.8 - 0.15

    def test_no_source_text_no_penalty(self):
        """Unknown source without source text -> use raw confidence."""
        ctrl = _controller()
        fact = _fact(source="some_other_source", confidence=0.8)
        score = ctrl._score_confidence(fact, None)
        assert score == pytest.approx(0.80, abs=0.01)


# ---------------------------------------------------------------------------
# Utility — Heuristic
# ---------------------------------------------------------------------------


class TestUtilityHeuristic:
    def test_baseline(self):
        ctrl = _controller(utility_llm_enabled=False)
        fact = _fact(content="A basic fact about something", subject=None, tags=[])
        score = ctrl._heuristic_utility_score(fact)
        assert score == pytest.approx(0.5, abs=0.01)

    def test_subject_bonus(self):
        ctrl = _controller(utility_llm_enabled=False)
        fact = _fact(subject="Tim")
        score = ctrl._heuristic_utility_score(fact)
        assert score > 0.5

    def test_tags_bonus(self):
        ctrl = _controller(utility_llm_enabled=False)
        fact = _fact(subject=None, tags=["python", "ide", "settings"])
        score = ctrl._heuristic_utility_score(fact)
        assert score > 0.5

    def test_short_content_penalty(self):
        ctrl = _controller(utility_llm_enabled=False)
        fact = _fact(content="yes", subject=None, tags=[])
        score = ctrl._heuristic_utility_score(fact)
        assert score < 0.5

    def test_long_content_penalty(self):
        ctrl = _controller(utility_llm_enabled=False)
        fact = _fact(content="x " * 300, subject=None, tags=[])
        score = ctrl._heuristic_utility_score(fact)
        assert score < 0.5

    def test_clamped_to_0_1(self):
        ctrl = _controller(utility_llm_enabled=False)
        # Very short, no subject, no tags -> should be low but >= 0
        fact = _fact(content="x", subject=None, tags=[])
        score = ctrl._heuristic_utility_score(fact)
        assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# Utility — LLM
# ---------------------------------------------------------------------------


class TestUtilityLLM:
    @pytest.mark.asyncio
    async def test_llm_score_success(self):
        mock_client = AsyncMock()
        mock_client.complete = AsyncMock(return_value="0.85")
        ctrl = AdmissionController(
            config=AdmissionConfig(utility_llm_enabled=True),
            llm_client=mock_client,
        )
        score = await ctrl._score_utility(_fact())
        assert score == pytest.approx(0.85, abs=0.01)
        mock_client.complete.assert_called_once()

    @pytest.mark.asyncio
    async def test_llm_score_clamp(self):
        mock_client = AsyncMock()
        mock_client.complete = AsyncMock(return_value="1.5")
        ctrl = AdmissionController(
            config=AdmissionConfig(utility_llm_enabled=True),
            llm_client=mock_client,
        )
        score = await ctrl._score_utility(_fact())
        assert score == 1.0

    @pytest.mark.asyncio
    async def test_llm_score_fallback_on_error(self):
        mock_client = AsyncMock()
        mock_client.complete = AsyncMock(side_effect=Exception("API error"))
        ctrl = AdmissionController(
            config=AdmissionConfig(utility_llm_enabled=True),
            llm_client=mock_client,
        )
        score = await ctrl._score_utility(_fact())
        # Falls back to heuristic
        assert 0.0 <= score <= 1.0

    @pytest.mark.asyncio
    async def test_llm_score_fallback_on_unparseable(self):
        mock_client = AsyncMock()
        mock_client.complete = AsyncMock(return_value="I think about 0.7")
        ctrl = AdmissionController(
            config=AdmissionConfig(utility_llm_enabled=True),
            llm_client=mock_client,
        )
        score = await ctrl._score_utility(_fact())
        assert score == 0.5  # Neutral fallback

    @pytest.mark.asyncio
    async def test_llm_disabled_uses_heuristic(self):
        ctrl = _controller(utility_llm_enabled=False)
        score = await ctrl._score_utility(_fact())
        assert 0.0 <= score <= 1.0

    @pytest.mark.asyncio
    async def test_llm_prompt_includes_calibration_anchors(self):
        mock_client = AsyncMock()
        mock_client.complete = AsyncMock(return_value="0.7")
        ctrl = AdmissionController(
            config=AdmissionConfig(utility_llm_enabled=True),
            llm_client=mock_client,
        )
        await ctrl._score_utility(_fact())
        call_args = mock_client.complete.call_args
        _ = call_args.kwargs.get("prompt", "") or call_args.args[0] if call_args.args else ""
        # Check for calibration anchors
        assert "0.9" in str(call_args) or "birthday" in str(call_args)


# ---------------------------------------------------------------------------
# Composite Scoring
# ---------------------------------------------------------------------------


class TestCompositeScoring:
    @pytest.mark.asyncio
    async def test_above_threshold_admits(self):
        """High-quality fact should be admitted."""
        ctrl = _controller(utility_llm_enabled=False, shadow_mode=False)
        result = await ctrl.score(
            fact_input=_fact(category="preference", confidence=0.9),
            embedding=None,
            max_existing_similarity=0.3,
            source_text="Tim mentioned he prefers dark mode for coding",
            session=None,
        )
        assert result.admitted is True
        assert result.composite_score >= 0.55
        assert "ADMIT" in result.explanation
        assert len(result.scores) == 5

    @pytest.mark.asyncio
    async def test_below_threshold_rejects(self):
        """Low-quality fact should be rejected."""
        ctrl = _controller(utility_llm_enabled=False, shadow_mode=False)
        result = await ctrl.score(
            fact_input=_fact(
                content="ok",
                category=None,
                subject=None,
                confidence=0.3,
                source="sleep_reflection",
                tags=[],
            ),
            embedding=None,
            max_existing_similarity=0.89,  # Very similar to existing
            source_text=None,
            session=None,
        )
        assert result.admitted is False
        assert result.composite_score < 0.55
        assert "REJECT" in result.explanation

    @pytest.mark.asyncio
    async def test_threshold_boundary(self):
        """Exact threshold value should admit (>=)."""
        # We can't easily set an exact score, but we test the >= logic
        ctrl = _controller(utility_llm_enabled=False, shadow_mode=False, threshold=0.0)
        result = await ctrl.score(
            fact_input=_fact(),
            embedding=None,
            max_existing_similarity=None,
            source_text=None,
            session=None,
        )
        assert result.admitted is True  # threshold=0.0, any score passes

    @pytest.mark.asyncio
    async def test_weights_sum_correctly(self):
        """Verify weighted sum calculation."""
        ctrl = _controller(utility_llm_enabled=False, shadow_mode=False)
        result = await ctrl.score(
            fact_input=_fact(),
            embedding=None,
            max_existing_similarity=None,
            source_text=None,
            session=None,
        )
        # Manually verify: sum of (weight * score) for each dimension
        w = DEFAULT_WEIGHTS
        expected = sum(w[k] * result.scores[k] for k in w)
        assert result.composite_score == pytest.approx(expected, abs=0.001)


# ---------------------------------------------------------------------------
# Bypass
# ---------------------------------------------------------------------------


class TestBypass:
    @pytest.mark.asyncio
    async def test_user_direct_no_bypass(self):
        """F038-2.4: user_direct facts now go through scoring (no bypass)."""
        ctrl = _controller(shadow_mode=False, utility_llm_enabled=False)
        result = await ctrl.score(
            fact_input=_fact(source="user_direct"),
            embedding=None,
            max_existing_similarity=None,
            source_text=None,
            session=None,
        )
        assert result.admitted is True
        assert result.bypassed is False
        assert len(result.scores) == 5

    @pytest.mark.asyncio
    async def test_user_stated_bypasses(self):
        ctrl = _controller(shadow_mode=False)
        result = await ctrl.score(
            fact_input=_fact(source="user_stated"),
            embedding=None,
            max_existing_similarity=None,
            source_text=None,
            session=None,
        )
        assert result.admitted is True
        assert result.bypassed is True

    @pytest.mark.asyncio
    async def test_supersede_bypasses(self):
        ctrl = _controller(shadow_mode=False)
        result = await ctrl.score(
            fact_input=_fact(source="supersede"),
            embedding=None,
            max_existing_similarity=None,
            source_text=None,
            session=None,
        )
        assert result.admitted is True
        assert result.bypassed is True

    @pytest.mark.asyncio
    async def test_contradict_bypasses(self):
        ctrl = _controller(shadow_mode=False)
        result = await ctrl.score(
            fact_input=_fact(source="contradict"),
            embedding=None,
            max_existing_similarity=None,
            source_text=None,
            session=None,
        )
        assert result.admitted is True
        assert result.bypassed is True

    @pytest.mark.asyncio
    async def test_non_bypass_source_scored(self):
        ctrl = _controller(utility_llm_enabled=False, shadow_mode=False)
        result = await ctrl.score(
            fact_input=_fact(source="fact_extractor"),
            embedding=None,
            max_existing_similarity=None,
            source_text=None,
            session=None,
        )
        assert result.bypassed is False
        assert len(result.scores) == 5


# ---------------------------------------------------------------------------
# Shadow Mode
# ---------------------------------------------------------------------------


class TestShadowMode:
    @pytest.mark.asyncio
    async def test_shadow_always_admits(self):
        ctrl = _controller(utility_llm_enabled=False, shadow_mode=True)
        result = await ctrl.score(
            fact_input=_fact(
                content="ok",
                category=None,
                subject=None,
                confidence=0.1,
                tags=[],
            ),
            embedding=None,
            max_existing_similarity=0.94,
            source_text=None,
            session=None,
        )
        assert result.admitted is True
        assert result.shadow_mode is True

    @pytest.mark.asyncio
    async def test_shadow_logs_would_reject(self):
        ctrl = _controller(utility_llm_enabled=False, shadow_mode=True)
        result = await ctrl.score(
            fact_input=_fact(
                content="ok",
                category=None,
                subject=None,
                confidence=0.1,
                tags=[],
            ),
            embedding=None,
            max_existing_similarity=0.94,
            source_text=None,
            session=None,
        )
        assert "SHADOW_WOULD_REJECT" in result.explanation

    @pytest.mark.asyncio
    async def test_shadow_still_scores(self):
        ctrl = _controller(utility_llm_enabled=False, shadow_mode=True)
        result = await ctrl.score(
            fact_input=_fact(),
            embedding=None,
            max_existing_similarity=None,
            source_text=None,
            session=None,
        )
        assert len(result.scores) == 5
        assert result.composite_score > 0


# ---------------------------------------------------------------------------
# F038-2.4: user_direct Admission Bonus
# ---------------------------------------------------------------------------


class TestUserDirectBonus:
    @pytest.mark.asyncio
    async def test_admission_user_direct_bonus(self):
        """user_direct facts get +0.15 composite bonus."""
        ctrl = _controller(utility_llm_enabled=False, shadow_mode=False)
        # Score same fact with and without user_direct source
        fact_ud = _fact(source="user_direct")
        fact_ext = _fact(source="fact_extractor")

        result_ud = await ctrl.score(
            fact_input=fact_ud, embedding=None,
            max_existing_similarity=None, source_text=None, session=None,
        )
        result_ext = await ctrl.score(
            fact_input=fact_ext, embedding=None,
            max_existing_similarity=None, source_text=None, session=None,
        )
        # user_direct should score 0.15 higher
        assert abs(result_ud.composite_score - result_ext.composite_score - 0.15) < 0.001

    @pytest.mark.asyncio
    async def test_admission_user_direct_short_content_still_admitted(self):
        """A reasonable user_direct fact with the bonus should pass threshold."""
        ctrl = _controller(utility_llm_enabled=False, shadow_mode=False)
        # Short but valid fact — would score lower without bonus
        result = await ctrl.score(
            fact_input=_fact(
                content="Tim uses neovim as his primary editor",
                category="preference",
                subject="Tim",
                confidence=0.9,
                source="user_direct",
            ),
            embedding=None,
            max_existing_similarity=None,
            source_text=None,
            session=None,
        )
        assert result.admitted is True
        assert result.composite_score >= ctrl.config.threshold

    @pytest.mark.asyncio
    async def test_admission_user_direct_bonus_capped_at_1(self):
        """Bonus should not push composite above 1.0."""
        ctrl = _controller(utility_llm_enabled=False, shadow_mode=False)
        result = await ctrl.score(
            fact_input=_fact(
                content="Tim prefers dark mode in his IDE and uses it consistently across all environments",
                category="preference",
                subject="Tim",
                confidence=1.0,
                source="user_direct",
            ),
            embedding=None,
            max_existing_similarity=0.0,  # High novelty
            source_text="Tim prefers dark mode in his IDE and uses it consistently across all environments",
            session=None,
        )
        assert result.composite_score <= 1.0
