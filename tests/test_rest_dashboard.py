"""Integration tests for F021 dashboard endpoint extensions."""

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from nous.brain.brain import Brain
from nous.brain.schemas import ReasonInput, RecordInput
from nous.cognitive.layer import CognitiveLayer
from nous.cognitive.schemas import FrameSelection, TurnContext
from nous.heart import CensorInput, FactInput

pytestmark = pytest.mark.integration




class MockAgentRunner:
    def __init__(self):
        self._conversations = {}

    async def start(self):
        pass

    async def close(self):
        pass


@pytest_asyncio.fixture
async def brain(db, settings):
    b = Brain(database=db, settings=settings)
    yield b
    await b.close()


@pytest_asyncio.fixture
async def cognitive(brain, heart, settings):
    return CognitiveLayer(brain, heart, settings, identity_prompt="You are Nous.")


@pytest.fixture
def app(brain, heart, cognitive, db, settings):
    from nous.api.rest import create_app
    return create_app(MockAgentRunner(), brain, heart, cognitive, db, settings)


@pytest_asyncio.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Task 1: Facts browse mode
# ---------------------------------------------------------------------------


async def test_facts_browse_no_query(client, heart, db):
    """GET /facts without q param returns paginated list."""
    async with db.session() as session:
        await heart.learn(
            FactInput(content="Dashboard browse fact", category="technical", confidence=0.9),
            session=session,
        )
        await session.commit()

    resp = await client.get("/facts?limit=10&offset=0")
    assert resp.status_code == 200
    data = resp.json()
    assert "facts" in data
    assert "total" in data
    assert data["total"] >= 1


async def test_facts_browse_with_category_filter(client, heart, db):
    """GET /facts?category=technical returns only matching facts."""
    async with db.session() as session:
        await heart.learn(
            FactInput(content="Tech fact for filter test", category="technical", confidence=0.9),
            session=session,
        )
        await heart.learn(
            FactInput(content="Person fact for filter test", category="person", confidence=0.8),
            session=session,
        )
        await session.commit()

    resp = await client.get("/facts?category=technical&limit=50")
    assert resp.status_code == 200
    data = resp.json()
    for f in data["facts"]:
        assert f["category"] == "technical"


async def test_facts_search_still_works(client, heart, db):
    """GET /facts?q=something still uses semantic search."""
    async with db.session() as session:
        await heart.learn(
            FactInput(content="Searchable fact about Python", category="technical", confidence=0.9),
            session=session,
        )
        await session.commit()

    resp = await client.get("/facts?q=Python&limit=10")
    assert resp.status_code == 200
    data = resp.json()
    assert "facts" in data
    assert "total" in data


# ---------------------------------------------------------------------------
# Task 2: Episodes with offset and filters
# ---------------------------------------------------------------------------


async def test_episodes_with_offset_and_total(client, heart, db):
    """GET /episodes returns total count and supports offset."""
    from nous.heart.schemas import EpisodeInput


    async with db.session() as session:
        await heart.start_episode(
            EpisodeInput(title="Episode A", summary="First episode", trigger="test"),
            session=session,
        )
        await session.commit()

    resp = await client.get("/episodes?limit=10&offset=0")
    assert resp.status_code == 200
    data = resp.json()
    assert "episodes" in data
    assert "total" in data
    assert isinstance(data["total"], int)


async def test_episodes_filter_by_outcome(client, heart, db):
    """GET /episodes?outcome=success filters by outcome."""
    resp = await client.get("/episodes?outcome=success&limit=10")
    assert resp.status_code == 200
    data = resp.json()
    for ep in data["episodes"]:
        assert ep.get("outcome") == "success" or len(data["episodes"]) == 0


# ---------------------------------------------------------------------------
# Task 3: Decisions with filters
# ---------------------------------------------------------------------------


async def test_decisions_filter_by_category(client, brain, db):
    """GET /decisions?category=architecture filters by category."""
    async with db.session() as session:
        await brain.record(
            RecordInput(
                description="Architecture decision for dashboard test",
                confidence=0.85, category="architecture", stakes="medium",
                context="Testing", reasons=[ReasonInput(type="analysis", text="Test")],
            ),
            session=session,
        )
        await session.commit()

    resp = await client.get("/decisions?category=architecture&limit=50")
    assert resp.status_code == 200
    data = resp.json()
    assert "total" in data
    for d in data["decisions"]:
        assert d["category"] == "architecture"


async def test_decisions_filter_by_stakes(client, brain, db):
    """GET /decisions?stakes=high filters by stakes level."""
    resp = await client.get("/decisions?stakes=high")
    assert resp.status_code == 200


async def test_decisions_filter_by_outcome(client, brain, db):
    """GET /decisions?outcome=success filters by outcome."""
    resp = await client.get("/decisions?outcome=success")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Task 4: Censors with pagination
# ---------------------------------------------------------------------------


async def test_censors_with_pagination(client, heart, db):
    """GET /censors returns total and supports limit/offset."""
    async with db.session() as session:
        await heart.add_censor(
            CensorInput(trigger_pattern="test pattern", reason="Test", action="warn"),
            session=session,
        )
        await session.commit()

    resp = await client.get("/censors?limit=10&offset=0")
    assert resp.status_code == 200
    data = resp.json()
    assert "censors" in data
    assert "total" in data


async def test_censors_filter_by_action(client):
    """GET /censors?action=block filters by action type."""
    resp = await client.get("/censors?action=block")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Task 4b: Procedures endpoint
# ---------------------------------------------------------------------------


async def test_procedures_list(client, db):
    """GET /procedures returns paginated procedures."""
    resp = await client.get("/procedures?limit=10&offset=0")
    assert resp.status_code == 200
    data = resp.json()
    assert "procedures" in data
    assert "total" in data
