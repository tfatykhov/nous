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
PINNED PROD SHAPE (nous_eval.env_pin.PROD_SHAPE, enforced by pinned_settings):

    baseline           MRR@10 0.1620   recall@served 0.5614   served 81.1
    POSCTRL_graph_off  MRR@10 0.2260   recall@served 0.5614   served 54.6
    spread_off         MRR@10 0.1620   recall@served 0.5614   served 72.6  (see NOISE FLOOR)
    spread_on          MRR@10 0.1620   recall@served 0.5614   served 81.1
    cs_parity          MRR@10 0.1620   recall@served 0.5614   served 81.1
    cs_baseline        MRR@10 0.1620   recall@served 0.5614   served 81.1

Control moved 23/57 (dMRR +0.0640) => the set discriminates. The spreading arms
are a measured null — while spread_off serves 8.5 FEWER rows, so spreading
changes the served set and changes no measured outcome. recall@served is
identical across every arm INCLUDING graph_off.

NOISE FLOOR — read this before believing any small delta here. Repeating the
IDENTICAL command gave spread_off 57/57 ties (dMRR 0.0000) on one run and 56/57
with one query better (dMRR +0.0044) on the next. Same qrels, same corpus, same
pinned config: one query's ranking is not deterministic, presumably a score tie
broken by ordering. So **a delta of one query is indistinguishable from noise**,
and the instrument's resolution is ~±0.005 dMRR at n=57. Both spread_on and
cs_parity returned exactly 0.0000 with 57/57 ties on every run — those are ties
by identity, not by rounding, which is a stronger statement than spread_off's.

These absolute values SUPERSEDE an earlier run of the same command that recorded
baseline 0.0906 / control 24-57. That run was launched from a shell with prod's
`.env` exported and `Settings(_env_file=None)` does not block the process
environment, so a run labelled "pinned" was not — the leak moved baseline MRR by
79%. Every arm delta was unaffected, which is the useful part: the null survived
a config change large enough to move the absolutes that much.

CORROBORATION, independent of this qrel set: scripts/diag/qrel_generator_baseline.py
reports graph-ON and graph-OFF hitting the target in the SAME 20/57 queries, so
graph expansion changed top-10 membership in zero of 57 cases — on ground where
every target is the endpoint of the edge its query was written from.

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
from nous.heart.heart import Heart  # noqa: E402
from nous.storage.database import Database  # noqa: E402
from nous_eval.env_pin import (  # noqa: E402
    PROD_SHAPE,
    eval_off,
    pinned_settings,
)

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

# PROD_SHAPE / EVAL_OFF live in nous_eval.env_pin so every probe selects the
# SAME shape. Measured the hard way 2026-08-24: run from a checkout with prod's
# .env the control moved 23/57 (served 86.5); run from a worktree WITHOUT it,
# every arm tied and the control moved 0/57 (served 37.7). Same script, same
# qrels, same corpus — different system. A probe whose result depends on which
# directory it is launched from is not an instrument. Use --defaults to measure
# code-default behaviour deliberately instead.


async def run_arm(flags, qrels, base, top_k):
    s = base.model_copy(update=flags)
    db = Database(settings=s)
    await db.connect()
    emb = EmbeddingProvider(api_key=s.openai_api_key,
                            model="text-embedding-3-large", dimensions=1536)
    heart = Heart(database=db, settings=s, embedding_provider=emb)
    brain = Brain(database=db, settings=s, embedding_provider=emb)
    # All three are INDEX-ALIGNED with `qrels`, carrying None where the query
    # raised. Aggregation happens later, over a population shared by every arm.
    rr, hit, served, errors = [], [], [], []
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
                # manufacture ties instead.
                #
                # codex P2, same principle one metric over: a failure must not
                # append 0 to `served` either — that reads as "this arm served
                # fewer candidates", an arm-specific set change, which is
                # precisely the conclusion this probe draws from served deltas.
                errors.append((idx, f"{type(exc).__name__}: {exc}"))
                rr.append(None)
                hit.append(None)
                served.append(None)
                continue
            ids = [str(r.id) for r in res]
            gold = set(q["gold_ids"])
            rank = next((i + 1 for i, x in enumerate(ids[:top_k]) if x in gold), None)
            rr.append(1.0 / rank if rank else 0.0)
            hit.append(any(x in gold for x in ids))
            served.append(len(ids))
    finally:
        await brain.close()
        await db.disconnect()
    return {"rr": rr, "hit": hit, "served": served, "errors": errors}


async def run(args) -> int:
    qrels = [json.loads(x) for x in Path(args.qrels).read_text(
        encoding="utf-8").splitlines() if x.strip()]
    # Both settings sources must be shut off, not just the dotenv one — see
    # nous_eval.env_pin. `--defaults` pinned nothing at all before this.
    base = pinned_settings(
        db_host=args.host, db_port=args.port, db_name=args.db,
        db_user=args.user, db_password=os.environ["EVAL_DB_PASSWORD"],
        agent_id=args.agent,
        # The embedding key is the one input that MUST come from the process
        # environment, so it is read here and injected explicitly.
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
        **({} if args.defaults else PROD_SHAPE), **eval_off())
    shape = "CODE DEFAULTS" if args.defaults else "PROD SHAPE (pinned)"
    print(f"config: {shape}   (.env and NOUS_*/DB_* env NOT read)")

    print(f"qrels: {len(qrels)}   top_k={args.top_k}   db={args.db}\n")
    out = {}
    for name, flags in ARMS.items():
        out[name] = await run_arm(flags, qrels, base, args.top_k)
        print(f"  ran {name}")


    # The ONE population every number below is computed on: indices that
    # succeeded in EVERY arm. codex P1 — deriving the control's movement from a
    # baseline∩control intersection while reporting metrics over the all-arm
    # intersection lets the guard bless a null on a set the control was never
    # shown to move. The validity claim and the results must describe the same
    # queries or the guard is decorative.
    shared = [i for i in range(len(qrels))
              if all(r["rr"][i] is not None for r in out.values())]
    dropped = len(qrels) - len(shared)

    def paired(name, against="baseline"):
        """Paired deltas over `shared`. A query that raised in ANY arm is
        excluded everywhere, never zero-scored — see run_arm."""
        a, c = out[name]["rr"], out[against]["rr"]
        d = [a[i] - c[i] for i in shared]
        if not d:
            return (0.0, 0, 0, 0, dropped)
        return (statistics.mean(d), sum(1 for x in d if x > 1e-9),
                sum(1 for x in d if x < -1e-9), sum(1 for x in d if abs(x) <= 1e-9),
                dropped)

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
    # Same `shared` population the control was judged on (codex P2): per-arm
    # `ok` lists would let one arm's exception shrink only that arm's set, so
    # an asymmetric failure surfaces as an arm-specific metric change.
    print(f"\n--- per-arm metrics over {len(shared)} queries valid in ALL arms"
          + (f" ({dropped} dropped)" if dropped else "") + " ---")
    for name in ARMS:
        r = out[name]
        mrr = statistics.mean([r["rr"][i] for i in shared]) if shared else 0.0
        rec = (sum(1 for i in shared if r["hit"][i]) / len(shared)) if shared else 0.0
        srv = statistics.mean([r["served"][i] for i in shared]) if shared else 0.0
        print(f"{name:<20} MRR@{args.top_k}={mrr:.4f}  "
              f"recall@served={rec:.4f}  avg_served={srv:.1f}")

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
