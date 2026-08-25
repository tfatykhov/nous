"""Explain a qrel mine's yield by measuring BOTH halves of its gate separately.

`_validate_query` keeps a row only when graph-off MISSES top-K and graph-on
HITS top-K. A yield of zero therefore has two completely different causes, and
they call for opposite responses:

  * half 1 fails — the generator writes questions retrieval already answers.
    A property of the GENERATOR; no edge family can beat it. Workaround:
    `generate_graph_qrels --no-reachability-gate`.
  * half 2 fails — graph expansion cannot reach the target. A property of the
    GRAPH, i.e. the thing under test. NOT a harness problem, and emphatically
    not something to work around.

Attributing a yield to the wrong half has produced wrong verdicts twice
(decisions 7b29cf7f, 004641d7) — and a third time from this very script, see
below.

MEASURED 2026-08-24, `nous_prod_20260801`, PROD_SHAPE, n=56 from 60 edges:

    half 1  graph-OFF hits top-10 : 19/56 (33.9%)  => ceiling 66.1%
    half 2  graph-ON  hits top-10 : 18/56 (32.1%)
    KEPT (off miss AND on hit)    : 0/56  (0.0%)
    [diagnostic] raw vector top-50: 94.7%, median rank 2

The halves are equal to within the instrument's noise floor (~1 query at this
n — see arm_separation.py), and graph-ON is if anything the LOWER of the two.
Graph expansion did not move a single target INTO the top-10, on the most
favourable ground available: every target is the endpoint of the very edge its
query was written from. That is why the mine yields 0, and it is a measurement
about the graph, not a bug.

Robust across three configurations measured while fixing this probe — code
defaults (25/58 vs 25/58), prod shape with a keyword-only Brain (20/57 vs
20/57), and prod shape with the Brain wired correctly (19/56 vs 18/56). The
CEILING moves with the shape (56.9% → 64.9% → 66.1%); KEPT is 0 in every one.

CORRECTED THREE TIMES, all found by codex, none by me:
  1. The ceiling came from a raw vector top-50 union query — wrong by ~11x,
     because the gate runs `run_recall_pipeline` at top-10 and a target at raw
     rank 11-50 misses the gate yet still yields a qrel. (The 94.7% diagnostic
     was never wrong; it simply was not the gate.)
  2. Settings were then pinned so hard the probe measured code defaults, which
     the miner never runs — it builds from a bare `Settings()` reading prod .env.
  3. The probe gave `Brain` an embedding provider and the miner did not, so the
     miner was gating on keyword-only decision search while every sampled target
     IS a decision. Fixed in the MINER (it was the wrong one), not worked around
     here; both now build through `retrieval_runner`'s constructors.

    python scripts/diag/qrel_generator_baseline.py \
        --db nous_prod_20260801 --agent nous-default -n 60 --allow-inferred

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
from nous.api.retrieval_pipeline import run_recall_pipeline  # noqa: E402
from nous.storage.database import Database  # noqa: E402
from nous_eval.env_pin import (  # noqa: E402
    PROD_SHAPE,
    eval_off,
    pinned_settings,
)
from nous_eval.generate_graph_qrels import (  # noqa: E402
    fetch_edge_candidates,
    generate_query,
)
from nous_eval.retrieval_runner import (  # noqa: E402
    _build_brain_for_eval,
    _build_heart_for_eval,
    make_eval_embedding_provider,
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
    # Pinned: no .env, no NOUS_*/DB_* process env. A ceiling that moves with the
    # launch directory is not a ceiling. See nous_eval.env_pin.
    #
    # PROD_SHAPE is applied by default because this probe's whole claim is that
    # it reproduces the miner's gate — and the miner builds its settings from a
    # bare `Settings()`, which DOES read prod's .env (codex P1). Pinning to bare
    # code defaults would have measured a configuration the miner never runs,
    # and the same shape difference already moved a baseline MRR by 79%. Pass
    # --defaults to measure code-default behaviour deliberately instead.
    s = pinned_settings(
        db_host=args.host, db_port=args.port, db_name=args.db,
        db_user=args.user, db_password=os.environ["EVAL_DB_PASSWORD"],
        agent_id=args.agent,
        **({} if args.defaults else PROD_SHAPE),
        # CREDENTIALS are the deliberate exception to the pin — they are
        # inputs, not configuration, and hiding them just breaks the run.
        # `anthropic_auth_token` is NOT optional here: prod authenticates with
        # a Max-subscription OAT and leaves `ANTHROPIC_API_KEY` unset, so
        # passing only the api_key made every generate_query raise (observed
        # 2026-08-24 — 60/60 RuntimeError on the first pinned run).
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        anthropic_auth_token=os.environ.get("ANTHROPIC_AUTH_TOKEN", ""),
        **eval_off(),
    )
    # BOTH halves of `_validate_query`, reproduced exactly. Measuring only the
    # graph-off half attributes the whole yield to the generator, which is how
    # a 5.2% "ceiling" got published; the mine keeps a row only when graph-off
    # MISSES *and* graph-on HITS, so both must be observed to explain a yield.
    s_off = s.model_copy(update={"graph_recall_enabled": False})
    s_on = s.model_copy(update={"graph_recall_enabled": True})
    db = Database(settings=s)
    await db.connect()
    # Built through the harness's OWN constructors (codex P1). Hand-rolling
    # these is how the probe and the miner drifted apart in the first place —
    # the provider's model must match the corpus's vectors, and `Brain`
    # silently degrades to keyword-only decision search without one.
    emb = make_eval_embedding_provider(s)
    client = create_client(s)
    await client.start()
    try:
      async with _build_heart_for_eval(db, s) as heart:
        brain = _build_brain_for_eval(db, s, emb)
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

        generated = declined = gate_excluded = 0
        on_hits = kept = vec_found = 0
        vec_ranks: list[int] = []
        for i, c in enumerate(cands, 1):
            try:
                gen = await generate_query(c, client, args.model)
            except Exception as exc:
                # Print the MESSAGE, not just the class. A bare "RuntimeError"
                # 60 times says nothing about whether the corpus, the model or
                # the credentials are at fault — it cost a full run to find out.
                print(f"  [{i}] generator error: {type(exc).__name__}: "
                      f"{str(exc)[:120]}")
                continue
            if gen is None:
                declined += 1
                continue
            generated += 1

            # THE GATE, both halves.
            try:
                off_results, _ = await run_recall_pipeline(
                    gen[0], heart, brain, s_off, limit=args.top_k,
                    rerank_by_score=True)
                on_results, _ = await run_recall_pipeline(
                    gen[0], heart, brain, s_on, limit=args.top_k,
                    rerank_by_score=True)
            except Exception as exc:
                print(f"  [{i}] pipeline error: {type(exc).__name__}: "
                      f"{str(exc)[:120]}")
                generated -= 1
                continue
            tgt = str(c.target_id)
            off_hit = tgt in [str(r.id) for r in off_results[:args.top_k]]
            on_hit = tgt in [str(r.id) for r in on_results[:args.top_k]]
            if off_hit:
                gate_excluded += 1
            if on_hit:
                on_hits += 1
            if not off_hit and on_hit:
                kept += 1

            # Secondary diagnostic ONLY. Raw vector findability describes how
            # semantically easy the generator's questions are; it is NOT the
            # gate and no ceiling is derived from it.
            vec = await emb.embed(gen[0])
            async with db.session() as sess:
                rows = await sess.execute(text(_NODE_UNION),
                                          {"a": s.agent_id, "v": str(vec),
                                           "k": args.vector_k})
                vids = [str(r.id) for r in rows.all()]
            vrank = next((j + 1 for j, rid in enumerate(vids)
                          if rid == str(c.target_id)), None)
            if vrank is not None:
                vec_found += 1
                vec_ranks.append(vrank)
            if i % 10 == 0:
                print(f"  [{i}/{len(cands)}] generated={generated} "
                      f"off_hit={gate_excluded} on_hit={on_hits} kept={kept}")

        print("\n" + "=" * 68)
        print(f"queries generated            : {generated}  (declined {declined})")
        if not generated:
            print("no queries generated — nothing to report")
            return 2
        off_pct = 100.0 * gate_excluded / generated
        on_pct = 100.0 * on_hits / generated
        print(f"\nGATE, half 1 — graph-OFF finds target in top-{args.top_k}:")
        print(f"    {gate_excluded}/{generated} ({off_pct:.1f}%) excluded  "
              f"=> ceiling {100 - off_pct:.1f}%")
        print(f"GATE, half 2 — graph-ON finds target in top-{args.top_k}:")
        print(f"    {on_hits}/{generated} ({on_pct:.1f}%)")
        print(f"\nKEPT (off MISS and on HIT)   : {kept}/{generated} "
              f"({100.0 * kept / generated:.1f}%)")
        vpct = 100.0 * vec_found / generated
        print(f"\n[diagnostic, NOT the gate] target in raw vector top-"
              f"{args.vector_k}: {vec_found}/{generated} ({vpct:.1f}%)"
              + (f", median rank {sorted(vec_ranks)[len(vec_ranks) // 2]}"
                 if vec_ranks else ""))
        print("=" * 68)
        print("READ THE TWO HALVES SEPARATELY — that is the point of this probe.\n"
              "A low KEPT with a HIGH ceiling is not generator bias; it means\n"
              "graph expansion cannot reach the target, which is a finding about\n"
              "the GRAPH, not about the mine. Only a low ceiling indicts the\n"
              "generator, and then `--no-reachability-gate` is the workaround.\n\n"
              "Measure the ceiling through run_recall_pipeline at the validator's\n"
              "top_k, never a raw vector top-50: a target at raw rank 11-50 still\n"
              "misses the pipeline top-10 and still yields a qrel. That error\n"
              "understated the ceiling by ~11x here (codex P1).\n\n"
              "And match the MINER's config shape: it builds from a bare\n"
              "Settings() that reads prod's .env, so a probe pinned to bare code\n"
              "defaults measures a system the miner never runs.")
        return 0
    finally:
        await client.close()
        if emb is not None:
            await emb.close()
        await db.disconnect()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--db", required=True, help="corpus database name")
    p.add_argument("--agent", required=True)
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=5433)
    p.add_argument("--user", default="nous")
    p.add_argument("-n", type=int, default=60, help="edges to sample")
    # Defaults to the validator's own limit — the gate is measured at the same
    # top_k `generate_graph_qrels` validates at, or the number means nothing.
    p.add_argument("--top-k", type=int, default=10,
                   help="pipeline top-K for the gate (match the miner's --limit)")
    p.add_argument("--vector-k", type=int, default=50,
                   help="raw-vector K for the secondary diagnostic only")
    p.add_argument("--min-weight", type=float, default=0.7)
    p.add_argument("--allow-inferred", action="store_true",
                   help="Required on corpora whose decision-targeting edges are "
                        "all inferred. Circular for F065 penalty work; fine for "
                        "spreading arms, where the penalty is pinned at 1.0.")
    p.add_argument("--defaults", action="store_true",
                   help="Measure CODE-DEFAULT config instead of the prod shape "
                        "the miner actually runs. Results are not comparable "
                        "across the two.")
    p.add_argument("--model", default="claude-haiku-4-5-20251001")
    args = p.parse_args()
    if "EVAL_DB_PASSWORD" not in os.environ:
        print("set EVAL_DB_PASSWORD", file=sys.stderr)
        return 1
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
