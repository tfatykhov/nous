"""Capability Baseline Instrument — BARE pass (docs/research/018).

Stage 1 of the full run: pure cosine reachability per cell (no instance, no opus spend),
so it doubles as the cross-family smoke that catches corpus/validity flaws before the
expensive agentic + full-cycle pass. Measures, per cell, whether SIMILARITY ALONE surfaces
the answer fact in bare top-k — the direct test of the "is it all similarity?" thesis — plus
the cell-11 no-handle positive control (injected co-activation edge).

The agentic lens (cells needing the retrieve-and-reason loop, contradiction/recency flag
arms, abstention) runs in baseline_agentic.py against a live instance.

  uv run python scripts/diag/faculty/baseline.py
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
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

from scripts.diag.faculty.baseline_corpus import (  # noqa: E402
    CELLS, GLORPTAX_PROBE, NO_HANDLE_PAIRS, full_facts)

AGENT = "nous-baseline-eval"
GATE_K = 10
WIDE = 30
RUN = HERE / "runs" / (time.strftime("%Y%m%d-%H%M%S") + "-baseline-bare")
RUN.mkdir(parents=True, exist_ok=True)
LOG = open(RUN / "result.txt", "w", encoding="utf-8")


def out(*a):
    s = " ".join(str(x) for x in a); print(s); LOG.write(s + "\n"); LOG.flush()


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

    s = Settings().model_copy(update={
        "graph_neighbor_seed_score_enabled": True, "heart_graph_all_types_enabled": True,
        "graph_adjacency_boost_enabled": True, "residual_activation_enabled": False})
    db = Database(s); await db.connect()
    emb = EmbeddingProvider(api_key=s.openai_api_key, model="text-embedding-3-large", dimensions=1536)
    heart = Heart(database=db, settings=s, embedding_provider=emb); brain = Brain(db, s, emb)

    out("=== BASELINE bare pass: reset + ingest full 18-cell corpus (cosine only, no edges) ===")
    for tbl in ("heart.facts", "heart.episodes", "heart.working_memory"):
        psql(f"DELETE FROM {tbl} WHERE agent_id='{AGENT}'")
    psql(f"DELETE FROM brain.graph_edges WHERE agent_id='{AGENT}'")

    ids: dict[str, str] = {}
    async with db.session() as sess:
        for content, srcsess in full_facts():
            vec = await emb.embed(content)
            vlit = "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
            row = await sess.execute(text(
                "INSERT INTO heart.facts (agent_id, content, source, confidence, active, embedding) "
                "VALUES (:a, :c, :src, 0.9, TRUE, CAST(:v AS vector)) RETURNING id::text"
            ), {"a": AGENT, "c": content, "src": f"baseline:{srcsess}", "v": vlit})
            ids[content] = row.scalar_one()
        await sess.commit()
    n_facts = psql(f"SELECT count(*) FROM heart.facts WHERE agent_id='{AGENT}'")
    out(f"  inserted {n_facts} facts\n")

    def rank_of(results, token: str):
        return next((i + 1 for i, r in enumerate(results) if token and token.lower() in (r.description or "").lower()), None)

    async def bare(q, limit=WIDE):
        res, _ = await run_recall_pipeline(q, heart, brain, s, limit=limit, rerank_by_score=True)
        return res

    # ---- per-cell bare probe (cosine-only reachability) ----
    out("=== BARE reachability per cell (cosine only; top-%d gate) ===" % GATE_K)
    rows = []
    probes = [c for c in CELLS if c.get("query") and c.get("answer")] + [GLORPTAX_PROBE]
    for c in probes:
        res = await bare(c["query"])
        rank = rank_of(res, c["answer"])
        reach = bool(rank and rank <= GATE_K)
        seedrank = None
        if c.get("bridge_seed"):
            seedrank = rank_of(res, c["bridge_seed"])
        fb = [t for t in c.get("false", []) if rank_of(res, t) and rank_of(res, t) <= GATE_K]
        rows.append((c["id"], c["family"], rank, reach, seedrank, fb, c["predict"]))
        sd = f" seed_rank={seedrank}" if c.get("bridge_seed") else ""
        fbs = f" FALSE_BRIDGE_in_top{GATE_K}={fb}" if fb else ""
        out(f"  [{c['id']:18} {c['family']}] answer_rank={rank} reach={reach}{sd}{fbs}  <{c['predict']}>")

    # ---- cell 11 no-handle positive control (edge injection) ----
    out("\n=== cell 11 no-handle positive control (inject co-activation edge w0.9) ===")
    nh_rescued = 0
    for p in NO_HANDLE_PAIRS:
        sid, tid = ids.get(p["seed"]), ids.get(p["target"])
        psql(f"DELETE FROM brain.graph_edges WHERE agent_id='{AGENT}'")
        base_rank = rank_of(await bare(p["query"]), p["answer_token"])
        psql("INSERT INTO brain.graph_edges (source_id,target_id,source_type,target_type,"
             "agent_id,relation,weight,auto_linked,extraction_method) VALUES "
             f"('{sid}','{tid}','fact','fact','{AGENT}','related_to',0.9,false,'heuristic')")
        edge_rank = rank_of(await bare(p["query"]), p["answer_token"])
        rescued = bool(edge_rank and edge_rank <= GATE_K) and not (base_rank and base_rank <= GATE_K)
        nh_rescued += rescued
        out(f"  [{p['id']:14}] baseline_rank={base_rank} -> edge_rank={edge_rank}  "
            f"{'RESCUED into top-%d' % GATE_K if rescued else ''}")
    psql(f"DELETE FROM brain.graph_edges WHERE agent_id='{AGENT}'")

    # ---- summary ----
    out("\n" + "=" * 70 + "\n--- BARE-PASS SUMMARY ---")
    reached = [r for r in rows if r[3]]
    out(f"  cosine-reachable (top-{GATE_K}): {len(reached)}/{len(rows)} cells")
    out(f"  cells with a FALSE BRIDGE in top-{GATE_K}: {[r[0] for r in rows if r[5]]}")
    out(f"  cell-11 no-handle rescued by injected edge: {nh_rescued}/{len(NO_HANDLE_PAIRS)}")
    out("  (agentic lens + flag arms + abstention -> baseline_agentic.py)")
    out(f"\n  run dir: {RUN}")
    await db.disconnect()
    LOG.close()


if __name__ == "__main__":
    asyncio.run(main())
