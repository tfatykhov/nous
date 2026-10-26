"""F105: depth-1 parity, the query gate, the budget, the rescore and ``sat``
aggregation, all on at once in the pipeline (mocked CTE and cosines).

Seeds: DECISION_ONE (0.75), DECISION_TWO (None, so 0.5) and the heart fact FACT
(0.9). Under ``max`` a row's activation is at most its seed's score x decay
(0.45 here), so the activations above the decay that the mock returns (0.9,
0.6) are what ``sat`` gives a node that several seeds reach. Parity scores a
row ``activation / decay``, which would make them 1.8 and 1.2 without the cap
at 1. One run cannot show everything: a full prune leaves the budget nothing to
cut and the cap nothing to bind on, so the test runs the pipeline twice.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from test_retrieval_pipeline import (  # noqa: E402  (test-local helper reuse)
    DECISION_ONE_ID,
    EPISODE_ID,
    FACT_ID,
    _make_brain,
    _make_decision_summaries,
    _make_heart,
    _make_recall_results,
    _make_settings,
)

from nous.api.retrieval_pipeline import PipelineResult, run_recall_pipeline
from nous.brain.schemas import NeighborResult
from nous.observability.retrieval_trace import RetrievalTrace

SA = "spreading_activation"
SA_STAGE = "stage4_spreading_activation"
AT = datetime(2026, 1, 5, tzinfo=UTC)
NA, NB, NC = (UUID(f"ffffffff-0000-0000-0000-00000000000{i}") for i in range(1, 4))
DIM = 1536

COMBINED: dict[str, Any] = {
    "spreading_score_depth1_parity": True,
    "graph_neighbor_seed_score_enabled": True,
    "graph_inferred_edge_penalty": 1.0,
    "spreading_activation_decay": 0.5,
    "spreading_aggregation": "sat",
    "spreading_query_gate_enabled": True,
    "spreading_query_gate_mode": "seed_relative",
    "spreading_query_gate_seed_min": 0.19,
    "spreading_budget_enabled": True,
    "spreading_budget_max_rows": 1,
    "spreading_budget_cutline_rank": 0,
    "spreading_rescore_existing_enabled": True,
    "spreading_rescore_beta": 0.15,
}


async def _run(
    *,
    activated: list[tuple[UUID, str, float, int]],
    seeds_out: dict[UUID, UUID],
    cosines: dict[UUID, float | None],
    neighbors_by_node: dict[UUID, list[NeighborResult]] | None = None,
) -> tuple[list[PipelineResult], RetrievalTrace]:
    """Heart: FACT (0.9, a fact seed) and EPISODE (0.8, a rescore id); the CTE
    activates ``activated`` and gives EPISODE an activation of 1.0 (FACT's
    entry is ignored, it is a seed)."""
    heart = _make_heart(recall_results=_make_recall_results())
    brain = _make_brain(neighbors_by_node=neighbors_by_node or {}, contradictions=[],
                        decision_results=_make_decision_summaries())
    brain._resolve_node_descriptions = AsyncMock(
        return_value={nid: (f"row {nid}", AT) for nid, *_ in activated})
    brain.embeddings = MagicMock()
    brain.embeddings.embed = AsyncMock(return_value=[1.0] + [0.0] * (DIM - 1))
    settings = _make_settings(spreading_activation_enabled="true")
    for key, value in COMBINED.items():
        setattr(settings, key, value)

    async def fake_search(*_a: Any, seed_out=None, rescore_out=None, **_k: Any):
        if seed_out is not None:
            seed_out.update(seeds_out)
        if rescore_out is not None:
            rescore_out.update({EPISODE_ID: 1.0, FACT_ID: 1.0})
        return list(activated)

    tr = RetrievalTrace(query="q")
    with patch(
        "nous.brain.spreading_activation.spreading_activation_search", AsyncMock(side_effect=fake_search),
    ), patch(
        "nous.brain.spreading_activation.compute_graph_density", AsyncMock(return_value=5.0),
    ), patch(
        "nous.brain.spreading_activation.query_cosines", AsyncMock(return_value=cosines),
    ):
        results, _stats = await run_recall_pipeline(
            query="q", heart=heart, brain=brain, settings=settings, limit=10,
            rerank_by_score=True, trace=tr,
        )
    return results, tr


def _legs(tr: RetrievalTrace) -> dict[str, dict]:
    return {leg["name"]: leg for leg in tr.to_dict()["legs"]}


def _sa_rows(tr: RetrievalTrace) -> dict[str, dict]:
    return {e["neighbor_id"]: e for e in tr.to_dict()["expansions"] if e["stage"] == SA_STAGE}


def _rescored(tr: RetrievalTrace) -> list[dict]:
    cand = {c["id"]: c for c in tr.to_dict()["candidates"]}[str(EPISODE_ID)]
    return [m for m in cand["mutations"] if m["stage"] == "spreading_rescore"]


async def test_combined_arm_in_pipeline():
    # 1. The gate keeps every row (NC at half its activation). Parity scores NA
    #    and NB at the cap, the budget keeps NA alone, and EPISODE is raised.
    results, tr = await _run(
        activated=[(NA, "fact", 0.9, 1), (NB, "fact", 0.6, 1), (NC, "fact", 0.3, 2)],
        seeds_out={NA: DECISION_ONE_ID, NB: FACT_ID, NC: DECISION_ONE_ID},
        cosines={NA: 0.5, NB: 0.9, NC: 0.25, DECISION_ONE_ID: 0.5, FACT_ID: 0.5},
    )
    assert max(r.score or 0.0 for r in results) <= 1.0
    by_id = {r.id: r for r in results}
    assert by_id[NA].score == 1.0, "non-vacuous: the cap binds (0.9 / 0.5 = 1.8)"
    assert [r.id for r in results if r.source == SA] == [NA]
    assert _legs(tr)[SA]["n_returned"] == 3
    assert _legs(tr)[SA]["n_dropped"] == 2
    assert _sa_rows(tr)[str(NC)]["edge_weight"] == pytest.approx(0.15), "the gate ran"
    assert by_id[EPISODE_ID].score == pytest.approx(0.92)
    assert len(_rescored(tr)) == 1

    # 2. The gate prunes every row: spreading stays the mechanism, so the 1-hop
    #    fallback (which would re-add NA, DECISION_ONE's neighbour) does not run
    #    and the rescore still applies.
    one_hop = {DECISION_ONE_ID: [NeighborResult(
        id=NA, node_type="fact", description="one hop", edge_relation="related_to",
        edge_weight=0.8, created_at=AT,
    )]}
    results, tr = await _run(
        activated=[(NA, "fact", 0.9, 1)], seeds_out={NA: DECISION_ONE_ID},
        cosines={NA: -0.1, DECISION_ONE_ID: 0.5}, neighbors_by_node=one_hop,
    )
    assert max(r.score or 0.0 for r in results) <= 1.0
    legs = _legs(tr)
    assert legs[SA]["n_returned"] == 0
    assert "brain_graph" not in legs
    assert NA not in {r.id for r in results}
    assert [e for e in tr.to_dict()["expansions"] if e["stage"] != SA_STAGE] == []
    assert {r.id: r.score for r in results}[EPISODE_ID] == pytest.approx(0.92)
    assert len(_rescored(tr)) == 1
