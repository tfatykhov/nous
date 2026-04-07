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


# --- Task 2: REST endpoint tests ---

# brain, heart, db, settings fixtures come from conftest.py — do NOT redefine heart.

@pytest_asyncio.fixture
async def brain(db, settings):
    from nous.brain.brain import Brain
    b = Brain(database=db, settings=settings)
    yield b
    await b.close()


@pytest_asyncio.fixture
async def cognitive(brain, heart, settings):
    from nous.cognitive.layer import CognitiveLayer
    return CognitiveLayer(brain, heart, settings, identity_prompt="You are Nous.")


@pytest.fixture
def app(brain, heart, cognitive, db, settings):
    from nous.api.rest import create_app
    return create_app(MockAgentRunner(), brain, heart, cognitive, db, settings)


@pytest_asyncio.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_dashboard_rubric_endpoint(client, seed_rubric):
    resp = await client.get("/dashboard/rubric")
    assert resp.status_code == 200
    data = resp.json()
    assert data["active_rubric"]["version"] == "1.0.0"
    assert data["outcome_signals"]["total"] == 2
    assert "config" in data


@pytest.mark.asyncio
async def test_dashboard_rubric_endpoint_empty(client):
    """Returns gracefully when no rubric data exists."""
    resp = await client.get("/dashboard/rubric")
    assert resp.status_code == 200
    data = resp.json()
    assert data["active_rubric"] is None
    assert data["outcome_signals"]["total"] == 0


@pytest.mark.asyncio
async def test_dashboard_rubric_endpoint_no_correlations(client, seed_rubric):
    """Rubric exists with signals but no correlations yet."""
    resp = await client.get("/dashboard/rubric")
    assert resp.status_code == 200
    data = resp.json()
    assert data["correlations"]["data"] == []
    assert data["correlations"]["sample_size"] == 0
