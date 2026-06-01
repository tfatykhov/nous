"""Edge-weight surfacing-threshold sweep (advisor-mandated, pre-build).

The formation build's shape depends on ONE number: at what edge weight does a single
co-activation edge lift a genuinely-absent target into the top-10? Neighbor score is
~ seed_score x edge_weight (linear), so the 0.3->absent / 0.9->top-10 smoke leaves the
crossover unknown. This sweeps 0.3..0.9 on the truly-absent pairs and reports the crossover.

  - crossover BELOW a noise-safe weight -> formation-A-alone can pass cell 11.
  - crossover requires ~0.9 -> a single co-occurrence can't both surface AND be noise-safe
    -> the honest first milestone is A+B (weak first, surfaces only after repeated co-recall).

  uv run python scripts/diag/faculty/baseline_weightsweep.py
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
GATE_K = 10
WIDE = 40
WEIGHTS = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]


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

    base = {"graph_neighbor_seed_score_enabled": True, "heart_graph_all_types_enabled": True,
            "residual_activation_enabled": False}
    s_on = Settings().model_copy(update={**base, "graph_adjacency_boost_enabled": True})
    s_off = Settings().model_copy(update={**base, "graph_adjacency_boost_enabled": False})
    db = Database(s_on); await db.connect()
    emb = EmbeddingProvider(api_key=s_on.openai_api_key, model="text-embedding-3-large", dimensions=1536)
    heart = Heart(database=db, settings=s_on, embedding_provider=emb); brain = Brain(db, s_on, emb)

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

    def rank_of(res, tok):
        return next((i + 1 for i, r in enumerate(res) if tok.lower() in (r.description or "").lower()), None)

    async def probe(st, q, tok):
        res, _ = await run_recall_pipeline(q, heart, brain, st, limit=WIDE, rerank_by_score=True)
        return rank_of(res, tok)

    def set_edge(sid, tid, w):
        psql(f"DELETE FROM brain.graph_edges WHERE agent_id='{AGENT}'")
        psql("INSERT INTO brain.graph_edges (source_id,target_id,source_type,target_type,"
             "agent_id,relation,weight,auto_linked,extraction_method) VALUES "
             f"('{sid}','{tid}','fact','fact','{AGENT}','related_to',{w},false,'heuristic')")

    # find the truly-absent pairs (baseline rank None / > WIDE)
    print("\n=== sweep (absent pairs only) — rank at each weight; first weight into top-10 ===")
    crossovers = []
    for p in NO_HANDLE_PAIRS:
        sid, tid = ids[f"baseline:{p['id']}:seed"], ids[f"baseline:{p['id']}:target"]
        tok = p["answer_token"]
        psql(f"DELETE FROM brain.graph_edges WHERE agent_id='{AGENT}'")
        base = await probe(s_on, p["query"], tok)
        if base is not None and base <= GATE_K:
            print(f"  [{p['id']:12}] baseline rank={base} (already reachable) — skip")
            continue
        for label, st in (("adjON", s_on), ("adjOFF", s_off)):
            ranks = []
            cross = None
            for w in WEIGHTS:
                set_edge(sid, tid, w)
                r = await probe(st, p["query"], tok)
                ranks.append((w, r))
                if cross is None and r is not None and r <= GATE_K:
                    cross = w
            rs = "  ".join(f"{w}:{r}" for w, r in ranks)
            print(f"  [{p['id']:12} {label:6}] {rs}   -> crosses top-{GATE_K} at weight={cross}")
            if label == "adjOFF":  # adjacency-OFF isolates the weighted seed-score path
                crossovers.append(cross)
    psql(f"DELETE FROM brain.graph_edges WHERE agent_id='{AGENT}'")
    await db.disconnect()

    valid = [c for c in crossovers if c is not None]
    print("\n" + "=" * 60)
    print(f"weighted-path (adj-OFF) crossover weights: {crossovers}")
    if valid:
        hi = max(valid)
        print(f"max crossover = {hi}")
        if hi <= 0.6:
            print("VERDICT: surfacing threshold is noise-safe-ish -> FORMATION-A-ALONE can pass cell 11.")
        else:
            print("VERDICT: surfacing needs a HIGH weight (>0.6) -> a single co-occurrence can't both")
            print("         surface AND stay noise-safe -> first milestone should be A+B (strengthen-by-use).")


if __name__ == "__main__":
    asyncio.run(main())
