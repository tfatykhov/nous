"""The graph-qrel miner's acceptance criterion must be SATISFIABLE.

`_validate` keeps a candidate iff the gold is absent from graph-off top-K and
present in graph-on top-K. It called `run_recall_pipeline` without
`rerank_by_score`, which defaults False — and under stage-order assembly a
graph-reached row is appended AFTER the heart results, so it sits at index
>= limit and a `results[:limit]` slice can never contain it.

`on_rank` was therefore always None and the criterion was unsatisfiable on any
corpus. The mine returned 0 qrels by construction; that was diagnosed as a
harness bug on 2026-07-01 and left unfixed.

These tests pin the property the miner depends on, at the pipeline layer where
it actually holds or fails, so a future change to assembly order or to the
miner's call cannot silently make the mine unsatisfiable again.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from test_retrieval_pipeline import (  # noqa: E402  (test-local helper reuse)
    _make_brain,
    _make_decision_summaries,
    _make_heart,
    _make_settings,
)

from nous.api.retrieval_pipeline import run_recall_pipeline

GRAPH_TARGET = UUID("bbbbbbbb-0000-0000-0000-000000000001")
RESOLVED_AT = datetime(2026, 1, 5, tzinfo=UTC)


def _rank_of(results, target_id, limit):
    """Verbatim copy of nous_eval.generate_graph_qrels._rank_of."""
    for i, r in enumerate(results[:limit], 1):
        if r.id == target_id:
            return i
    return None


async def _run(*, rerank, n_heart, spread_score):
    """A pipeline call with `n_heart` direct hits and one graph-reached row."""
    from nous.api.retrieval_pipeline import PipelineResult

    heart_rows = [
        PipelineResult(id=UUID(int=9000 + i), type="fact",
                       description=f"direct hit {i}", score=0.50 - i * 0.01,
                       source="heart")
        for i in range(n_heart)
    ]
    heart = _make_heart(recall_results=[])
    brain = _make_brain(neighbors_by_node={}, contradictions=[],
                        decision_results=_make_decision_summaries())
    brain._resolve_node_descriptions = AsyncMock(
        return_value={GRAPH_TARGET: ("the graph-reached gold", RESOLVED_AT)})

    settings = _make_settings(spreading_activation_enabled="true")
    with patch(
        "nous.api.retrieval_pipeline._heart_results_to_pipeline",
        lambda *a, **k: list(heart_rows),
    ), patch(
        "nous.brain.spreading_activation.spreading_activation_search",
        AsyncMock(return_value=[(GRAPH_TARGET, "fact", spread_score, 1)]),
    ), patch(
        "nous.brain.spreading_activation.compute_graph_density",
        AsyncMock(return_value=5.0),
    ):
        results, _stats = await run_recall_pipeline(
            query="anything", heart=heart, brain=brain, settings=settings,
            limit=10, rerank_by_score=rerank,
        )
    return results


class TestMinerCriterionIsSatisfiable:
    @pytest.mark.asyncio
    async def test_graph_row_is_unreachable_in_topk_without_rerank(self):
        """The defect. With a full complement of direct hits, stage-order
        assembly puts the graph row past the top-K window — so the miner's
        `on_rank is not None` can never be true."""
        results = await _run(rerank=False, n_heart=10, spread_score=0.9)
        assert _rank_of(results, GRAPH_TARGET, 10) is None
        # It IS in the result set — it is the SLICE that hides it, which is why
        # this reads as "graph expansion found nothing" rather than as a bug.
        assert any(r.id == GRAPH_TARGET for r in results)

    @pytest.mark.asyncio
    async def test_graph_row_is_reachable_in_topk_with_rerank(self):
        """The fix. Same call, `rerank_by_score=True` — what prod runs."""
        results = await _run(rerank=True, n_heart=10, spread_score=0.9)
        assert _rank_of(results, GRAPH_TARGET, 10) is not None

    @pytest.mark.asyncio
    async def test_a_low_scoring_graph_row_is_still_correctly_excluded(self):
        """The fix must not make the criterion trivially TRUE either — a graph
        row that genuinely does not out-rank the direct hits stays out, so the
        mine keeps discriminating rather than accepting everything."""
        results = await _run(rerank=True, n_heart=10, spread_score=0.01)
        assert _rank_of(results, GRAPH_TARGET, 10) is None

    @pytest.mark.asyncio
    async def test_miner_passes_rerank_by_score(self):
        """Pins the miner's own call, not just the pipeline property it needs.

        Reads the source rather than executing `_validate`, which would require
        a live corpus, an embedder and an LLM.
        """
        import inspect

        from nous_eval import generate_graph_qrels

        src = inspect.getsource(generate_graph_qrels._validate_query)
        assert src.count("run_recall_pipeline(") == 2, "both arms must be present"
        assert src.count("limit=limit, rerank_by_score=True,") == 2, (
            "both validation arms must rank the way prod does, or the "
            "graph-on arm cannot see a graph row inside top-K"
        )
