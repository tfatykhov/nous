# The formatter reports what it emitted — rev 2

**Date:** 2026-08-24 · **Baseline:** `5c994fd`
**rev 2** after adversarial review. Rev 1 was framed as "wire recall@served and
the harness measures what matters". The review established that the metric is
**identically a no-op on gold**, and surfaced a live telemetry defect that is the
better justification for the same mechanism. Both are folded in below; every
correction is marked.

---

## 1. What is true (rev 1 got two of these wrong)

**F091 cannot supply recall@served.** `tr.finalize(results, …)` runs at
`retrieval_pipeline.py:804` *inside* the pipeline, before the formatter. The
harness (`retrieval_runner.py:567`) calls `run_recall_pipeline` directly, never
formats, never reads the F091 DB. **Stands.**

**CORRECTION 1 — rev 1 said `RENDERED` on the recall_deep path just means "the
pipeline returned it". False.** `tools.py:1506-1522` post-adjusts the trace after
finalize, calling `mark_not_delivered(…, "formatter_scope_filter")`. So the repo
*already* predicts section eligibility in a second place — the exact anti-pattern
rev 1 was written to avoid — and its own comment records that an earlier version
**inverted** the error.

**CORRECTION 2 — rev 1's magnitudes were shape-mix artifacts.** I fed the
formatter shapes the pipeline cannot produce under those scopes; the pipeline
gates its legs on the *same* `search_types` (`:856-873` heart, `:948-950` chunks,
`:1504` brain), and `:942-947` says that gating was deliberately aligned with the
formatter. Representative gap: **~8% under `["fact"]`** (spreading-produced
decision rows only), **0% under every scope the graph and hand-label qrels use**.
The 25%/75% figures are withdrawn.

## 2. The live defect this now leads with

`tools.py:1506-1522` only fires when `decision` is **absent** from `search_types`,
and only covers `source=="brain"` or `stage_origin=="brain_graph"`. So under
`search_types=["decision"]` a `heart_graph_memory` row is dropped by the
formatter and **still reads `rendered` in F091** — the trace claims content
reached the model that never did.

Reproduced:

```
scope=['fact']      (block runs: True)
  brain decision            emitted=False  marked=True   ok
  brain_graph decision      emitted=False  marked=True   ok
  heart_graph_memory fact   emitted=True   marked=False  ok
scope=['decision']  (block runs: False)
  heart_graph_memory fact   emitted=False  marked=False  *** FALSELY READS RENDERED ***
```

Incomplete in one direction, historically inverted in the other. That is what a
predicted re-derivation costs, and it is the case for making the formatter
report instead.

## 3. `r_at_served` is a tripwire, not a measurement

**CORRECTION 3, and the one that matters.** `memory_types` exists to route
retrieval to where gold lives, so **gold type ⊆ scope**; the formatter only drops
types *outside* the scoped sections; therefore **a gold row can never be in the
dropped set**. `r_at_served` ≡ recall over `retrieved_ids` on every well-formed
qrel — identically, by construction, not approximately.

In-repo scopes confirm it: graph qrels use the full 5-type surface
(`generate_graph_qrels.py:375`, 0% drop), hand-labels `["fact","decision"]` (0%),
LongMemEval `["episode","fact"]`. Under `["fact"]` the only droppable rows are
spreading-produced decisions, which are never gold.

So rev 1's "improves the denominator of every measurement" is **withdrawn**. The
value is a **conservation tripwire**: it catches future formatter/section changes,
mis-scoped qrels, and collector rot, and it retires the `metrics.py:49-55` debt
correctly rather than with a third re-derivation.

## 4. Change

**One idea: the formatter reports what it emitted. It is never predicted.**

### 4a — `_format_pipeline_text(..., emitted_out: list[tuple[UUID, str]] | None = None)`

Appended **at each point of emission**, not derived afterwards. `None` = no
collection, so the byte-identical `recall_deep` snapshot contract is untouched.
Matches the established side-channel idiom (`dropped_out` at
`retrieval_pipeline.py:910, 991`).

Emission sites that must all be wired — the review found three heart paths alone:
session-bucket loop (`:738-743`), "Other" (`:747-751`), flat (`:756-760`), plus
Graph-Connected Decisions (`:776-781`, **unconditional**), Brain (`:799-812`),
and exemplar (`:887`).

### 4b — F091 becomes the collector's consumer *(the fix)*

Delete the predictive block at `tools.py:1506-1522`. Mark not-delivered from the
set difference `results − emitted_out`. Eligibility then has exactly one
implementation, and §2's defect cannot recur in either direction.

### 4c — `QrelResult.served_ids` + `metrics.r_at_served`

**CORRECTION 4:** the runner does **not** call the formatter today
(`_run_one`, `:567-605`) — this plan *adds* that call. It must normalise
`memory_types or ["all"]`: `None` crashes (`"all" in None`), `[]` silently
narrows every gate.

`r_at_served` is **`None`** when uncollected — never `0.0`. **CORRECTION 5:**
exclude it from `compute_delta` / gate metric lists — `float(getattr(...))` at
`metrics.py:267-268` raises `TypeError` on `None`.

## 5. Acceptance

1. **Byte-identical text** with and without a collector.
2. **Conservation under `["all"]`:** `served_ids` == `retrieved_ids` as a
   **multiset** (**CORRECTION 6:** the same decision legitimately renders in
   *both* Graph-Connected and Brain sections, so a duplicate assert would fire on
   correct output). Parametrised over `session_group_heart` **both ways** — a
   collector wired only to the flat heart path passes otherwise.
3. **Divergence under `["decision"]`** on **synthetic fixtures, knowingly** — a
   real sparse-decision corpus produces no foreign-type rows, so this gate cannot
   run on live data.
   **CORRECTION 7:** rev 1's `r_at_served <= r_at_10` is wrong-direction and
   fails a *correct* implementation — served is the full ~77-row set, so gold at
   rank 15 gives `r_at_10=0, r_at_served=1`. Compare against full-list recall.
4. **The §2 defect is fixed:** `heart_graph_memory` under `["decision"]` reads
   not-delivered. Mutation: restoring the predictive block fails it.
5. **No silent zero:** absent collection ⇒ `r_at_served is None`.
6. Full suite identical to baseline.

## 6. Non-goals / known gaps

- Not solving the qrel-source problem. (Update 2026-08-24: the "generator bias,
  ~12% ceiling" framing turned out to be false — the real ceiling is 66.1% and
  the mine's 0-yield is caused by graph-ON hitting exactly the same rows as
  graph-OFF. See `2026-08-24-qrel-source.md` §7. That makes R@served *more*
  valuable, not less: F091 gold does not require graph expansion to work in
  order to exist.)
- **Parent episodes** (`tools.py:879-882`) are served content **not** in
  `results`. Flags OFF today, so gate 2 passes; collecting them would break it
  when F067 Phase 2 flips. **Scoped out of the collector, and pinned by a test
  asserting that choice** so it fails loudly rather than silently.
- The **exemplar section prints no `(id: …)`** (`:894-908`) — collection works
  from `r.id`, but any test verifying served-ness by regexing printed ids is
  silently wrong there.
- The aging pruner still soft-trims tool results >4000 chars on **later** turns
  (`compaction.py:894`), so formatter-exact == model-exact only on the turn the
  tool ran.
