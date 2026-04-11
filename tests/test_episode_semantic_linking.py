"""Tests for F040: Semantic episode↔episode linking in EpisodeSummarizer."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nous.handlers.episode_summarizer import EpisodeSummarizer


def _make_summarizer(**overrides):
    """Create an EpisodeSummarizer with mocked dependencies."""
    heart = overrides.get("heart", MagicMock())
    brain = overrides.get("brain", None)
    settings = overrides.get("settings", MagicMock())
    bus = MagicMock()
    bus.on = MagicMock()
    llm_client = overrides.get("llm_client", None)
    graph_linker = overrides.get("graph_linker", None)

    s = EpisodeSummarizer(
        heart=heart,
        brain=brain,
        settings=settings,
        bus=bus,
        llm_client=llm_client,
        graph_linker=graph_linker,
    )
    if "embedder" in overrides:
        s._embedder = overrides["embedder"]
    return s


@pytest.mark.asyncio
async def test_link_similar_episodes_no_graph_linker():
    """Returns 0 when no graph_linker is set."""
    s = _make_summarizer(graph_linker=None)
    s._embedder = MagicMock()
    result = await s._link_similar_episodes(uuid.uuid4(), "some summary")
    assert result == 0


@pytest.mark.asyncio
async def test_link_similar_episodes_no_embedder():
    """Returns 0 when no embedder is set."""
    s = _make_summarizer(graph_linker=MagicMock())
    s._embedder = None
    result = await s._link_similar_episodes(uuid.uuid4(), "some summary")
    assert result == 0


@pytest.mark.asyncio
async def test_link_similar_episodes_empty_summary():
    """Returns 0 when summary text is empty."""
    s = _make_summarizer(graph_linker=MagicMock())
    s._embedder = MagicMock()
    result = await s._link_similar_episodes(uuid.uuid4(), "")
    assert result == 0


@pytest.mark.asyncio
async def test_link_similar_episodes_creates_edges():
    """Creates edges when similar episodes are found above threshold."""
    episode_id = uuid.uuid4()
    similar_id_1 = uuid.uuid4()
    similar_id_2 = uuid.uuid4()

    # Mock embedder
    embedder = AsyncMock()
    embedder.embed = AsyncMock(return_value=[0.1] * 1536)

    # Mock graph_linker
    graph_linker = MagicMock()
    graph_linker.agent_id = "test-agent"

    # Mock DB session with query results
    mock_row_1 = MagicMock()
    mock_row_1.id = similar_id_1
    mock_row_1.similarity = 0.85

    mock_row_2 = MagicMock()
    mock_row_2.id = similar_id_2
    mock_row_2.similarity = 0.80

    mock_result = MagicMock()
    mock_result.__iter__ = MagicMock(return_value=iter([mock_row_1, mock_row_2]))

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()

    # Set up context manager
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)
    graph_linker.db = MagicMock()
    graph_linker.db.session = MagicMock(return_value=mock_ctx)

    # create_edge returns an edge object on success
    mock_edge = MagicMock()
    graph_linker.create_edge = AsyncMock(return_value=mock_edge)

    settings = MagicMock()
    settings.graph_threshold_episode_episode = 0.75

    s = _make_summarizer(graph_linker=graph_linker, settings=settings, embedder=embedder)

    result = await s._link_similar_episodes(episode_id, "Test summary about something")

    assert result == 2
    assert graph_linker.create_edge.call_count == 2
    mock_session.commit.assert_awaited_once()

    # Verify edge creation args
    first_call = graph_linker.create_edge.call_args_list[0]
    assert first_call.kwargs["source_id"] == episode_id
    assert first_call.kwargs["target_id"] == similar_id_1
    assert first_call.kwargs["source_type"] == "episode"
    assert first_call.kwargs["target_type"] == "episode"
    assert first_call.kwargs["relation"] == "related_to"


@pytest.mark.asyncio
async def test_link_similar_episodes_no_matches():
    """Returns 0 when no similar episodes are found."""
    episode_id = uuid.uuid4()

    embedder = AsyncMock()
    embedder.embed = AsyncMock(return_value=[0.1] * 1536)

    graph_linker = MagicMock()
    graph_linker.agent_id = "test-agent"

    mock_result = MagicMock()
    mock_result.__iter__ = MagicMock(return_value=iter([]))

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)
    graph_linker.db = MagicMock()
    graph_linker.db.session = MagicMock(return_value=mock_ctx)

    settings = MagicMock()
    settings.graph_threshold_episode_episode = 0.75

    s = _make_summarizer(graph_linker=graph_linker, settings=settings, embedder=embedder)

    result = await s._link_similar_episodes(episode_id, "Test summary")
    assert result == 0


@pytest.mark.asyncio
async def test_link_similar_episodes_handles_errors():
    """Returns 0 and logs debug when an exception occurs."""
    embedder = AsyncMock()
    embedder.embed = AsyncMock(side_effect=RuntimeError("embedding failed"))

    graph_linker = MagicMock()
    graph_linker.agent_id = "test-agent"

    settings = MagicMock()
    settings.graph_threshold_episode_episode = 0.75

    s = _make_summarizer(graph_linker=graph_linker, settings=settings, embedder=embedder)

    result = await s._link_similar_episodes(uuid.uuid4(), "Test summary")
    assert result == 0


@pytest.mark.asyncio
async def test_link_similar_episodes_below_threshold_skipped():
    """Edges below threshold are not created even if returned by query."""
    episode_id = uuid.uuid4()
    similar_id = uuid.uuid4()

    embedder = AsyncMock()
    embedder.embed = AsyncMock(return_value=[0.1] * 1536)

    graph_linker = MagicMock()
    graph_linker.agent_id = "test-agent"

    # Row below the exact threshold (query uses threshold * 0.9 so it can appear)
    mock_row = MagicMock()
    mock_row.id = similar_id
    mock_row.similarity = 0.72  # Below 0.75 threshold

    mock_result = MagicMock()
    mock_result.__iter__ = MagicMock(return_value=iter([mock_row]))

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)
    graph_linker.db = MagicMock()
    graph_linker.db.session = MagicMock(return_value=mock_ctx)

    settings = MagicMock()
    settings.graph_threshold_episode_episode = 0.75

    s = _make_summarizer(graph_linker=graph_linker, settings=settings, embedder=embedder)

    result = await s._link_similar_episodes(episode_id, "Test summary")
    assert result == 0
    graph_linker.create_edge.assert_not_called()


@pytest.mark.asyncio
async def test_link_similar_episodes_threshold_fallback():
    """Uses default threshold of 0.75 when setting is missing."""
    episode_id = uuid.uuid4()

    embedder = AsyncMock()
    embedder.embed = AsyncMock(return_value=[0.1] * 1536)

    graph_linker = MagicMock()
    graph_linker.agent_id = "test-agent"

    mock_result = MagicMock()
    mock_result.__iter__ = MagicMock(return_value=iter([]))

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)
    graph_linker.db = MagicMock()
    graph_linker.db.session = MagicMock(return_value=mock_ctx)

    # Settings without the attribute — getattr should use default 0.75
    settings = MagicMock(spec=[])

    s = _make_summarizer(graph_linker=graph_linker, settings=settings, embedder=embedder)

    result = await s._link_similar_episodes(episode_id, "Test summary")
    assert result == 0  # No matches, but should not error
