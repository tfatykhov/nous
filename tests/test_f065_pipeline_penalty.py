"""F065 Commit C: tests for the inferred-edge penalty multiplier in
retrieval_pipeline.

Covers test plan items 3, 4, 4b verbatim plus spreading-activation
defense-in-depth.

The helper is pure (no DB / runner), so we feed it constructed
NeighborResult objects and inspect the score.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from nous.api.retrieval_pipeline import (
    _f065_provenance_penalty,
    _graph_expanded_to_pipeline,
    _heart_graph_to_pipeline,
)
from nous.brain.schemas import NeighborResult
from nous.config import Settings


def _neighbor(
    *,
    method: str = "heuristic",
    relation: str = "related_to",
    weight: float = 1.0,
    seed_score: float | None = None,
    node_type: str = "decision",
    description: str = "x",
) -> NeighborResult:
    return NeighborResult(
        id=uuid.uuid4(),
        node_type=node_type,
        description=description,
        edge_relation=relation,
        edge_weight=weight,
        created_at=datetime.now(UTC),
        extraction_method=method,
        seed_score=seed_score,
    )


class TestProvenancePenaltyHelper:
    def test_default_1_0_is_byte_identical_to_f022_baseline(self) -> None:
        settings = Settings(graph_inferred_edge_penalty=1.0)
        decay = settings.graph_recall_decay
        for method in ("deterministic", "heuristic", "inferred"):
            n = _neighbor(method=method, weight=1.0)
            assert _f065_provenance_penalty(n, 1.0, decay, settings) == pytest.approx(decay)

    def test_active_penalty_downweights_inferred_only(self) -> None:
        settings = Settings(graph_inferred_edge_penalty=0.7)
        decay = settings.graph_recall_decay
        det = _neighbor(method="deterministic", weight=1.0)
        heu = _neighbor(method="heuristic", weight=1.0)
        inf = _neighbor(method="inferred", weight=1.0, relation="contradicts")

        assert _f065_provenance_penalty(det, 1.0, decay, settings) == pytest.approx(decay)
        assert _f065_provenance_penalty(heu, 1.0, decay, settings) == pytest.approx(decay)
        assert _f065_provenance_penalty(inf, 1.0, decay, settings) == pytest.approx(decay * 0.7)

    def test_null_extraction_method_treated_as_heuristic(self) -> None:
        """F065 spec rule: NULL → heuristic (fail-open).
        We construct a NeighborResult with extraction_method='' which is
        falsy, and confirm the helper applies no penalty."""
        settings = Settings(graph_inferred_edge_penalty=0.7)
        decay = settings.graph_recall_decay
        # Use pydantic to bypass the str default: construct then mutate.
        n = _neighbor(method="heuristic", weight=1.0)
        # Simulate a row that somehow arrives without a tier (e.g.
        # SA-synthesized via a future bypass path).
        n.extraction_method = ""  # falsy, drives `or "heuristic"` fallback
        assert _f065_provenance_penalty(n, 1.0, decay, settings) == pytest.approx(decay)

    def test_spreading_activation_short_circuits_penalty(self) -> None:
        """Defense in depth: SA results never get the inferred penalty
        even if their extraction_method happens to be 'inferred'."""
        settings = Settings(graph_inferred_edge_penalty=0.7)
        decay = settings.graph_recall_decay
        n = _neighbor(
            method="inferred",
            relation="spreading_activation",
            weight=1.0,
        )
        # No penalty applied — score is just base * decay.
        assert _f065_provenance_penalty(n, 1.0, decay, settings) == pytest.approx(decay)


class TestPipelineHelpers:
    def test_graph_expanded_applies_penalty(self) -> None:
        settings = Settings(graph_inferred_edge_penalty=0.7)
        decay = settings.graph_recall_decay
        inferred = _neighbor(method="inferred", relation="contradicts", weight=1.0)
        deterministic = _neighbor(method="deterministic", relation="supersedes", weight=1.0)
        results = _graph_expanded_to_pipeline([inferred, deterministic], settings)
        assert results[0].score == pytest.approx(decay * 0.7)
        assert results[1].score == pytest.approx(decay)

    def test_heart_graph_applies_penalty(self) -> None:
        """The Heart-graph path must apply the penalty too (P1 from review)."""
        settings = Settings(graph_inferred_edge_penalty=0.7)
        decay = settings.graph_recall_decay
        inferred = _neighbor(method="inferred", relation="contradicts", weight=1.0)
        results = _heart_graph_to_pipeline([inferred], settings)
        assert results[0].score == pytest.approx(decay * 0.7)

    def test_default_settings_byte_identical_in_pipelines(self) -> None:
        """With the dark-launch default 1.0, both pipelines produce the
        same scores they would have under pre-F065 behavior."""
        settings = Settings()  # default penalty=1.0
        decay = settings.graph_recall_decay
        inferred = _neighbor(method="inferred", weight=0.85)
        # Both helpers should return base * decay only.
        ge = _graph_expanded_to_pipeline([inferred], settings)
        hg = _heart_graph_to_pipeline([inferred], settings)
        assert ge[0].score == pytest.approx(0.85 * decay)
        assert hg[0].score == pytest.approx(0.85 * decay)


class TestSeedScoreScoring:
    """Plan 1.2: Stage 2 / Stage 4 converters join Path A's seed-anchored
    scale via the shared _score_memory_neighbor — seed × edge × penalty
    when graph_neighbor_seed_score_enabled and the loop threaded a
    seed_score; exact legacy formula otherwise."""

    def test_heart_graph_to_pipeline_uses_seed_score_when_flag_on(self) -> None:
        settings = Settings(
            graph_neighbor_seed_score_enabled=True,
            graph_inferred_edge_penalty=0.5,
            graph_recall_decay=0.7,
        )
        n = _neighbor(method="inferred", weight=0.8, seed_score=0.9)
        [res] = _heart_graph_to_pipeline([n], settings)
        assert res.score == pytest.approx(0.9 * 0.8 * 0.5)

    def test_heart_graph_to_pipeline_legacy_when_flag_off_or_no_seed(self) -> None:
        """Flag off, or seed_score None (spreading rows / legacy callers),
        must reproduce the pre-plan-1.2 formula edge × decay × penalty
        exactly — the byte-identical flag-off invariant."""
        settings = Settings(
            graph_neighbor_seed_score_enabled=False,
            graph_recall_decay=0.7,
        )
        n = _neighbor(weight=0.8, seed_score=0.9)
        [res] = _heart_graph_to_pipeline([n], settings)
        assert res.score == pytest.approx(0.8 * 0.7)

        settings_on = Settings(
            graph_neighbor_seed_score_enabled=True,
            graph_recall_decay=0.7,
        )
        n2 = _neighbor(weight=0.8, seed_score=None)
        [res2] = _heart_graph_to_pipeline([n2], settings_on)
        assert res2.score == pytest.approx(0.8 * 0.7)

    def test_seed_score_path_clamps_edge_weight_above_one(self) -> None:
        """graph_edges.weight has no DB CHECK <= 1; a rogue 1.3 edge must not
        push a seed-scored neighbor above the direct-hit scale (codex PR #558
        P2 — mirrors the spreading-CTE clamp)."""
        settings = Settings(
            graph_neighbor_seed_score_enabled=True,
            graph_recall_decay=0.7,
        )
        n = _neighbor(weight=1.3, seed_score=0.9)
        [res] = _heart_graph_to_pipeline([n], settings)
        assert res.score == pytest.approx(0.9 * 1.0), (
            "edge term must clamp at 1.0, not multiply seed by 1.3"
        )

    def test_graph_expanded_to_pipeline_uses_seed_score_for_one_hop(self) -> None:
        """1-hop expansion rows (seed_score threaded) score seed×edge×penalty
        under the flag; spreading rows (seed_score None, edge_weight = bounded
        activation) keep activation × decay."""
        settings = Settings(
            graph_neighbor_seed_score_enabled=True,
            graph_recall_decay=0.7,
        )
        one_hop = _neighbor(
            node_type="fact", description="f", weight=0.7, seed_score=0.6,
        )
        spreading = _neighbor(
            node_type="fact", description="f2",
            relation="spreading_activation", weight=0.4,
        )
        res = {
            r.description: r
            for r in _graph_expanded_to_pipeline([one_hop, spreading], settings)
        }
        assert res["f"].score == pytest.approx(0.6 * 0.7)
        # Spreading branch: observed once the converter swap lands (Step 4);
        # pins the forward regression that spreading stays on activation×decay.
        assert res["f2"].score == pytest.approx(0.4 * 0.7)
