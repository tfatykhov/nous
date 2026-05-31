"""Phase 0 BARE-PIPELINE lens — does the current similarity pipe register the controls
(PASS) and fail the concept-bridge faculty cases (FAIL)? That separation = a working
discriminating instrument (docs/research/017).

Gives the current system its BEST shot: heart_graph_all_types ON, seed_score ON,
adjacency ON, co-mention edges built, rerank_by_score=True. Private invented entities,
fresh agent namespace. Per concept-bridge item: validity gate (bridge fact OUTSIDE
top-k, else the item tests nothing) + answer-in-top-k grade.

  uv run python scripts/diag/faculty/measure_bare.py
"""
from __future__ import annotations

import asyncio
import os
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
    "DB_USER": "nous", "DB_PASSWORD": "nous_eval", "NOUS_AGENT_ID": "nous-faculty-eval",
})

from scripts.diag.faculty.corpus import (  # noqa: E402
    CONCEPT_BRIDGE, CONTROL_POSITIVE, PREDICTIONS, direct_load_facts,
)

AGENT = "nous-faculty-eval"
K = 10
MEAS_LIMIT = 30
RUN_DIR = HERE / "runs" / (time.strftime("%Y%m%d-%H%M%S") + "-bare")
RUN_DIR.mkdir(parents=True, exist_ok=True)
LOG = open(RUN_DIR / "result.txt", "w", encoding="utf-8")


def out(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    LOG.write(s + "\n")
    LOG.flush()


async def main() -> None:
    from sqlalchemy import text

    from nous.api.retrieval_pipeline import run_recall_pipeline
    from nous.brain.brain import Brain
    from nous.brain.embeddings import EmbeddingProvider
    from nous.brain.graph_densifier import GraphDensifier
    from nous.brain.graph_linker import GraphLinker
    from nous.config import Settings
    from nous.heart.heart import Heart
    from nous.storage.database import Database

    s = Settings()
    # Best shot for the CURRENT system: all association consumers on, deterministic.
    s_meas = s.model_copy(update={
        "graph_neighbor_seed_score_enabled": True,
        "heart_graph_all_types_enabled": True,
        "graph_adjacency_boost_enabled": True,
        "residual_activation_enabled": False,
        "comention_linking_enabled": True,
    })
    db = Database(s)
    await db.connect()
    emb = EmbeddingProvider(api_key=s.openai_api_key, model="text-embedding-3-large", dimensions=1536)
    heart = Heart(database=db, settings=s_meas, embedding_provider=emb)
    brain = Brain(db, s_meas, emb)

    out(f"run dir: {RUN_DIR}")
    out("PRE-REGISTERED PREDICTIONS:")
    for k, v in PREDICTIONS.items():
        out(f"   {k:18} -> {v}")
    out("\nbest-shot flags: heart_graph_all_types=ON seed_score=ON adjacency=ON "
        "comention=BUILT residual=OFF rerank=ON")

    # Reset namespace + direct-load facts.
    async with db.session() as sess:
        for tbl in ("brain.graph_edges", "heart.facts"):
            await sess.execute(text(f"DELETE FROM {tbl} WHERE agent_id=:a"), {"a": AGENT})
        await sess.commit()
    facts = direct_load_facts()
    async with db.session() as sess:
        for content, src in facts:
            vec = await emb.embed(content)
            vlit = "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
            await sess.execute(text(
                "INSERT INTO heart.facts (agent_id, content, source, confidence, active, embedding) "
                "VALUES (:a, :c, :src, 0.9, TRUE, CAST(:v AS vector))"
            ), {"a": AGENT, "c": content, "src": src, "v": vlit})
        await sess.commit()
    # Give the current system its co-mention shot (expect 0 between single-token-concept pairs).
    linker = GraphLinker(db, emb, s_meas, AGENT)
    dens = GraphDensifier(db=db, graph_linker=linker, embedder=emb, settings=s_meas, agent_id=AGENT)
    n_cm = await dens.build_comention_edges()
    out(f"loaded {len(facts)} facts; built {n_cm} co_mention edges\n")

    async def fact_rank(query: str, ilike: str):
        """Rank of the first fact whose content ILIKE %ilike% in the top MEAS_LIMIT."""
        res, _ = await run_recall_pipeline(query, heart, brain, s_meas, limit=MEAS_LIMIT, rerank_by_score=True)
        for i, r in enumerate(res):
            if ilike.lower() in (r.description or "").lower():
                return i + 1, (r.metadata or {}).get("stage_origin", r.source)
        return None, "-"

    out("=" * 70)
    # --- Positive control: directly-named fact must be in top-k ---
    rank, via = await fact_rank(CONTROL_POSITIVE["query"], CONTROL_POSITIVE["answer_token"])
    ctrl_pass = bool(rank and rank <= K)
    out(f"[control_positive] '{CONTROL_POSITIVE['query']}' -> answer rank={rank} via={via} "
        f"top{K}={ctrl_pass}  predict=PASS  {'OK' if ctrl_pass else '!! INSTRUMENT BROKEN'}")

    # --- Concept-bridge: validity gate + answer-in-top-k ---
    out("\n--- concept_bridge (predict FAIL bare) ---")
    valid, recovered = [], []
    for c in CONCEPT_BRIDGE:
        h1_rank, _ = await fact_rank(c["query"], c["hop1_token"])
        br_rank, br_via = await fact_rank(c["query"], c["answer_token"])
        disjoint = (br_rank is None) or (br_rank > K)   # bridge OUTSIDE top-k => valid item
        if disjoint:
            valid.append(c["id"])
        in_topk = bool(br_rank and br_rank <= K)
        if disjoint and in_topk:
            recovered.append(c["id"])
        out(f"  [{c['id']:11}] hop1_rank={h1_rank} bridge(answer)_rank={br_rank} via={br_via} "
            f"valid(disjoint)={disjoint} answer_in_top{K}={in_topk}")

    out("\n--- SUMMARY (bare lens) ---")
    out(f"control_positive: {'PASS' if ctrl_pass else 'FAIL (instrument broken!)'}")
    out(f"concept_bridge valid items (bridge outside top-{K}): {len(valid)}/{len(CONCEPT_BRIDGE)}  {valid}")
    out(f"concept_bridge answer recovered into top-{K}: {len(recovered)}/{len(valid) or 0}  {recovered}")
    discriminates = ctrl_pass and len(valid) >= 1 and len(recovered) == 0
    out("")
    if discriminates:
        out("INSTRUMENT OK: control PASSES, concept-bridge is valid+FAILS (as pre-registered) "
            "-> the bare lens cleanly registers the faculty gap. Proceed to add the agentic "
            "lens + remaining classes.")
    elif not ctrl_pass:
        out("BROKEN INSTRUMENT: positive control failed -> fix retrieval/loading before trusting any faculty result.")
    elif len(valid) == 0:
        out("INCONCLUSIVE: concept-bridge items are vector-reachable (not disjoint) -> add filler so the bridge sits outside top-k.")
    elif recovered:
        out(f"SURPRISE vs prediction: the current system RECOVERED {recovered} concept-bridges -> "
            "investigate (cosine link? co-mention? this would re-sequence the roadmap).")
    await db.disconnect()
    LOG.close()


if __name__ == "__main__":
    asyncio.run(main())
