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

Measured 2026-08-24 on qrels_graph_nogate.jsonl (57 qrels, nous_prod_20260801),
PINNED PROD SHAPE (see _PROD_SHAPE — these numbers are not reproducible without it):

    baseline           MRR@10 0.0906   recall@served 0.5614   served 77.6
    POSCTRL_graph_off  MRR@10 0.1527   recall@served 0.5614   served 54.6
    spread_off         MRR@10 0.0906   recall@served 0.5614   served 68.9
    spread_on          MRR@10 0.0906   recall@served 0.5614   served 77.6
    cs_parity          MRR@10 0.0906   recall@served 0.5614   served 77.6
    cs_baseline        MRR@10 0.0906   recall@served 0.5614   served 77.6

Control moved 24/57 (dMRR +0.0621) => the set discriminates. All three spreading
arms were 57/57 ties at dMRR exactly 0.0000 — a measured null — while spread_off
serves 8.7 FEWER rows, so spreading changes the served set and changes no measured
outcome. recall@served is identical across every arm INCLUDING graph_off.

The null is on FAVOURABLE ground: gold are decision-targets of inferred edges,
precisely what spreading depth-1 traverses.
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
    # codex P1: cs_parity's MATCHED control. It pins the same preconditions with
    # parity OFF, because `graph_neighbor_seed_score_enabled` defaults to FALSE —
    # comparing cs_parity against plain `baseline` would attribute the scoring
    # policy's effect to the parity change. `nous_eval/retrieval.py` already
    # defines this pairing as `cs_baseline`; the probe must not diverge from it.
    "cs_baseline": {"spreading_activation_enabled": "true",
                    "spreading_score_depth1_parity": False,
                    "graph_neighbor_seed_score_enabled": True,
                    "graph_inferred_edge_penalty": 1.0},
}

# Arms compared against something other than plain `baseline`.
MATCHED_CONTROL = {"cs_parity": "cs_baseline"}

