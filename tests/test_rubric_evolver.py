"""Tests for F024 Phase 3b rubric evolver handler."""
import uuid
from datetime import datetime, UTC
from unittest.mock import AsyncMock, MagicMock

import pytest

from nous.events import Event


def _mock_rubric_version(version="1.0.0"):
    rv = MagicMock()
    rv.id = uuid.uuid4()
    rv.version = version
    rv.dimensions = [
        {"name": "Recall", "weight": 0.25, "description": "test", "scoring_criteria": "t", "min_weight": 0.10, "max_weight": 0.40},
        {"name": "Tool Selection", "weight": 0.25, "description": "test", "scoring_criteria": "t", "min_weight": 0.10, "max_weight": 0.40},
        {"name": "Confidence Calibration", "weight": 0.25, "description": "test", "scoring_criteria": "t", "min_weight": 0.10, "max_weight": 0.40},
        {"name": "Proactivity", "weight": 0.25, "description": "test", "scoring_criteria": "t", "min_weight": 0.10, "max_weight": 0.40},
    ]
    rv.outcome_correlations = {}
    rv.created_at = datetime.now(UTC)
    return rv


class _AsyncCtx:
    def __init__(self, s):
        self._s = s
    async def __aenter__(self):
        return self._s
    async def __aexit__(self, *a):
        pass


class TestRubricEvolver:
    @pytest.mark.asyncio
    async def test_skip_when_disabled(self):
        from nous.handlers.rubric_evolver import RubricEvolver

        settings = MagicMock()
        settings.rubric_evolution_enabled = False

        evolver = RubricEvolver(
            rubric_manager=MagicMock(),
            db=MagicMock(),
            settings=settings,
            agent_id="test",
        )
        result = await evolver.run_evolution_cycle()
        assert result is None

    @pytest.mark.asyncio
    async def test_skip_when_no_active_rubric(self):
        from nous.handlers.rubric_evolver import RubricEvolver

        settings = MagicMock()
        settings.rubric_evolution_enabled = True

        rubric_mgr = MagicMock()
        rubric_mgr.get_active = AsyncMock(return_value=None)

        evolver = RubricEvolver(
            rubric_manager=rubric_mgr,
            db=MagicMock(),
            settings=settings,
            agent_id="test",
        )
        result = await evolver.run_evolution_cycle()
        assert result is None


class TestGapAnalysis:
    def test_find_gap_episodes(self):
        from nous.handlers.rubric_evolver import RubricEvolver

        episodes = [
            {"episode_id": "a", "scores": {"Recall": 8, "Tool": 8, "Cal": 8, "Pro": 8}, "signals": ["corrected"]},
            {"episode_id": "b", "scores": {"Recall": 9, "Tool": 7, "Cal": 8, "Pro": 9}, "signals": ["corrected", "reworked"]},
            {"episode_id": "c", "scores": {"Recall": 3, "Tool": 4, "Cal": 5, "Pro": 3}, "signals": ["corrected"]},
        ]
        gaps = RubricEvolver.find_gap_episodes(episodes, score_threshold=7)
        assert len(gaps) == 2
        assert gaps[0]["episode_id"] == "a"


class TestAntiGoodhartGuardrail:
    def test_detect_score_inflation(self):
        from nous.handlers.rubric_evolver import RubricEvolver

        episodes = [
            {"scores": {"Recall": 9, "Tool": 9, "Cal": 9, "Pro": 9}, "signals": ["corrected"]},
            {"scores": {"Recall": 8, "Tool": 9, "Cal": 8, "Pro": 9}, "signals": ["reworked"]},
            {"scores": {"Recall": 9, "Tool": 8, "Cal": 9, "Pro": 8}, "signals": ["corrected"]},
        ]
        assert RubricEvolver.check_goodhart(episodes, score_threshold=8) is True

    def test_no_inflation_when_outcomes_good(self):
        from nous.handlers.rubric_evolver import RubricEvolver

        episodes = [
            {"scores": {"Recall": 9, "Tool": 9, "Cal": 9, "Pro": 9}, "signals": ["completed", "praised"]},
            {"scores": {"Recall": 8, "Tool": 9, "Cal": 8, "Pro": 9}, "signals": ["completed"]},
        ]
        assert RubricEvolver.check_goodhart(episodes, score_threshold=8) is False


class TestRollbackTrigger:
    def test_detect_outcome_degradation(self):
        from nous.handlers.rubric_evolver import RubricEvolver

        before = [{"signals": ["completed"]}] * 8 + [{"signals": ["corrected"]}] * 2
        after = [{"signals": ["completed"]}] * 6 + [{"signals": ["corrected"]}] * 4
        assert RubricEvolver.check_degradation(before, after, threshold=0.15) is True

    def test_no_degradation(self):
        from nous.handlers.rubric_evolver import RubricEvolver

        before = [{"signals": ["completed"]}] * 8 + [{"signals": ["corrected"]}] * 2
        after = [{"signals": ["completed"]}] * 8 + [{"signals": ["corrected"]}] * 2
        assert RubricEvolver.check_degradation(before, after, threshold=0.15) is False
