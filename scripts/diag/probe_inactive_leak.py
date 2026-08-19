"""A/B probe: does retrieval surface inactive (superseded) facts via Path A?

For each active superseding fact (one that has a `supersedes` edge to its
inactive predecessor), query the pipeline with that fact's content and count how
many top-K results are INACTIVE facts — i.e. obsolete facts leaking into recall.

Run the SAME script under baseline code (pre-fix) and fix code; the delta is the
fix's effect. Output is written to reports/probe_inactive_leak_<tag>.json.

Env (point at the fresh prod copy on :5433):
  NOUS_EVAL_DB_HOST=localhost NOUS_EVAL_DB_PORT=5433 NOUS_EVAL_DB_NAME=nous_eval_prod
  NOUS_EVAL_DB_PASSWORD=nous_eval NOUS_EVAL_AGENT_ID=nous-default
  NOUS_EMBEDDING_MODEL=text-embedding-3-large
  NOUS_HEART_GRAPH_ALL_TYPES_ENABLED=true  (Path A — the consumer under test)
  OPENAI_API_KEY=...   (real query embeddings)
  PROBE_TAG=fix|baseline   PROBE_LIMIT=40
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

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
TAG = os.environ.get("PROBE_TAG", "fix")
LIMIT = int(os.environ.get("PROBE_LIMIT", "40"))
TOP_K = 10


async def _sample(conn_settings: Settings) -> list[dict]:
    """Active superseding facts that have a supersedes edge to an inactive
    predecessor — the exact structure the fix governs."""
    db = Database(conn_settings)
    await db.connect()
    try:
        async with db.session() as s:
            rows = await s.execute(text("""
                SELECT f.id AS new_id, f.content AS content, e.target_id AS old_id
                FROM brain.graph_edges e
                JOIN heart.facts f ON f.id = e.source_id AND f.agent_id = e.agent_id
                JOIN heart.facts o ON o.id = e.target_id AND o.agent_id = e.agent_id
                WHERE e.agent_id = :a AND e.relation = 'supersedes'
                  AND f.active = true AND o.active = false
                  AND length(f.content) >= 20
                ORDER BY f.id
                LIMIT :lim
            """), {"a": AGENT, "lim": LIMIT})
            return [dict(r._mapping) for r in rows]
    finally:
        await db.disconnect()


async def main() -> None:
    eval_settings = EvalSettings()
    base = Settings()
    scoped = _settings_for_eval_db(eval_settings, base)

    sample = await _sample(scoped)
    print(f"[{TAG}] probing {len(sample)} superseding-fact queries "
          f"(Path A={scoped.heart_graph_all_types_enabled}, "
          f"embed={scoped.embedding_model})")

    eval_db = Database(scoped)
    await eval_db.connect()
    queries_with_leak = 0
    total_inactive_results = 0
    own_predecessor_hits = 0
    per_query = []
    try:
        async with _build_heart_for_eval(eval_db, scoped) as heart:
            brain = _build_brain_for_eval(eval_db, scoped, heart._embeddings)
            for row in sample:
                results, _stats = await run_recall_pipeline(
                    query=row["content"], heart=heart, brain=brain,
                    settings=scoped, limit=TOP_K, rerank_by_score=True,
                )
                fact_ids = [str(r.id) for r in results if r.type == "fact"]
                # which retrieved facts are inactive?
                async with eval_db.session() as s:
                    if fact_ids:
                        inact = await s.execute(text(
                            "SELECT id FROM heart.facts WHERE agent_id=:a "
                            "AND active=false AND id = ANY(CAST(:ids AS uuid[]))"
                        ), {"a": AGENT, "ids": fact_ids})
                        inactive_hits = {str(r.id) for r in inact}
                    else:
                        inactive_hits = set()
                if inactive_hits:
                    queries_with_leak += 1
                    total_inactive_results += len(inactive_hits)
                if str(row["old_id"]) in inactive_hits:
                    own_predecessor_hits += 1
                per_query.append({
                    "new_id": str(row["new_id"]),
                    "old_id": str(row["old_id"]),
                    "n_results": len(results),
                    "inactive_hits": sorted(inactive_hits),
                })
    finally:
        await eval_db.disconnect()

    summary = {
        "tag": TAG,
        "agent": AGENT,
        "queries": len(sample),
        "queries_with_inactive_leak": queries_with_leak,
        "total_inactive_fact_results": total_inactive_results,
        "own_predecessor_surfaced": own_predecessor_hits,
        "path_a": scoped.heart_graph_all_types_enabled,
        "embedding_model": scoped.embedding_model,
    }
    print(f"[{TAG}] RESULT: {json.dumps(summary, indent=2)}")
    out = Path("reports") / f"probe_inactive_leak_{TAG}.json"
    out.write_text(json.dumps({"summary": summary, "per_query": per_query}, indent=2))
    print(f"[{TAG}] wrote {out}")


if __name__ == "__main__":
    asyncio.run(main())
