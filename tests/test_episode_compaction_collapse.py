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
