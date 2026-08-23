# Spreading Activation — fix plan (rev 3)

**Supersedes** rev 1 and rev 2 of this file. Rev 1 led with a scoring fix; rev 2
led with an off-arm. Both were optimising inside the wrong frame. The frame is:
**F022 was built to be associative, it has never once run in that configuration,
and three independent things prevent it.**

**Sources:**
- `docs/reviews/2026-08-23-spreading-activation-review.md` — plumbing, scoring, dead code
- `docs/reviews/2026-08-23-spreading-activation-algorithm-validation.md` — traversal semantics
**Baseline:** `e2f60ee`.
**Measurements:** two sources, distinguished throughout — the frozen clone
`nous_prod_20260801` (simulation of the CTE, cosine relevance, edge census) and
**live prod** `nous_system.retrieval_log` via F091 (what actually shipped to the
model, 2026-08-20 → 08-23). Where they disagree, live prod wins.

---

## 1. What is actually wrong

Three cuts, on three different paths, each independently fatal to associativity:

| # | path | defect | measured |
|---|---|---|---|
| **R** | **read** | traversal excludes every associative edge | **0 of 2,607** walkable; the 1-hop leg it suppresses can walk all 2,607 |
| **W** | **write** | co-occurrence noise gate discards most of the corpus | `cooccurrence_max_episode_facts=6` skips 159/422 episodes holding **3,822 of 4,454 facts (85.8%)** |
| **S** | **score** | spreading rows are decayed twice and cannot rank | ceiling `seed × 0.35`; **2.86× below** the 1-hop leg it replaces, against a 0.72–0.83 cutline |

Net effect: 22.8% of active facts (1,420/6,226) have any associative edge at all,
and none of those edges is reachable by the mechanism built to traverse them.

### Confirmed on live production telemetry (F091, 2026-08-20 → 08-23)

Cut **S** is no longer a projection. Over 4 days of real prod retrieval
(71 recall_deep calls, 56 candidate-sampled):

- spreading fires on **71 of 71** calls — 13.8 rendered + 20.0 dropped per call
- **max score ever observed: 0.3354** — the predicted `seed × 0.35` ceiling, exact
- **0 of 770 rendered spreading rows has reached the top 10.** Best rank ever: 38.
  Average: 72.2. Path A, walking the same graph on the seed-score branch, took
  **231** top-10 slots over the same window.

**And a correction that changes the plan's disposition:** production redundancy is
**0.0%** — of 770 distinct rendered spreading rows, **not one was also delivered
by any other leg**. The simulation's ~50% figure measured against the query's
vector top-50; production measures against what the pipeline actually delivered,
which is the decision-relevant comparison. **Every spreading row is net-new.**
This shifts the prior for Phase E from "probably retire" toward "repair" — the
mechanism contributes items nothing else contributes; it simply cannot rank them.

**New finding — a filter-pushdown inefficiency (NOT the integrity bug an earlier
draft of this section claimed).** 251 of ~1,891 activated candidates (13%) drop
at `spreading_content_unresolved`. That is **policy working as designed**: 309
traversable endpoints are decisions with `outcome IN ('superseded','noise')`,
which `_resolve_node_descriptions` filters per the 2026-07-27 demotion decision.
They still consume slots in the CTE's `LIMIT 40` window and clear the 0.1 floor
before Python discards them, ~4.5 per call. **Phase A6** pushes the predicate into
the CTE. See "A6 corrected" for the measurements that falsify the dangling-edge
reading — a sweep would have deleted 3,929 `supersedes` lineage edges.

## 2. The experimental design question, decided up front

**R, W and S are each necessary and none is sufficient.** That has a hard
consequence for how this gets measured:

- Fix **R** alone → associative rows finally arrive, score 0.175, rank below
  everything. Measures ≈ null.
- Fix **S** alone → better-ranked *cosine* rows. Measures the wrong mechanism.
- Fix **W** alone → more associative edges that nothing traverses. Measures exactly null.

