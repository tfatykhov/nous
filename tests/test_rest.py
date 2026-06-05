"""Integration tests for REST API endpoints.

Uses httpx AsyncClient with ASGITransport for async HTTP testing.
MockAgentRunner for /chat endpoints.
Real Postgres (existing conftest.py fixtures) for DB-backed endpoints.
"""

import asyncio
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from nous.brain.brain import Brain
from nous.brain.schemas import ReasonInput, RecordInput
from nous.cognitive.layer import CognitiveLayer
from nous.cognitive.schemas import FrameSelection, TurnContext
from nous.heart import CensorInput, FactInput

# ---------------------------------------------------------------------------
# Mock AgentRunner
# ---------------------------------------------------------------------------


class MockAgentRunner:
    """Returns canned responses from run_turn(), tracks call history."""

    def __init__(self) -> None:
        self.run_turn_calls: list[tuple] = []
        self.end_conversation_calls: list[tuple] = []
        self._conversations: dict = {}
        self._ledgers: dict = {}
        self._pending_corrections: dict = {}
        self.preset_response = "This is a test response from Nous."
        self.preset_context = TurnContext(
            system_prompt="You are Nous.",
            frame=FrameSelection(
                frame_id="conversation",
                frame_name="Conversation",
                confidence=0.9,
                match_method="default",
            ),
            decision_id=None,
            active_censors=[],
            context_token_estimate=100,
        )

    async def run_turn(self, session_id, user_message, agent_id=None, **kwargs):
        self.run_turn_calls.append((session_id, user_message, agent_id))
        return self.preset_response, self.preset_context, {"input_tokens": 100, "output_tokens": 50}

    async def end_conversation(self, session_id, agent_id=None):
        self.end_conversation_calls.append((session_id, agent_id))

    async def start(self):
        pass

    async def close(self):
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def brain(db, settings):
    """Brain without embeddings for REST tests."""
    b = Brain(database=db, settings=settings)
    yield b
    await b.close()


@pytest_asyncio.fixture
async def cognitive(brain, heart, settings):
    """CognitiveLayer wired to Brain and Heart."""
    return CognitiveLayer(brain, heart, settings, identity_prompt="You are Nous.")


@pytest.fixture
def mock_runner():
    return MockAgentRunner()


@pytest.fixture
def app(mock_runner, brain, heart, cognitive, db, settings):
    """Create Starlette app with REST routes."""
    from nous.api.rest import create_app

    return create_app(mock_runner, brain, heart, cognitive, db, settings)


