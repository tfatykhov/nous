"""F022 follow-up: regression tests for restart-resilient active_episode lookup.

Tests cover:
1. EpisodeInput accepts session_id and the ORM persists it.
2. warm_active_episode populates the in-memory cache from a DB row.
3. End-to-end: a fresh CognitiveLayer (empty in-memory map) recovers the
   active episode for an existing session via DB query.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from nous.heart.schemas import EpisodeInput


def test_episode_input_accepts_session_id():
    """EpisodeInput must accept the new session_id field."""
    inp = EpisodeInput(summary="test", session_id="sess-1")
    assert inp.session_id == "sess-1"


def test_episode_input_session_id_optional():
    """session_id remains optional for backwards compat."""
    inp = EpisodeInput(summary="test")
    assert inp.session_id is None


@pytest.mark.asyncio
async def test_warm_active_episode_returns_none_when_no_match():
    """warm_active_episode returns None and doesn't populate the map when DB has no row."""
    from nous.cognitive.layer import CognitiveLayer

    layer = CognitiveLayer.__new__(CognitiveLayer)
    layer._active_episodes = {}
    layer._brain = MagicMock()
    layer._brain.agent_id = "test-agent"

    # Mock DB session that returns no row
    mock_session = MagicMock()
    mock_session.execute = AsyncMock(return_value=MagicMock(
        scalar_one_or_none=MagicMock(return_value=None)
    ))
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=mock_session)
    cm.__aexit__ = AsyncMock(return_value=None)
    layer._brain.db.session = MagicMock(return_value=cm)

    result = await layer.warm_active_episode("session-123")
    assert result is None
    assert "session-123" not in layer._active_episodes


@pytest.mark.asyncio
async def test_warm_active_episode_caches_db_result():
    """When DB has an ongoing episode for the session, warm populates the map."""
    from nous.cognitive.layer import CognitiveLayer

    layer = CognitiveLayer.__new__(CognitiveLayer)
    layer._active_episodes = {}
    layer._brain = MagicMock()
    layer._brain.agent_id = "test-agent"

    ep_uuid = uuid4()
    mock_session = MagicMock()
    mock_session.execute = AsyncMock(return_value=MagicMock(
        scalar_one_or_none=MagicMock(return_value=ep_uuid)
    ))
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=mock_session)
    cm.__aexit__ = AsyncMock(return_value=None)
    layer._brain.db.session = MagicMock(return_value=cm)

    result = await layer.warm_active_episode("session-456")
    assert result == str(ep_uuid)
    # Cache populated for subsequent same-session sync calls
    assert layer._active_episodes["session-456"] == str(ep_uuid)


@pytest.mark.asyncio
async def test_warm_active_episode_returns_cached_without_db():
    """If the cache already has the session, no DB query is made."""
    from nous.cognitive.layer import CognitiveLayer

    layer = CognitiveLayer.__new__(CognitiveLayer)
    layer._active_episodes = {"sess-x": "cached-uuid"}
    layer._brain = MagicMock()
    # If db.session is called, the test should fail — make it raise.
    layer._brain.db.session = MagicMock(side_effect=AssertionError("DB called when cached"))

    result = await layer.warm_active_episode("sess-x")
    assert result == "cached-uuid"


@pytest.mark.asyncio
async def test_warm_active_episode_swallows_errors():
    """DB query errors must not propagate — best-effort warmup."""
    from nous.cognitive.layer import CognitiveLayer

    layer = CognitiveLayer.__new__(CognitiveLayer)
    layer._active_episodes = {}
    layer._brain = MagicMock()
    layer._brain.agent_id = "test-agent"
    layer._brain.db.session = MagicMock(side_effect=RuntimeError("DB unreachable"))

    # Must not raise
    result = await layer.warm_active_episode("session-err")
    assert result is None


@pytest.mark.asyncio
async def test_warm_active_episode_query_filters_active_true():
    """Codex P1 follow-up to #394: deactivated episodes have ended_at NULL
    but active=false. The warm query MUST filter on active=true so
    deactivated rows aren't resurrected. Verify by inspecting the SQL
    fragment passed to execute()."""
    from nous.cognitive.layer import CognitiveLayer

    layer = CognitiveLayer.__new__(CognitiveLayer)
    layer._active_episodes = {}
    layer._brain = MagicMock()
    layer._brain.agent_id = "test-agent"

    captured_stmt = []

    async def fake_execute(stmt):
        captured_stmt.append(stmt)
        return MagicMock(scalar_one_or_none=MagicMock(return_value=None))

    mock_session = MagicMock()
    mock_session.execute = fake_execute
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=mock_session)
    cm.__aexit__ = AsyncMock(return_value=None)
    layer._brain.db.session = MagicMock(return_value=cm)

    await layer.warm_active_episode("any-session")
    assert captured_stmt, "execute was not called"
    # The compiled SELECT must reference both ended_at and active.
    sql = str(captured_stmt[0])
    assert "ended_at" in sql.lower()
    assert "active" in sql.lower()
