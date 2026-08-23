# Spreading Activation — mechanism review

**Date:** 2026-08-23
**Scope:** `nous/brain/spreading_activation.py`, its Stage-4 call site in
`nous/api/retrieval_pipeline.py`, the scoring path in
`_graph_expanded_to_pipeline`, and the F055 seed wire.
**Method:** code read at `e2f60ee` + measurement against the prod clone
`nous_prod_20260801` (agent `nous-default`, 45,635 edges / 40,396 traversable).
**Status of the mechanism:** LIVE in prod.

---

## 0. Is it even on?

Yes. `spreading_activation_enabled` defaults to `"auto"` and neither `.env` nor
`.env.prod-snapshot` overrides it. Auto compares graph density against
`spreading_activation_density_threshold = 3.0`.

Measured traversable density on the prod clone: **3.776** (40,396 edges /
10,697 nodes, after the `autobehavior_exclusion_sql` cut). Above the threshold,
so every `recall_deep` call with a decision hit *or* a fact hit takes the
spreading branch, and the 1-hop fallback is skipped.

Live config that matters (all defaults, none overridden):

| setting | value |
|---|---|
| `spreading_activation_decay` | 0.5 |
| `spreading_activation_max_depth` | 2 |
| `graph_recall_decay` | 0.7 |
| activation floor (hardcoded, `retrieval_pipeline.py:1616`) | `> 0.1` |
| `_SPREADING_OVERFETCH_LIMIT` / `_SPREADING_RESULT_CAP` | 40 / 20 |
| `NOUS_GRAPH_NEIGHBOR_SEED_SCORE_ENABLED` | **true** (set in both env files) |

---

## 1. What the SQL costs — nothing. Don't optimise it.

`EXPLAIN (ANALYZE, BUFFERS)` on the exact production CTE with 8 seeds:

```
Execution Time: 3.167 ms
->  Bitmap Heap Scan on graph_edges e
      Recheck Cond: ((source_id = a.id) OR (target_id = a.id))
      ->  BitmapOr
            ->  Bitmap Index Scan on idx_graph_edges_source_type
            ->  Bitmap Index Scan on idx_graph_edges_target_type
```

The undirected `OR` join resolves to a `BitmapOr` across both endpoint indexes.
Degree distribution is benign: avg 7.55, p90 15, p99 55, max 248, zero nodes
above 500.

**I went looking for a query-plan problem and there isn't one.** Any proposal
framed as "make the spreading query faster" is aimed at 3 ms. The real cost is
result slots and discarded work, which is where the rest of this goes.

---

## 2. F2 (headline) — spreading rows are decayed twice and cannot rank

`spreading_activation_search` already composes the decay per hop inside the CTE:
`activation = seed × ∏(weight × 0.5)`.

Then `_graph_expanded_to_pipeline` (`retrieval_pipeline.py:2515`) scores the row
with `_score_memory_neighbor`. Spreading rows carry `seed_score = None` (the
`NeighborResult` at `:1662` never sets it), so the `graph_neighbor_seed_score`
branch is skipped even though **that flag is ON in prod**, and it falls through
to:

```python
_f065_provenance_penalty(n, n.edge_weight, settings.graph_recall_decay, settings)
#   → edge_relation == "spreading_activation" → return base_score * decay
#   → activation * 0.7
```

So the pipeline score is `seed × ∏(weight × 0.5) × 0.7` — the CTE's decay, and
then `graph_recall_decay` on top of it.

Measured ceiling over 10 random-seed trials (5 decisions + 3 facts at seed 0.5,
production LIMIT 40 and floor applied):

```
max pipeline score:  0.128 0.131 0.135 0.138 0.150 0.152 0.175 0.175 0.175 0.175
```

**The best spreading row in any trial scored 0.175.** The documented direct-hit
top-k cutline is 0.72–0.83 (`config.py:2218`, the rationale for the
`graph_neighbor_seed_score` flag). A spreading row is roughly 4× below the worst
direct hit and cannot displace one under `rerank_by_score`; under the default
`rerank_by_score=False` it is tail-appended and only survives because
`recall_deep` renders the whole list rather than a top-k slice.

**Correction — I first called this an accident; it is not.** The carve-out
comment at `retrieval_pipeline.py:2510-2514` explicitly says spreading rows
"fall through to the **legacy activation×decay**". It *names* the extra
multiplication. The plan-1.2 author saw it and kept it. The accurate label is a
**documented, retained, never-adjudicated legacy carve-out** — not a defect.

