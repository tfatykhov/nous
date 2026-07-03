"""Task 8: graph_link_candidate_window_days wiring in GraphLinker.

Unit tests — no DB required. Captures the SQL params passed to session.execute
to verify cutoff behaviour without needing a live Postgres instance.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from nous.brain.graph_linker import GraphLinker
from nous.config import Settings


def _mock_settings(**overrides):
    """Create a mock Settings for GraphLinker candidate-window tests."""
    s = MagicMock(spec=Settings)
    s.cross_type_linking_enabled = overrides.get("cross_type_linking_enabled", True)
    s.cross_type_link_min_content_chars = overrides.get("cross_type_link_min_content_chars", 0)
    s.cross_type_threshold = overrides.get("cross_type_threshold", 0.80)
    s.graph_link_candidate_window_days = overrides.get("graph_link_candidate_window_days", 60)
    s.tinyhippo_lite_enabled = False
    return s


def _make_linker(**setting_overrides):
    db = MagicMock()
    embedder = AsyncMock()
    embedder.embed = AsyncMock(return_value=[0.1] * 3)
    settings = _mock_settings(**setting_overrides)
    linker = GraphLinker(db, embedder, settings, "test-agent")
    return linker


class TestGraphLinkerCandidateWindow:
    """graph_link_candidate_window_days wiring in GraphLinker.link_fact_to_decisions."""

    @pytest.mark.asyncio
    async def test_window_days_positive_bounds_cutoff(self):
        """With window_days=7, cutoff param is approximately 7 days ago."""
        linker = _make_linker(graph_link_candidate_window_days=7)

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)

        before = datetime.now(UTC) - timedelta(days=7, seconds=2)
        await linker.link_fact_to_decisions(uuid4(), "Some fact content long enough", mock_session)
        after = datetime.now(UTC) - timedelta(days=7)

        call_kwargs = mock_session.execute.call_args[0][1]
        cutoff = call_kwargs["cutoff"]
        assert before <= cutoff <= after, f"Expected cutoff ~7 days ago, got {cutoff}"

    @pytest.mark.asyncio
    async def test_window_days_zero_uses_far_past(self):
        """With window_days=0, cutoff param is far-past (no effective time filter)."""
        linker = _make_linker(graph_link_candidate_window_days=0)

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)

        await linker.link_fact_to_decisions(uuid4(), "Some fact content long enough", mock_session)

        call_kwargs = mock_session.execute.call_args[0][1]
        cutoff = call_kwargs["cutoff"]
        # Far-past sentinel: well before any real data
        assert cutoff <= datetime(2000, 1, 1, tzinfo=UTC), f"Expected far-past cutoff, got {cutoff}"

    @pytest.mark.asyncio
    async def test_default_window_days_is_sixty(self):
        """Default window (60 days) produces cutoff approximately 60 days ago."""
        linker = _make_linker(graph_link_candidate_window_days=60)

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)

        before = datetime.now(UTC) - timedelta(days=60, seconds=2)
        await linker.link_fact_to_decisions(uuid4(), "Some fact content long enough", mock_session)
        after = datetime.now(UTC) - timedelta(days=60)

        call_kwargs = mock_session.execute.call_args[0][1]
        cutoff = call_kwargs["cutoff"]
        assert before <= cutoff <= after, f"Expected cutoff ~60 days ago, got {cutoff}"
