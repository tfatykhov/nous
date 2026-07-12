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


def _make_brain(
    *, neighbors_by_node, contradictions, decision_results,
    neighbors_side_effect_override=None,
):
    """Build a MagicMock Brain whose .neighbors and .query are AsyncMocks.

    ``neighbors_by_node`` maps node_id → list[NeighborResult]. When the caller
    passes a ``neighbor_type``, the mock honors the SQL pushdown contract by
    filtering the per-node list down to that type — exactly as
    ``Brain._neighbors`` does in real life. This is critical for Path A's
    Stage 2b fan-out test, which calls ``brain.neighbors`` once per neighbor
    type at a small LIMIT.

    ``neighbors_side_effect_override`` accepts a custom async callable that
    fully replaces the lookup logic — used by the exception-path test.
    """
    brain = MagicMock()
    brain.agent_id = "nous-test-agent"
    brain.query = AsyncMock(return_value=decision_results)

    async def neighbors_side_effect(
        node_id, node_type="decision", limit=10, *, neighbor_type=None,
    ):
        rows = neighbors_by_node.get(node_id, [])
        if neighbor_type:
            rows = [r for r in rows if r.node_type == neighbor_type]
        return rows[:limit]

    brain.neighbors = AsyncMock(
        side_effect=neighbors_side_effect_override or neighbors_side_effect,
    )

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
    heart_graph_all_types_enabled=False,
    heart_graph_neighbors_per_seed=3,
    episode_chunks_enabled=False,
    episode_chunk_recall_limit=10,
    coherent_ranking_enabled=False,
    spreading_activation_density_threshold=3.0,
):
    return SimpleNamespace(
        graph_recall_enabled=graph_recall_enabled,
        cross_type_linking_enabled=cross_type_linking_enabled,
        spreading_activation_enabled=spreading_activation_enabled,
        spreading_activation_density_threshold=spreading_activation_density_threshold,
        contradiction_detection=contradiction_detection,
        graph_recall_decay=graph_recall_decay,
        graph_recall_max_expand=graph_recall_max_expand,
        graph_recall_max_neighbors=graph_recall_max_neighbors,
        heart_graph_all_types_enabled=heart_graph_all_types_enabled,
        heart_graph_neighbors_per_seed=heart_graph_neighbors_per_seed,
        episode_chunks_enabled=episode_chunks_enabled,
        episode_chunk_recall_limit=episode_chunk_recall_limit,
        coherent_ranking_enabled=coherent_ranking_enabled,
    )


# ---------------------------------------------------------------------------
# run_recall_pipeline: structured behavior
# ---------------------------------------------------------------------------