Nor does the code support reading the 0.7 as a deliberate precision guard:
`config.py:2213-2228` treats exactly this sub-cutline ceiling as *the problem*
the seed-score flag exists to fix, and `_f065_provenance_penalty`'s docstring
frames the spreading short-circuit as preventing double *penalty*, saying
nothing about wanting the decay.

**Two corrections to the numbers, same source:**
- **Prod runs `rerank_by_score=True`**, not the default `False`
  (`tools.py:1398-1401` sets `chunks_rerank` from `episode_chunks_enabled=true`
  in both env files, passed at `:1423`). So results *are* score-sorted; spreading
  rows sink to the bottom of the rendered list rather than sitting wherever
  stage-order left them.
- **The 0.175 ceiling is trial-specific**, not a property of the mechanism. The
  trials pinned every seed at 0.5; real seeds are `d.score or 0.5` and
  coherent-RRF fact scores up to ~1.0, so the mechanism ceiling at depth 1 is
  `seed × 0.35`. The conclusion survives — a strong seed still tops out ~0.35
  against a 0.72–0.83 cutline — but the specific number is a trial artefact.

**Fix:** stop applying `graph_recall_decay` to a row whose relation is
`spreading_activation`. One line, ×1.43 on every spreading row. It is a ranking
change and needs the A/B — but it is **membership-invariant by construction**
(see §2b), so the realistic outcome is "tail rows reorder", not "graph rows
flood top-k".

---

## 2b. F2b (sharpest finding) — spreading SUPPRESSES a leg that scores 2.86× higher

This follows from §2 but neither I nor the first draft of the plan drew it. It
came out of the adversarial review's "why is there no off-arm?" question.

Stage 4 is an either/or: `if use_spreading: … ` and then
`if not use_spreading: # Fall back to 1-hop` (`retrieval_pipeline.py:1708`).
Spreading does not *augment* the 1-hop expansion — it **replaces** it, and since
prod density (3.776) sits above the auto threshold, spreading wins every time.

But the 1-hop leg scores its rows on a completely different scale. It threads
`n.seed_score = dec.score` (`:1759`), so `_score_memory_neighbor` takes the
seed-score branch that is **ON in prod**:

| leg | score for the same (seed, edge, neighbour) |
|---|---|
| 1-hop fallback | `seed × edge_weight × penalty` = `seed × w` (penalty 1.0 in prod) |
| spreading | `seed × w × 0.5 × 0.7` = `seed × w × 0.35` |

**Ratio 1 / 0.35 = 2.86×.** For an identical seed, identical edge, and identical
neighbour, the leg that is switched OFF would have scored the row 2.86× higher
than the leg that is switched ON.

So the live configuration takes graph rows that could plausibly reach the
0.72–0.83 cutline (`seed × w` with a strong seed and a weight-1.0 edge reaches
~0.8) and replaces them with rows capped at `seed × 0.35`. What spreading buys
in exchange is depth 2 — which, per §3, returns nothing in 7 of 10 queries.

Two honest counterweights before anyone reaches for the off switch:
- The 1-hop fallback is **decision-only** (`for dec in decision_results`), so on
  a fact-only query it produces nothing while spreading produces something.
  Partly redundant, though: Path A (Stage 2b) is live in prod
  (`NOUS_HEART_GRAPH_ALL_TYPES_ENABLED=true`) and already expands fact seeds.
- 1-hop is capped at `graph_recall_max_neighbors=3` per decision × 5 decisions =
  15 rows, against spreading's 20.

**This makes the off-arm (`spreading_activation_enabled=false`) the first
experiment that should run, ahead of any scoring or tuning change.**

---

## 3. F1 — depth 2 is 85% of the work and fires on 3 queries in 10

Ten random-seed trials, production CTE, counting nodes that clear the floor:

| trial | passes floor | depth 1 | depth 2 | of which chunk/episode |
|---|---|---|---|---|
| 1 | 12 | 12 | 0 | 0 |
| 2 | 40 | 26 | 14 | 11 |
| 3 | 30 | 30 | 0 | 0 |
| 4 | 13 | 13 | 0 | 2 |
| 5 | 16 | 16 | 0 | 0 |
| 6 | 38 | 9 | **29** | 17 |
| 7 | 28 | 11 | **17** | 10 |
| 8 | 10 | 10 | 0 | 1 |
| 9 | 23 | 23 | 0 | 0 |
| 10 | 12 | 12 | 0 | 0 |
| **total** | **222** | **162** | **60** | **38** |

Depth 2 generates ~85% of all CTE rows (134–418 per trial vs 22–44 at depth 1)
and contributes **27% of surviving nodes — but bimodally**: zero in 7 of 10
queries, and 60 nodes concentrated in the other 3. When it does fire, 38 of
those 60 (63%) are chunks or episodes.

