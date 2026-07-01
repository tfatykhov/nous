# F075 Layer 3 — Date-Window Retrieval Leg (Design)

**Status:** Design approved 2026-07-01. Ready for implementation planning.
**Feature:** A seed-independent, date-keyed retrieval leg fused into the recall pipeline via RRF, to answer temporal queries whose gold the vector/keyword legs miss.

---

## 1. Motivation (why this, and why not the alternatives)

A 2026-07-01 investigation established, with measurement on the prod-shape eval corpus (`:5433/nous_eval_prod`, agent `nous-default`), that **graph expansion does not improve retrieval**:

- Graph-expanded results rank 13–28, never top-10 (n=20 probe).
- A confirming probe rescored graph neighbors with the Path-A `seed_score × edge_weight` formula: **0/9 reachable golds cracked top-10** — scoring is not the cause.
- An edge-semantics audit of all 15,771 edges: avg source↔target cosine 0.675; `related_to` is **72% of the graph** at cos 0.69; **~92% of edges connect same-topic content**; only ~8.5% are orthogonal. The graph is *precise but redundant* — it reconnects content the vector leg already retrieves.

Conclusion: score-space normalization (audit "R2") and seed-score-for-decisions are dead ends. The lever is not scoring; it is a retrieval signal orthogonal to embeddings.

