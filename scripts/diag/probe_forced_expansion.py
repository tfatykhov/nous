"""Forced-graph-expansion A/B probe.

The top-K probe showed graph-expanded neighbors rank below vector hits, so the
inactive-fact filter never bit in the ranked output. This probe FORCES graph
expansion two ways and measures the fix's effect:

  (1) DIRECT: call brain.neighbors(seed, neighbor_type='fact') — the Path A
      expansion primitive — and count inactive-fact neighbors. This is the
      ground-truth mechanistic surface (no ranking dilution).
  (2) PIPELINE (cranked): run_recall_pipeline with graph params turned up
      (neighbors/seed, max_expand, near-zero decay) so neighbors reach top-K,
      and count inactive facts in the results.

Run under baseline (pre-fix) and fix code; the delta is the fix's effect.

Env: same as probe_inactive_leak.py, plus the crank knobs are set in-code.
  PROBE_TAG=fix|baseline   PROBE_LIMIT=60
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from uuid import UUID

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
from nous.brain.spreading_activation import spreading_activation_search

AGENT = os.environ.get("NOUS_EVAL_AGENT_ID", "nous-default")
TAG = os.environ.get("PROBE_TAG", "fix")
LIMIT = int(os.environ.get("PROBE_LIMIT", "60"))
TOP_K = 10

# Crank graph expansion so neighbors actually surface.
_CRANK = {
    "heart_graph_all_types_enabled": True,
    "heart_graph_neighbors_per_seed": 10,
    "graph_recall_enabled": True,
    "graph_recall_max_expand": 30,
    "graph_recall_max_neighbors": 10,
    "graph_recall_decay": 0.99,
    "cross_encoder_enabled": False,
}


async def _sample(conn_settings: Settings) -> list[dict]:
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
                ORDER BY f.id LIMIT :lim
            """), {"a": AGENT, "lim": LIMIT})
            return [dict(r._mapping) for r in rows]
    finally:
        await db.disconnect()


async def _inactive_ids(db: Database, ids: list[str]) -> set[str]:
    if not ids:
        return set()
    async with db.session() as s:
        r = await s.execute(text(
            "SELECT id FROM heart.facts WHERE agent_id=:a AND active=false "
            "AND id = ANY(CAST(:ids AS uuid[]))"
        ), {"a": AGENT, "ids": ids})
        return {str(x.id) for x in r}


async def main() -> None:
    eval_settings = EvalSettings()
    base = Settings()
    scoped = _settings_for_eval_db(eval_settings, base)
    scoped = scoped.model_copy(update=_CRANK)

    sample = await _sample(scoped)
    print(f"[{TAG}] forced-expansion probe: {len(sample)} seeds, "
          f"neighbors/seed={scoped.heart_graph_neighbors_per_seed}, "
          f"decay={scoped.graph_recall_decay}")

    eval_db = Database(scoped)
    await eval_db.connect()
    direct_inactive_neighbors = 0           # (1) brain.neighbors (BR-1 already filters)
    direct_predecessor_hits = 0
    spread_inactive_total = 0               # (1b) spreading_activation_search (raw ids)
    spread_predecessor_hits = 0
    pipe_queries_with_leak = 0              # (2) pipeline cranked
    pipe_total_inactive = 0
    per = []
    try:
        async with _build_heart_for_eval(eval_db, scoped) as heart:
            brain = _build_brain_for_eval(eval_db, scoped, heart._embeddings)
            for row in sample:
                # (1) direct neighbors primitive (content-resolution already
                # drops inactive facts via BR-1 on both arms — control)
                neigh = await brain.neighbors(
                    UUID(str(row["new_id"])), node_type="fact",
                    neighbor_type="fact", limit=20,
                )
                neigh_ids = [str(n.id) for n in neigh]
                dn_inactive = await _inactive_ids(eval_db, neigh_ids)
                direct_inactive_neighbors += len(dn_inactive)
                if str(row["old_id"]) in dn_inactive:
                    direct_predecessor_hits += 1

                # (1b) spreading activation from the superseding fact seed —
                # emits RAW activated ids with no active filter, so the
                # supersedes traversal is the actual resurrection vector.
                async with eval_db.session() as sa_s:
                    activated = await spreading_activation_search(
                        sa_s, AGENT, [(UUID(str(row["new_id"])), "fact", 1.0)], scoped,
                    )
                act_ids = [str(nid) for (nid, _t, _s) in activated]
                sp_inactive = await _inactive_ids(eval_db, act_ids)
                spread_inactive_total += len(sp_inactive)
                if str(row["old_id"]) in sp_inactive:
                    spread_predecessor_hits += 1

                # (2) cranked pipeline
                results, _ = await run_recall_pipeline(
                    query=row["content"], heart=heart, brain=brain,
                    settings=scoped, limit=TOP_K, rerank_by_score=True,
                )
                rfacts = [str(r.id) for r in results if r.type == "fact"]
                pn_inactive = await _inactive_ids(eval_db, rfacts)
                if pn_inactive:
                    pipe_queries_with_leak += 1
                    pipe_total_inactive += len(pn_inactive)

                per.append({
                    "new_id": str(row["new_id"]), "old_id": str(row["old_id"]),
                    "direct_neighbors": len(neigh_ids),
                    "direct_inactive": sorted(dn_inactive),
                    "spread_activated": len(act_ids),
                    "spread_inactive": sorted(sp_inactive),
                    "pipeline_inactive": sorted(pn_inactive),
                })
    finally:
        await eval_db.disconnect()

    summary = {
        "tag": TAG, "seeds": len(sample),
        "direct_neighbors_inactive_total": direct_inactive_neighbors,
        "direct_neighbors_predecessor_surfaced": direct_predecessor_hits,
        "spreading_inactive_total": spread_inactive_total,
        "spreading_predecessor_surfaced": spread_predecessor_hits,
        "pipeline_queries_with_inactive_leak": pipe_queries_with_leak,
        "pipeline_total_inactive_results": pipe_total_inactive,
    }
    print(f"[{TAG}] RESULT: {json.dumps(summary, indent=2)}")
    Path("reports").joinpath(f"probe_forced_expansion_{TAG}.json").write_text(
        json.dumps({"summary": summary, "per": per}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