The cause is arithmetic, not sparse data. A depth-2 node needs
`seed × w₁ × w₂ × 0.25 > 0.1`, i.e. `seed × w₁w₂ > 0.4`. At a typical seed of
0.5 that demands `w₁w₂ > 0.8`. Edge weights on the clone:

| relation | method | count | avg weight |
|---|---|---|---|
| `related_to` | inferred | 22,077 | **0.410** |
| `related_to` | deterministic | 7,053 | 1.000 |
| `part_of` | deterministic | 4,116 | 1.000 |
| `summarized_by` | inferred | 3,705 | 0.341 |
| `evidence_for` | inferred | 1,916 | 0.279 |
| `extracted_from` | deterministic | 1,369 | 1.000 |

A two-hop path over the dominant relation scores `0.5 × 0.41 × 0.41 × 0.25 =
0.021` — five times under the floor. Only two-hop **weight-1.0** chains survive,
and those are `part_of` / `extracted_from` / `related_to:deterministic`. That is
why the depth-2 yield is 63% chunk/episode: at depth 2 the mechanism is
functioning as an **episode-sibling expander** — "other chunks of an episode I
already retrieved" — not as associative recall.

Worth flagging as a knife-edge: at depth 1 the floor needs `seed × w × 0.5 >
0.1`, i.e. `w > 0.4` at seed 0.5. The dominant relation averages **0.410**. Half
the graph's most common edge type sits within a rounding error of being cut.
Seeds ranked lower than ~0.3 (`w > 0.67` required) get almost nothing through
at all.

**Options, none free:**
- (a) `max_depth = 1` — removes 85% of rows for 27% of yield. Honest only if the
  A/B shows the 3-in-10 depth-2 contribution is not carrying anything.
- (b) Make the floor relative (`> 0.1 × max_seed_score`) instead of absolute, so
  a weak-seed query is not silently starved.
- (c) Raise `spreading_activation_decay` toward 0.75 so depth 2 isn't
  pre-annihilated before the floor sees it.

(b) and (c) are the interesting ones — (a) amputates a mechanism whose failure
is a tuning artefact. All three change ranking; all three need the MAB A/B.

---

## 4. Dead code and dead config

| # | Item | Evidence |
|---|---|---|
| D1 | `spreading_activation_alpha` / `beta` / `gamma` (`config.py:1183-1185`) | Consumed nowhere. They are the original F022 design's combined scorer (`α·vector + β·activation + γ·recency`, `docs/plans/2026-03-08-graph-augmented-recall.md:1387`) which was never built. **Already flagged as BR-26 in the 2026-06-09 brain audit** and still present 2.5 months later. |
| D2 | `AUTOBEHAVIOR_EXCLUDED_RELATIONS` import (`spreading_activation.py:16`) | Imported, never referenced — only `autobehavior_exclusion_sql` is used. |
| D3 | `ResidualActivator.seed_for_spreading` (`residual_activation.py:165`) | **Zero callers anywhere** — not prod, not the eval harness, only its own unit test. Its two settings (`residual_top_n_seeds`, `residual_seed_weight`) are consequently inert; `residual_seed_weight` reaches runtime only as a startup log argument at `main.py:149`. |
| D4 | Docstring claims a wiring that does not exist (`residual_activation.py:28`) | States `recall_deep` "calls `compute_activations` + `seed_for_spreading` (consumed by F022 spreading_activation)". The first half is true (`tools.py:1374`); the second is not. |
| D5 | Duplicated mode gate | `retrieval_pipeline.py:1530-1534` re-implements the `"true"`/`"false"` check inline, so `should_use_spreading_activation`'s first two branches are unreachable from the only production caller. Not a bug; a third mode value would diverge across two sites. |

**Correction to an earlier read of my own:** I initially had F055 down as
entirely inert in prod. It is not — `NOUS_RESIDUAL_ACTIVATION_ENABLED=true`,
and the boost path is fully wired (`tools.py:1374` → `heart.py:1133`
`boost_scores` → `tools.py:1569` `record_surfaced`). Only the *spreading-seed*
leg is dead. The narrower claim is the correct one.

---

## 5. D3 is also the cheapest capability gain available

