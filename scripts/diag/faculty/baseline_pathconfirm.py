"""Prod-shape confirmation (advisor-mandated): does a formed edge reach the agent?

Tests an injected co-activation edge (weight 0.9) on the three retrieval paths that
actually exist, at the PROD pool depth (limit=10), for genuinely-absent targets:

  A. pre_turn injection  = Heart.search_facts (context.py:417) — hybrid cosine+keyword, NO graph
  B. recall_deep default = run_recall_pipeline(rerank_by_score=False) — graph neighbours at 11+
  C. recall_deep reranked= run_recall_pipeline(rerank_by_score=True)  — score-sorted (F051 harness only)

Prediction: the edge is INVISIBLE in A (no graph) and B (rerank off), VISIBLE only in C —
confirming the agent's real paths are graph-blind and the first lever is path-unification,
not formation.

  uv run python scripts/diag/faculty/baseline_pathconfirm.py
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _load(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        os.environ[k] = v


_load(REPO / ".env.prod-snapshot")
os.environ.update({
    "DB_HOST": "127.0.0.1", "DB_PORT": "5433", "DB_NAME": "nous_eval_live",
    "DB_USER": "nous", "DB_PASSWORD": "nous_eval", "NOUS_AGENT_ID": "nous-baseline-eval",
})

from scripts.diag.faculty.baseline_corpus import NO_HANDLE_PAIRS, smoke_facts  # noqa: E402

AGENT = "nous-baseline-eval"
PROD_LIMIT = 10


def psql(q: str) -> str:
    return subprocess.run(
        ["docker", "exec", "nous-eval-scratch", "psql", "-U", "nous", "-d", "nous_eval_live", "-tAc", q],
        capture_output=True, text=True).stdout.strip()


async def main() -> None:
    from sqlalchemy import text

    from nous.api.retrieval_pipeline import run_recall_pipeline
    from nous.brain.brain import Brain
    from nous.brain.embeddings import EmbeddingProvider
    from nous.config import Settings
    from nous.heart.heart import Heart
    from nous.storage.database import Database

    upd = {"graph_neighbor_seed_score_enabled": True, "heart_graph_all_types_enabled": True,
           "graph_adjacency_boost_enabled": True, "residual_activation_enabled": False}
    s_norerank = Settings().model_copy(update=upd)
    db = Database(s_norerank); await db.connect()
    emb = EmbeddingProvider(api_key=s_norerank.openai_api_key, model="text-embedding-3-large", dimensions=1536)
    heart = Heart(database=db, settings=s_norerank, embedding_provider=emb); brain = Brain(db, s_norerank, emb)

    print("=== reset + ingest corpus ===")
    for tbl in ("heart.facts", "heart.episodes", "heart.working_memory"):
        psql(f"DELETE FROM {tbl} WHERE agent_id='{AGENT}'")
    psql(f"DELETE FROM brain.graph_edges WHERE agent_id='{AGENT}'")
    ids: dict[str, str] = {}
    async with db.session() as sess:
        for content, src in smoke_facts():
            vec = await emb.embed(content)
            vlit = "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
            row = await sess.execute(text(
                "INSERT INTO heart.facts (agent_id, content, source, confidence, active, embedding) "
                "VALUES (:a, :c, :src, 0.9, TRUE, CAST(:v AS vector)) RETURNING id::text"
            ), {"a": AGENT, "c": content, "src": src, "v": vlit})
            ids[src] = row.scalar_one()
        await sess.commit()

    def rank_pipe(res, tok):
        return next((i + 1 for i, r in enumerate(res) if tok.lower() in (r.description or "").lower()), None)

    def rank_facts(facts, tok):
        return next((i + 1 for i, f in enumerate(facts) if tok.lower() in (getattr(f, "content", "") or "").lower()), None)

    print(f"\n{'pair':12} {'cosine_rank':11} | A pre_turn(search_facts) | B recall_deep(rerank=F) | C recall_deep(rerank=T)")
    print("-" * 95)
    for p in NO_HANDLE_PAIRS:
        sid, tid = ids[f"baseline:{p['id']}:seed"], ids[f"baseline:{p['id']}:target"]
        q, tok = p["query"], p["answer_token"]
        # cosine baseline rank (no edge), wide window for context
        psql(f"DELETE FROM brain.graph_edges WHERE agent_id='{AGENT}'")
        wide, _ = await run_recall_pipeline(q, heart, brain, s_norerank, limit=40, rerank_by_score=True)
        cos = rank_pipe(wide, tok)
        if cos is not None and cos <= PROD_LIMIT:
            print(f"{p['id']:12} rank={cos} (already in top-10) — skip")
            continue
        # inject edge w0.9
        psql("INSERT INTO brain.graph_edges (source_id,target_id,source_type,target_type,"
             "agent_id,relation,weight,auto_linked,extraction_method) VALUES "
             f"('{sid}','{tid}','fact','fact','{AGENT}','related_to',0.9,false,'heuristic')")
        # A: pre_turn path
        facts = await heart.search_facts(q, limit=PROD_LIMIT)
        a = rank_facts(facts, tok)
        # B: recall_deep default (rerank off)
        rb, _ = await run_recall_pipeline(q, heart, brain, s_norerank, limit=PROD_LIMIT, rerank_by_score=False)
        b = rank_pipe(rb, tok)
        # C: recall_deep rerank on
        rc, _ = await run_recall_pipeline(q, heart, brain, s_norerank, limit=PROD_LIMIT, rerank_by_score=True)
        c = rank_pipe(rc, tok)
        def fmt(x): return f"top10@{x}" if (x and x <= PROD_LIMIT) else (f"@{x}" if x else "ABSENT")
        print(f"{p['id']:12} cos={str(cos):<5} | {fmt(a):^23} | {fmt(b):^23} | {fmt(c):^23}")

    psql(f"DELETE FROM brain.graph_edges WHERE agent_id='{AGENT}'")
    await db.disconnect()
    print("\nINTERPRETATION: if A and B are ABSENT/@11+ while C surfaces into top-10, the formed edge")
    print("is invisible to BOTH paths the agent uses by default -> path-unification is the first lever.")


if __name__ == "__main__":
    asyncio.run(main())
