# Removing the qrel blocker

**Date:** 2026-08-24 · **Baseline:** `cfaf135`
**Blocker:** every A/B-gated phase of the spreading programme waits on a qrel set
the repo does not have. `reports/_qrels_smoke.jsonl` is 12 rows with
`reviewed_by: null`; the N3 60-query set left no artifact; the graph-qrel miner
is now *satisfiable* (#605) but reportedly near-zero yield.

---

## 1. Restating the blocker precisely

Three claims are in play and they are **not** equally established:

| # | claim | status |
|---|---|---|
| A | The miner's criterion was unsatisfiable (`rerank_by_score` omitted) | **Verified**, fixed in #605 |
| B | The LLM generator writes vector-findable queries ~88% of the time, so the graph-only criterion has a ~12% ceiling for ANY edge family | **Unverified by me.** From decision `7b29cf7f` (parallel session, 60 bridges) |
| C | Therefore no usable qrel set can be mined | **Does not follow from B** — see §2 |

The plan attacks them in that order of cheapness, and stops as soon as one
resolves the blocker.

## 2. The assumption worth attacking first

**The graph-only criterion may not be needed at all.**

The miner keeps a qrel only when graph-off MISSES and graph-on HITS. That is a
strong contract, written so the `graph_targeted` source could not be contaminated
by rank-shuffle rows (codex P2, 2026-05-23). But a **paired A/B does not need
graph-only qrels.** It needs qrels on which arms *can* differ. If 88% of
generated queries are vector-findable, those are ties — they contribute no
signal, but they do not invalidate the comparison. The remaining tail carries it,
and the cost of ties is `n`, not correctness.

So claim C smuggles in a requirement the measurement does not have. **Test that
before solving generator bias**, because if a broad mine discriminates, the
blocker dissolves for the price of one flag.

## 3. Phases — each stops the plan if it succeeds

### P0 — Measure the generator's unconditional baseline *(≈60 Haiku + 60 embed calls)*

For N sampled edges, generate the query as the miner does, then ask ONE question:
**is the target in the query's vector top-50, with no graph involved?**

That is the number claim B rests on, and it is one arm. Decision `7b29cf7f`'s own
recorded lesson is that this arm dissolved two competing stories about *edge
families* — the generator's baseline hit-rate is a property of the generator, and
must be measured before any yield is attributed to a mechanism.

- **Reproduces ~88%** → the graph-only criterion is capped; go to P1 with that
  known, and do not spend anything further on tuning edge selection.
- **Does not reproduce** → the parallel session's population differed from the
  live one; re-run the miner as fixed in #605 and re-measure yield directly.

**Record the sampling frame.** `7b29cf7f` notes its own arms ran at `limit=10`
vs `limit=80` and were therefore not comparable; the vector-only figure is
limit-independent, which is why it is the arm to reproduce.

### P1 — Drop the criterion, keep the queries *(≈1 flag + one matrix run)*

Add `--no-reachability-gate` to `generate_graph_qrels`. Mine ~150 qrels with
query + gold and **no** graph-only precondition. Then run the existing arms —
`spread_force_off` / `spread_force_on` / `cs_baseline` / `cs_depth1_parity` — and
ask only:

> Do any two arms separate, on any metric, with a paired test?

- **Yes** → **blocker removed.** The qrel source is the miner minus its contract;
  ties cost `n`, which is cheap to buy.
- **No** → either the arms are genuinely null (a real answer, worth having) or
  the queries cannot discriminate. Distinguish with a **positive control**: an
  arm known to change retrieval grossly (`graph_recall_enabled=false`). If the
  control does not separate either, the QUERIES are the problem, not the arms —
  go to P2.

The positive control is the load-bearing part. Without it a null is
uninterpretable, which is the trap that produced two wrong verdicts in
`7b29cf7f`.

### P2 — Real queries from prod telemetry *(only if P1's control fails)*

F091 has been recording since 2026-08-20. Live now:

```
pipeline  97 retrievals   92 distinct queries   avg 62 chars
context  287 retrievals  202 distinct queries   avg 334 chars
```

These are **real questions the agent was actually asked** — no generator, so no
generator bias, and the distribution is the one that matters. Growing daily.

**The trap, stated up front.** Labelling gold by judging the *retrieved
candidates* would build a self-referential oracle: it can only ever confirm what
retrieval already found. That is exactly the failure recorded in `ac40336b` —
prod recall read 0.993 against an oracle defined by the retriever's own
similarity function, while the same pipeline on ground-truth answers read 0.539,
and *the gap between those numbers was where the entire improvement lived*.

So gold must be established **against the corpus**, not against the candidate
list: for each real query, search `heart.facts` / `episode_chunks` / `episodes` /
`brain.decisions` independently of what the pipeline returned. More expensive,
and the only version that can measure a miss.

### P3 — End-task gold *(fallback, highest cost)*

MAB-style: questions with known answers; gold is whatever row contains the
answer. Immune to both generator bias and self-reference. Already exists
externally. Only worth building in-repo if P0–P2 all fail.

## 4. What "blocker removed" means

A checked-in qrel file with written provenance, on which **a positive control
separates**. That last clause is the whole gate — a qrel set that cannot detect a
change known to be large cannot adjudicate a change believed to be small, and
shipping one would manufacture nulls for every phase that follows.

## 5. Non-goals

- Not re-tuning edge selection (`--min-weight`, `--allow-inferred`). P0 decides
  whether that lever exists at all; tuning it first is optimising inside an
  unverified premise.
- Not adjudicating C-R / C-S / the enablement bundle. This plan produces the
  instrument; it deliberately does not spend it.
- Not reviving the N3 query set. Gone, and P2 supersedes it with real traffic.

## 6. Risks

| risk | mitigation |
|---|---|
| P1 null is read as "the mechanism does nothing" | The positive control. A null without it is uninterpretable. |
| P2 labelling drifts self-referential under time pressure | Gold search is corpus-wide by construction; a candidate-only shortcut is the `ac40336b` failure and must fail review. |
| 92 queries is thin | It grows daily, and P2 only runs if P1's control fails. Report `n` with every result. |
| Reproducing 88% is taken as vindicating the miner | It is not — it caps the graph-ONLY criterion, which P1 abandons anyway. |
