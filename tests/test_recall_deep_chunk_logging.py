"""F067 observability: pin the recall_deep chunk-surfacing log line.

The log line is the only direct visibility we have into whether the
chunk-recall leg is alive in prod (F055 residual_activation can carry
the same info but couples observability to a behavior-changing feature
flag). Operators grep ``docker logs nous-agent`` for ``recall_deep``
to spot-check chunk counts in real traffic.
"""
from __future__ import annotations

import logging
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nous.api.tools import create_nous_tools


@pytest.fixture
def mixed_pipeline_results():
    """Order matters for first_chunk_rank: chunks at positions 2 and 3
    (1-indexed) within a 4-item list."""
    from nous.api.retrieval_pipeline import PipelineResult, PipelineStats
    results = [
        PipelineResult(id=uuid.uuid4(), type="fact", description="f1",
                       score=0.95, source="heart"),
        PipelineResult(id=uuid.uuid4(), type="chunk", description="c1",
                       score=0.9, source="heart"),
        PipelineResult(id=uuid.uuid4(), type="chunk", description="c2",
                       score=0.85, source="heart"),
        PipelineResult(id=uuid.uuid4(), type="episode", description="e1",
                       score=0.7, source="heart"),
    ]
    stats = PipelineStats(chunks_searched=True)
    return results, stats


@pytest.fixture
def chunks_buried_results():
    """Chunks at positions 11 and 12 of a 12-item list — simulating the
    prod-observed case where chunks score lower than facts/decisions and
    sit at the bottom of the global result list."""
    from nous.api.retrieval_pipeline import PipelineResult, PipelineStats
    results = [
        PipelineResult(id=uuid.uuid4(), type="fact", description=f"f{i}",
                       score=0.9 - i * 0.01, source="heart")
        for i in range(10)
    ] + [
        PipelineResult(id=uuid.uuid4(), type="chunk", description=f"c{i}",
                       score=0.5 - i * 0.01, source="heart")
        for i in range(2)
    ]
    stats = PipelineStats(chunks_searched=True)
    return results, stats


def _make_settings(chunks_enabled: bool):
    s = MagicMock()
    s.episode_chunks_enabled = chunks_enabled
    s.residual_activation_enabled = False
    s.action_gating_enabled = False
    s.claim_verification_enabled = False
    s.context_compaction_enabled = False
    s.execution_ledger_enabled = False
    s.action_gating_external_only = False
    s.action_gating_mode = "off"
    s.claim_verification_mode = "off"
    s.recall_include_parent_episodes = False
    return s


@pytest.mark.asyncio
async def test_recall_deep_logs_chunk_surfacing_when_chunks_present(
    mixed_pipeline_results, caplog
):
    """The INFO line must include chunks_enabled, chunks_searched, and
    n_chunk_results — these are the three signals operators care about.
    """
    brain = MagicMock()
    brain.agent_id = "test-agent"
    heart = MagicMock()
    settings = _make_settings(chunks_enabled=True)

    tools = create_nous_tools(brain, heart, settings=settings)
    recall_deep = tools["recall_deep"]

    with patch(
        "nous.api.retrieval_pipeline.run_recall_pipeline",
        new=AsyncMock(return_value=mixed_pipeline_results),
    ), caplog.at_level(logging.INFO, logger="nous.api.tools"):
        await recall_deep(query="hello world", limit=10)

    log_lines = [
        rec.getMessage() for rec in caplog.records if "recall_deep" in rec.getMessage()
    ]
    assert log_lines, "expected at least one 'recall_deep' INFO log line"
    msg = log_lines[-1]
    assert "agent=test-agent" in msg
    assert "chunks_enabled=True" in msg
    assert "chunks_searched=True" in msg
    # 2 chunks total in the 4-item list
    assert "n_chunks_total=2" in msg
    # Both are in positions 2 and 3, so both fall within top-10
    assert "n_chunks_top10=2" in msg
    # First chunk is at rank 2 (1-indexed)
    assert "first_chunk_rank=2" in msg
    assert "n_total=4" in msg


@pytest.mark.asyncio
async def test_recall_deep_logs_buried_chunks_at_correct_rank(
    chunks_buried_results, caplog
):
    """Prod-observed pattern: chunks retrieved but buried at positions
    11-12 behind facts. Top-10 count should be 0 even though
    n_chunks_total=2."""
    brain = MagicMock()
    brain.agent_id = "test-agent"
    heart = MagicMock()
    settings = _make_settings(chunks_enabled=True)

    tools = create_nous_tools(brain, heart, settings=settings)
    recall_deep = tools["recall_deep"]

    with patch(
        "nous.api.retrieval_pipeline.run_recall_pipeline",
        new=AsyncMock(return_value=chunks_buried_results),
    ), caplog.at_level(logging.INFO, logger="nous.api.tools"):
        await recall_deep(query="hello", limit=10)

    msg = [
        rec.getMessage() for rec in caplog.records if "recall_deep" in rec.getMessage()
    ][-1]
    assert "n_chunks_total=2" in msg
    assert "n_chunks_top10=0" in msg
    assert "first_chunk_rank=11" in msg
    assert "n_total=12" in msg


@pytest.mark.asyncio
async def test_recall_deep_logs_zero_chunks_when_disabled(caplog):
    """Counterpart: chunks off → flags reflect that. Operators rely on
    zero counts being explicit, not absent."""
    from nous.api.retrieval_pipeline import PipelineResult, PipelineStats
    results = [
        PipelineResult(id=uuid.uuid4(), type="fact", description=f"f{i}",
                       score=0.5, source="heart")
        for i in range(3)
    ]
    stats = PipelineStats(chunks_searched=False)

    brain = MagicMock()
    brain.agent_id = "test-agent"
    heart = MagicMock()
    settings = _make_settings(chunks_enabled=False)

    tools = create_nous_tools(brain, heart, settings=settings)
    recall_deep = tools["recall_deep"]

    with patch(
        "nous.api.retrieval_pipeline.run_recall_pipeline",
        new=AsyncMock(return_value=(results, stats)),
    ), caplog.at_level(logging.INFO, logger="nous.api.tools"):
        await recall_deep(query="hi", limit=10)

    msg = [
        rec.getMessage() for rec in caplog.records if "recall_deep" in rec.getMessage()
    ][-1]
    assert "chunks_enabled=False" in msg
    assert "chunks_searched=False" in msg
    assert "n_chunks_total=0" in msg
    assert "n_chunks_top10=0" in msg
    assert "first_chunk_rank=n/a" in msg
