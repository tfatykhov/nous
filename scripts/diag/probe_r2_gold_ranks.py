"""R2 A/B probe: gold-chunk ranks, vector-only vs hybrid chunk search.

Read-only against the MAB eval corpus (nous_mab, agent
mab-eval-prod_memory-8f18622a, 528 chunks). For each of the 5 CR probe
questions from the 2026-07-02 root-cause doc, ranks all chunks with the
production `_search_episode_chunks` in both flag states and reports the
best gold-chunk rank per arm.

Doc baseline (vector-only, text-embedding-3-large@1536): golds at ranks
40 / 8 / 21 / 50 / 16 — 4/5 outside the top-10 recall limit.

Usage (from repo root; OPENAI_API_KEY via .env):
    uv run python scripts/diag/probe_r2_gold_ranks.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import text

AGENT_ID = "mab-eval-prod_memory-8f18622a"
EMBED_MODEL = "text-embedding-3-large"  # matches the stored corpus vectors
EMBED_DIMS = 1536
RECALL_LIMIT = 10  # prod default episode_chunk_recall_limit

# (question, [ILIKE conditions identifying the gold chunk])
PROBES = [
    ("What type of music does Aki Takase play?", ["%carnatic%"]),
    ("What is the country of citizenship of David Farragut?", ["%farragut%", "%denmark%"]),
    ("Which country was Shaman King created in?", ["%shaman king%", "%greece%"]),
    ("Who is the author of Hard Times?", ["%hard times%", "%luther king%"]),
    ("Which religion is Henry of Blois affiliated with?", ["%blois%", "%church of scotland%"]),
]


async def main() -> None:
    from nous.api.retrieval_pipeline import _search_episode_chunks
    from nous.brain.embeddings import EmbeddingProvider
    from nous.config import Settings
    from nous.storage.database import Database

    settings = Settings().model_copy(update={
        "db_host": "127.0.0.1", "db_port": 5433, "db_name": "nous_mab",
        "db_user": "nous", "db_password": "nous_eval",
    })
    db = Database(settings)
    await db.connect()
    embedder = EmbeddingProvider(
        api_key=settings.openai_api_key, model=EMBED_MODEL, dimensions=EMBED_DIMS,
    )
    heart = SimpleNamespace(db=db, _embeddings=embedder, agent_id=AGENT_ID)

    try:
        async with db.session() as s:
            total = (await s.execute(text(
                "SELECT count(*) FROM heart.episode_chunks WHERE agent_id = :a"
            ), {"a": AGENT_ID})).scalar()
        print(f"corpus: {total} chunks, agent={AGENT_ID}")

        report = []
        for question, conds in PROBES:
            where = " AND ".join("content ILIKE :c%d" % i for i in range(len(conds)))
            params = {("c%d" % i): c for i, c in enumerate(conds)}
            params["a"] = AGENT_ID
            async with db.session() as s:
                gold_ids = {r[0] for r in (await s.execute(text(
                    f"SELECT id FROM heart.episode_chunks WHERE agent_id = :a AND {where}"
                ), params)).all()}

            row = {"question": question, "golds": len(gold_ids)}
            for arm, flag in (("vector_only", False), ("hybrid", True)):
                results = await _search_episode_chunks(
                    heart=heart, query=question, agent_id=AGENT_ID, limit=total,
                    settings=SimpleNamespace(chunk_hybrid_search_enabled=flag),
                )
                ranks = [i + 1 for i, r in enumerate(results) if r[0] in gold_ids]
                best = ranks[0] if ranks else None
                row[arm] = {
                    "gold_rank": best,
                    "in_top_%d" % RECALL_LIMIT: bool(best and best <= RECALL_LIMIT),
                    # R1 composition: the proposed episode_chunk_recall_limit=30
                    "in_top_30": bool(best and best <= 30),
                }
            report.append(row)
            print(f"  {question[:55]:<55} gold@ vector={row['vector_only']['gold_rank']} "
                  f"hybrid={row['hybrid']['gold_rank']}")

        n = len(report)
        v_hits = sum(1 for r in report if r["vector_only"]["in_top_%d" % RECALL_LIMIT])
        h_hits = sum(1 for r in report if r["hybrid"]["in_top_%d" % RECALL_LIMIT])
        v30 = sum(1 for r in report if r["vector_only"]["in_top_30"])
        h30 = sum(1 for r in report if r["hybrid"]["in_top_30"])
        print("\n=== SUMMARY ===")
        print(json.dumps({
            "recall_limit": RECALL_LIMIT,
            "gold_in_top_k": {"vector_only": f"{v_hits}/{n}", "hybrid": f"{h_hits}/{n}"},
            "gold_in_top_30": {"vector_only": f"{v30}/{n}", "hybrid": f"{h30}/{n}"},
            "detail": report,
        }, indent=2))
    finally:
        await db.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