class TestCoherentRanking:
    """F080: when coherent_ranking_enabled, recall_deep excludes censors +
    procedures from the ranked pool (knowledge-only). Asserts on the ``types``
    the pipeline requests from ``heart.recall`` — what isn't searched can't rank.
    """

    @pytest.mark.asyncio
    async def test_excludes_censor_and_procedure_when_enabled(self):
        heart = _make_heart(recall_results=_make_recall_results())
        brain = _make_brain(
            neighbors_by_node={}, contradictions=[], decision_results=[],
        )
        settings = _make_settings(coherent_ranking_enabled=True)

        _results, stats = await run_recall_pipeline(
            query="anything", heart=heart, brain=brain, settings=settings,
            limit=10, memory_types=["all"],
        )

        types_arg = heart.recall.await_args.kwargs["types"]
        assert "censor" not in types_arg
        assert "procedure" not in types_arg
        assert set(types_arg) == {"episode", "fact"}
        assert stats.coherent_ranking_applied is True

    @pytest.mark.asyncio
    async def test_keeps_censor_and_procedure_when_disabled(self):
        heart = _make_heart(recall_results=_make_recall_results())
        brain = _make_brain(
            neighbors_by_node={}, contradictions=[], decision_results=[],
        )
        settings = _make_settings(coherent_ranking_enabled=False)

        _results, stats = await run_recall_pipeline(
            query="anything", heart=heart, brain=brain, settings=settings,
            limit=10, memory_types=["all"],
        )

        types_arg = heart.recall.await_args.kwargs["types"]
        assert "censor" in types_arg
        assert "procedure" in types_arg
        assert stats.coherent_ranking_applied is False

    @pytest.mark.asyncio
    async def test_explicit_procedure_request_honored_when_enabled(self):
        # codex P1: coherent ranking excludes procedures/censors only from the
        # implicit "all" pool; an explicit memory_types=["procedure"] is honored.
        heart = _make_heart(recall_results=_make_recall_results())
        brain = _make_brain(
            neighbors_by_node={}, contradictions=[], decision_results=[],
        )
        settings = _make_settings(coherent_ranking_enabled=True)

        _r, stats = await run_recall_pipeline(
            query="anything", heart=heart, brain=brain, settings=settings,
            limit=10, memory_types=["procedure"],
        )

        assert heart.recall.await_args.kwargs["types"] == ["procedure"]
        # telemetry reflects that the filter did NOT run (explicit, not search_all)
        assert stats.coherent_ranking_applied is False


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
    async def test_rerank_by_score_merges_graph_into_top_k(self):
        """F065 follow-up (2026-05-23): when rerank_by_score=True, the
        assembled list is stably re-sorted by score descending. Graph-
        expanded items whose score exceeds a heart_result's score should
        be able to rank into the top window — the whole point of the flag.
        Stable sort preserves stage order for equal scores so the snapshot-
        protected default path is untouched.
        """
        # Build a heart pool with a LOW score and a graph_expanded with a
        # HIGH score. Without the flag the graph item ends up last; with
        # the flag it should leapfrog into rank 1.
        low_score_heart = RecallResult(
            type="fact",
            id=FACT_ID,
            summary="low-scored heart hit",
            score=0.10,
        )
        heart = _make_heart(recall_results=[low_score_heart])
        # Brain neighbors with a high edge_weight so the score after
        # decay (0.7 default) is still ~0.9 — beats the heart item.
        high_score_neighbor = NeighborResult(
            id=HEART_GRAPH_DECISION_ID,
            node_type="decision",
            description="high-scoring graph neighbor",
            edge_relation="supports",
            edge_weight=1.3,  # 1.3 * 0.7 decay = 0.91, beats 0.10
            created_at=datetime.now(UTC),
        )
        brain = _make_brain(
            neighbors_by_node={
                FACT_ID: [high_score_neighbor],
                EPISODE_ID: [],
            },
            contradictions=[],
            decision_results=[],
        )
        settings = _make_settings()

        # Baseline (rerank_by_score=False, default): stage order — heart first.
        baseline_results, _ = await run_recall_pipeline(
            query="anything",
            heart=heart,
            brain=brain,
            settings=settings,
            limit=10,
        )
        assert baseline_results[0].id == FACT_ID, "default path keeps stage order"

        # Reset mocks so the heart.recall returns the same result on the
        # opt-in call.
        heart = _make_heart(recall_results=[low_score_heart])
        brain = _make_brain(
            neighbors_by_node={
                FACT_ID: [high_score_neighbor],
                EPISODE_ID: [],
            },
            contradictions=[],
            decision_results=[],
        )

        # Opt-in (rerank_by_score=True): score-sorted — graph item climbs.
        merged_results, _ = await run_recall_pipeline(
            query="anything",
            heart=heart,
            brain=brain,
            settings=settings,
            limit=10,
            rerank_by_score=True,
        )
        assert merged_results[0].id == HEART_GRAPH_DECISION_ID, (
            "rerank_by_score=True must place the high-scored graph item at rank 1"
        )
        assert merged_results[1].id == FACT_ID

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
# Path A Stage 2b: heart_graph_memory_neighbors
# ---------------------------------------------------------------------------


