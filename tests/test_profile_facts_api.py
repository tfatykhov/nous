"""REST tests for /profile/facts + /facts/{id} edit endpoints (dashboard identity UI)."""
import uuid as _uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from nous.heart import FactInput, Heart

_PROFILE_AGENT = f"test-profile-{_uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Isolated fixtures — unique agent id so committed facts on the session-scoped
# Postgres don't persist/dedup-collide across re-runs (tests-P1-2).
# ---------------------------------------------------------------------------


@pytest.fixture
def settings():
    # conftest's settings fixture is a bare Settings() with no test-critical
    # overrides, so construct directly and pin a unique agent id.
    from nous.config import Settings

    return Settings().model_copy(update={"agent_id": _PROFILE_AGENT})


@pytest_asyncio.fixture(autouse=True)
async def _ensure_agent(db):
    from sqlalchemy import text

    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO nous_system.agents (id, name, config) "
                "VALUES (:id, :n, '{}'::jsonb) ON CONFLICT (id) DO NOTHING"
            ),
            {"id": _PROFILE_AGENT, "n": "Profile API Test Agent"},
        )
        await session.commit()


@pytest_asyncio.fixture
async def heart(db, mock_embeddings, settings):
    h = Heart(db, settings, embedding_provider=mock_embeddings)
    yield h
    await h.close()


@pytest_asyncio.fixture
async def brain(db, settings):
    from nous.brain.brain import Brain

    b = Brain(database=db, settings=settings)
    yield b
    await b.close()


@pytest_asyncio.fixture
async def cognitive(brain, heart, settings):
    from nous.cognitive import CognitiveLayer

    return CognitiveLayer(brain, heart, settings, identity_prompt="You are Nous.")


@pytest_asyncio.fixture
async def identity_manager(db, settings):
    from nous.identity.manager import IdentityManager

    return IdentityManager(db, settings.agent_id)


def _build_app(brain, heart, cognitive, db, settings, identity_manager):
    from unittest.mock import AsyncMock

    from nous.api.rest import create_app
    from nous.api.runner import AgentRunner

    mock_runner = AsyncMock(spec=AgentRunner)
    return create_app(
        mock_runner, brain, heart, cognitive, db, settings,
        identity_manager=identity_manager,
    )


@pytest.fixture
def app(brain, heart, cognitive, db, settings, identity_manager):
    return _build_app(brain, heart, cognitive, db, settings, identity_manager)


@pytest_asyncio.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# GET /profile/facts (Task 1)
# ---------------------------------------------------------------------------


class TestProfileFactsEndpoint:
    @pytest.mark.asyncio
    async def test_returns_tier1_categories_only(self, client, heart, db):
        async with db.session() as session:
            await heart.learn(FactInput(content="Tim prefers Celsius for temperature readings", category="preference", subject="Tim"), session=session)
            await heart.learn(FactInput(content="Tim lives in Silver Spring Maryland United States", category="person", subject="Tim"), session=session)
            await heart.learn(FactInput(content="Postgres seventeen with pgvector extension is the datastore", category="technical", subject="stack"), session=session)
            await session.commit()
        resp = await client.get("/profile/facts")
        assert resp.status_code == 200
        data = resp.json()
        cats = {f["category"] for f in data["facts"]}
        assert cats == {"preference", "person"}  # exact — agent is module-unique
        assert data["total"] == len(data["facts"]) == 2

    @pytest.mark.asyncio
    async def test_limit_active_and_validation(self, client, heart, db):
        resp = await client.get("/profile/facts?limit=1")
        assert resp.status_code == 200
        assert len(resp.json()["facts"]) <= 1
        # include-inactive path (tests-P3 coverage)
        resp = await client.get("/profile/facts?active=false")
        assert resp.status_code == 200
        resp = await client.get("/profile/facts?limit=notanumber")
        assert resp.status_code == 400
