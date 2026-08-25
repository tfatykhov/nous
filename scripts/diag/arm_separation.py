"""Paired A/B over a qrel file — WITH A MANDATORY POSITIVE CONTROL.

    python scripts/diag/arm_separation.py \
        --qrels $NOUS_EVAL_FIXTURES_DIR/v2026-Q2/qrels_graph_nogate.jsonl \
        --db nous_prod_20260801 --agent nous-default

THE CONTROL IS NOT OPTIONAL, AND THAT IS THE POINT. "We cannot measure it" and
"we measured it and it is zero" are different findings, and nothing distinguishes
them except an arm expected to move grossly. This script always runs
`graph_recall_enabled=False` alongside whatever you asked for, and REFUSES TO
REPORT arm results if that control does not separate — because a null from an
inert qrel set is not a null, it is an absence of measurement, and the difference
has produced wrong verdicts before (decisions 7b29cf7f, ac40336b).

Measured 2026-08-24 on qrels_graph_nogate.jsonl (57 qrels, nous_prod_20260801):

    baseline           MRR@10 0.1620   recall@served 0.5614   served 86.5
    POSCTRL_graph_off  MRR@10 0.2260   recall@served 0.5614   served 60.1
    spread_off         MRR@10 0.1620   recall@served 0.5614   served 78.1
    spread_on          MRR@10 0.1620   recall@served 0.5614   served 86.5
    cs_parity          MRR@10 0.1620   recall@served 0.5614   served 86.5

Control moved 23/57 (dMRR +0.0640) => the set discriminates. The spreading arms
were 57/57 ties at dMRR exactly 0.0000 — a measured null, on a set whose gold are
decision-targets of inferred edges, i.e. FAVOURABLE ground for spreading.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from nous.api.retrieval_pipeline import run_recall_pipeline  # noqa: E402
from nous.brain.brain import Brain  # noqa: E402
from nous.brain.embeddings import EmbeddingProvider  # noqa: E402
from nous.config import Settings  # noqa: E402
from nous.heart.heart import Heart  # noqa: E402
from nous.storage.database import Database  # noqa: E402

CONTROL = "POSCTRL_graph_off"

# Arms are (name -> Settings overrides). `baseline` and the control are always
# present; everything else is the question being asked.
ARMS: dict[str, dict] = {
    "baseline": {},
    CONTROL: {"graph_recall_enabled": False},
    "spread_off": {"spreading_activation_enabled": "false"},
    "spread_on": {"spreading_activation_enabled": "true"},
    # C-S parity carries its PRECONDITIONS — the branch is gated on the scoring
    # policy that defines parity, so omitting either makes the flag inert and
    # this arm would silently measure the baseline.
    "cs_parity": {"spreading_activation_enabled": "true",
                  "spreading_score_depth1_parity": True,
                  "graph_neighbor_seed_score_enabled": True,
                  "graph_inferred_edge_penalty": 1.0},
}

_EVAL_OFF = {
    "event_bus_enabled": False, "fact_extraction_enabled": False,
    "episode_summary_enabled": False, "sleep_enabled": False,
    "heartbeat_enabled": False, "schedule_enabled": False,
    "subtask_enabled": False, "dag_enabled": False,
    "query_expansion_enabled": False,
    "actionability_backfill_on_startup": False,
}


async def run_arm(flags, qrels, base, top_k):
    s = base.model_copy(update=flags)
    db = Database(settings=s)
    await db.connect()
    emb = EmbeddingProvider(api_key=s.openai_api_key,
                            model="text-embedding-3-large", dimensions=1536)
    heart = Heart(database=db, settings=s, embedding_provider=emb)
    brain = Brain(database=db, settings=s, embedding_provider=emb)
    rr, hits, served = [], 0, []
    try:
        for q in qrels:
            try:
                res, _ = await run_recall_pipeline(
                    query=q["query"], heart=heart, brain=brain, settings=s,
                    limit=top_k, memory_types=q.get("memory_types"),
                    rerank_by_score=True)
            except Exception:
                rr.append(0.0)
                served.append(0)
                continue
            ids = [str(r.id) for r in res]
            served.append(len(ids))
            gold = set(q["gold_ids"])
            rank = next((i + 1 for i, x in enumerate(ids[:top_k]) if x in gold), None)
            rr.append(1.0 / rank if rank else 0.0)
            hits += any(x in gold for x in ids)
    finally:
        await brain.close()
        await db.disconnect()
    return {"mrr": statistics.mean(rr), "rr": rr,
            "recall_at_served": hits / max(len(qrels), 1),
            "avg_served": statistics.mean(served)}


async def run(args) -> int:
    qrels = [json.loads(x) for x in Path(args.qrels).read_text(
        encoding="utf-8").splitlines() if x.strip()]
    base = Settings().model_copy(update={
        "db_host": args.host, "db_port": args.port, "db_name": args.db,
        "db_user": args.user, "db_password": os.environ["EVAL_DB_PASSWORD"],
        "agent_id": args.agent, **_EVAL_OFF})

    print(f"qrels: {len(qrels)}   top_k={args.top_k}   db={args.db}\n")
    out = {}
    for name, flags in ARMS.items():
        out[name] = await run_arm(flags, qrels, base, args.top_k)
        r = out[name]
        print(f"{name:<20} MRR@{args.top_k}={r['mrr']:.4f}  "
              f"recall@served={r['recall_at_served']:.4f}  "
              f"avg_served={r['avg_served']:.1f}")

    b = out["baseline"]["rr"]

    def paired(name):
        d = [x - y for x, y in zip(out[name]["rr"], b)]
        return (statistics.mean(d), sum(1 for x in d if x > 1e-9),
                sum(1 for x in d if x < -1e-9), sum(1 for x in d if abs(x) <= 1e-9))

    cd, cb, cw, _ = paired(CONTROL)
    moved = cb + cw
    print("\n" + "=" * 62)
    print(f"POSITIVE CONTROL moved {moved}/{len(qrels)} queries (dMRR {cd:+.4f})")
    if not moved:
        print("\nCONTROL DID NOT SEPARATE — arm results withheld.")
        print("An arm known to change retrieval grossly produced no movement, so")
        print("these queries cannot discriminate and any null below would be an")
        print("ABSENCE OF MEASUREMENT, not a result. Fix the qrel set first.")
        print("=" * 62)
        return 3
    print("Set discriminates => a null on the arms below is a RESULT.")
    print("=" * 62)

    print("\n--- paired vs baseline (per-query reciprocal rank) ---")
    for name in ARMS:
        if name in ("baseline", CONTROL):
            continue
        d, better, worse, ties = paired(name)
        print(f"{name:<20} dMRR={d:+.4f}  better={better:>3}  "
              f"worse={worse:>3}  ties={ties:>3}")
    print("\nCAVEAT to carry with any null: check what the gold ARE. If they are "
          "targets of the same\nedge family the mechanism traverses, the null is "
          "on favourable ground. And a cosine-\nfindable qrel set cannot credit "
          "'related but dissimilar' — that needs an end-task oracle.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--qrels", required=True)
    p.add_argument("--db", required=True)
    p.add_argument("--agent", required=True)
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=5433)
    p.add_argument("--user", default="nous")
    p.add_argument("--top-k", type=int, default=10)
    args = p.parse_args()
    if "EVAL_DB_PASSWORD" not in os.environ:
        print("set EVAL_DB_PASSWORD", file=sys.stderr)
        return 1
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