class TestPathAStage2b:
    """Path A (heart_graph_all_types_enabled) — Stage 2b emits non-decision
    graph neighbors from fact/episode/chunk seeds. All tests use real Settings
    flag via _make_settings(heart_graph_all_types_enabled=True)."""

    @pytest.mark.asyncio
    async def test_flag_off_byte_identical_to_baseline(self):
        """Path A must be invisible when the flag is off — no extra items,
        no extra brain.neighbors calls beyond the decision-only Stage 2."""
        heart = _make_heart(recall_results=_make_recall_results())
        brain = _make_brain(
            neighbors_by_node={
                FACT_ID: _make_neighbors_for_heart_seed(),
                EPISODE_ID: [],
            },
            contradictions=[],
            decision_results=[],
        )
        settings = _make_settings(heart_graph_all_types_enabled=False)

        results, stats = await run_recall_pipeline(
            query="anything", heart=heart, brain=brain,
            settings=settings, limit=10,
        )
        # No memory-neighbor results in pipeline output
        assert all(
            r.metadata.get("stage_origin") != "heart_graph_memory"
            for r in results
        ), "Stage 2b emitted results with flag off"
        # No memory-neighbor stage_errors created
        assert "heart_graph_memory_neighbors" not in stats.n_stage_errors
        assert "heart_graph_memory_duplicates" not in stats.n_stage_errors

    @pytest.mark.asyncio
    async def test_flag_on_appends_mixed_type_neighbors(self):
        """Path A happy path: with the flag on, fact-seed mem_neighbors of
        every non-decision type land in pipeline output with the correct
        ``type`` (not hardcoded to 'decision'), source='graph_expanded',
        and stage_origin='heart_graph_memory'."""
        from uuid import uuid4

        fact_nbr_id = uuid4()
        ep_nbr_id = uuid4()
        chunk_nbr_id = uuid4()
        proc_nbr_id = uuid4()
        # Mix of types neighboring FACT_ID seed.
        nbrs = [
            NeighborResult(
                id=fact_nbr_id, node_type="fact",
                description="FACT-NBR-DESC",
                edge_relation="related_to", edge_weight=0.8,
                created_at=datetime.now(UTC),
            ),
            NeighborResult(
                id=ep_nbr_id, node_type="episode",
                description="EP-NBR-DESC",
                edge_relation="discussed_in", edge_weight=0.7,
                created_at=datetime.now(UTC),
            ),
            NeighborResult(
                id=chunk_nbr_id, node_type="chunk",
                description="CHUNK-NBR-DESC",
                edge_relation="summarized_by", edge_weight=0.9,
                created_at=datetime.now(UTC),
            ),
            NeighborResult(
                id=proc_nbr_id, node_type="procedure",
                description="PROC-NBR-DESC",
                edge_relation="related_to", edge_weight=0.6,
                created_at=datetime.now(UTC),
            ),
        ]
        heart = _make_heart(recall_results=_make_recall_results())
        brain = _make_brain(
            neighbors_by_node={FACT_ID: nbrs, EPISODE_ID: nbrs},
            contradictions=[], decision_results=[],
        )
        settings = _make_settings(heart_graph_all_types_enabled=True)

        results, _stats = await run_recall_pipeline(
            query="anything", heart=heart, brain=brain,
            settings=settings, limit=20,
        )
        memory_results = [
            r for r in results
            if r.metadata.get("stage_origin") == "heart_graph_memory"
        ]
        types = {r.type for r in memory_results}
        # All four non-decision types present.
        assert types == {"fact", "episode", "chunk", "procedure"}, (
            f"expected all 4 non-decision types, got {types}"
        )
        # Each entry has source='graph_expanded' and a real description
        # (proves we're not emitting placeholders).
        for r in memory_results:
            assert r.source == "graph_expanded"
            assert "NBR-DESC" in (r.description or ""), (
                f"description not propagated for {r.type}: {r.description!r}"
            )

    @pytest.mark.asyncio
    async def test_strongest_seed_score_wins_for_duplicate_neighbor(self):
        """P2: a neighbor reached from multiple seeds must keep the STRONGEST seed
        score, not first-seed-wins — else a weak-seed-first neighbor is permanently
        under-scored below the top-k cutline the seed-score fix exists to clear."""
        from uuid import uuid4

        low_seed = uuid4()
        high_seed = uuid4()
        nbr_id = uuid4()

        def _nbr():
            return NeighborResult(
                id=nbr_id, node_type="fact", description="shared neighbor",
                edge_relation="related_to", edge_weight=0.8,
                created_at=datetime.now(UTC),
            )

        # Weak seed FIRST (0.4), strong seed SECOND (0.9) — the order that trips the bug.
        recall = [
            RecallResult(type="fact", id=low_seed, summary="weak seed", score=0.4),
            RecallResult(type="fact", id=high_seed, summary="strong seed", score=0.9),
        ]
        heart = _make_heart(recall_results=recall)
        brain = _make_brain(
            neighbors_by_node={low_seed: [_nbr()], high_seed: [_nbr()]},
            contradictions=[], decision_results=[],
        )
        settings = _make_settings(heart_graph_all_types_enabled=True)
        settings.graph_neighbor_seed_score_enabled = True
        settings.graph_inferred_edge_penalty = 0.5  # unused for related_to; set for safety

        results, _ = await run_recall_pipeline(
            query="anything", heart=heart, brain=brain, settings=settings, limit=20,
        )
        nbr_result = next(r for r in results if r.id == nbr_id)
        # strongest seed (0.9) x edge_weight (0.8) = 0.72, NOT 0.4 x 0.8 = 0.32
        assert abs(nbr_result.score - 0.72) < 1e-6, (
            f"expected strongest-seed score 0.72, got {nbr_result.score}"
        )

    @pytest.mark.asyncio
    async def test_best_composed_path_wins_not_phantom_combination(self):
        """P2: keep the PATH with the best composed (seed x edge) score — never combine
        a later seed with the first path's edge (a path that does not exist)."""
        from uuid import uuid4

        low_seed = uuid4()
        high_seed = uuid4()
        nbr_id = uuid4()

        def _nbr(weight):
            return NeighborResult(
                id=nbr_id, node_type="fact", description="shared neighbor",
                edge_relation="related_to", edge_weight=weight,
                created_at=datetime.now(UTC),
            )

        # Weak seed + STRONG edge FIRST (0.4*0.9 = 0.36) vs strong seed + WEAK edge
        # LATER (0.9*0.3 = 0.27). The first path is genuinely better and must win —
        # the phantom combination would be 0.9*0.9 = 0.81.
        recall = [
            RecallResult(type="fact", id=low_seed, summary="weak seed strong edge", score=0.4),
            RecallResult(type="fact", id=high_seed, summary="strong seed weak edge", score=0.9),
        ]
        heart = _make_heart(recall_results=recall)
        brain = _make_brain(
            neighbors_by_node={low_seed: [_nbr(0.9)], high_seed: [_nbr(0.3)]},
            contradictions=[], decision_results=[],
        )
        settings = _make_settings(heart_graph_all_types_enabled=True)
        settings.graph_neighbor_seed_score_enabled = True
        settings.graph_inferred_edge_penalty = 0.5

        results, _ = await run_recall_pipeline(
            query="anything", heart=heart, brain=brain, settings=settings, limit=20,
        )
        nbr_result = next(r for r in results if r.id == nbr_id)
        # best REAL path = 0.4 x 0.9 = 0.36; NOT phantom 0.81 and NOT 0.27
        assert abs(nbr_result.score - 0.36) < 1e-6, (
            f"expected best composed path 0.36, got {nbr_result.score}"
        )

    @pytest.mark.asyncio
    async def test_skips_decisions_and_dedups_against_pool(self):
        """Stage 2b must (1) skip neighbors of type='decision' (already
        handled by Stage 2), (2) skip duplicates already in heart_results,
        (3) skip duplicates already in chunk_results, (4) count the
        chunk/heart duplicates as heart_graph_memory_duplicates so eval
        can distinguish corroboration from noise."""
        from uuid import uuid4

        dup_chunk_id = uuid4()  # will already be in chunk_results
        fresh_fact_id = uuid4()  # should make it through
        decision_neighbor_id = uuid4()  # must be skipped
        nbrs = [
            NeighborResult(
                id=fresh_fact_id, node_type="fact", description="FRESH",
                edge_relation="related_to", edge_weight=0.8,
                created_at=datetime.now(UTC),
            ),
            NeighborResult(
                id=FACT_ID, node_type="fact", description="DUP-HEART",
                edge_relation="related_to", edge_weight=0.7,
                created_at=datetime.now(UTC),
            ),
            NeighborResult(
                id=dup_chunk_id, node_type="chunk", description="DUP-CHUNK",
                edge_relation="summarized_by", edge_weight=0.9,
                created_at=datetime.now(UTC),
            ),
            NeighborResult(
                id=decision_neighbor_id, node_type="decision",
                description="MUST-SKIP-DECISION",
                edge_relation="informed_by", edge_weight=0.9,
                created_at=datetime.now(UTC),
            ),
        ]
        heart = _make_heart(recall_results=_make_recall_results())
        brain = _make_brain(
            neighbors_by_node={FACT_ID: nbrs, EPISODE_ID: nbrs},
            contradictions=[], decision_results=[],
        )
        # Enable the F067 chunk-search leg so acc.chunk_results gets populated,
        # exercising Stage 2b's chunk-result dedup branch.
        settings = _make_settings(
            heart_graph_all_types_enabled=True,
            episode_chunks_enabled=True,
        )
        # Pre-load a chunk result that matches dup_chunk_id to test the
        # chunk_results dedup branch.
        original_search_chunks_path = (
            "nous.api.retrieval_pipeline._search_episode_chunks"
        )
        # _search_episode_chunks returns 4-tuples (id, content, score,
        # episode_id) — see retrieval_pipeline.py:825. The earlier draft
        # of this test used a 3-tuple, which prevented the test from
        # catching the production unpacking bug (`{cid for cid, _, _ in
        # acc.chunk_results}` against 4-tuples). Match the real shape.
        from uuid import uuid4 as _u
        dup_chunk_episode_id = _u()
        with patch(original_search_chunks_path, new=AsyncMock(
            return_value=[(
                dup_chunk_id, "dup chunk content", 0.5, dup_chunk_episode_id,
            )],
        )):
            results, stats = await run_recall_pipeline(
                query="anything", heart=heart, brain=brain,
                settings=settings, limit=20,
            )

        mem = [
            r for r in results
            if r.metadata.get("stage_origin") == "heart_graph_memory"
        ]
        mem_ids = {r.id for r in mem}
        assert fresh_fact_id in mem_ids, "fresh non-dup fact must pass"
        assert FACT_ID not in mem_ids, "heart-result dup must be filtered"
        assert dup_chunk_id not in mem_ids, "chunk-result dup must be filtered"
        assert decision_neighbor_id not in mem_ids, (
            "decision neighbor must be skipped (Stage 2's territory)"
        )
        # Duplicate counter fired (FACT_ID + dup_chunk_id each observed
        # once per seed × once per neighbor_type they match).
        dup_count = stats.n_stage_errors.get("heart_graph_memory_duplicates", 0)
        assert dup_count >= 2, (
            f"expected >=2 dedup events (heart + chunk), got {dup_count}"
        )

    @pytest.mark.asyncio
    async def test_brain_neighbors_exception_increments_counter(self):
        """When brain.neighbors raises, Stage 2b logs+counts per-(seed, nbr_type)
        and continues with the next seed. Counter must surface to stats."""
        async def always_raise(*args, **kwargs):
            raise RuntimeError("simulated brain.neighbors failure")

        heart = _make_heart(recall_results=_make_recall_results())
        brain = _make_brain(
            neighbors_by_node={},
            contradictions=[],
            decision_results=[],
            neighbors_side_effect_override=always_raise,
        )
        settings = _make_settings(heart_graph_all_types_enabled=True)

        # Should NOT propagate the exception — pipeline returns normally.
        results, stats = await run_recall_pipeline(
            query="anything", heart=heart, brain=brain,
            settings=settings, limit=10,
        )
        # Heart results still flow through; only Stage 2 + 2b were affected.
        assert any(r.source == "heart" for r in results), (
            "heart_results must still arrive when brain.neighbors fails"
        )
        # Counter incremented at least once. The fan-out runs N seeds ×
        # 4 nbr_types per seed; with 2 heart seeds (fact + episode), that's
        # 8 expected exception events.
        count = stats.n_stage_errors.get("heart_graph_memory_neighbors", 0)
        assert count >= 1, (
            f"expected heart_graph_memory_neighbors counter to increment, "
            f"got {stats.n_stage_errors}"
        )


