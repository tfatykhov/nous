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
async def test_chunks_enabled_but_memory_types_excludes_facts(fake_pipeline_results):
    """Codex P2 gate (PR #443): chunks_enabled=True + memory_types that
    excludes facts → no chunk fetch happens in run_recall_pipeline (see
    retrieval_pipeline.py:301), so rerank_by_score must also stay False
    to preserve legacy stage-order output on those non-chunk recall paths.
    """
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
        # Decision-only recall: chunks won't be fetched even with flag on
        await recall_deep(
            query="anything", limit=10, memory_types=["decision"],
        )

    kwargs = mock_pipeline.call_args.kwargs
    assert kwargs["rerank_by_score"] is False, (
        f"chunks-enabled but memory_types=['decision'] must NOT score-rerank "
        f"(chunks aren't fetched); got rerank_by_score={kwargs.get('rerank_by_score')!r}"
    )


@pytest.mark.asyncio
async def test_chunks_enabled_with_explicit_fact_type_reranks(fake_pipeline_results):
    """Counterpart to the test above: chunks_enabled=True + memory_types
    explicitly listing "fact" DOES fetch chunks, so rerank must fire."""
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
        await recall_deep(
            query="anything", limit=10, memory_types=["fact", "episode"],
        )

    kwargs = mock_pipeline.call_args.kwargs
    assert kwargs["rerank_by_score"] is True


def test_formatter_buckets_graph_via_stage_origin_metadata_under_rerank():
    """Codex P2 (PR #443): once ``rerank_by_score`` globally re-sorts the
    result list, the formatter's previous position-based heuristic for
    classifying ``source="graph_expanded"`` results into "Graph-Connected
    Decisions" (heart-side) vs "Brain Decisions" (brain-side) breaks.

    This regression test pins the new behavior: bucketing is driven by
    ``metadata["stage_origin"]``, which survives a global re-sort.
    """
    from nous.api.retrieval_pipeline import PipelineResult, PipelineStats
    from nous.api.tools import _format_pipeline_text

    # Construct a result list where post-rerank order would mis-bucket
    # under the old positional logic: a heart-side graph-expanded
    # decision sits AFTER a brain decision because of a lower score.
    heart_fact = PipelineResult(
        id=uuid.uuid4(), type="fact", description="heart fact",
        score=0.9, source="heart",
    )
    heart_graph_dec = PipelineResult(
        id=uuid.uuid4(), type="decision", description="heart-side graph dec",
        score=0.3, source="graph_expanded", edge_relation="causes",
        metadata={"stage_origin": "heart_graph"},
    )
    brain_dec = PipelineResult(
        id=uuid.uuid4(), type="decision", description="brain dec",
        score=0.7, source="brain",
        metadata={"category": "design", "stakes": "low",
                  "confidence": 0.8, "raw_score": 0.7},
    )
    brain_graph_dec = PipelineResult(
        id=uuid.uuid4(), type="decision", description="brain-side graph dec",
        score=0.5, source="graph_expanded", edge_relation="informed_by",
        metadata={"stage_origin": "brain_graph"},
    )
    # Post-rerank by score descending — the heart_graph_dec is now AFTER
    # brain_dec, which would mis-bucket it under the old positional logic.
    results = sorted(
        [heart_fact, heart_graph_dec, brain_dec, brain_graph_dec],
        key=lambda r: r.score, reverse=True,
    )
    stats = PipelineStats()

    out = _format_pipeline_text(results, stats, search_types=["all"])

    # heart_graph_dec must land in the Graph-Connected Decisions section
    assert "=== Graph-Connected Decisions ===" in out
    assert "heart-side graph dec" in out.split(
        "=== Graph-Connected Decisions ==="
    )[1].split("=== Brain Decisions ===")[0]

    # brain_graph_dec must land in the Brain Decisions section
    assert "brain-side graph dec" in out.split("=== Brain Decisions ===")[1]
    # Cross-check: heart-side dec must NOT appear in the Brain section
    assert "heart-side graph dec" not in out.split("=== Brain Decisions ===")[1]


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
