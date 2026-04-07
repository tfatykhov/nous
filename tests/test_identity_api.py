"""Tests for Spec 008 PR 3 — Identity REST API endpoints.

Verifies:
- GET /identity returns sections and initiation state
- PUT /identity/{section} updates content
- PUT /identity/{section} rejects invalid sections
- POST /reinitiate resets identity
- Telegram /identity command (integration)
"""

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from nous.brain.brain import Brain
from nous.cognitive import CognitiveLayer
from nous.identity.manager import IdentityManager, VALID_SECTIONS


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def brain(db, settings):
    b = Brain(database=db, settings=settings)
    yield b
    await b.close()


@pytest_asyncio.fixture
async def cognitive(brain, heart, settings):
    return CognitiveLayer(brain, heart, settings, identity_prompt="You are Nous.")


@pytest_asyncio.fixture
async def identity_manager(db, settings):
    return IdentityManager(db, settings.agent_id)


@pytest.fixture
def app(brain, heart, cognitive, db, settings, identity_manager):
    """Create Starlette app with identity_manager wired in."""
    from nous.api.rest import create_app
    from nous.api.runner import AgentRunner

    mock_runner = AsyncMock(spec=AgentRunner)
    return create_app(
        mock_runner, brain, heart, cognitive, db, settings,
        identity_manager=identity_manager,
    )


@pytest_asyncio.fixture
async def client(app):
    """Async HTTP client for the Starlette app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# GET /identity
# ---------------------------------------------------------------------------


class TestGetIdentity:
    @pytest.mark.asyncio
    async def test_empty_identity(self, client):
        resp = await client.get("/identity")
        assert resp.status_code == 200
        data = resp.json()
        assert data["sections"] == {}
        assert data["is_initiated"] is False

    @pytest.mark.asyncio
    async def test_with_sections(self, client, identity_manager, db):
        async with db.session() as session:
            await identity_manager.update_section("character", "I am Nous", updated_by="test", session=session)
            await session.commit()

        resp = await client.get("/identity")
        assert resp.status_code == 200
        data = resp.json()
        assert "character" in data["sections"]
        assert data["sections"]["character"] == "I am Nous"


# ---------------------------------------------------------------------------
# PUT /identity/{section}
# ---------------------------------------------------------------------------


class TestUpdateIdentitySection:
    @pytest.mark.asyncio
    async def test_update_valid_section(self, client):
        resp = await client.put(
            "/identity/character",
            json={"content": "I am Nous, a cognitive agent."},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "updated"

        # Verify it was stored
        resp2 = await client.get("/identity")
        assert "cognitive agent" in resp2.json()["sections"]["character"]

    @pytest.mark.asyncio
    async def test_reject_invalid_section(self, client):
        resp = await client.put(
            "/identity/invalid_section",
            json={"content": "test"},
        )
        assert resp.status_code == 400
        assert "Invalid section" in resp.json()["error"]

    @pytest.mark.asyncio
    async def test_reject_missing_content(self, client):
        resp = await client.put("/identity/character", json={})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_reject_invalid_json(self, client):
        resp = await client.put(
            "/identity/character",
            content=b"not json",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /reinitiate
# ---------------------------------------------------------------------------


class TestReinitiate:
    @pytest.mark.asyncio
    async def test_reinitiate_clears_identity(self, client, identity_manager, db):
        # Set up identity first
        async with db.session() as session:
            await identity_manager.update_section("character", "I am Nous", updated_by="test", session=session)
            await identity_manager.mark_initiated(session=session)
            await session.commit()

        # Verify initiated
        resp = await client.get("/identity")
        assert resp.json()["is_initiated"] is True

        # Reinitiate
        resp = await client.post("/reinitiate")
        assert resp.status_code == 200
        assert resp.json()["status"] == "reset"

        # Verify cleared
        resp = await client.get("/identity")
        data = resp.json()
        assert data["is_initiated"] is False
        assert data["sections"] == {}


# ---------------------------------------------------------------------------
# IdentityManager.reset_identity
# ---------------------------------------------------------------------------


class TestResetIdentity:
    @pytest.mark.asyncio
    async def test_reset_clears_sections_and_flag(self, identity_manager, db):
        async with db.session() as session:
            await identity_manager.update_section("character", "test", updated_by="test", session=session)
            await identity_manager.update_section("preferences", "test prefs", updated_by="test", session=session)
            await identity_manager.mark_initiated(session=session)
            await session.commit()

        assert await identity_manager.is_initiated() is True

        await identity_manager.reset_identity()

        assert await identity_manager.is_initiated() is False
        sections = await identity_manager.get_current()
        assert sections == {}
