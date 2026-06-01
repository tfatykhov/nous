"""SMOKE for the two load-bearing limit cells (docs/research/018 cells 11 + 18).

Pure bare-pipeline + edge injection (no live instance), full ~50-fact corpus. Re-smoke
after the register-contrast redesign. Answers:
  - Cell 11 validity gate: is the target genuinely OUTSIDE bare top-10 (disjoint)?
  - Cell 11 positive control: does an injected co-activation edge pull a disjoint target
    INTO top-10 — and via which consumer (adjacency boost vs seed-score path)?
  - Cell 18: now that the target is disjoint (only route in = the weighted seed-score
    neighbor path), does edge WEIGHT (0.3 vs 0.9) modulate whether/where it lands?

  uv run python scripts/diag/faculty/baseline_smoke.py
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
GATE_K = 10   # disjoint = target NOT in bare top-10
WIDE = 30     # visibility window for rank movement


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
    s_adj = Settings().model_copy(update={**base, "graph_adjacency_boost_enabled": True})
    s_noadj = Settings().model_copy(update={**base, "graph_adjacency_boost_enabled": False})
    db = Database(s_adj); await db.connect()
    emb = EmbeddingProvider(api_key=s_adj.openai_api_key, model="text-embedding-3-large", dimensions=1536)
    heart = Heart(database=db, settings=s_adj, embedding_provider=emb); brain = Brain(db, s_adj, emb)

    print("=== SMOKE v2: reset + direct-insert ~50-fact corpus (register-contrast targets) ===")
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
    n_facts = psql(f"SELECT count(*) FROM heart.facts WHERE agent_id='{AGENT}'")
    print(f"  inserted {n_facts} facts\n")

    def rank_of(results, token: str) -> int | None:
        return next((i + 1 for i, r in enumerate(results) if token.lower() in (r.description or "").lower()), None)

    async def probe(settings, q, tok):
        res, _ = await run_recall_pipeline(q, heart, brain, settings, limit=WIDE, rerank_by_score=True)
        return rank_of(res, tok)

    def set_edge(src_id: str, tgt_id: str, weight: float) -> None:
        psql(f"DELETE FROM brain.graph_edges WHERE agent_id='{AGENT}'")
        psql("INSERT INTO brain.graph_edges (source_id,target_id,source_type,target_type,"
             "agent_id,relation,weight,auto_linked,extraction_method) VALUES "
             f"('{src_id}','{tgt_id}','fact','fact','{AGENT}','related_to',{weight},false,'heuristic')")

    disjoint_ct = 0
    for p in NO_HANDLE_PAIRS:
        seed_id, tgt_id = ids[f"baseline:{p['id']}:seed"], ids[f"baseline:{p['id']}:target"]
        q, tok = p["query"], p["answer_token"]
        psql(f"DELETE FROM brain.graph_edges WHERE agent_id='{AGENT}'")
        base_rank = await probe(s_adj, q, tok)
        disjoint = base_rank is None or base_rank > GATE_K
        disjoint_ct += disjoint
        print(f"--- {p['id']}  target '{tok}' ---")
        print(f"  [gate] baseline target_rank={base_rank}  -> {'DISJOINT (valid)' if disjoint else f'!! in top-{GATE_K} (handle leak)'}")
        # cell 11 + 18: edge effect under both consumers, at two weights
        for label, st in (("adj-ON", s_adj), ("adj-OFF", s_noadj)):
            set_edge(seed_id, tgt_id, 0.3); r3 = await probe(st, q, tok)
            set_edge(seed_id, tgt_id, 0.9); r9 = await probe(st, q, tok)
            in10 = lambda r: bool(r and r <= GATE_K)
            wsig = "WEIGHT MODULATES" if (r3 != r9) else "weight inert"
            print(f"    [{label}] w0.3 rank={r3}(top{GATE_K}={in10(r3)})  "
                  f"w0.9 rank={r9}(top{GATE_K}={in10(r9)})  -> {wsig}")
        print()

    psql(f"DELETE FROM brain.graph_edges WHERE agent_id='{AGENT}'")
    await db.disconnect()
    print(f"=== smoke done: {disjoint_ct}/{len(NO_HANDLE_PAIRS)} pairs valid-disjoint ===")


if __name__ == "__main__":
    asyncio.run(main())