Cross-turn residual activation is computed on every `recall_deep` call
(`tools.py:1374`) and — correcting my first draft — is **already threaded all the
way into `run_recall_pipeline`** (`tools.py:1422` → `retrieval_pipeline.py:389,
911`), where it reaches `Heart.recall`'s score boost. It is not stranded in a
local. What it never reaches is the **Stage 4 seed list**, which is built only
from the current query's top-5 decisions and top-3 facts
(`retrieval_pipeline.py:1517-1521`). So a graph walk whose whole purpose is
association never starts from anything the conversation touched on a previous
turn, despite the shaping function for exactly that (`seed_for_spreading`) being
written, tested, and documented as wired.

**But wiring it today would measure a null for a reason that has nothing to do
with the idea.** `seed_for_spreading` applies `residual_seed_weight = 0.3`
(`residual_activation.py:180-182`), so a residual seed enters at ≤ 0.3. Clearing
the depth-1 floor from a 0.3 seed needs `0.3 × w × 0.5 > 0.1`, i.e. **w > 0.67**
— against a dominant-relation average of 0.410. Residual seeds die *inside the
CTE*, which the §2 score fix never touches.

So D3 is gated on the **floor** (§3), not on the score fix. Either delete it as
a failed design, or wire it only after the floor question is settled.

---

## 6. Test gaps

`tests/test_spreading_activation.py` has 16 tests covering the density gate
modes, the four relation/method exclusions, and MAX-vs-SUM aggregation. Nothing
covers:

- **the `> 0.1` activation floor** — the mechanism that, per §3, discards ~98% of
  depth-2 rows and is the knife-edge in §3's last paragraph. Completely untested.
- `_SPREADING_RESULT_CAP` truncation at 20.
- `exclude_ids` — that known duplicates are cut *inside* the final SELECT before
  the LIMIT, which is the invariant PR #556 was opened to establish.
- the resolve-drops-everything → `use_spreading = False` → 1-hop fallback path
  (`retrieval_pipeline.py:1688-1698`).
- the double decay in §2 — no test pins the score a spreading row ends up with,
  which is why the extra 0.7 survived plan 1.2's scoring rework.

The floor and the score are the two numbers this whole mechanism turns on, and
neither has a test.

---

Note on placement: the floor (`retrieval_pipeline.py:1616`), cap (`:1645`), and
1-hop fallback (`:1688-1698`) live in the **pipeline**, not the CTE module, so
their tests belong in `tests/test_retrieval_pipeline.py`. Only `exclude_ids`
belongs with the CTE. A relative floor (§3, 3a) must also update the F091 mirror
predicate `_act <= 0.1` at `:1639`, or the telemetry stops agreeing with the
filter.

---

## 6b. The cost nobody measured: rendered tokens

`recall_deep` renders every result, and `_resolve_node_descriptions` does not
truncate, so up to 20 spreading rows per call reach the model at full content
length. Average content on the clone: fact 209 chars, episode 577, chunk 847.

A 20-row spreading block is therefore roughly **4k–17k chars (~1k–4k tokens) per
`recall_deep` call**, worst case in exactly the depth-2-heavy queries where the
output is 63% chunks. Since these rows sort last under `rerank_by_score=True`,
they are the lowest-scored content in the block.

This is the mechanism's real prod cost — not the 3 ms — and it is unmeasured
end-to-end. Any off-arm should report it. (Formatter-level truncation, if any,
is unverified; treat the figure as an upper bound.)

---

## 7. Recommended order

**Ship now — zero ranking risk:**
1. D1 — delete `spreading_activation_alpha/beta/gamma`.
2. D2 — drop the unused import.
3. D4 — correct the `residual_activation.py` docstring.
4. Hoist the hardcoded `0.1` floor to a setting at default 0.1 (exact no-op).
5. Add the §6 tests against current behaviour, at the right layer, so any later
   tuning has a baseline that fails loudly.

**Measure before touching anything else — the existence question first:**
6. **Off-arm**: `spreading_activation_enabled=false` vs `auto`. Per §2b, this is
   the comparison with real prod consequences — spreading suppresses a leg that
   scores 2.86× higher. `nous_eval` already defines this arm (`spread_force_off`).
7. `max_depth=1` as a control — isolates whether depth 2 earns its 85% of rows.

**Only if 6/7 say the mechanism is worth keeping:**
8. F2 (double decay) — one line, ×1.43, **membership-invariant** (§2b), so it
   moves ranking only.
9. F1 3a/3b — relative floor, or decay 0.5 → 0.75. One variable per arm. Note
   3b composes with F2: `0.83 × 1.0 × 0.75 ≈ 0.62` approaches the cutline, so
   state the combined ceiling before flipping both.
10. D3 — wire residual seeds (gated on the **floor**, per §5) or delete them.

**Do not do:** anything aimed at the CTE's execution time (§1).
