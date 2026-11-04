"""Tests for F040 graph densification phase in sleep handler."""

from __future__ import annotations

import logging

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
async def test_graph_densification_phase_runs(handler, caplog):
    """Graph densification phase runs backfill and cluster discovery when densifier is wired.

    F043 regression guard: the orphan_edges_created total MUST equal the sum of
    per-type edge counts and MUST NOT include the _ce_stats counters (which would
    double-count). With facts=3, decisions=2, ce survived=5 + pruned=3, the
    correct total is 5 — NOT 13.
    """
    densifier = MagicMock()
    densifier.run_backfill_cycle = AsyncMock(return_value={
        "facts": 3,
        "decisions": 2,
        "episodes": 0,
        "procedures": 0,
        "_ce_stats": {"survived": 5, "pruned": 3},
    })
    densifier.discover_clusters = AsyncMock(return_value=3)
    handler._graph_densifier = densifier

    sleep_stats = {}
    with caplog.at_level(logging.INFO, logger="nous.handlers.sleep_handler"):
        result = await handler._phase_graph_densification(sleep_stats)

    assert result is True
    densifier.run_backfill_cycle.assert_awaited_once()
    densifier.discover_clusters.assert_awaited_once_with(max_bridges=20)
    # Regression guard: 5 (3+2+0+0), NOT 13 (which would mean _ce_stats leaked in).
    assert sleep_stats["orphan_edges_created"] == 5
    assert sleep_stats["bridge_edges_created"] == 3
    # F043: CE counters surfaced into sleep_stats.
    assert sleep_stats["ce_backfill_survived"] == 5
    assert sleep_stats["ce_backfill_pruned"] == 3
    # F043: log line carries the CE survived/pruned summary.
    assert "CE survived=5 pruned=3" in caplog.text


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
    densifier.run_backfill_cycle = AsyncMock(return_value={"facts": 7, "decisions": 0, "episodes": 0, "procedures": 0})
    densifier.discover_clusters = AsyncMock(side_effect=RuntimeError("cluster error"))
    handler._graph_densifier = densifier

    sleep_stats = {}
    result = await handler._phase_graph_densification(sleep_stats)

    # The whole phase fails because the exception propagates to the outer try/except
    assert result is False
    # But orphan_edges_created was set before the error
    assert sleep_stats.get("orphan_edges_created") == 7


@pytest.mark.asyncio
async def test_graph_densification_reports_orphan_window_counters(handler):
    """J-7: the orphan-window counters land in sleep_stats as flat ints and
    stay out of the orphan_edges_created sum (3 + 2 = 5, not 5 + 200 + 1)."""
    densifier = MagicMock()
    densifier.run_backfill_cycle = AsyncMock(return_value={
        "facts": 3,
        "decisions": 2,
        "_ce_stats": {"survived": 0, "pruned": 0},
        "_orphans_visited": 200,
        "_orphan_window_wraps": 1,
    })
    densifier.discover_clusters = AsyncMock(return_value=0)
    handler._graph_densifier = densifier

    sleep_stats = {}
    assert await handler._phase_graph_densification(sleep_stats) is True

    assert sleep_stats["orphan_edges_created"] == 5
    assert sleep_stats["orphans_visited"] == 200
    assert sleep_stats["orphan_window_wraps"] == 1


@pytest.mark.asyncio
async def test_graph_densification_reports_cross_episode_chunk_edges(handler):
    """J-3: the live cross-episode chunk edges count toward
    orphan_edges_created and are also reported on their own."""
    densifier = MagicMock()
    densifier.run_backfill_cycle = AsyncMock(return_value={
        "facts": 1,
        "chunks": 0,
        "chunks_cross_episode": 4,
        "_ce_stats": {"survived": 0, "pruned": 0},
    })
    densifier.discover_clusters = AsyncMock(return_value=0)
    handler._graph_densifier = densifier

    sleep_stats = {}
    assert await handler._phase_graph_densification(sleep_stats) is True

    assert sleep_stats["orphan_edges_created"] == 5
    assert sleep_stats["chunk_cross_episode_edges"] == 4