# ---------------------------------------------------------------------------
# Spreading-activation branch: content resolution + density-gate caching
# ---------------------------------------------------------------------------


SPREAD_FACT_ID = UUID("77777777-7777-7777-7777-777777777777")


class TestSpreadingContentResolution:
    """The spreading branch must resolve real node content via
    ``Brain._resolve_node_descriptions`` (shared with ``_neighbors``) instead
    of fabricating ``[<ntype>] <uuid8>`` placeholder descriptions, and must
    drop nodes the resolver does not return (inactive/missing rows)."""

    def _spreading_fixtures(self, *, resolved):
        heart = _make_heart(recall_results=[])
        brain = _make_brain(
            neighbors_by_node={},
            contradictions=[],
            decision_results=_make_decision_summaries(),
        )
        brain._resolve_node_descriptions = AsyncMock(return_value=resolved)
        return heart, brain

    @pytest.mark.asyncio
    async def test_spreading_results_carry_real_content(self):
        resolved_at = datetime(2026, 1, 5, tzinfo=UTC)
        heart, brain = self._spreading_fixtures(
            resolved={
                SPREAD_FACT_ID: ("real fact content about pgvector", resolved_at)
            },
        )
        settings = _make_settings(spreading_activation_enabled="true")

        with patch(
            "nous.brain.spreading_activation.spreading_activation_search",
            AsyncMock(return_value=[(SPREAD_FACT_ID, "fact", 0.6)]),
        ), patch(
            "nous.brain.spreading_activation.compute_graph_density",
            AsyncMock(return_value=5.0),
        ):
            results, stats = await run_recall_pipeline(
                query="anything", heart=heart, brain=brain,
                settings=settings, limit=10,
            )

        assert stats.spreading_activation_used is True
        spread = next(r for r in results if r.id == SPREAD_FACT_ID)
        assert spread.source == "spreading_activation"
        assert spread.description == "real fact content about pgvector"

    @pytest.mark.asyncio
    async def test_spreading_drops_unresolvable_nodes(self):
        heart, brain = self._spreading_fixtures(resolved={})
        settings = _make_settings(spreading_activation_enabled="true")

        with patch(
            "nous.brain.spreading_activation.spreading_activation_search",
            AsyncMock(return_value=[(SPREAD_FACT_ID, "fact", 0.6)]),
        ), patch(
            "nous.brain.spreading_activation.compute_graph_density",
            AsyncMock(return_value=5.0),
        ):
            results, stats = await run_recall_pipeline(
                query="anything", heart=heart, brain=brain,
                settings=settings, limit=10,
            )

        # Zero hits resolved -> the pipeline falls back to 1-hop (round-6
        # refinement), so spreading is reported unused for this recall.
        assert stats.spreading_activation_used is False
        assert all(r.id != SPREAD_FACT_ID for r in results), (
            "unresolvable spreading node must be dropped, not surfaced as a "
            "placeholder"
        )

    @pytest.mark.asyncio
    async def test_spreading_overfetches_and_caps_after_resolution(self):
        """Codex P2 round 5 (PR #555): the CTE's hard LIMIT ran BEFORE the
        resolution drop, so dropped rows consumed the window. The pipeline
        must over-fetch (limit=40) and cap appended results at 20 AFTER
        resolution, so drops backfill from lower-ranked valid nodes."""
        # 30 resolvable hits: first 10 unresolvable (dropped), next 20 resolve.
        hits = [(UUID(int=1000 + i), "fact", 0.9 - i * 0.01) for i in range(30)]
        resolved = {
            nid: (f"content {i}", datetime(2026, 1, 5, tzinfo=UTC))
            for i, (nid, _t, _a) in enumerate(hits)
            if i >= 10
        }
        heart, brain = self._spreading_fixtures(resolved=resolved)
        settings = _make_settings(spreading_activation_enabled="true")
        search_mock = AsyncMock(return_value=hits)

        with patch(
            "nous.brain.spreading_activation.spreading_activation_search",
            search_mock,
        ):
            results, stats = await run_recall_pipeline(
                query="anything", heart=heart, brain=brain,
                settings=settings, limit=10,
            )

        assert stats.spreading_activation_used is True
        # Over-fetch wiring: the CTE limit must be raised above the 20 cap.
        assert search_mock.await_args.kwargs.get("limit") == 40
        spread_ids = [r.id for r in results if r.source == "spreading_activation"]
        # All 20 resolvable hits surface — the 10 dropped rows did not
        # consume the cap.
        assert len(spread_ids) == 20
        assert set(spread_ids) == set(resolved.keys())

    @pytest.mark.asyncio
    async def test_spreading_cap_bounds_appended_results(self):
        """Even when everything resolves, at most 20 spreading results append."""
        hits = [(UUID(int=2000 + i), "fact", 0.9 - i * 0.01) for i in range(30)]
        resolved = {
            nid: (f"content {i}", datetime(2026, 1, 5, tzinfo=UTC))
            for i, (nid, _t, _a) in enumerate(hits)
        }
        heart, brain = self._spreading_fixtures(resolved=resolved)
        settings = _make_settings(spreading_activation_enabled="true")

        with patch(
            "nous.brain.spreading_activation.spreading_activation_search",
            AsyncMock(return_value=hits),
        ):
            results, _stats = await run_recall_pipeline(
                query="anything", heart=heart, brain=brain,
                settings=settings, limit=10,
            )

        spread_ids = [r.id for r in results if r.source == "spreading_activation"]
        assert len(spread_ids) == 20
        # Highest-activation resolvable hits win the cap (order preserved).
        assert spread_ids == [nid for nid, _t, _a in hits[:20]]

    @pytest.mark.asyncio
    async def test_spreading_zero_resolved_falls_back_to_one_hop(self):
        """If every spreading hit is dropped by resolution (inactive/foreign/
        dangling), fall back to 1-hop expansion instead of returning an empty
        graph expansion. Pre-PR#555 this state was unreachable (placeholders
        always appended); the drop made it real."""
        heart, brain = self._spreading_fixtures(resolved={})
        one_hop = NeighborResult(
            id=UUID(int=3001),
            node_type="decision",
            description="one-hop fallback neighbor",
            edge_relation="supports",
            edge_weight=0.8,
            created_at=datetime(2026, 1, 5, tzinfo=UTC),
        )
        brain.neighbors = AsyncMock(return_value=[one_hop])
        settings = _make_settings(spreading_activation_enabled="true")

        with patch(
            "nous.brain.spreading_activation.spreading_activation_search",
            AsyncMock(return_value=[(SPREAD_FACT_ID, "fact", 0.6)]),
        ):
            results, stats = await run_recall_pipeline(
                query="anything", heart=heart, brain=brain,
                settings=settings, limit=10,
            )

        assert any(r.id == one_hop.id for r in results), (
            "1-hop fallback must fire when spreading resolves nothing"
        )
        assert stats.spreading_activation_used is False

    @pytest.mark.asyncio
    async def test_true_mode_skips_density_query(self):
        heart, brain = self._spreading_fixtures(resolved={})
        settings = _make_settings(spreading_activation_enabled="true")
        density_mock = AsyncMock(return_value=5.0)

        with patch(
            "nous.brain.spreading_activation.spreading_activation_search",
            AsyncMock(return_value=[]),
        ), patch(
            "nous.brain.spreading_activation.compute_graph_density",
            density_mock,
        ):
            await run_recall_pipeline(
                query="anything", heart=heart, brain=brain,
                settings=settings, limit=10,
            )

        assert density_mock.await_count == 0, (
            "forced-on mode must not pay the density aggregate query"
        )

    @pytest.mark.asyncio
    async def test_auto_mode_density_gate_cached_across_recalls(self):
        heart, brain = self._spreading_fixtures(resolved={})
        settings = _make_settings(
            spreading_activation_enabled="auto",
            spreading_activation_density_threshold=3.0,
        )
        density_mock = AsyncMock(return_value=5.0)

        with patch(
            "nous.brain.spreading_activation.spreading_activation_search",
            AsyncMock(return_value=[]),
        ), patch(
            "nous.brain.spreading_activation.compute_graph_density",
            density_mock,
        ):
            await run_recall_pipeline(
                query="anything", heart=heart, brain=brain,
                settings=settings, limit=10,
            )
            await run_recall_pipeline(
                query="anything", heart=heart, brain=brain,
                settings=settings, limit=10,
            )

        assert density_mock.await_count == 1, (
            "density gate must be TTL-cached per Brain instance across recalls"
        )


