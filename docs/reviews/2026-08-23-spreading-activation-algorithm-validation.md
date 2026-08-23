# Spreading Activation — algorithm validation

**Date:** 2026-08-23
**Companion to:** `docs/reviews/2026-08-23-spreading-activation-review.md` (which covers
plumbing, scoring and dead code). This document asks one question: **does the
traversal select the right candidates?**
**Method:** measurement on the prod clone `nous_prod_20260801`. A random fact acts
as the query proxy; its vector top-3 facts + top-5 decisions become seeds, exactly
matching production's seed shape; the production CTE then runs and every surviving
candidate is scored by cosine against the query proxy. No embedding API needed —
the clone's stored embeddings are the same `text-embedding-3-large` space.

---

## PRODUCTION TELEMETRY (F091, `nous_system.retrieval_log`, live prod DB)

Added after the simulation work. **This supersedes the simulated estimates where
they disagree** — it is 4 days of real prod retrieval (2026-08-20 → 08-23,
71 `pipeline` + 220 `context` retrievals, 56 pipeline rows candidate-sampled).

### It is selecting. It has never once ranked.

`stage4_spreading_activation` fired in **71 of 71** recall_deep calls — 1,206
expansions over 551 distinct neighbours. Per call: **13.8 rendered, 20.0 dropped,
40.7% survival**.

Every leg's rendered rows, by score and final rank:

| leg | rendered | avg score | **max score** | avg final rank | **best rank ever** | **in top-10** |
|---|---|---|---|---|---|---|
| heart_primary | 774 | 0.845 | 1.000 | 29.8 | 1 | **286** |
| brain | 391 | 0.795 | 1.000 | 25.9 | 5 | 15 |
| heart_graph_memory (Path A) | 729 | 0.725 | 1.000 | 28.0 | 1 | **231** |
| chunk | 1120 | 0.683 | 0.969 | 34.1 | 3 | 15 |
| heart_graph | 41 | 0.633 | 0.779 | 37.1 | 16 | 0 |
| keyed | 205 | 0.534 | 0.550 | 51.4 | 33 | 0 |
| keyed_r2 | 330 | 0.489 | 0.505 | 58.9 | 35 | 0 |
| **graph_expanded (= spreading survivors)** | **770** | **0.163** | **0.335** | **72.2** | **38** | **0** |

Three things are now facts rather than projections:

1. **Max score ever observed: 0.3354.** The predicted ceiling was `seed × 0.35`.
   Confirmed to three decimals on live data.
2. **Zero of 770 rendered spreading rows has reached the top 10 in four days.**
   Best rank ever achieved is **38**; the average is 72. They land at the bottom
   of a ~100-item list. Meanwhile Path A — the same graph, scored on the
   seed-score branch — took 231 top-10 slots.
3. The mechanism **selects** fine and **ranks** not at all. Cut S is not a
   theoretical concern; it is the whole observed behaviour.

### Correction to §1: in production, spreading is NOT redundant

The simulation measured redundancy against *the query's vector top-50* and found
~50%. Production measures the decision-relevant thing — **what the other legs
actually delivered in the same retrieval** — and finds:

> **770 distinct rendered spreading rows. 0 of them were also delivered by any
> other leg. 0.0% redundant.**

Both numbers are true and they answer different questions. The production one
matters: **every spreading row is an item nothing else in the pipeline
contributes.** This materially strengthens the case for repairing the mechanism
rather than retiring it, and it partially reverses the lean of §1.

### Where the other 59% goes

| drop stage | n | what it is |
|---|---|---|
| `spreading_result_cap` | 437 | the `_SPREADING_RESULT_CAP=20` truncation |
| `spreading_activation_floor` | 428 | the `> 0.1` activation floor |
| **`spreading_content_unresolved`** | **251** | **activated node points at a row that no longer resolves** |

**RETRACTED — I first called the 251 a dangling-edge integrity bug from migration
016's dropped FKs. Measured on prod, that is false on every count:**

