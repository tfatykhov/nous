"""Tests for nous.api.retrieval_pipeline (F051 Phase 1).

Covers:

- ``run_recall_pipeline`` returns structured ``PipelineResult`` + ``PipelineStats``
  with stage-ordered results.
- ``_format_pipeline_text`` (in ``nous.api.tools``) reproduces the legacy
  ``recall_deep`` text byte-identically against a committed snapshot.
- The ``recall_deep`` tool closure delegates to the pipeline.

All tests use ``unittest.mock`` rather than a live DB so they execute the
pure pipeline logic in isolation and stay deterministic across machines.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from nous.api.retrieval_pipeline import (
    PipelineResult,
    PipelineStats,
    run_recall_pipeline,
)
from nous.api.tools import _format_pipeline_text
from nous.brain.schemas import DecisionSummary, NeighborResult
from nous.heart.schemas import RecallResult


# ---------------------------------------------------------------------------
# Fixtures: synthetic UUIDs + mock heart/brain/settings
# ---------------------------------------------------------------------------


FACT_ID = UUID("11111111-1111-1111-1111-111111111111")
EPISODE_ID = UUID("22222222-2222-2222-2222-222222222222")
HEART_GRAPH_DECISION_ID = UUID("33333333-3333-3333-3333-333333333333")
DECISION_ONE_ID = UUID("44444444-4444-4444-4444-444444444444")
DECISION_TWO_ID = UUID("55555555-5555-5555-5555-555555555555")
GRAPH_DECISION_ID = UUID("66666666-6666-6666-6666-666666666666")


def _make_recall_results() -> list[RecallResult]:
    return [
        RecallResult(
            type="fact",
            id=FACT_ID,
            summary="fact summary one",
            score=0.9,
        ),
        RecallResult(
            type="episode",
            id=EPISODE_ID,
            summary="episode summary two",
            score=0.8,
        ),
    ]


def _make_decision_summaries() -> list[DecisionSummary]:
    now = datetime.now(UTC)
    return [
        DecisionSummary(
            id=DECISION_ONE_ID,
            description="brain decision one",
            confidence=0.85,
            category="architecture",
            stakes="medium",
            outcome="pending",
            score=0.75,
            created_at=now,
        ),
        DecisionSummary(
            id=DECISION_TWO_ID,
            description="brain decision two",
            confidence=0.60,
            category="tooling",
            stakes="low",
            outcome="pending",
            score=None,  # Drives the truthy-elision path in the formatter
            created_at=now,
        ),
    ]


def _make_neighbors_for_heart_seed() -> list[NeighborResult]:
    """Decisions linked to the FACT_ID seed (F022 Phase 2)."""
    return [
        NeighborResult(
            id=HEART_GRAPH_DECISION_ID,
            node_type="decision",
            description="decision via heart graph",
            edge_relation="supports",
            edge_weight=0.8,
            created_at=datetime.now(UTC),
        ),
    ]


def _make_neighbors_for_brain_seed() -> list[NeighborResult]:
    """Decisions linked to DECISION_ONE_ID via 1-hop expansion."""
    return [
        NeighborResult(
            id=GRAPH_DECISION_ID,
            node_type="decision",
            description="decision via brain graph",
            edge_relation="contradicts",
            edge_weight=0.7,
            created_at=datetime.now(UTC),
        ),
    ]


class _FakeContradictionEdge:
    """Minimal stand-in for storage.models.GraphEdge rows."""

    def __init__(self, src_id: UUID, src_type: str, tgt_id: UUID, tgt_type: str) -> None:
        self.source_id = src_id
        self.source_type = src_type
        self.target_id = tgt_id
        self.target_type = tgt_type
        self.relation = "contradicts"


class _FakeScalars:
    def __init__(self, items):
        self._items = items

    def all(self):
        return list(self._items)


class _FakeExecuteResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return _FakeScalars(self._items)


class _FakeSession:
    """Minimal AsyncSession stand-in for the contradiction SELECT."""

    def __init__(self, contradictions):
        self._contradictions = contradictions

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def execute(self, _stmt):
        return _FakeExecuteResult(self._contradictions)


def _make_brain(*, neighbors_by_node, contradictions, decision_results):
    """Build a MagicMock Brain whose .neighbors and .query are AsyncMocks."""
    brain = MagicMock()
    brain.agent_id = "nous-test-agent"
    brain.query = AsyncMock(return_value=decision_results)

    async def neighbors_side_effect(node_id, node_type="decision", limit=10):
        return neighbors_by_node.get(node_id, [])

    brain.neighbors = AsyncMock(side_effect=neighbors_side_effect)

    # brain.db.session() returns an async context manager; same _FakeSession
    # is used for the (skipped) density check AND the contradictions SELECT.
    brain.db = MagicMock()
    brain.db.session = MagicMock(return_value=_FakeSession(contradictions))
    return brain


def _make_heart(*, recall_results):
    heart = MagicMock()
    heart.recall = AsyncMock(return_value=recall_results)
    return heart


def _make_settings(
    *,
    graph_recall_enabled=True,
    cross_type_linking_enabled=True,
    spreading_activation_enabled="false",
    contradiction_detection=True,
    graph_recall_decay=0.7,
    graph_recall_max_expand=5,
    graph_recall_max_neighbors=3,
):
    return SimpleNamespace(
        graph_recall_enabled=graph_recall_enabled,
        cross_type_linking_enabled=cross_type_linking_enabled,
        spreading_activation_enabled=spreading_activation_enabled,
        contradiction_detection=contradiction_detection,
        graph_recall_decay=graph_recall_decay,
        graph_recall_max_expand=graph_recall_max_expand,
        graph_recall_max_neighbors=graph_recall_max_neighbors,
    )


# ---------------------------------------------------------------------------
# run_recall_pipeline: structured behavior
# ---------------------------------------------------------------------------


class TestRunRecallPipeline:
    @pytest.mark.asyncio
    async def test_full_pipeline_all_stages_fire(self):
        """All five stages fire and structured results are returned in stage order."""
        heart = _make_heart(recall_results=_make_recall_results())
        decisions = _make_decision_summaries()
        contradictions = [
            _FakeContradictionEdge(
                DECISION_ONE_ID, "decision", DECISION_TWO_ID, "decision"
            )
        ]
        brain = _make_brain(
            neighbors_by_node={
                FACT_ID: _make_neighbors_for_heart_seed(),
                EPISODE_ID: [],
                DECISION_ONE_ID: _make_neighbors_for_brain_seed(),
                DECISION_TWO_ID: [],
            },
            contradictions=contradictions,
            decision_results=decisions,
        )
        settings = _make_settings()

        results, stats = await run_recall_pipeline(
            query="anything",
            heart=heart,
            brain=brain,
            settings=settings,
            limit=10,
        )

        # Stage order: heart -> heart_graph -> brain -> brain_graph
        sources = [r.source for r in results]
        assert sources == [
            "heart",  # fact
            "heart",  # episode
            "graph_expanded",  # cross-type from heart seed
            "brain",  # decision 1
            "brain",  # decision 2
            "graph_expanded",  # 1-hop from brain seed
        ]

        # Stats
        assert isinstance(stats, PipelineStats)
        assert stats.n_heart_results == 2
        assert stats.n_brain_results == 2
        assert stats.n_graph_expanded == 1  # only the brain-side expansion counts
        assert stats.graph_expansion_used is True
        assert stats.spreading_activation_used is False
        assert stats.contradiction_checks_ran is True
        assert len(stats.contradiction_edges) == 1

        # Contradiction attached to source result
        d1 = next(r for r in results if r.id == DECISION_ONE_ID)
        assert DECISION_TWO_ID in d1.contradicts

    @pytest.mark.asyncio
    async def test_decisions_only_skips_heart(self):
        heart = _make_heart(recall_results=[])
        brain = _make_brain(
            neighbors_by_node={},
            contradictions=[],
            decision_results=_make_decision_summaries(),
        )
        settings = _make_settings()

        results, stats = await run_recall_pipeline(
            query="anything",
            heart=heart,
            brain=brain,
            settings=settings,
            limit=10,
            memory_types=["decision"],
        )
        heart.recall.assert_not_called()
        assert all(r.type == "decision" for r in results)
        assert stats.n_heart_results == 0
        assert stats.n_brain_results == 2

    @pytest.mark.asyncio
    async def test_facts_only_skips_brain(self):
        heart = _make_heart(recall_results=_make_recall_results()[:1])
        brain = _make_brain(
            neighbors_by_node={},
            contradictions=[],
            decision_results=[],
        )
        settings = _make_settings(graph_recall_enabled=False)

        results, stats = await run_recall_pipeline(
            query="anything",
            heart=heart,
            brain=brain,
            settings=settings,
            limit=10,
            memory_types=["fact"],
        )
        brain.query.assert_not_called()
        assert len(results) == 1
        assert results[0].type == "fact"
        assert stats.n_brain_results == 0


# ---------------------------------------------------------------------------
# _format_pipeline_text: byte-identical snapshot
# ---------------------------------------------------------------------------


SNAPSHOT_PATH = Path(__file__).parent / "fixtures" / "recall_deep_text_snapshot.txt"


class TestFormatPipelineTextSnapshot:
    @pytest.mark.asyncio
    async def test_format_matches_committed_snapshot(self):
        """Byte-identical text output against tests/fixtures/recall_deep_text_snapshot.txt.

        This is the F051 hard invariant: the formatter must reproduce
        the pre-refactor recall_deep text exactly for the same inputs.
        """
        heart = _make_heart(recall_results=_make_recall_results())
        decisions = _make_decision_summaries()
        contradictions = [
            _FakeContradictionEdge(
                DECISION_ONE_ID, "decision", DECISION_TWO_ID, "decision"
            )
        ]
        brain = _make_brain(
            neighbors_by_node={
                FACT_ID: _make_neighbors_for_heart_seed(),
                EPISODE_ID: [],
                DECISION_ONE_ID: _make_neighbors_for_brain_seed(),
                DECISION_TWO_ID: [],
            },
            contradictions=contradictions,
            decision_results=decisions,
        )
        settings = _make_settings()

        results, stats = await run_recall_pipeline(
            query="anything",
            heart=heart,
            brain=brain,
            settings=settings,
            limit=10,
        )
        text = _format_pipeline_text(results, stats, ["all"])

        expected = SNAPSHOT_PATH.read_text(encoding="utf-8")
        assert text == expected, (
            f"Formatted text drifted from committed snapshot.\n"
            f"--- expected ---\n{expected}\n--- got ---\n{text}\n"
        )


# ---------------------------------------------------------------------------
# recall_deep: thin wrapper delegates to pipeline
# ---------------------------------------------------------------------------


class TestRecallDeepDelegatesToPipeline:
    @pytest.mark.asyncio
    async def test_recall_deep_calls_run_recall_pipeline(self):
        """recall_deep tool closure delegates to run_recall_pipeline + formats."""
        from nous.api.tools import create_nous_tools

        heart = _make_heart(recall_results=[])
        brain = _make_brain(
            neighbors_by_node={},
            contradictions=[],
            decision_results=[],
        )
        settings = _make_settings()

        tools = create_nous_tools(brain=brain, heart=heart, settings=settings)

        with patch(
            "nous.api.retrieval_pipeline.run_recall_pipeline",
            new=AsyncMock(return_value=([], PipelineStats())),
        ) as mock_pipeline:
            result = await tools["recall_deep"](query="anything", limit=5)

        mock_pipeline.assert_awaited_once()
        kwargs = mock_pipeline.await_args.kwargs
        assert kwargs["query"] == "anything"
        assert kwargs["limit"] == 5
        assert kwargs["heart"] is heart
        assert kwargs["brain"] is brain
        assert kwargs["settings"] is settings

        assert "content" in result
        # Empty results from "all" search -> both Heart + Brain empty sections
        # (preserved exactly from pre-refactor recall_deep behavior).
        text = result["content"][0]["text"]
        assert text == (
            "=== Heart Memory ===\nNo results found.\n"
            "\n=== Brain Decisions ===\nNo results found."
        )

    @pytest.mark.asyncio
    async def test_recall_deep_pipeline_exception_returns_error(self):
        from nous.api.tools import create_nous_tools

        heart = _make_heart(recall_results=[])
        brain = _make_brain(
            neighbors_by_node={},
            contradictions=[],
            decision_results=[],
        )
        settings = _make_settings()

        tools = create_nous_tools(brain=brain, heart=heart, settings=settings)

        with patch(
            "nous.api.retrieval_pipeline.run_recall_pipeline",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            result = await tools["recall_deep"](query="anything")

        text = result["content"][0]["text"]
        assert "Error searching memory" in text
        assert "boom" in text


# ---------------------------------------------------------------------------
# PipelineResult / PipelineStats are exported and frozen
# ---------------------------------------------------------------------------


class TestExports:
    def test_pipeline_result_is_frozen(self):
        r = PipelineResult(
            id=FACT_ID,
            type="fact",
            description="x",
            score=0.5,
        )
        with pytest.raises((AttributeError, Exception)):
            r.score = 0.9  # type: ignore[misc]

    def test_pipeline_stats_defaults(self):
        s = PipelineStats()
        assert s.ce_reranked is False
        assert s.mmr_applied is False
        assert s.graph_expansion_used is False
        assert s.spreading_activation_used is False
        assert s.contradiction_checks_ran is False
        assert s.n_heart_results == 0
        assert s.n_brain_results == 0
        assert s.n_graph_expanded == 0
        assert s.n_stage_errors == {}
        assert s.contradiction_edges == []
