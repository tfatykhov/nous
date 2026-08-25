"""Measure the qrel GENERATOR's unconditional baseline — run this before
attributing a low mine yield to any mechanism.

One arm, one question: for a query `generate_graph_qrels` writes from an edge,
is the TARGET already in that query's vector top-50, with no graph involved?

WHY THIS EXISTS. `_validate_query` keeps a qrel only when graph-off MISSES and
graph-on HITS. If the generator writes questions semantic search already answers,
that criterion is capped no matter which edges you feed it — and the cap is a
property of the GENERATOR, not of the edge family. Attributing a low yield to
cosine edges, or to co-occurrence edges, without this number has produced two
wrong verdicts already (decisions 7b29cf7f, 004641d7).

Measured 2026-08-24 on `nous_prod_20260801`: 55/58 (94.8%) in vector top-50, at
median rank 2, 52/58 in the top 10 => graph-only ceiling ~5%.

    python scripts/diag/qrel_generator_baseline.py \
        --db nous_prod_20260801 --agent nous-default -n 60

Reuses the miner's own `fetch_edge_candidates` / `generate_query`, so this
measures the real generator rather than an approximation of it.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import text  # noqa: E402

from nous.api.anthropic_client import create_client  # noqa: E402
from nous.brain.embeddings import EmbeddingProvider  # noqa: E402
from nous.config import Settings  # noqa: E402
from nous.storage.database import Database  # noqa: E402
from nous_eval.generate_graph_qrels import (  # noqa: E402
    fetch_edge_candidates,
    generate_query,
)

_NODE_UNION = """
    SELECT id FROM (
      SELECT id, embedding FROM heart.facts
        WHERE agent_id=:a AND active AND embedding IS NOT NULL
      UNION ALL SELECT id, embedding FROM heart.episode_chunks
        WHERE agent_id=:a AND embedding IS NOT NULL
      UNION ALL SELECT id, embedding FROM heart.episodes
        WHERE agent_id=:a AND embedding IS NOT NULL
      UNION ALL SELECT id, embedding FROM brain.decisions
        WHERE agent_id=:a AND embedding IS NOT NULL
    ) n ORDER BY n.embedding <=> CAST(:v AS vector) LIMIT :k
"""


async def run(args) -> int:
    s = Settings().model_copy(update={
        "db_host": args.host, "db_port": args.port, "db_name": args.db,
        "db_user": args.user, "db_password": os.environ["EVAL_DB_PASSWORD"],
        "agent_id": args.agent,
    })
    db = Database(settings=s)
    await db.connect()
    emb = EmbeddingProvider(api_key=s.openai_api_key,
                            model=args.embed_model, dimensions=1536)
    client = create_client(s)
    await client.start()
    try:
        cands = await fetch_edge_candidates(
            db, agent_id=s.agent_id, sample_size=args.n,
            min_weight=args.min_weight, allow_inferred=args.allow_inferred)
        print(f"edge candidates sampled : {len(cands)}")
        if not cands:
            # An empty candidate set is itself a finding, not a failure to run.
            print("\nNO CANDIDATES. That is a RESULT: the miner requires "
                  "target_type='decision' and (without --allow-inferred) "
                  "excludes extraction_method='inferred'. On a corpus whose "
                  "decision-targeting edges are ALL inferred, the candidate set "
                  "is empty by construction — independently of generator bias.")
            print("Check:  SELECT target_type, extraction_method, count(*) "
                  "FROM brain.graph_edges WHERE agent_id=... GROUP BY 1,2;")
            return 2

        generated = declined = in_topk = 0
        ranks: list[int] = []
        for i, c in enumerate(cands, 1):
            try:
                gen = await generate_query(c, client, args.model)
            except Exception as exc:
                print(f"  [{i}] generator error: {type(exc).__name__}")
                continue
            if gen is None:
                declined += 1
                continue
            generated += 1
            vec = await emb.embed(gen[0])
            async with db.session() as sess:
                rows = await sess.execute(text(_NODE_UNION),
                                          {"a": s.agent_id, "v": str(vec),
                                           "k": args.top_k})
                ids = [str(r.id) for r in rows.all()]
            rank = next((j + 1 for j, rid in enumerate(ids)
                         if rid == str(c.target_id)), None)
            if rank is not None:
                in_topk += 1
                ranks.append(rank)
            if i % 10 == 0:
                print(f"  [{i}/{len(cands)}] generated={generated} "
                      f"in_top{args.top_k}={in_topk}")

        print("\n" + "=" * 60)
        print(f"queries generated          : {generated}  (declined {declined})")
        if not generated:
            print("no queries generated — nothing to report")
            return 2
        pct = 100.0 * in_topk / generated
        print(f"target in VECTOR top-{args.top_k}    : {in_topk}/{generated} ({pct:.1f}%)")
        print(f"  => graph-ONLY ceiling    : {100 - pct:.1f}%")
        if ranks:
            ranks.sort()
            print(f"  median rank when found   : {ranks[len(ranks) // 2]}")
            print(f"  in top-10                : {sum(1 for r in ranks if r <= 10)}"
                  f"/{generated}")
        print("=" * 60)
        print("A high number here means the graph-only criterion is capped for "
              "EVERY edge family. Do not tune edge selection against it; use\n"
              "`generate_graph_qrels --no-reachability-gate` instead, and pair\n"
              "the resulting set with a positive control (arm_separation.py).")
        return 0
    finally:
        await client.close()
        await db.disconnect()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--db", required=True, help="corpus database name")
    p.add_argument("--agent", required=True)
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=5433)
    p.add_argument("--user", default="nous")
    p.add_argument("-n", type=int, default=60, help="edges to sample")
    p.add_argument("--top-k", type=int, default=50)
    p.add_argument("--min-weight", type=float, default=0.7)
    p.add_argument("--allow-inferred", action="store_true",
                   help="Required on corpora whose decision-targeting edges are "
                        "all inferred. Circular for F065 penalty work; fine for "
                        "spreading arms, where the penalty is pinned at 1.0.")
    p.add_argument("--model", default="claude-haiku-4-5-20251001")
    p.add_argument("--embed-model", default="text-embedding-3-large")
    args = p.parse_args()
    if "EVAL_DB_PASSWORD" not in os.environ:
        print("set EVAL_DB_PASSWORD", file=sys.stderr)
        return 1
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