| my claim | measured |
|---|---|
| edges point at rows that no longer exist | **0 truly-missing rows in the whole graph** |
| 13% of activations hit dangling edges | dangling edges are **18 of 58,941 traversable** (0.0%) |
| ~6% of the graph dangles | 3,964 do — but **3,929 are `supersedes`**, which is *supposed* to point at a deactivated fact. That is lineage. The sweep I proposed would have deleted 3,929 supersession-history edges. |

**What the 251 actually are: policy, working as designed.**
`_resolve_node_descriptions` filters decisions with a demoted outcome
(`brain.py:1584-1614` — the 2026-07-27 `decision_outcome_score_factors` decision,
whose own comment reads *"Demoted outcomes are FILTERED here rather than demoted.
The asymmetry with `_query` is deliberate"*). Endpoint census over traversable
edges:

| resolver refusal reason | endpoints |
|---|---|
| **decision: outcome in (superseded, noise)** | **309** |
| fact: inactive (superseded / F027) | 3 |
| abandoned decisions, empty content, inactive procedures | 0 |

So the traversal keeps reaching 309 decisions (2.1% of 14,533 traversable
endpoints) that policy will always refuse — ~4.5 drops per call. It is a
**filter-pushdown inefficiency**, not breakage: those nodes consume slots in the
CTE's `LIMIT 40` window and clear the 0.1 floor before Python discards them.
Pushing the outcome predicate into the CTE is output-identical and frees the
window for nodes that can actually render.

*(Checked and cleared: `keyed`/`keyed_r2` rendering rows is not a land-dark leak —
`NOUS_KEYED_FACT_LEG_ENABLED=true` and `NOUS_EXEMPLAR_MODE_ENABLED=true` are set
in both env files.)*

**One telemetry limitation to record:** F091 stamps `hop=2` on every spreading
expansion unconditionally (`retrieval_pipeline.py:1681` — a multi-hop CTE has no
single seed to attribute), so the production data **cannot** separate depth-1
from depth-2. The depth split in §1–§2 remains simulation-only, and Phase E's
`max_depth=1` arm is still the only way to measure it.

---

## Verdict

**Depth 1: yes, it selects relevant candidates — and half of them redundantly.**
**Depth 2: it selects a real thing, but not the thing the name implies.**

| bucket | trials | nodes | avg cos to query | already in query's vector top-50 |
|---|---|---|---|---|
| random baseline | 20 | 4,000 | 0.213 | — |
| **spreading depth-1** | 18 | 119 | **0.498** | **49.9%** |
| **spreading depth-2** | 7 | 103 | **0.392** | **16.6%** |
| vector top-10 (reference) | 20 | 200 | 0.646 | 100% |

Normalising between random (0.213) and a direct vector hit (0.646):
depth-1 lands **66%** of the way, depth-2 **41%**.

So neither depth is noise. Depth-2 at 0.392 is still ~1.8× the random baseline —
**I am not going to call it drift**, and the report below explains what it
actually is.

---

## 0. The headline: spreading is FORBIDDEN from walking the associative edges

The design intent of F022 was associative recall. The traversal cannot do it —
not because the associative edges are missing, but because they are **explicitly
excluded**.

Every edge on the clone, classified by how it was derived, against which leg is
permitted to traverse it:

| edge kind | edges | % of graph | **spreading may walk** | 1-hop (`_neighbors`) may walk |
|---|---|---|---|---|
| **cosine** (`inferred`; weight = similarity) | 27,832 | 61.0% | 27,825 | 27,825 |
| **structural** (`deterministic`; weight = 1.0) | 15,163 | 33.2% | 12,538 | 12,866 |
| **associative** (`co_mention` / `co_occurrence`) | 2,607 | 5.7% | **0** | **2,607** |
| other | 33 | 0.1% | 33 | 33 |

**Spreading activation can walk 0 of the 2,607 associative edges. The 1-hop leg
it suppresses can walk all of them.**

So the live production configuration:

1. **Builds** associative edges — `NOUS_COOCCURRENCE_LINKING_ENABLED=TRUE` in
   both env files.
2. **Runs the one leg forbidden to traverse them** — `spreading_activation_search`
   applies `autobehavior_exclusion_sql()` (`spreading_activation.py:133`), which
   drops `co_occurred` by relation and `co_mention` by extraction method.
3. **Which switches off the leg that is permitted** — Stage 4 is either/or
   (`retrieval_pipeline.py:1708`), and `brain._neighbors` filters only
   `RETRIEVAL_EXCLUDED_RELATIONS = {supersedes, contradicts}` (`brain.py:1366`).

### Why the exclusion exists, and why it is right in one place and wrong in the other

`autobehavior_exclusion_sql()` has **two call sites with different purposes**:

- `compute_graph_density` (`spreading_activation.py:42`) — **correct**. The
  documented rationale (`graph_constants.py:17-23`) is that auto-behaviour must
  not be *driven* by edges "whose builder flags can flip silently and change
  behaviour... Counting these can flip auto-spreading on." A builder toggle
  should not silently push an agent over the density threshold. Sound.
- `spreading_activation_search` (`:133`) — **wrong**. This is retrieval
  traversal, not an auto-behaviour trigger. The gate rationale does not transfer:
  walking an edge is not being driven by it.

The codebase already says so. `graph_constants.py:25-28`:

> `co_occurred` / `co_mention` **ARE legitimate associative connectivity for
> retrieval**, so they are NOT excluded there — changing that is a live ranking
> decision (deferred, eval-gated).

The predicate written for the density gate was applied to the traversal as well,
and one shared helper hid the distinction.

### What the blocked edges would actually contribute

Depth-1 expansion from production-shaped seeds, 14 trials, split by whether the
edge is currently traversable:

| leg | nodes | avg cos to query | already in query's vector top-50 |
|---|---|---|---|
| cosine + structural (walked today) | 132 | 0.5324 | 48.2% |
| **associative (blocked today)** | **34** | **0.4974** | **30.9%** |

**Equivalent relevance, substantially less redundancy.** 69% of associative hits
are outside the query's vector top-50, against 52% for the edges spreading
actually walks. That is precisely the profile an associative leg should have:
as relevant, but reaching material similarity does not.

Volume is modest — ~2.4 nodes per query at depth 1 — because the associative
population is only 5.7% of the graph. But it is the *highest-quality* depth, and
it is 100% blocked.

**Fix:** split the predicate. Keep `autobehavior_exclusion_sql()` on the density
gate; give the traversal `RETRIEVAL_EXCLUDED_RELATIONS`, matching its sibling
leg. This is the single change that makes spreading activation associative rather
than a second-order cosine walk.

*(Stale doc note: CLAUDE.md's F076 row says the co-mention builder's "retrieval
consumers (Path A / adjacency / seed-score) default off". All three are ON in
prod — `NOUS_HEART_GRAPH_ALL_TYPES_ENABLED`, `NOUS_GRAPH_ADJACENCY_BOOST_ENABLED`,
`NOUS_GRAPH_NEIGHBOR_SEED_SCORE_ENABLED` are all `true`. Path A is today the only
live consumer of associative edges.)*

---

## 1. Depth 1 is the cosine graph, walked slowly

Every surviving candidate, broken down by the relation that carried its hop
(8 coherent-seed trials):

| depth | relation | method | arrives as | n |
|---|---|---|---|---|
| 1 | `evidence_for` | inferred | fact | 69 |
| 1 | `related_to` | inferred | fact | 68 |
| 1 | `related_to` | inferred | decision | 13 |
| 1 | `evidence_for` | inferred | decision | 10 |
| 1 | `extracted_from` | deterministic | episode | 7 |
| 1 | `summarized_by` | inferred | chunk | 4 |
| 1 | `discussed_in` | inferred | episode | 2 |

**160 of 173 depth-1 arrivals (92%) come through an `inferred` edge.** And
`inferred` edge weight *is* cosine similarity — `graph_linker.py:203,297` build
these as `1 - (embedding <=> vec) >= threshold` and store
`weight=float(row.similarity)` (`:329, :241`).

So depth-1 spreading is: *find things cosine-similar to things cosine-similar to
the query.* Second-order similarity. That is why **49.9% of its output is already
in the query's own vector top-50** — half of it could be had by raising `K`.

This is not a defect; the measured relevance is real. But it bounds what the
mechanism can be worth at depth 1: roughly, the value of the ~50% that
second-order similarity reaches and first-order does not.

## 2. Depth 2 is not association — it is "the rest of that conversation"

| depth | relation | method | arrives as | n |
|---|---|---|---|---|
| 2 | `extracted_from` | deterministic | fact | 35 |
| 2 | `part_of` | deterministic | chunk | 23 |
| 2 | `extracted_from` | deterministic | episode | 3 |

**61 of 61 depth-2 arrivals — 100% — come through a `deterministic` structural
edge. Zero inferred.** Exactly as the arithmetic predicts: at seed 0.5 with
decay 0.5, depth 2 needs `w₁ × w₂ > 0.8`, and only weight-1.0 edges qualify.

The path is fully explicit:

```
fact --extracted_from--> episode --extracted_from (BACKWARDS)--> every other fact
                                 --part_of (BACKWARDS)--------> every chunk
```

`extracted_from` and `part_of` are **directional** (fact→episode, chunk→episode).
The CTE joins undirected — `(e.source_id = a.id OR e.target_id = a.id)`
(`spreading_activation.py:155`) — so from the episode it walks them backwards and
returns everything that episode contains.

Depth 2 is therefore **episode-context expansion**: "here are the other facts and
chunks from the same conversation." That explains all three earlier observations
at once — why it is 63% chunk/episode, why it is bimodal (fires only when a seed
happens to have an episode anchor), and why it is 84% novel to vector search
(shared provenance is genuinely not a cosine relation).

**This is a legitimate retrieval behaviour that vector search cannot do.** It is
just not the behaviour the mechanism's name, config, or docs describe, and it
arrives by accident of undirected traversal rather than by design. If episode-
context expansion is wanted, it should be a named leg with its own limit — not an
emergent side effect of a decay constant failing to reach an inferred edge.

## 3. MAX discards real convergence — and my first measurement said otherwise

Classic spreading activation sums activation across converging paths;
corroboration by several independent seeds is the discriminative signal. Plan 1.2
switched `SUM` → `MAX` to stop unbounded scores dominating the merge
(`spreading_activation.py:164`, decision `97ec2098`).

I measured whether that discarded anything, with **random** seeds first:

> 12 of 227 surviving nodes multi-seed = **5.3%** — convergence is rare, MAX is fine.

That was wrong, and the error was the sampling frame. Production seeds are the
top-K for *one* query, so they are semantically clustered and land in overlapping
neighbourhoods. Random seeds are unrelated and cannot converge by construction —
the measurement had assumed its own conclusion. Re-run with production-shaped
coherent seeds (vector top-3 facts + top-5 decisions from one query proxy):

| | random seeds | **coherent (production-shaped) seeds** |
|---|---|---|
| multi-seed share of survivors | 5.3% (12/227) | **34.9% (80/229)** |
| per-trial range | 0–15% | 0–100%, median ~40% |
| mean `SUM/MAX` ratio | ~1.5 | ~1.9 |

**About a third of surviving candidates are reached from more than one seed, and
MAX scores them identically to a candidate reached from one.** The corroboration
signal is there and is being thrown away.

But plain `SUM` is not the fix either: average path count per node is 2.2–15.2,
and with random seeds those multiple paths were almost entirely *same-seed*
routes — cycles in an undirected graph. `SUM` would score cycle count as if it
were evidence.

**The correct form separates the two:** take `MAX` **per seed** (best path from
each origin — kills cycle inflation), then combine **across seeds** with noisy-OR,
`1 - ∏(1 - aᵢ)`. That is bounded in [0,1], monotonically increasing in
corroboration, and degenerates to exactly today's MAX when only one seed reaches
the node — so the change is inert for the 65% single-seed majority and only
affects the 35% that carry real convergent evidence. The CTE already tracks
`origin` trivially (I added it as one column for this measurement).

## 4. No degree normalisation — which is why the chunk flood exists

Every spreading-activation formulation and every personalised-PageRank variant
divides a node's outgoing activation by its degree: a hub spreads thinly. This
CTE passes the **full** `a × w × decay` to every neighbour regardless of how many
there are.

With `NOUS_EPISODE_CHUNK_MAX_PER_EPISODE=100`, an episode can hold 100 chunks and
100 extracted facts. Reached at depth 1, it hands **full undivided activation to
all 200** at depth 2. That is not a hypothetical: §2 shows depth 2 is 100%
episode-mediated, and max observed node degree on the clone is 248.

Degree normalisation would fix the flood at its cause rather than at the floor.
Note it interacts with §3 — normalise per-seed activation, then combine.

## 5. Edge weight conflates two incommensurable quantities

| relation | what `weight` means | observed |
|---|---|---|
| `related_to` / `evidence_for` / `summarized_by` (inferred) | **cosine similarity** — strength of association | avg 0.410 / 0.279 / 0.341 |
| `part_of` / `extracted_from` / `discussed_in` (deterministic) | **certainty that the edge exists** — always 1.0 | 1.000 |

The CTE multiplies them in one product. So "this chunk belongs to this episode"
(certainty 1.0) propagates activation *more strongly* than "these two facts are
0.9 cosine-similar" — even though as an **association** the second is clearly the
stronger claim. Structural edges systematically out-propagate semantic ones.

This is the root cause of §2's 100%-deterministic depth 2, and it is upstream of
the floor: no floor setting fixes a traversal that prefers provenance to meaning.
A per-relation propagation coefficient (structural edges damped to ~0.5–0.6, since
provenance certainty ≠ association strength) would decouple the two.

## 6. The relevance-combination step was never built

Activation is a function of seed score and edge weights only. **A candidate's own
similarity to the query is never consulted after seeding.** Semantic drift is
therefore unbounded by construction — nothing pulls a wandering path back toward
the question.

F022's original design had exactly this term:
`α·vector_score + β·graph_activation + γ·recency`
(`docs/plans/2026-03-08-graph-augmented-recall.md:1387-1389`). It was never
implemented, and `spreading_activation_alpha/beta/gamma` have sat in `config.py`
consuming nothing since — flagged dead as BR-26 on 2026-06-09.

So the companion review's cheapest deletion and this document's deepest
algorithmic gap are the same three lines. Worth deciding deliberately: **delete
them as a failed design, or build the term they were for.** The measured drift
(0.646 → 0.498 → 0.392) is the thing β·activation + α·vector was meant to arrest.

---

## What this changes about the remediation plan

The companion plan's Phase 2 asks "should this mechanism exist?" via an off-arm.
This document says the question is malformed, because **the mechanism is two
mechanisms** with different value propositions, sharing one flag:

- **depth 1** — second-order cosine. Relevant (0.498), ~50% redundant with a
  larger `K`. Competes directly with the 1-hop leg it suppresses.
- **depth 2** — episode-context expansion. 100% structural, 84% novel, weaker
  relevance (0.392). Competes with nothing; nothing else in the pipeline does it.

An off-arm measures their *sum* and cannot tell you which one carried the result.
`max_depth=1` (already planned as a control) separates them and should be read as
a **first-class arm, not a control** — it is the only measurement that isolates
episode-context expansion.

Add to the plan, in priority order:

1. **Un-block the associative edges** (§0) — split `autobehavior_exclusion_sql`
   so the density gate keeps its exclusion and the traversal uses
   `RETRIEVAL_EXCLUDED_RELATIONS`. This is the difference between spreading
   activation being associative and being a second-order cosine walk. **Do this
   before any arm that judges whether the mechanism is worth keeping** — otherwise
   the off-arm passes verdict on a mechanism that was never allowed to do the
   thing it exists for.
2. **Read `max_depth=1` as the arm that decides depth-2's fate**, not as a control.
3. **Per-seed MAX + cross-seed noisy-OR** (§3) — restores a signal present in 35%
   of candidates, provably inert for the other 65%.
4. **Degree normalisation** (§4) — fixes the chunk flood at its cause.
5. **Per-relation propagation coefficients** (§5) — stop provenance out-propagating
   meaning.
6. **Decide alpha/beta/gamma deliberately** (§6) — delete, or build the term.

2–5 are all ranking changes and all need the Phase 0 harness. But note the
oracle caveat cuts *hardest* here: every measurement in this document is cosine,
and §2's depth-2 finding is precisely a mechanism that cosine is built to
undervalue. **Do not use a cosine oracle to kill episode-context expansion.**
That needs an end-task judge.
