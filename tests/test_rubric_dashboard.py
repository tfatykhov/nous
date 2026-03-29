"""Tests for rubric dashboard query and endpoint."""

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from nous.storage.models import Episode, OutcomeSignal, RubricVersion


class MockAgentRunner:
    def __init__(self):
        self._conversations = {}

    async def start(self):
        pass

    async def close(self):
        pass


@pytest_asyncio.fixture
async def seed_rubric(db, settings):
    """Seed a rubric version, an episode, and outcome signals."""
    async with db.session() as session:
        rv = RubricVersion(
            agent_id=settings.agent_id,
            version="1.0.0",
            parent_version=None,
            change_reason="Initial rubric",
            dimensions=[
                {"name": "Recall", "weight": 0.25, "description": "Memory retrieval",
                 "scoring_criteria": "1-10", "min_weight": 0.10, "max_weight": 0.40},
                {"name": "Tool Selection", "weight": 0.25, "description": "Tool choice",
                 "scoring_criteria": "1-10", "min_weight": 0.10, "max_weight": 0.40},
                {"name": "Confidence Calibration", "weight": 0.25, "description": "Calibration",
                 "scoring_criteria": "1-10", "min_weight": 0.10, "max_weight": 0.40},
                {"name": "Proactivity", "weight": 0.25, "description": "Anticipation",
                 "scoring_criteria": "1-10", "min_weight": 0.10, "max_weight": 0.40},
            ],
            outcome_correlations={},
            status="active",
        )
        session.add(rv)
        await session.flush()

        # Create a real episode (OutcomeSignal has FK to heart.episodes)
        ep = Episode(
            agent_id=settings.agent_id,
            summary="Test episode for rubric dashboard",
            outcome="success",
        )
        session.add(ep)
        await session.flush()

        for sig_type in ["completed", "praised"]:
            sig = OutcomeSignal(
                agent_id=settings.agent_id,
                episode_id=ep.id,
                signal_type=sig_type,
                confidence=0.85,
                evidence="Test evidence",
            )
            session.add(sig)
        await session.commit()
    return rv


@pytest.mark.asyncio
async def test_get_rubric_dashboard_data(db, settings, seed_rubric):
    from nous.api.dashboard_queries import get_rubric_dashboard_data

    async with db.session() as session:
        data = await get_rubric_dashboard_data(session, settings.agent_id, settings)

    assert data["active_rubric"] is not None
    assert data["active_rubric"]["version"] == "1.0.0"
    assert len(data["active_rubric"]["dimensions"]) == 4
    assert data["outcome_signals"]["total"] == 2
    assert "completed" in data["outcome_signals"]["by_type"]
    assert "praised" in data["outcome_signals"]["by_type"]
    assert len(data["version_history"]) >= 1
    assert len(data["weight_history"]) >= 1
    assert "config" in data