# ---------------------------------------------------------------------------
# Heart-seeded spreading activation (NOUS_SPREADING_HEART_SEEDS_ENABLED)
# ---------------------------------------------------------------------------


SPREAD_NEIGHBOR_ID = UUID("88888888-8888-8888-8888-888888888888")


class TestSpreadingHeartSeeds:
    """F022 extension (default behavior): the spreading CTE is seeded with
    the top heart FACT results (RRF-scored) alongside decision seeds, so
    spreading fires on decision-less corpora and leverages the fact/chunk
    graph instead of decisions only."""

    def _fixtures(self, *, recall_results, decision_results, resolved):
        heart = _make_heart(recall_results=recall_results)
        brain = _make_brain(
            neighbors_by_node={},
            contradictions=[],
            decision_results=decision_results,
        )
        brain._resolve_node_descriptions = AsyncMock(return_value=resolved)
        return heart, brain

    @pytest.mark.asyncio
    async def test_fact_seeds_fire_without_decisions(self):
        resolved_at = datetime(2026, 1, 5, tzinfo=UTC)
        heart, brain = self._fixtures(
            recall_results=_make_recall_results(),  # fact 0.9 + episode 0.8
            decision_results=[],
            resolved={SPREAD_NEIGHBOR_ID: ("spread-reached fact", resolved_at)},
        )
        settings = _make_settings(spreading_activation_enabled="true")
        search_mock = AsyncMock(
            return_value=[(SPREAD_NEIGHBOR_ID, "fact", 0.5)]
        )

        with patch(
            "nous.brain.spreading_activation.spreading_activation_search",
            search_mock,
        ):
            results, stats = await run_recall_pipeline(
                query="anything", heart=heart, brain=brain,
                settings=settings, limit=10,
            )

        # Seeds: only the FACT heart result, with its RRF score — episodes
        # are not seeds (reachable via traversal instead).
        seeds_arg = search_mock.await_args.args[2]
        assert seeds_arg == [(FACT_ID, "fact", 0.9)]
        assert stats.spreading_activation_used is True
        assert any(r.id == SPREAD_NEIGHBOR_ID for r in results)

    @pytest.mark.asyncio
    async def test_non_decision_spread_hits_route_to_heart_memory(self):
        """Codex P2 round 3 (PR #556): fact/episode/chunk/procedure spreading
        hits must carry stage_origin='heart_graph_memory' (typed Heart
        Memory rendering, like Path A) — NOT 'brain_graph', which makes
        recall_deep present memory facts under '=== Brain Decisions ==='."""
        resolved_at = datetime(2026, 1, 5, tzinfo=UTC)
        heart, brain = self._fixtures(
            recall_results=_make_recall_results(),
            decision_results=[],
            resolved={
                SPREAD_NEIGHBOR_ID: ("spread-reached fact", resolved_at),
                GRAPH_DECISION_ID: ("spread-reached decision", resolved_at),
            },
        )
        settings = _make_settings(spreading_activation_enabled="true")

        with patch(
            "nous.brain.spreading_activation.spreading_activation_search",
            AsyncMock(return_value=[
                (SPREAD_NEIGHBOR_ID, "fact", 0.5),
                (GRAPH_DECISION_ID, "decision", 0.4),
            ]),
        ):
            results, _stats = await run_recall_pipeline(
                query="anything", heart=heart, brain=brain,
                settings=settings, limit=10,
            )

        fact_row = next(r for r in results if r.id == SPREAD_NEIGHBOR_ID)
        decision_row = next(r for r in results if r.id == GRAPH_DECISION_ID)
        assert fact_row.metadata.get("stage_origin") == "heart_graph_memory"
        assert decision_row.metadata.get("stage_origin") == "brain_graph"

    @pytest.mark.asyncio
    async def test_candidate_exclusions_pushed_into_cte(self):
        """Codex P2 round 2 (PR #556): known duplicates (seeds, decision ids,
        heart/chunk/graph-stage candidates) must be excluded INSIDE the
        activation query, not post-filtered — otherwise they consume the
        CTE's result window and novel neighbors below the limit are lost."""
        heart, brain = self._fixtures(
            recall_results=_make_recall_results(),
            decision_results=_make_decision_summaries(),
            resolved={},
        )
        settings = _make_settings(spreading_activation_enabled="true")
        search_mock = AsyncMock(return_value=[])

        with patch(
            "nous.brain.spreading_activation.spreading_activation_search",
            search_mock,
        ):
            await run_recall_pipeline(
                query="anything", heart=heart, brain=brain,
                settings=settings, limit=10,
            )

        excluded = search_mock.await_args.kwargs.get("exclude_ids")
        assert excluded is not None, "exclude_ids must be passed to the CTE"
        # Heart/chunk candidates and decision ids are known pre-call.
        assert EPISODE_ID in excluded, "heart candidate must be CTE-excluded"
        assert FACT_ID in excluded, "fact candidate/seed must be CTE-excluded"
        assert DECISION_ONE_ID in excluded, "decision id must be CTE-excluded"

    @pytest.mark.asyncio
    async def test_combines_decision_and_fact_seeds(self):
        heart, brain = self._fixtures(
            recall_results=_make_recall_results(),
            decision_results=_make_decision_summaries(),
            resolved={},
        )
        settings = _make_settings(spreading_activation_enabled="true")
        search_mock = AsyncMock(return_value=[])

        with patch(
            "nous.brain.spreading_activation.spreading_activation_search",
            search_mock,
        ):
            await run_recall_pipeline(
                query="anything", heart=heart, brain=brain,
                settings=settings, limit=10,
            )

        seeds_arg = search_mock.await_args.args[2]
        assert (DECISION_ONE_ID, "decision", 0.75) in seeds_arg
        assert (FACT_ID, "fact", 0.9) in seeds_arg

    @pytest.mark.asyncio
    async def test_spread_hit_already_in_graph_stage_not_duplicated(self):
        """Codex P2 (PR #556): a node already surfaced by Stage 2 heart-graph
        (or Path A) must not be re-appended by heart-seeded spreading — in
        the decision-less path ``seen_ids`` is empty, so the dedup set must
        include prior graph-stage outputs, not just direct heart/chunk ids."""
        resolved_at = datetime(2026, 1, 5, tzinfo=UTC)
        heart = _make_heart(recall_results=_make_recall_results())
        brain = _make_brain(
            neighbors_by_node={FACT_ID: _make_neighbors_for_heart_seed()},
            contradictions=[],
            decision_results=[],  # decision-less: seen_ids starts empty
        )
        brain._resolve_node_descriptions = AsyncMock(
            return_value={
                HEART_GRAPH_DECISION_ID: ("decision via heart graph", resolved_at)
            }
        )
        settings = _make_settings(spreading_activation_enabled="true")

        with patch(
            "nous.brain.spreading_activation.spreading_activation_search",
            AsyncMock(return_value=[(HEART_GRAPH_DECISION_ID, "decision", 0.6)]),
        ):
            results, _stats = await run_recall_pipeline(
                query="anything", heart=heart, brain=brain,
                settings=settings, limit=10,
            )

        assert sum(1 for r in results if r.id == HEART_GRAPH_DECISION_ID) == 1, (
            "Stage-2 graph result must not be re-appended by spreading"
        )

    @pytest.mark.asyncio
    async def test_spread_hit_already_in_candidates_not_duplicated(self):
        """A spreading hit that is already a heart candidate (e.g. the
        EPISODE_ID heart result) must not append a second ranked row."""
        resolved_at = datetime(2026, 1, 5, tzinfo=UTC)
        heart, brain = self._fixtures(
            recall_results=_make_recall_results(),
            decision_results=[],
            resolved={EPISODE_ID: ("episode summary two", resolved_at)},
        )
        settings = _make_settings(spreading_activation_enabled="true")

        with patch(
            "nous.brain.spreading_activation.spreading_activation_search",
            AsyncMock(return_value=[(EPISODE_ID, "episode", 0.7)]),
        ):
            results, _stats = await run_recall_pipeline(
                query="anything", heart=heart, brain=brain,
                settings=settings, limit=10,
            )

        assert sum(1 for r in results if r.id == EPISODE_ID) == 1, (
            "existing heart candidate must not be re-appended by spreading"
        )


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