The repo's standing discipline is one variable per arm. **That rule is for
tuning — for choosing between configurations of a working mechanism.** Applied
here it would produce three nulls and the false conclusion that association
doesn't help. This is the "measures a null for the wrong reason" trap, and this
plan has now walked into it twice in draft.

**Design: an ablation ladder.** Ship R+W+S as one enablement bundle, measure the
bundle against baseline, then remove one component at a time to attribute. The
bundle answers *does it work*; the ablations answer *which part carried it*.
Every rung is still a single `Settings` override, so nothing about rollback or
flag discipline changes.

---

## Phase A — cleanup + baseline tests — ✅ SHIPPED

No ranking change; `recall_deep` byte-identical.

> **Status 2026-08-23.** All 8 items landed. 18 new tests in
> `tests/test_spreading_phase_a.py` (16 SQLite + 2 postgres-only), all 4
> mutations caught. Full suite **151 failed / 56 errors on branch AND on clean
> baseline** — identical, zero regressions; passes 5321 → 5339. The Postgres-only
> CTE tests were validated on a freshly-migrated `nous_phase_a` database, which
> matters because SQLite skips them and A6/A8 are both SQL changes.
>
> **Two corrections found while implementing, both worth recording:**
> - **A5 was misattributed.** The stale "retrieval consumers default off" claim
>   is in `spreading_activation.py`'s own `compute_graph_density` docstring, not
>   in CLAUDE.md — which contains no co-mention text at all. Fixed at the real
>   site, and the exclusion's justification rewritten to rest on the durable
>   argument (a *builder* flag must not decide whether auto-mode fires) rather
>   than on consumer state that has since changed.
> - **Three planned tests already existed.** `test_result_cap_truncates_at_20`,
>   the resolve-drops-everything fallback, and the `exclude_ids` pushdown are all
>   covered in `tests/test_retrieval_pipeline.py`. Only the floor, the
>   suppression invariant, the score, and hop depth were genuinely untested.
>   Writing the plan's list verbatim would have duplicated a third of it.
>
> Also of note: three pre-existing SQLite failures in
> `tests/test_spreading_activation.py` are caused by `AS seeds(id, node_type,
> score)` — SQLite does not support column-alias lists on a `VALUES` derived
> table. They fail identically on clean `main` and pass under Postgres. Not
> fixed here (out of scope), but they are why those tests give no SQLite signal.

| # | change | file |
|---|---|---|
| A1 | delete `spreading_activation_alpha` / `beta` / `gamma` (BR-26, dead since 2026-06-09) | `config.py:1183-1185` |
| A2 | drop unused `AUTOBEHAVIOR_EXCLUDED_RELATIONS` import | `spreading_activation.py:16` |
| A3 | correct the docstring claiming `recall_deep` calls `seed_for_spreading` | `residual_activation.py:28-30` |
| A4 | hoist the hardcoded `0.1` floor to `spreading_activation_floor` (default 0.1, exact no-op) — **both** the filter `:1616` and the F091 mirror `:1639` | `retrieval_pipeline.py` |
| A5 | fix the stale CLAUDE.md F076 row (says co-mention retrieval consumers "default off"; all three are `true` in prod) | `CLAUDE.md` |
| A6 | **push the decision-outcome policy filter into the CTE** — see "A6 corrected" below. Not a dangling-edge sweep. | `spreading_activation.py` |
| A7 | **bound `spreading_activation_max_depth` and `spreading_activation_decay`** — both are plain `int`/`float` with no `Field` constraints (`config.py:1181-1182`). `max_depth` is the exponent of an exponential traversal and `decay > 1.0` would *amplify* activation per hop, breaking the ≤1 bound that MAX aggregation relies on. Add `ge=1, le=3` and `gt=0.0, le=1.0`. | `config.py` |
| A8 | **make the CTE return the winning path's depth** so F091 records a real hop — see "A8" below. | `spreading_activation.py`, `retrieval_pipeline.py` |

