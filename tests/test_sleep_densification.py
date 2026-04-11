"""Tests for F040 graph densification phase in sleep handler."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from nous.config import Settings
from nous.events import EventBus
from nous.handlers.sleep_handler import SleepHandler


@pytest.fixture
def settings():
    return Settings(_env_file=None, graph_backfill_enabled=True)


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def handler(settings, bus):
    brain = MagicMock()
    heart = MagicMock()
    h = SleepHandler(brain, heart, settings, bus)
    return h


@pytest.mark.asyncio
async def test_graph_densification_phase_runs(handler):
    """Graph densification phase runs backfill and cluster discovery when densifier is wired."""
    densifier = MagicMock()
    densifier.run_backfill_cycle = AsyncMock(return_value={"edges_created": 5, "by_type": {"fact": 3, "decision": 2}})
    densifier.discover_clusters = AsyncMock(return_value=3)
    handler._graph_densifier = densifier

    sleep_stats = {}
    result = await handler._phase_graph_densification(sleep_stats)

    assert result is True
    densifier.run_backfill_cycle.assert_awaited_once()
    densifier.discover_clusters.assert_awaited_once_with(max_bridges=20)
    assert sleep_stats["orphan_edges_created"] == 5
    assert sleep_stats["bridge_edges_created"] == 3


@pytest.mark.asyncio
async def test_graph_densification_phase_skips_when_disabled(settings, bus):
    """Graph densification phase is skipped when config disabled."""
    settings.graph_backfill_enabled = False
    brain = MagicMock()
    heart = MagicMock()
    h = SleepHandler(brain, heart, settings, bus)

    densifier = MagicMock()
    densifier.run_backfill_cycle = AsyncMock()
    h._graph_densifier = densifier

    sleep_stats = {}
    result = await h._phase_graph_densification(sleep_stats)

    assert result is True
    densifier.run_backfill_cycle.assert_not_awaited()
    assert "orphan_edges_created" not in sleep_stats


@pytest.mark.asyncio
async def test_graph_densification_phase_skips_when_no_densifier(handler):
    """Graph densification phase is skipped when no densifier wired."""
    assert handler._graph_densifier is None

    sleep_stats = {}
    result = await handler._phase_graph_densification(sleep_stats)

    assert result is True
    assert "orphan_edges_created" not in sleep_stats


@pytest.mark.asyncio
async def test_graph_densification_phase_handles_errors(handler):
    """Graph densification phase returns False on error."""
    densifier = MagicMock()
    densifier.run_backfill_cycle = AsyncMock(side_effect=RuntimeError("db down"))
    handler._graph_densifier = densifier

    sleep_stats = {}
    result = await handler._phase_graph_densification(sleep_stats)

    assert result is False


@pytest.mark.asyncio
async def test_graph_densification_partial_failure(handler):
    """Backfill succeeds but discover_clusters raises — stats still has backfill result."""
    densifier = MagicMock()
    densifier.run_backfill_cycle = AsyncMock(return_value={"edges_created": 7, "by_type": {"fact": 7}})
    densifier.discover_clusters = AsyncMock(side_effect=RuntimeError("cluster error"))
    handler._graph_densifier = densifier

    sleep_stats = {}
    result = await handler._phase_graph_densification(sleep_stats)

    # The whole phase fails because the exception propagates to the outer try/except
    assert result is False
    # But orphan_edges_created was set before the error
    assert sleep_stats.get("orphan_edges_created") == 7
