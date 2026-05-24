"""F067 follow-up: pin the recall_deep score-rerank behavior gating.

Two invariants:

1. When ``episode_chunks_enabled=False`` (prod default), ``recall_deep``
   must call ``run_recall_pipeline`` with ``rerank_by_score=False`` —
   preserving the byte-identical legacy stage-order output.

2. When ``episode_chunks_enabled=True``, it must pass
   ``rerank_by_score=True`` so chunks appended after the fact stage can
   actually reach a top-K consumer.

Without invariant 2, the F067 chunk-recall leg is silently dead in
production: the table grows, embeddings are spent, but chunks never
surface in the top-K returned to the agent.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nous.api.tools import create_nous_tools


@pytest.fixture
def fake_pipeline_results():
    """A predictable mix of fact + chunk results for assertion."""
    from nous.api.retrieval_pipeline import PipelineResult, PipelineStats
    results = [
        PipelineResult(
            id=uuid.uuid4(), type="fact", description=f"fact {i}",
            score=0.5 - i * 0.01, source="heart",
        )
        for i in range(10)
    ] + [
        PipelineResult(
            id=uuid.uuid4(), type="chunk", description=f"chunk {i}",
            score=0.8 - i * 0.01, source="heart",
        )
        for i in range(5)
    ]
    stats = PipelineStats()
    return results, stats


def _make_settings(chunks_enabled: bool):
    """Minimal settings stub with the fields recall_deep touches."""
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
    return s


@pytest.mark.asyncio
async def test_chunks_disabled_uses_legacy_stage_order(fake_pipeline_results):
    """Invariant 1: chunks off → rerank_by_score=False (byte-identical legacy)."""
    brain = MagicMock()
    brain.agent_id = "test"
    heart = MagicMock()
    settings = _make_settings(chunks_enabled=False)

    tools = create_nous_tools(brain, heart, settings=settings)
    recall_deep = tools["recall_deep"]

    with patch(
        "nous.api.retrieval_pipeline.run_recall_pipeline",
        new=AsyncMock(return_value=fake_pipeline_results),
    ) as mock_pipeline:
        await recall_deep(query="anything", limit=10)

    mock_pipeline.assert_called_once()
    kwargs = mock_pipeline.call_args.kwargs
    assert kwargs["rerank_by_score"] is False, (
        f"chunks-disabled must not score-rerank (preserves legacy output); "
        f"got rerank_by_score={kwargs.get('rerank_by_score')!r}"
    )


@pytest.mark.asyncio
async def test_chunks_enabled_triggers_score_rerank(fake_pipeline_results):
    """Invariant 2: chunks on → rerank_by_score=True so chunks reach top-K."""
    brain = MagicMock()
    brain.agent_id = "test"
    heart = MagicMock()
    settings = _make_settings(chunks_enabled=True)

    tools = create_nous_tools(brain, heart, settings=settings)
    recall_deep = tools["recall_deep"]

    with patch(
        "nous.api.retrieval_pipeline.run_recall_pipeline",
        new=AsyncMock(return_value=fake_pipeline_results),
    ) as mock_pipeline:
        await recall_deep(query="anything", limit=10)

    mock_pipeline.assert_called_once()
    kwargs = mock_pipeline.call_args.kwargs
    assert kwargs["rerank_by_score"] is True, (
        f"chunks-enabled must score-rerank so chunks reach top-K; "
        f"got rerank_by_score={kwargs.get('rerank_by_score')!r}"
    )


@pytest.mark.asyncio
async def test_chunks_enabled_default_attribute_missing_safe(fake_pipeline_results):
    """Safety: settings without the new attribute don't break recall_deep.

    Old settings objects in tests / smoke fixtures may not have
    ``episode_chunks_enabled``. The ``getattr(..., False)`` fallback
    must keep them on the legacy path.
    """
    brain = MagicMock()
    brain.agent_id = "test"
    heart = MagicMock()
    settings = MagicMock(spec=[])  # bare mock with NO attributes
    # Make the attributes recall_deep touches default-safe
    settings.residual_activation_enabled = False
    settings.action_gating_enabled = False
    settings.claim_verification_enabled = False
    settings.context_compaction_enabled = False
    settings.execution_ledger_enabled = False
    settings.action_gating_external_only = False
    settings.action_gating_mode = "off"
    settings.claim_verification_mode = "off"
    # episode_chunks_enabled deliberately NOT set on this mock

    tools = create_nous_tools(brain, heart, settings=settings)
    recall_deep = tools["recall_deep"]

    with patch(
        "nous.api.retrieval_pipeline.run_recall_pipeline",
        new=AsyncMock(return_value=fake_pipeline_results),
    ) as mock_pipeline:
        await recall_deep(query="anything", limit=10)

    # Default to False (legacy path) when attribute missing
    kwargs = mock_pipeline.call_args.kwargs
    assert kwargs["rerank_by_score"] is False