### A6 corrected — the "13% dangling edges" claim was wrong

**I reported this as an integrity bug caused by migration 016 dropping the
`graph_edges` FKs. Measured on prod, that is false on every count, and the
cleanup I proposed would have been destructive.**

| claim | measured on prod |
|---|---|
| "edges point at rows that no longer exist" | **0 truly-missing rows in the entire graph** |
| "13% of activations hit dangling edges" | dangling edges are **18 of 58,941 traversable** (0.0%) |
| "6% of the graph dangles" | 3,964 do — of which **3,929 are `supersedes`** |

A `supersedes` edge is *supposed* to point at a deactivated fact — that is what
lineage is. My liveness predicate labelled it breakage. **A sweep would have
deleted 3,929 supersession-history edges.** Migration 016's missing FKs have not
produced a single orphaned reference here.

**What the 251 `spreading_content_unresolved` drops actually are:** policy, working
as designed. `_resolve_node_descriptions` filters decisions whose outcome is
demoted (`brain.py:1584-1614`, the 2026-07-27 `decision_outcome_score_factors`
decision — its own comment says *"Demoted outcomes are FILTERED here rather than
demoted. The asymmetry with `_query` is deliberate"*). Endpoint census of
traversable edges:

| resolver refusal reason | endpoints |
|---|---|
| **decision: outcome in (superseded, noise)** | **309** |
| fact: inactive (superseded/F027) | 3 |
| everything else (abandoned, empty content, inactive procedure) | 0 |

**The real inefficiency is a filter-pushdown one, not an integrity one.** 309
endpoints (2.1% of 14,533) can *never* surface, yet the traversal reaches them
every call — ~4.5 drops per call — and they consume slots in the CTE's `LIMIT 40`
window and pass the 0.1 floor before being discarded. Push the same
outcome predicate into the CTE's final `SELECT` (or fold it into `exclude_ids`)
so the window is spent on nodes that can actually be rendered.

Output-identical by construction: the rows being excluded are ones the Python
resolver already drops. Verify with a byte-identical `recall_deep` snapshot.

### A8 — make the CTE report real hop depth

The CTE already computes `depth` in the recursive term (`spreading_activation.py:146`)
and the final `SELECT` discards it, so `retrieval_pipeline.py:1681` hardcodes
`hop=2` for every spreading expansion. Consequence: **production telemetry cannot
separate depth-1 from depth-2**, which is precisely the split the whole depth
question turns on.

Return the depth of the **winning path** (the one whose activation survived MAX),
not `MIN(depth)` — those differ, and the winning path is what the score means.
Use `ROW_NUMBER() OVER (PARTITION BY id, node_type ORDER BY activation DESC,
depth ASC)`; **not `DISTINCT ON`**, which SQLite does not support and the suite's
default backend is SQLite (same constraint that forced `CASE` over `LEAST` at
`:149`).

Then widen the return tuple to `(id, node_type, activation, depth)` — two
production callers (`retrieval_pipeline.py:1605`, `scripts/diag/probe_forced_expansion.py:128`)
plus tests — and pass it to `tr.expansion(hop=...)`.

**Why this is worth doing before Phase E:** it turns the depth split into a
question answerable from data that is *already accumulating*, instead of an A/B
arm that has to be run. Observation beats intervention, and it may let Phase E's
`max_depth=1` arm be dropped entirely.

### Phase A tests, at the right layer

Floor/cap/fallback are pipeline concerns, not CTE concerns; only `exclude_ids`
belongs with the CTE:

- `test_floor_drops_below_threshold_nodes` *(pipeline)*
- `test_floor_default_is_prior_constant` *(pipeline)* — pins A4 as a no-op
- `test_result_cap_truncates_at_20` *(pipeline)*
- **`test_spreading_suppresses_one_hop_fallback`** *(pipeline)* — pins the
  either/or at `:1708`. Nothing asserts this today, which is why a leg being
  replaced by a 2.86×-weaker one was never a visible test failure.
- `test_spreading_row_scores_activation_times_graph_decay` *(pipeline)* — pins
  today's double-decayed score so Phase C fails loudly
- `test_exclude_ids_cut_before_limit` *(CTE)* — the PR #556 invariant
- `test_unresolved_content_falls_back_to_one_hop` *(pipeline, `:1688-1698`)*

**Verify:** full suite + CI green; snapshot unchanged.

---

## Phase B — the instrument (blocks C onward)

`grep -rln nous_prod_20260801` over `scripts/` and `reports/` returns nothing:
the 60-query harness behind decision `8f1cf413` left no artifact, and neither did
its query set or qrels.

**Deliverable:** `scripts/diag/spreading_ab.py` — thin, importing
`nous_eval.metrics`, not a new harness. Runs `run_recall_pipeline` against the
frozen clone with `rerank_by_score=True` (**state why**: `tools.py:1398-1401`
derives it from `NOUS_EPISODE_CHUNKS_ENABLED=true`, so that is prod's real mode).

**Reports:** recall@10, MRR, nDCG@10, **recall@served**, and **rendered tokens of
the spreading block** (~1k–4k/call, currently unmeasured).

**Acceptance = plumbing invariants**, not effect reproduction (rev 2's gate was
self-defeating — the N3 query set is gone, so failing to reproduce it would
permanently block the programme):
- equal-arm runs byte-identical
- recall@served conserved across a score-only rescale
- the published trial arithmetic reproduces (depth-2 survival counts, `seed × 0.35` ceiling)

**Two properties that must be printed in every report:**
1. **recall@served alongside recall@10** — the bundle changes set membership;
   only recall@served distinguishes "found something new" from "reordered".
2. **The oracle's blind spot.** Cosine cannot credit association — that is the
   whole point of association. **A cosine null is not a verdict on this bundle.**

**Qrel provenance must be written down this time.**

---

## Phase C — the enablement bundle (R + W + S)

Three flags, all default false, shipped together, measured together.

### C-R — un-block associative traversal

`autobehavior_exclusion_sql()` serves two call sites with different **purposes**:

| call site | purpose | exclusion correct? |
|---|---|---|
| `compute_graph_density` (`spreading_activation.py:42`) | decides *whether the mechanism runs* | **yes** — a builder flag must not silently flip auto-mode (`graph_constants.py:17-23`) |
| `spreading_activation_search` (`:133`) | *the traversal itself* | **no** — walking an edge is not being driven by it |

`graph_constants.py:25-28` already states co-occurrence edges "ARE legitimate
associative connectivity for retrieval". One shared helper erased the distinction.

**Change:** traversal takes `RETRIEVAL_EXCLUDED_RELATIONS` (`{supersedes,
contradicts}`), matching its sibling `brain._neighbors` (`brain.py:1366`).
**Density gate untouched — do not unify the predicates.**

**Flag:** `NOUS_SPREADING_TRAVERSE_ASSOCIATIVE_EDGES`.
**Measured upside** (14 trials, depth-1): cos **0.4974** vs 0.5324 for edges
walked today (equivalent relevance) at **30.9%** vs 48.2% vector-top-50
redundancy (markedly more novel). ~2.4 nodes/query at today's edge density —
which C-W addresses.

### C-W — stop the write path discarding 86% of the corpus

`cooccurrence_max_episode_facts = 6` skips any episode that produced more than 6
facts. Rationale (`config.py:2301-2307`): *"a focused conversation co-mentions a
few related things; a rambling one touches many unrelated topics."* Sound
intuition, calibrated against an episode-size distribution this corpus does not
have:

| | episodes | facts |
|---|---|---|
| within gate (≤6 facts) | 263 (62%) | **632 (14.2%)** |
| **skipped by gate (>6)** | **159 (38%)** | **3,822 (85.8%)** |

The gate discards the *substantive* episodes — a real working session produces
many facts. This is the direct cause of 22.8% associative coverage.

**Change:** replace the all-or-nothing episode skip with a **per-fact fan-out
bound**, mirroring what co-mention already does (`comention_max_edges_per_node`).
Keep the episode; bound how many partners each fact links to (nearest-in-episode
first). A rambling episode then contributes a few links per fact instead of being
dropped whole, and pair count stays linear rather than quadratic.

**Flag:** `NOUS_COOCCURRENCE_FANOUT_BOUND` (0 = today's episode-skip behaviour).
**Requires a backfill** to populate edges for the 3,822 previously-skipped facts —
smoke 1–2 batches before the full run, and measure edge precision on a sample
before trusting the yield.

### C-S — stop double-decaying spreading rows

`_f065_provenance_penalty` (`retrieval_pipeline.py:2387-2388`) applies
`graph_recall_decay` to a score the CTE already composed per hop. Not a defect —
the carve-out comment at `:2510-2514` names it — but a **retained, never-adjudicated
legacy behaviour**, and it is why a spreading row scores 2.86× below the same
neighbour reached by the 1-hop leg.

**Change:** return `activation` unmultiplied for `edge_relation ==
"spreading_activation"`. ×1.43.
**Flag:** `NOUS_SPREADING_SCORE_DOUBLE_DECAY_FIX`.

**Membership-invariant by construction** — floor, resolution drop and cap all act
on CTE activation *before* pipeline scoring, and nothing downstream drops by score
in prod config (F071 is id-based; recency resolver demotes facts only; MMR and CE
off at `.env:145,164`). So recall@served cannot move from C-S alone. **That is a
harness self-test, not a prediction.**

---

## Phase D — ablation attribution

Bundle vs baseline first. Then, from the bundle, remove one at a time:

| rung | configuration | isolates |
|---|---|---|
| D0 | baseline (today) | — |
| D1 | **bundle** (R+W+S) | does association work at all |
| D2 | bundle − R | how much came from un-blocking traversal |
| D3 | bundle − W | how much came from edge coverage |
| D4 | bundle − S | how much came from letting the rows rank |

D1 vs D0 is the decision. D2–D4 are attribution, and only worth running if D1
moves. If D1 is flat, read D2–D4 anyway before concluding — a flat bundle with a
large D2 gap means R helped and something else cancelled it.

---

## Phase E — the judgment (only after D)

Now that the mechanism has run in its intended configuration at least once, it is
fair to ask whether it earns its place. Per the validation, **this is two
mechanisms sharing one flag** and a single off-arm cannot attribute:

- **E1 — off-arm.** `spreading_activation_enabled=false` (`spread_force_off`
  already exists, `nous_eval/retrieval.py:307-315`). Off ≠ nothing: the 1-hop
  fallback engages, carrying the seed-score scoring live in prod.
- **E2 — `max_depth=1`.** Separates the two mechanisms: E1−E2 isolates depth-1
  second-order cosine; D0−E2 isolates depth-2 episode-context expansion.
- **E3 — redundancy control.** Baseline vs a larger heart `limit` at equal token
  cost. If raising `K` matches depth-1's contribution, depth-1 spreading is paying
  a graph traversal for what the vector index already had.

**Reading E2 is where the oracle caveat bites hardest.** Depth-2 is 100%
structural — episode-context expansion, shared provenance, 84% novel to vector
search. That is precisely what cosine cannot score. **Do not delete it on a
cosine measurement.** If E2 is the arm that matters, it needs an end-task judge.

If episode-context expansion survives, **promote it to a named leg with its own
limit** rather than leaving it an emergent side effect of a decay constant failing
to reach an inferred edge.

---

## Phase F — algorithmic refinements (only if E keeps the mechanism)

Each independently flagged. All address causes no floor or decay setting reaches.

- **F1 — per-seed MAX, then cross-seed noisy-OR** `1 - ∏(1 - aᵢ)`. Today's plain
  MAX scores a candidate corroborated by three seeds identically to one reached
  from one, and **34.9% of survivors are multi-seed** under production seed shape.
  Plain SUM is not the alternative (2.2–15.2 paths/node, mostly same-seed cycles —
  SUM would score cycles as evidence). Per-seed MAX kills cycle inflation;
  noisy-OR restores corroboration, stays in [0,1], and **degenerates to today's
  behaviour for the 65% single-seed majority**.
- **F2 — degree normalisation.** Every spreading-activation and PPR formulation
  divides outgoing activation by degree; this does not. An episode reached at
  depth 1 hands full undivided activation to up to 200 children — the chunk
  flood's cause, currently mopped up at the floor. **Composes with F1**: normalise
  per-seed, then combine.
- **F3 — per-relation propagation coefficients.** `weight` means cosine
  similarity on `inferred` edges (avg 0.28–0.41) and edge-existence certainty on
  `deterministic` ones (always 1.0). Multiplied in one product, "this chunk
  belongs to this episode" out-propagates "these facts are 0.9 similar" — which is
  why depth 2 is 100% deterministic. Damp structural edges to ~0.5–0.6.
- **F4 — relative floor.** `activation > floor_frac × max_seed_score`. Removes
  the property that a weak-seed query is silently starved. (A4 makes this a config
  arm.) Must also move the F091 mirror predicate.
- **F5 — decay 0.5 → 0.75.** Not a depth-2 knob: it raises *depth-1* 1.5× and
  composes with C-S — `0.83 × 1.0 × 0.75 ≈ 0.62`, approaching the cutline. State
  the combined ceiling before flipping both.
- **F6 — residual seeds (`seed_for_spreading`).** Wire or delete. Gated on **F4**,
  not on C-S: residual seeds enter at ≤0.3 (`residual_activation.py:180-182`) and
  need `w > 0.67` to clear today's floor against a 0.410 average — they die inside
  the CTE, which no score fix touches. `residual_activations` already reaches
  `run_recall_pipeline` (`tools.py:1422`); only the Stage 4 seed list lacks it.

---

## Sequencing

```
Phase A (cleanup + tests) ─────────────> ships now, independent

Phase B (harness) ──> Phase C (bundle R+W+S) ──> Phase D (ablation)
                                                      │
                                                Phase E (judgment)
                                                      │
                                          ┌───────────┴───────────┐
                                    retire / narrow          Phase F (refine)
```

**The load-bearing ordering is C before E.** Judging whether spreading activation
earns its place while it is barred from every associative edge, fed by a write
path that discards 86% of the corpus, and scored 2.86× below the leg it
suppresses, would answer a question nobody asked.

## Non-goals

- **CTE execution time.** 3.167 ms, correct `BitmapOr` over both endpoint indexes,
  max degree 248. A latency-framed fix optimises the one dimension already fine.
- **Unifying the two exclusion predicates.** The density-gate exclusion is
  load-bearing against silent auto-mode flips. Split, never merge.
- Per-hop fan-out cap in the CTE (distinct from C-W's write-side bound). Benign at
  the measured degree distribution; revisit on drift.

## Risks

| risk | mitigation |
|---|---|
| Bundle moves, attribution ambiguous | Phase D ablations, pre-planned |
| C-W adds noisy edges | Fan-out bound (not gate removal) + sample precision measurement before trusting yield; backfill smoked on 1–2 batches first |
| Cosine oracle convicts depth-2 | Stated in every report; E2 needs an end-task judge to return a verdict |
| Harness unvalidatable | Acceptance is plumbing invariants, so a missing N3 query set cannot block the programme |
| C-R raises density and trips auto-mode elsewhere | Density gate deliberately keeps its own exclusion — that is why the predicates stay split |
| Flags become permanent | Each deleted in the PR that unflags it, per #556 |

## Rollback

Phase A: revert. C/E/F: every arm is a `Settings` override defaulting to current
behaviour — rollback is config, not deploy. C-W's backfill is additive (new edges,
no mutation); rollback is a delete by `extraction_method='co_occurrence'` and
`created_at >`.
