"""3e spike: does per-stage score normalization promote graph items into top-K?

For a sample of queries, run run_recall_pipeline (rerank_by_score=True, Path A on)
with score_space_normalize OFF (baseline) vs ON (variant), and count how many of
the top-K results are graph-expanded items. If normalization floods top-K with
graph items, that is the exact R-1/R-5 regression mechanism (densely-connected
non-gold outranks specific gold) — strong evidence to DEFER.

Env: NOUS_EVAL_DB_* -> :5433/nous_eval_prod, NOUS_EMBEDDING_MODEL large,
NOUS_HEART_GRAPH_ALL_TYPES_ENABLED=true, OPENAI_API_KEY (in .env). PROBE_LIMIT=40.
"""
from __future__ import annotations

import asyncio
import os

from sqlalchemy import text

from nous.config import Settings
from nous.storage.database import Database
from nous_eval.config import EvalSettings
from nous_eval.retrieval_runner import (
    _build_brain_for_eval,
    _build_heart_for_eval,
    _settings_for_eval_db,
)
from nous.api.retrieval_pipeline import run_recall_pipeline

AGENT = os.environ.get("NOUS_EVAL_AGENT_ID", "nous-default")
LIMIT = int(os.environ.get("PROBE_LIMIT", "40"))
TOP_K = 10
_GRAPH = {"graph_expanded", "spreading_activation"}


def _is_graph(r) -> bool:
    return r.source in _GRAPH or (r.metadata or {}).get("stage_origin") in {"heart_graph", "brain_graph"}


async def _queries(s: Settings) -> list[str]:
    db = Database(s)
    await db.connect()
    try:
        async with db.session() as sess:
            rows = await sess.execute(text(
                "SELECT content FROM heart.facts WHERE agent_id=:a AND active=true "
                "AND length(content) BETWEEN 30 AND 200 ORDER BY id LIMIT :n"
            ), {"a": AGENT, "n": LIMIT})
            return [r[0] for r in rows]
    finally:
        await db.disconnect()


async def main() -> None:
    base = _settings_for_eval_db(EvalSettings(), Settings())
    qs = await _queries(base)
    print(f"score-space spike: {len(qs)} queries, Path A={base.heart_graph_all_types_enabled}, "
          f"rerank_by_score=True")

    async def run(normalize: bool) -> dict:
        s = base.model_copy(update={"score_space_normalize_enabled": normalize})
        db = Database(s)
        await db.connect()
        graph_in_topk = 0
        queries_with_graph = 0
        try:
            async with _build_heart_for_eval(db, s) as heart:
                brain = _build_brain_for_eval(db, s, heart._embeddings)
                for q in qs:
                    results, _ = await run_recall_pipeline(
                        query=q, heart=heart, brain=brain, settings=s,
                        limit=TOP_K, rerank_by_score=True,
                    )
                    g = sum(1 for r in results[:TOP_K] if _is_graph(r))
                    graph_in_topk += g
                    if g:
                        queries_with_graph += 1
        finally:
            await db.disconnect()
        return {"graph_in_topk_total": graph_in_topk, "queries_with_graph": queries_with_graph}

    baseline = await run(False)
    variant = await run(True)
    print(f"  baseline (norm OFF): {baseline}")
    print(f"  variant  (norm ON ): {variant}")
    print(f"  delta graph-in-topK: {variant['graph_in_topk_total'] - baseline['graph_in_topk_total']}"
          f"  (queries affected: {variant['queries_with_graph']} vs {baseline['queries_with_graph']})")


if __name__ == "__main__":
    asyncio.run(main())
