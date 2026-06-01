"""Flag arm for cells 12 (contradiction) + 13 (recency) — docs/research/018.

BARE test (where the recency resolver actually lives — run_recall_pipeline, NOT the pre_turn
context-injection path the agent used). The resolver needs: same `subject`, differing
`event_date`, and difflib >= 0.55 (or a contradicts edge). The baseline direct-inserts left
subject/event_date NULL and phrased the pair too differently, so it could never fire. This
sets up the pair properly, then probes resolver-OFF vs resolver-ON: does the CURRENT value get
tagged + out-rank the SUPERSEDED one?

  uv run python scripts/diag/faculty/baseline_flagarm.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import date
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

AGENT = "nous-baseline-eval"

# Parallel phrasing (difflib >= 0.55) + shared subject + differing event_date.
SETUP = [
    {"src": "baseline:s_b1", "content": "My primary bank is Halloway Federal.",
     "subject": "primary bank", "date": "2025-06-15", "role": "c12 STALE"},
    {"src": "baseline:s_b2", "content": "My primary bank is Pellan Mutual.",
     "subject": "primary bank", "date": "2026-04-01", "role": "c12 CURRENT"},
    {"src": "baseline:s_d1", "content": "I chose the Korren framework for the dashboard.",
     "subject": "dashboard framework", "date": "2025-09-15", "role": "c13 STALE"},
    {"src": "baseline:s_d2", "content": "I chose the Aurelis framework for the dashboard.",
     "subject": "dashboard framework", "date": "2026-02-15", "role": "c13 CURRENT"},
]
PROBES = [
    {"cell": "c12", "query": "What is my current primary bank?", "current": "Pellan Mutual", "stale": "Halloway"},
    {"cell": "c13", "query": "What dashboard framework am I using now?", "current": "Aurelis", "stale": "Korren"},
]


async def main() -> None:
    from sqlalchemy import text

    from nous.api.retrieval_pipeline import run_recall_pipeline
    from nous.brain.brain import Brain
    from nous.brain.embeddings import EmbeddingProvider
    from nous.config import Settings
    from nous.heart.heart import Heart
    from nous.storage.database import Database

    base = {"graph_neighbor_seed_score_enabled": True, "heart_graph_all_types_enabled": True,
            "graph_adjacency_boost_enabled": True, "residual_activation_enabled": False}
    s_off = Settings().model_copy(update={**base, "recency_resolver_enabled": False})
    s_on = Settings().model_copy(update={**base, "recency_resolver_enabled": True})
    db = Database(s_off); await db.connect()
    emb = EmbeddingProvider(api_key=s_off.openai_api_key, model="text-embedding-3-large", dimensions=1536)
    heart = Heart(database=db, settings=s_off, embedding_provider=emb); brain = Brain(db, s_off, emb)

    print("=== FLAG ARM (cells 12/13): set subject+event_date+parallel phrasing, re-embed ===")
    async with db.session() as sess:
        for f in SETUP:
            vec = await emb.embed(f["content"])
            vlit = "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
            await sess.execute(text(
                "UPDATE heart.facts SET content=:c, subject=:s, event_date=:d, "
                "embedding=CAST(:v AS vector) WHERE agent_id=:a AND source=:src"
            ), {"c": f["content"], "s": f["subject"], "d": date.fromisoformat(f["date"]),
                "v": vlit, "a": AGENT, "src": f["src"]})
            print(f"  set [{f['role']:11}] {f['content']}  (event_date={f['date']}, subject='{f['subject']}')")
        await sess.commit()

    def find(results, token):
        for i, r in enumerate(results):
            if token.lower() in (r.description or "").lower():
                return i + 1, r.metadata.get("recency_status"), round(r.score or 0, 3)
        return None, None, None

    print()
    for p in PROBES:
        print(f"--- {p['cell']}: '{p['query']}'  (current={p['current']!r} stale={p['stale']!r}) ---")
        for label, st in (("resolver OFF", s_off), ("resolver ON ", s_on)):
            res, _ = await run_recall_pipeline(p["query"], heart, brain, st, limit=15, rerank_by_score=True)
            cr, cs, csc = find(res, p["current"])
            sr, ss, ssc = find(res, p["stale"])
            verdict = ""
            if cr and sr:
                verdict = "CURRENT out-ranks stale" if cr < sr else "!! stale out-ranks current"
            print(f"    [{label}] current: rank={cr} status={cs} score={csc}   "
                  f"stale: rank={sr} status={ss} score={ssc}   {verdict}")
        print()

    await db.disconnect()
    print("=== flag arm done ===")


if __name__ == "__main__":
    asyncio.run(main())
