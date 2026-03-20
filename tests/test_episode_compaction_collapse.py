"""Tests for issue #169: Collapse compaction episodes."""

import pytest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from nous.heart.episodes import EpisodeManager


@pytest.mark.asyncio
async def test_bump_compaction_count_increments():
    """bump_compaction_count increments the counter on the episode."""
    db = MagicMock()
    episode_id = uuid4()

    mock_episode = MagicMock()
    mock_episode.compaction_count = 0

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_episode

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.flush = AsyncMock()

    db.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    db.session.return_value.__aexit__ = AsyncMock(return_value=False)

    manager = EpisodeManager(db=db, embeddings=None, agent_id="test")

    await manager.bump_compaction_count(episode_id)

    assert mock_episode.compaction_count == 1
    mock_session.flush.assert_called_once()


@pytest.mark.asyncio
async def test_bump_compaction_count_increments_existing():
    """bump_compaction_count increments from existing count."""
    db = MagicMock()
    episode_id = uuid4()

    mock_episode = MagicMock()
    mock_episode.compaction_count = 3

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_episode

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.flush = AsyncMock()

    db.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    db.session.return_value.__aexit__ = AsyncMock(return_value=False)

    manager = EpisodeManager(db=db, embeddings=None, agent_id="test")

    await manager.bump_compaction_count(episode_id)

    assert mock_episode.compaction_count == 4


@pytest.mark.asyncio
async def test_bump_compaction_count_missing_episode():
    """bump_compaction_count raises ValueError for missing episode."""
    db = MagicMock()
    episode_id = uuid4()

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    db.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    db.session.return_value.__aexit__ = AsyncMock(return_value=False)

    manager = EpisodeManager(db=db, embeddings=None, agent_id="test")

    with pytest.raises(ValueError, match="not found"):
        await manager.bump_compaction_count(episode_id)


# ===========================================================================
# CognitiveLayer.pre_compaction tests (Task 3, issue #169)
# ===========================================================================

from nous.cognitive.layer import CognitiveLayer
from nous.heart.schemas import EpisodeInput


def _mock_settings():
    """Create minimal mock settings for CognitiveLayer."""
    s = MagicMock()
    s.NOUS_AGENT_ID = "test-agent"
    s.NOUS_AGENT_NAME = "Test"
    s.NOUS_MODEL = "claude-sonnet-4-6"
    s.NOUS_CONTEXT_WINDOW = 0
    s.NOUS_ANTI_HALLUCINATION_PROMPT = False
    s.NOUS_RELEVANCE_FLOOR_ENABLED = True
    s.NOUS_RELEVANCE_DROP_RATIO = 0.6
    s.NOUS_BUDGET_SCALE_ENABLED = True
    s.NOUS_CONTEXT_BUDGET_OVERRIDES = {}
    s.NOUS_STALENESS_PENALTY_ENABLED = False
    s.NOUS_STALENESS_HALF_LIFE_DAYS = 14
    s.NOUS_GRAPH_RECALL_ENABLED = False
    s.NOUS_SPREADING_ACTIVATION_ENABLED = "false"
    s.NOUS_CROSS_TYPE_LINKING_ENABLED = False
    s.NOUS_TOOL_PRUNING_ENABLED = False
    s.NOUS_COMPACTION_ENABLED = True
    s.NOUS_COMPACTION_THRESHOLD = 50000
    s.NOUS_KEEP_RECENT_TOKENS = 20000
    return s


TEST_AGENT = "test-agent"
TEST_SESSION = "test-session"


@pytest.mark.asyncio
async def test_pre_compaction_keeps_episode_open():
    """pre_compaction does NOT end the episode — it bumps compaction count."""
    brain = MagicMock()
    brain.db = MagicMock()
    brain.embeddings = MagicMock()
    heart = MagicMock()
    heart.end_episode = AsyncMock()
    heart.start_episode = AsyncMock()
    heart.bump_episode_compaction_count = AsyncMock()
    settings = _mock_settings()

    cognitive = CognitiveLayer(brain, heart, settings, bus=None)
    old_episode_id = str(uuid4())
    cognitive._active_episodes[TEST_SESSION] = old_episode_id

    await cognitive.pre_compaction(
        agent_id=TEST_AGENT,
        session_id=TEST_SESSION,
        message_snapshot=[{"role": "user", "content": "test"}],
    )

    heart.end_episode.assert_not_called()
    heart.start_episode.assert_not_called()
    heart.bump_episode_compaction_count.assert_called_once()
    assert cognitive._active_episodes[TEST_SESSION] == old_episode_id


@pytest.mark.asyncio
async def test_pre_compaction_no_episode_no_error():
    """pre_compaction with no active episode doesn't error or create one."""
    brain = MagicMock()
    brain.db = MagicMock()
    brain.embeddings = MagicMock()
    heart = MagicMock()
    heart.bump_episode_compaction_count = AsyncMock()
    settings = _mock_settings()

    cognitive = CognitiveLayer(brain, heart, settings, bus=None)

    await cognitive.pre_compaction(
        agent_id=TEST_AGENT,
        session_id=TEST_SESSION,
        message_snapshot=[{"role": "user", "content": "test"}],
    )

    heart.bump_episode_compaction_count.assert_not_called()


@pytest.mark.asyncio
async def test_pre_compaction_bump_failure_non_fatal():
    """If bump_compaction_count fails, episode stays active and no error raised."""
    brain = MagicMock()
    brain.db = MagicMock()
    brain.embeddings = MagicMock()
    heart = MagicMock()
    heart.bump_episode_compaction_count = AsyncMock(side_effect=RuntimeError("DB error"))
    settings = _mock_settings()

    cognitive = CognitiveLayer(brain, heart, settings, bus=None)
    old_episode_id = str(uuid4())
    cognitive._active_episodes[TEST_SESSION] = old_episode_id

    await cognitive.pre_compaction(
        agent_id=TEST_AGENT,
        session_id=TEST_SESSION,
        message_snapshot=[],
    )

    assert cognitive._active_episodes[TEST_SESSION] == old_episode_id


@pytest.mark.asyncio
async def test_pre_compaction_still_emits_event():
    """pre_compaction still emits conversation_compacting event."""
    brain = MagicMock()
    brain.db = MagicMock()
    brain.embeddings = MagicMock()
    heart = MagicMock()
    heart.bump_episode_compaction_count = AsyncMock()
    settings = _mock_settings()
    bus = MagicMock()
    bus.emit = AsyncMock()

    cognitive = CognitiveLayer(brain, heart, settings, bus=bus)
    cognitive._active_episodes[TEST_SESSION] = str(uuid4())

    snapshot = [{"role": "user", "content": "test"}]
    await cognitive.pre_compaction(
        agent_id=TEST_AGENT,
        session_id=TEST_SESSION,
        message_snapshot=snapshot,
    )

    bus.emit.assert_called_once()
    event = bus.emit.call_args[0][0]
    assert event.type == "conversation_compacting"
    assert event.data["message_snapshot"] == snapshot