@pytest_asyncio.fixture
async def client(app):
    """Async HTTP client using httpx ASGITransport."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_decision(brain, session):
    """Insert a decision for testing list/get endpoints."""
    return await brain.record(
        RecordInput(
            description="Test decision for REST API",
            confidence=0.85,
            category="architecture",
            stakes="medium",
            context="Testing REST endpoints",
            reasons=[ReasonInput(type="analysis", text="Test reason")],
            tags=["test", "rest"],
        ),
        session=session,
    )


async def _seed_episode(heart, session):
    """Insert an episode for testing list endpoint."""
    from nous.heart.schemas import EpisodeInput

    return await heart.start_episode(
        EpisodeInput(
            title="Test Episode",
            summary="A test episode for REST API",
            trigger="test",
        ),
        session=session,
    )


async def _seed_fact(heart, session):
    """Insert a fact for testing search endpoint."""
    return await heart.learn(
        FactInput(
            content="Test fact for REST API search",
            category="technical",
            confidence=0.9,
        ),
        session=session,
    )


async def _seed_censor(heart, session):
    """Insert a censor for testing list endpoint."""
    return await heart.add_censor(
        CensorInput(
            trigger_pattern="test censor pattern",
            reason="Test censor reason",
            action="steer",
        ),
        session=session,
    )


# ---------------------------------------------------------------------------
# Chat endpoint tests
# ---------------------------------------------------------------------------


async def test_chat_basic(client, mock_runner):
    """POST /chat -> 200, response has message + session_id + frame."""
    resp = await client.post("/chat", json={"message": "Hello Nous!"})

    assert resp.status_code == 200
    data = resp.json()
    assert "response" in data
    assert data["response"] == "This is a test response from Nous."
    assert "session_id" in data
    assert "frame" in data
    assert len(mock_runner.run_turn_calls) == 1


async def test_chat_with_session(client, mock_runner):
    """POST /chat with session_id -> same session reused."""
    session_id = f"test-session-{uuid.uuid4().hex[:8]}"
    resp = await client.post("/chat", json={"message": "Hello!", "session_id": session_id})

    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == session_id
    assert mock_runner.run_turn_calls[0][0] == session_id


async def test_end_chat(client, mock_runner):
    """DELETE /chat/{session_id} -> 200."""
    session_id = f"test-session-{uuid.uuid4().hex[:8]}"
    resp = await client.delete(f"/chat/{session_id}")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ended"
    assert data["session_id"] == session_id
    assert len(mock_runner.end_conversation_calls) == 1


# ---------------------------------------------------------------------------
# Status endpoint test
# ---------------------------------------------------------------------------


async def test_status(client, settings):
    """GET /status -> agent info + calibration + memory counts."""
    resp = await client.get("/status")

    assert resp.status_code == 200
    data = resp.json()
    assert "agent_id" in data
    assert "agent_name" in data or "model" in data
    assert "calibration" in data or "memory" in data


# ---------------------------------------------------------------------------
# Decisions endpoint tests
# ---------------------------------------------------------------------------


async def test_list_decisions(client, brain, session):
    """GET /decisions -> list with total."""
    await _seed_decision(brain, session)
    await session.commit()

    resp = await client.get("/decisions")

    assert resp.status_code == 200
    data = resp.json()
    assert "decisions" in data
    assert isinstance(data["decisions"], list)
    # total may or may not be present depending on implementation
    if "total" in data:
        assert isinstance(data["total"], int)


async def test_get_decision(client, brain):
    """GET /decisions/{id} -> detail."""
    # Seed without test session so data commits to DB and is visible
    # to the endpoint's own session (different connection).
    detail = await brain.record(
        RecordInput(
            description="Test decision for REST get",
            confidence=0.85,
            category="architecture",
            stakes="medium",
            context="Testing REST get endpoint",
            reasons=[ReasonInput(type="analysis", text="Test reason")],
            tags=["test", "rest-get"],
        ),
    )

    resp = await client.get(f"/decisions/{detail.id}")

    assert resp.status_code == 200
    data = resp.json()
    assert data["description"] == "Test decision for REST get"


async def test_get_decision_not_found(client):
    """GET /decisions/{bad_id} -> 404."""
    fake_id = str(uuid.uuid4())
    resp = await client.get(f"/decisions/{fake_id}")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Episodes endpoint test
# ---------------------------------------------------------------------------


async def test_list_episodes(client, heart, session):
    """GET /episodes -> list."""
    await _seed_episode(heart, session)
    await session.commit()

    resp = await client.get("/episodes")

    assert resp.status_code == 200
    data = resp.json()
    assert "episodes" in data
    assert isinstance(data["episodes"], list)


# ---------------------------------------------------------------------------
# Facts endpoint tests
# ---------------------------------------------------------------------------


async def test_search_facts(client, heart, session):
    """GET /facts?q=test -> results."""
    await _seed_fact(heart, session)
    await session.commit()

    resp = await client.get("/facts", params={"q": "test fact REST"})

    assert resp.status_code == 200
    data = resp.json()
    assert "facts" in data
    assert isinstance(data["facts"], list)


async def test_search_facts_no_query(client):
    """GET /facts without q -> 200 (browse mode, F021)."""
    resp = await client.get("/facts")
    assert resp.status_code == 200
    data = resp.json()
    assert "facts" in data
    assert "total" in data


# ---------------------------------------------------------------------------
# Censors endpoint test
# ---------------------------------------------------------------------------


async def test_list_censors(client, heart, session):
    """GET /censors -> active censors."""
    await _seed_censor(heart, session)
    await session.commit()

    resp = await client.get("/censors")

    assert resp.status_code == 200
    data = resp.json()
    assert "censors" in data
    assert isinstance(data["censors"], list)


# ---------------------------------------------------------------------------
# Frames endpoint test
# ---------------------------------------------------------------------------


async def test_list_frames(client):
    """GET /frames -> frame list (6 default frames from seed.sql)."""
    resp = await client.get("/frames")

    assert resp.status_code == 200
    data = resp.json()
    assert "frames" in data
    assert isinstance(data["frames"], list)


# ---------------------------------------------------------------------------
# Calibration endpoint test
# ---------------------------------------------------------------------------


async def test_calibration(client):
    """GET /calibration -> report."""
    resp = await client.get("/calibration")

    assert resp.status_code == 200
    data = resp.json()
    # Should contain calibration fields
    assert "total_decisions" in data or "calibration" in data


# ---------------------------------------------------------------------------
# Health endpoint tests
# ---------------------------------------------------------------------------


async def test_health(client):
    """GET /health -> healthy."""
    resp = await client.get("/health")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"


async def test_health_db_down(mock_runner, brain, heart, cognitive, settings):
    """GET /health with broken DB -> unhealthy."""
    from unittest.mock import AsyncMock, MagicMock

    from nous.api.rest import create_app
    from nous.storage.database import Database

    # Create a mock database that fails on health check
    mock_db = MagicMock(spec=Database)

    # Make session() context manager raise an exception
    mock_session_cm = AsyncMock()
    mock_session_cm.__aenter__ = AsyncMock(side_effect=Exception("DB connection failed"))
    mock_session_cm.__aexit__ = AsyncMock(return_value=False)
    mock_db.session.return_value = mock_session_cm

    app = create_app(mock_runner, brain, heart, cognitive, mock_db, settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/health")

    # Should return unhealthy (either 200 with unhealthy status or 503)
    data = resp.json()
    if resp.status_code == 200:
        assert data["status"] == "unhealthy"
    else:
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# /chat/stream SSE keepalive (defense against client read timeout during
# stalls in pre_turn / compaction / any non-streaming phase of stream_chat)
# ---------------------------------------------------------------------------


class _StallingStreamRunner(MockAgentRunner):
    """Runner whose stream_chat stalls before yielding any events.

    Simulates a slow pre_turn / compaction / cold-cache recall.
    """

    def __init__(self, stall_seconds: float) -> None:
        super().__init__()
        self.stall_seconds = stall_seconds

    async def stream_chat(
        self,
        session_id,
        user_message,
        platform=None,
        user_id=None,
        user_display_name=None,
    ):
        from nous.api.runner import StreamEvent

        # Simulate stall — no events emitted for this duration
        await asyncio.sleep(self.stall_seconds)
        yield StreamEvent(type="text_delta", text="hello")
        yield StreamEvent(type="done", stop_reason="end_turn")


@pytest.mark.asyncio
async def test_chat_stream_emits_keepalive_during_stall(
    brain, heart, cognitive, db, settings
):
    """Server emits SSE comment-line pings while stream_chat is stalled."""
    from nous.api.rest import create_app

    # Tighten ping interval and stall longer than it so a ping is forced.
    object.__setattr__(settings, "sse_ping_interval", 1)
    runner = _StallingStreamRunner(stall_seconds=2.5)

    app = create_app(runner, brain, heart, cognitive, db, settings)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", timeout=30
    ) as c:
        async with c.stream(
            "POST", "/chat/stream", json={"message": "hi"}
        ) as resp:
            assert resp.status_code == 200
            chunks: list[str] = []
            async for chunk in resp.aiter_text():
                chunks.append(chunk)
            body = "".join(chunks)

    # SSE comment-line keepalive must appear before the data event,
    # since the runner stalls 2.5s with ping_interval=1.
    assert ": keepalive" in body, f"no keepalive in response: {body!r}"
    # Real event still flows through after the stall completes.
    assert '"type": "text_delta"' in body
    assert '"text": "hello"' in body
    # At least 2 keepalives expected (2.5s stall, 1s interval)
    assert body.count(": keepalive") >= 2


@pytest.mark.asyncio
async def test_chat_stream_no_keepalive_when_events_flow(
    brain, heart, cognitive, db, settings
):
    """When events flow faster than ping_interval, no keepalive comments."""
    from nous.api.rest import create_app

    object.__setattr__(settings, "sse_ping_interval", 5)
    runner = _StallingStreamRunner(stall_seconds=0.0)  # no stall

    app = create_app(runner, brain, heart, cognitive, db, settings)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", timeout=10
    ) as c:
        async with c.stream(
            "POST", "/chat/stream", json={"message": "hi"}
        ) as resp:
            assert resp.status_code == 200
            body = "".join([c async for c in resp.aiter_text()])

    assert '"type": "text_delta"' in body
    assert ": keepalive" not in body