# PROD SHAPE — pinned explicitly, NOT inherited from an ambient .env.
#
# Measured the hard way 2026-08-24: run from a checkout with prod's .env the
# control moved 23/57 (served 86.5); run from a worktree WITHOUT it, every arm
# tied and the control moved 0/57 (served 37.7). Same script, same qrels, same
# corpus — different system. A probe whose result depends on which directory it
# is launched from is not an instrument.
#
# These are the flags that are `true` in prod and `false` by code default, i.e.
# exactly the ones an ambient-.env run would silently flip. Override with
# --defaults to measure code-default behaviour deliberately instead.
_PROD_SHAPE = {
    "episode_chunks_enabled": True,
    "chunk_hybrid_search_enabled": True,
    "episode_chunk_recall_limit": 30,
    "heart_graph_all_types_enabled": True,
    "graph_neighbor_seed_score_enabled": True,
    "graph_adjacency_boost_enabled": True,
    "graph_inferred_edge_penalty": 1.0,
    "keyed_fact_leg_enabled": True,
    "exemplar_mode_enabled": True,
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
    rr, hits, served, errors = [], 0, [], []
    try:
        for idx, q in enumerate(qrels):
            try:
                res, _ = await run_recall_pipeline(
                    query=q["query"], heart=heart, brain=brain, settings=s,
                    limit=top_k, memory_types=q.get("memory_types"),
                    rerank_by_score=True)
            except Exception as exc:
                # codex P1: NEVER score a raise as reciprocal rank 0. Arms run
                # separately, so an asymmetric failure would read as genuine
                # paired movement — and could make the positive control appear
                # to separate on nothing but exceptions, manufacturing the very
                # validity signal this script gates on. Symmetric failures would
                # manufacture ties instead. The query is EXCLUDED from every arm
                # (see `_valid` below) and reported.
                errors.append((idx, f"{type(exc).__name__}: {exc}"))
                rr.append(None)
                # codex P2: do NOT append 0 to `served`. Averaging a failure as
                # "served nothing" depresses this arm's candidate count and can
                # masquerade as an arm-specific set change — the same wrong
                # conclusion the rank-0 handling produced, one metric over.
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
    ok = [x for x in rr if x is not None]
    return {"mrr": statistics.mean(ok) if ok else 0.0, "rr": rr,
            "recall_at_served": hits / max(len(ok), 1),
            "avg_served": statistics.mean(served) if served else 0.0,
            "errors": errors}


async def run(args) -> int:
    qrels = [json.loads(x) for x in Path(args.qrels).read_text(
        encoding="utf-8").splitlines() if x.strip()]
    # `_env_file=None` is load-bearing (codex P1): pinning nine flags via
    # model_copy still lets Settings() read an ambient .env FIRST, so any other
    # retrieval override sitting next to the script — NOUS_SPREADING_ACTIVATION_
    # DECAY, an RRF knob, a floor — silently varies the run by launch directory.
    # `--defaults` was worse still: it pinned nothing at all.
    base = Settings(_env_file=None).model_copy(update={
        "db_host": args.host, "db_port": args.port, "db_name": args.db,
        "db_user": args.user, "db_password": os.environ["EVAL_DB_PASSWORD"],
        "agent_id": args.agent,
        # The embedding key is the one thing that MUST come from the environment.
        "openai_api_key": os.environ.get("OPENAI_API_KEY", ""),
        **({} if args.defaults else _PROD_SHAPE), **_EVAL_OFF})
    shape = "CODE DEFAULTS" if args.defaults else "PROD SHAPE (pinned)"
    print(f"config: {shape}   (.env NOT read)")

    print(f"qrels: {len(qrels)}   top_k={args.top_k}   db={args.db}\n")
    out = {}
    for name, flags in ARMS.items():
        out[name] = await run_arm(flags, qrels, base, args.top_k)
        print(f"  ran {name}")


    def paired(name, against="baseline"):
        """Paired deltas over indices valid in BOTH arms. A query that raised in
        either arm is excluded, never zero-scored — see run_arm."""
        a, c = out[name]["rr"], out[against]["rr"]
        d = [x - y for x, y in zip(a, c) if x is not None and y is not None]
        if not d:
            return (0.0, 0, 0, 0, 0)
        return (statistics.mean(d), sum(1 for x in d if x > 1e-9),
                sum(1 for x in d if x < -1e-9), sum(1 for x in d if abs(x) <= 1e-9),
                len(a) - len(d))

    cd, cb, cw, _ct, cskipped = paired(CONTROL)
    moved = cb + cw
    print("\n" + "=" * 62)
    n_err = sum(len(r["errors"]) for r in out.values())
    if n_err:
        print(f"!! {n_err} pipeline error(s) across arms — those queries are "
              f"EXCLUDED from every comparison, not scored as misses")
        for nm, r in out.items():
            for idx, msg in r["errors"][:3]:
                print(f"     {nm} qrel[{idx}]: {msg[:90]}")
    comparable = len(qrels) - cskipped
    print(f"POSITIVE CONTROL moved {moved}/{comparable} comparable queries "
          f"(dMRR {cd:+.4f})")
    if not moved:
        print("\nCONTROL DID NOT SEPARATE — arm results withheld.")
        print("An arm known to change retrieval grossly produced no movement, so")
        print("these queries cannot discriminate and any null below would be an")
        print("ABSENCE OF MEASUREMENT, not a result. Fix the qrel set first.")
        print("=" * 62)
        return 3
    print("Set discriminates => a null on the arms below is a RESULT.")
    print("=" * 62)

    # Per-arm metrics are printed HERE, after the control passes — never before
    # (codex P1). Printing them during the run meant every MRR was already on
    # screen when the guard fired, so "arm results withheld" was false and a
    # reader could draw the exact "all arms tie" conclusion the control exists
    # to forbid. Observed for real: a broken-embedding run showed six arms at
    # 0.0000 above a message claiming nothing had been reported.
    print("\n--- per-arm metrics ---")
    for name in ARMS:
        r = out[name]
        print(f"{name:<20} MRR@{args.top_k}={r['mrr']:.4f}  "
              f"recall@served={r['recall_at_served']:.4f}  "
              f"avg_served={r['avg_served']:.1f}")

    print("\n--- paired vs baseline (per-query reciprocal rank) ---")
    for name in ARMS:
        if name in ("baseline", CONTROL) or name in MATCHED_CONTROL.values():
            continue
        against = MATCHED_CONTROL.get(name, "baseline")
        d, better, worse, ties, skipped = paired(name, against)
        note = "" if against == "baseline" else f"   [vs {against}]"
        skip = f"  skipped={skipped}" if skipped else ""
        print(f"{name:<20} dMRR={d:+.4f}  better={better:>3}  "
              f"worse={worse:>3}  ties={ties:>3}{skip}{note}")
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
    p.add_argument("--defaults", action="store_true",
                   help="Measure CODE-DEFAULT config instead of the pinned prod "
                        "shape. Results are not comparable across the two.")
    args = p.parse_args()
    if "EVAL_DB_PASSWORD" not in os.environ:
        print("set EVAL_DB_PASSWORD", file=sys.stderr)
        return 1
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