The chosen target is **temporal / event-ordering** retrieval (the field's measured biggest gap: BEAM event-ordering wall, LongMemEval temporal). Of three mechanisms considered — (A) temporal-intent-gated chain traversal from seeds, (B) date-window retrieval leg, (C) anchor-and-expand — the deployed Nous advisor and a follow-up empirical test both selected **B**. Reason: A traverses *from vector-retrieved seeds*, but temporal queries fail vector seed retrieval (the 0/9 result), so A is built on the broken leg. B keys on `event_date` directly and is seed-independent.

B is also the **already-scoped-but-unbuilt F075 Layer 3** (the `NOUS_DATE_AWARE_BOOST_*` config flags that ship today with no consumer).

### Validated evidence (both on `:5433/nous_eval_prod`, n=22 each, synthetic time-framed queries vs dated-fact golds)

| Test | date parsed | window ⊇ gold | vanilla top-10 | fused top-10 | rescued | lost |
|---|---|---|---|---|---|---|
| Oracle window (gold's date ±7d) | — | 22/22 | 15/22 | 21/22 | 6 | **0** |
| Realistic (window parsed from query by Haiku) | 22/22 | 22/22 | 20/22 | 22/22 | 2 | **0** |

Consistent across n=44: **rescue rate among vanilla-misses ~86–100%, zero regressions** (RRF fusion of a small/empty leg never demotes an existing hit). Haiku date-parsing was not a bottleneck (100% parse + window-contains-gold on time-referenced queries).

---

## 2. Scope

**In scope (v1):** a date-window retrieval leg over `heart.facts`, fused via `_rrf_merge_n`, gated + land-dark, with an A/B measurement.

**Bounded by (explicit, not hidden):**
1. **Query realism** — only helps queries carrying a parseable time reference. No time reference → parser returns no date → leg inert → falls back to vanilla (safe).
2. **Coverage cap** — only ~9.1% of facts (262/2890) carry `event_date`. B helps when the *answer* is dated. Widening reach depends on `event_date` extraction coverage (prior audit: ~45% `dated_event` miss) — tracked as a paired follow-on, not bundled here.
3. Validated on synthetic queries, one corpus, n=44.

**Non-goals (v1):** Approach A (entity-anchored chain traversal), multi-hop bridge, cross-episode `happened_before` chain generation, improving `event_date` extraction coverage. These are separate specs. A and multi-hop reuse the measurement + (for A) the fusion foundation built here.

---

## 3. Architecture

A new retrieval leg parallel to the existing vector + keyword legs, fused by rank. Three components.

### 3.1 DateWindowParser
- **Input:** the raw query string.
- **Output:** `DateWindow | None` = `{start: date, end: date}` or `None`.
- **Pre-gate (cheap):** a regex/keyword pre-check for temporal signal (month names, 4-digit years, "yesterday/last week/in <month>/around/late/early/mid", ISO dates). If no signal → return `None` **without** an LLM call. Keeps the LLM off the hot path for the ~majority of non-temporal queries.
- **LLM parse (Haiku):** on pre-gate hit, one forced-tool Haiku call (`{has_date, start_date, end_date}`), `system=""`, ~256 output tokens, anchored on "today" passed in. Validated at 100% on time-referenced queries.
- **Cross-cutting:** per-hour budget cap (mirror `query_expansion_max_per_hour`), in-process cache with TTL (mirror `query_expansions`), `asyncio.wait_for` timeout. **Fail-open:** any timeout / budget breach / parse error / `has_date=false` → `None`.
- **Padding:** the returned window is padded by `NOUS_DATE_LEG_PAD_DAYS` (default 2) at query time (PAD=2 validated).

### 3.2 Date-window fact retrieval
- **Input:** `DateWindow`, the query embedding, `agent_id`, `K = NOUS_DATE_LEG_K` (default 15).
- **SQL:** `SELECT id FROM heart.facts WHERE agent_id=:a AND active AND event_date IS NOT NULL AND event_date BETWEEN :lo AND :hi ORDER BY embedding <=> :qvec LIMIT :k`.
- Returns a **ranked list of fact IDs** (relevance-within-window). Ranking within the window by cosine is load-bearing — it is the "relevance floor AND date-proximity" the validated tests used; do **not** rank by date-distance alone.
- The query embedding is the same vector the pipeline already computes for the vector leg (reuse it; do not re-embed).

### 3.3 RRF fusion (Nth leg)
- Feed the date-leg ranked list into **`_rrf_merge_n`** (`nous/heart/search.py:295`) as an additional ranked list alongside the existing vector + keyword lists.
- Within-leg rank scoring with `k = NOUS_RRF_K` (60) gives k-dampening: the leg contributes `1/(k+rank)` per hit, so it lifts genuine matches without flooding top-K, and an empty leg is a no-op. No hand-tuned injection cap.

### 3.4 Wiring point
`heart.recall`'s hybrid fact path is where `_rrf_merge_n` fuses ranked lists (see `facts.py:1792` / `search.py`). The date leg is added there as one more ranked list when a `DateWindow` is present. Fact-only in v1. Determine at implementation time the cleanest thread for the parsed window + query embedding from `run_recall_pipeline` down to the fusion site (candidate: compute the window once in `run_recall_pipeline`, pass it into the fact search alongside the existing query).

---

## 4. Data flow

```
query
  → DateWindowParser (regex pre-gate → Haiku; None if no temporal signal)
  → if window: date-window SQL (event_date in [lo,hi], ranked by cosine, LIMIT K)
  → _rrf_merge_n([vector_leg, keyword_leg, date_leg])   # date_leg absent → today's behavior
  → existing rerank_by_score / graph / MMR / etc.
  → results
```

---

## 5. Configuration

Repurpose the dead F075 Layer 3 flags; add leg-specific knobs. All land-dark.

| Flag | default | purpose |
|---|---|---|
| `NOUS_DATE_LEG_ENABLED` | `false` | master switch. Off = byte-identical to today. |
| `NOUS_DATE_LEG_MODEL` | `claude-haiku-4-5-20251001` | parser model |
| `NOUS_DATE_LEG_K` | `15` | date-leg depth (validated) |
| `NOUS_DATE_LEG_PAD_DAYS` | `2` | window padding (validated) |
| `NOUS_DATE_LEG_TIMEOUT_SECONDS` | `2.0` | parser timeout; breach → fail-open |
| `NOUS_DATE_LEG_MAX_PER_HOUR` | `500` | Haiku budget cap (mirror query-expansion) |
| `NOUS_DATE_LEG_CACHE_TTL_DAYS` | `30` | parsed-window cache retention |

The legacy `NOUS_DATE_AWARE_BOOST_*` flags are superseded; note their removal/repurpose in the plan.

---

## 6. Error handling & the zero-regression guarantee

- Parser timeout / budget breach / error / no-date → `None` → **no date leg** → `_rrf_merge_n` receives the same lists as today → **fused == vanilla**. Proven: 0 losses across 44 cases.
- Empty window result set → empty leg → same no-op.
- All LLM I/O is fail-open; the leg never blocks or degrades the base retrieval.
- Off by default; enabling it is A/B-gated.

---

## 7. Measurement plan

**Unit-level (dev):** the rescue harness — sample dated facts, generate time-framed queries, compare vanilla vs vanilla+leg top-10; assert `rescued > 0` and `lost == 0`.

**A/B gate (before flip):**
- Corpus: LongMemEval temporal-reasoning subset (+ the prod-shape set as a non-temporal guardrail).
- Generator: **Opus** (`--gen-model claude-opus-4-8`) — never a Sonnet/Haiku proxy (sign flips by generator).
- One variable: `NOUS_DATE_LEG_ENABLED` off vs on.
- Pass = temporal MRR / QA-accuracy uplift **and no regression on non-temporal sources** (the zero-regression property should hold end-to-end).
- n ≥ 100 where the corpus allows; report any coverage-driven ceiling.

---

## 8. Follow-ons (separate specs, not this plan)

1. **`event_date` extraction coverage** — lift the 9.1% dated fraction (the reach multiplier for this leg).
2. **Approach A** — entity-anchored temporal chain traversal for point queries, once B validates that dates carry signal in prod A/B; reuses this measurement + fusion.
3. **Multi-hop bridge** — reuses the additive-leg + measurement foundation.

---

## 9. Testing (TDD targets for the plan)

- `DateWindowParser`: pre-gate rejects non-temporal queries with no LLM call; Haiku path parses "late April 2026" / "mid-May" / ISO dates; fail-open on timeout/budget/error/`has_date=false`; cache hit path.
- Date-window SQL: window + `active` + `event_date NOT NULL` filter; cosine ordering; K limit; agent-scoped.
- Fusion: date leg appended as an Nth list to `_rrf_merge_n`; empty leg → byte-identical to today; a strong in-window hit lifts into top-K.
- End-to-end: `NOUS_DATE_LEG_ENABLED=false` → recall output unchanged (snapshot); `=true` → rescue on a seeded temporal fixture, zero loss.
