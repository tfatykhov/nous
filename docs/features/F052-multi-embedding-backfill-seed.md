# F052 — Multi-Embedding Seed for Graph Densification Backfill

**Status:** 📝 Proposed v2 (2026-04-26 — incorporates 3-agent spec review fixes)
**Proposed by:** Tim
**Date:** 2026-04-26
**Depends on:** F040 (graph densification — shipped), F042 (CE rerank — shipped), F043 (CE in backfill — shipped), F045 (CE-aware thresholds — shipped), F050 (multi-query expansion — shipped Phase 1), F051 (eval harness — shipped)
**Blocks:** none
**Related:** F040 backfill orphan rate, `/dashboard/density`

---

## Problem

`F040` graph densification backfill walks orphan facts/decisions/episodes/procedures and links each to similar nodes via hybrid search + cosine verification. The orphan is encoded **once** — single embedding from the orphan's stored content — and used to seed both candidate generation (`hybrid_search` at `nous/brain/graph_densifier.py:195`) and threshold gating (cosine verification at `:244-256`).

**The miss**: orphans whose nearest semantic neighbor sits **just below** the per-relation cosine threshold (e.g., 0.81 vs the F045 fact↔fact gate at 0.82, or 0.64 vs CE-mode 0.65) never get an edge — even when a real relationship exists. A single embedding samples one point in the orphan's semantic neighborhood; the actual neighbor may be reachable from a slightly different paraphrase.

Cumulative effect across cycles: orphan rate plateaus, density grows slowly, the densification dashboard shows a long tail of nodes that "should" be linked but aren't.

### What this is not

Not a CE precision tweak (F045 already covers that). Not a threshold relaxation (would inflate edge count without precision evidence). Not a new graph-traversal mechanism (F022 spreading activation handles that at read time). F052 is specifically about **widening candidate generation in `_backfill_same_type` at backfill time** without sacrificing precision, by reusing F050's expander to seed N alternate query embeddings per orphan.

---

## Goals

1. **Reduce orphan rate** — measurable per-type drop in `/dashboard/density` orphan-percent after one sleep cycle, attributable solely to the new candidate-gen seed.
2. **Preserve edge precision** — LLM-judged sample of new edges (≥30 per type) must show no statistically significant precision regression vs F040+F043+F045 baseline.
3. **Pre-deploy validation only** — every gate decision happens on the eval corpus via the F051 harness; **no flag flip in prod until eval shows positive lift**. Explicit user requirement: *"test before deploying to live instance — there it is harder to catch things."*
4. **Reuse, don't fork** — F050 `QueryExpander`, `hybrid_search_multi` (already RRF-fuses N variant embeddings via the `queries` parameter), F042 `ce_rerank_backfill_candidates`, F045 `_get_threshold` — all unchanged. F052 is a wedge between expander and existing densifier, not a parallel pipeline.
5. **Default-off ship** — feature flag `NOUS_GRAPH_BACKFILL_MULTI_EMBEDDING_ENABLED=false` lands disabled. Eval harness flips it via the same `RuntimeConfig.reset()` mechanism F051 uses.
6. **Singleton expander reuse** — F052 reuses `Heart`'s already-wired `_query_expander`; never instantiates a second `QueryExpander`. Preserves F050's `_warned_once` semantics + shared budget accounting.

## Non-goals

- **Phase 1 is `_backfill_same_type` ONLY.** Cross-type backfill (`_backfill_cross_type` at `graph_densifier.py:276-440`) uses common-template re-embedding + raw vector SQL + keyword-only hybrid_search — three different retrieval paths. The "cosine verification uses original `orphan_embedding`" precision invariant **does not hold** for cross-type (cross-type compares re-embedded `source_embedding` against re-embedded `target_embedding` at `:394-395`). Adding multi-embedding seed to cross-type without addressing the re-embed asymmetry would break the precision-safe invariant. **Cross-type extension deferred to F052.4** — see §Deferred.
- **No production A/B in Phase 1** — eval harness only. If eval shows lift, *then* a separate decision opens production rollout.
- **No change to F040's threshold logic** — variants widen candidate **generation**; cosine verification still uses the **original orphan embedding** against the existing per-relation threshold (F045 if CE backfill enabled, else F040 base).
- **No change to F040's traversal scope** — same `NOUS_GRAPH_BACKFILL_MAX_*` per-cycle caps. F052 doesn't enlarge the orphan walk; it enriches what each walk attempt sees.
- **No new sleep-cycle cadence** — runs inside the existing `sleep_handler.py` backfill phase, no new scheduler.
- **No spreading-activation interaction** — F052 affects edge **creation** during backfill; F022 reads edges at recall time. Independent layers.
- **No re-embedding of stored content** — orphan's stored embedding is untouched. We compute *additional* embeddings for paraphrases, but do not write them back to the row.

## Deferred (with rationale)

- **F052.1 — Production shadow mode.** Mirror F050's "compute but don't act" pattern: log what edges *would* be created with multi-embedding seed enabled, without writing them. Useful only if eval signal is ambiguous and we need prod-traffic data before flipping. Not needed for clear pos/neg eval results.
- **F052.2 — Adaptive variant count per orphan.** Cheap orphans (already linked-adjacent, short content) use 1 variant; semantically dense or long orphans use 3. Current spec uses fixed 3.
- **F052.3 — Variant-embedding caching.** Reuse `heart.query_expansions` cache table from F050 to avoid re-embedding identical orphan content across cycles. Likely low-value (each orphan walked at most once before becoming non-orphan).
- **F052.4 — Cross-type backfill extension.** Apply multi-embedding seed to `_backfill_cross_type`. Requires deciding whether variants get re-encoded through `common_template_text(source_type, variant)` (changes the semantic meaning of the variant) and how the re-embedded source/target cosine gate composes with the wider candidate net. Non-trivial design — needs its own spec + eval methodology.

---

## Mechanism (same-type only — Phase 1)

### Today (F040 + F043 + F045) — `_backfill_same_type`

```
orphan
  ├─ orphan_embedding ◄── stored once at write time
  ├─ orphan_content   ◄── stored at write time
  │
  ├─ hybrid_search(query_text=orphan_content, embedding=orphan_embedding) → candidates [up to 10]
  ├─ if F043: ce_rerank_backfill_candidates(query=orphan_content, candidates=...) → survived [0..N]
  └─ for each survived: cosine(orphan_embedding, candidate.embedding) > threshold → edge
```

### With F052 (multi-embedding seed) — `_backfill_same_type` only

```
orphan
  ├─ orphan_embedding   ◄── stored once at write time
  ├─ orphan_content     ◄── stored at write time
  │
  ├─ queries = await heart.expand_query_pairs(orphan_content)
  │     # F050-gated:
  │     # - if expansion disabled / short content / budget exhausted / expander is None / Haiku fails / embed fails:
  │     #     returns [(orphan_content, orphan_embedding)]   ← single-pair byte-identical fallback (NEVER None)
  │     # - else: returns [(orphan_content, orphan_embedding), (variant_1, vec_1), (variant_2, vec_2)]
  │
  ├─ hybrid_search_multi(queries=queries)
  │     # Existing F050 RRF fusion across N searches.
  │     # When len(queries)==1, search.py:319-332 delegates to single-query hybrid_search → byte-identical to today.
  │     # When len(queries)>1, RRF-fuses ranked lists, returns deduped union.
  │
  ├─ if F043: ce_rerank_backfill_candidates(query=orphan_content, candidates=...) → survived [0..N]
  │     # Unchanged. CE keys on ORIGINAL orphan_content for relevance signal — variants never reach CE.
  │
  └─ for each survived: cosine(orphan_embedding, candidate.embedding) > threshold → edge
       # Unchanged. Original embedding gates precision. Variants influence candidate gen only.
       # weight = float(sim_row.similarity) is original-embedding cosine — preserved by construction.
```

**Invariant**: when `multi_embedding_enabled=False` OR the F050 gate skips expansion (short content, budget exhausted, expander unset), `expand_query_pairs` returns `[(orphan_content, orphan_embedding)]` and `hybrid_search_multi` short-circuits at `search.py:319-332` to a single-query path *byte-identical* to today's `hybrid_search`.

**Why this is precision-safe (same-type)**:
- Candidate generation widens (more shots on goal).
- CE rerank still keys on the original `orphan_content` — variants don't bias relevance scoring downstream.
- Cosine verification still uses the original `orphan_embedding` — the precision gate is unchanged.
- Net effect: same precision floor, more candidates that *can* clear it.

**Why this is precision-safe-not-yet-proven (cross-type — Phase 1 excludes)**:
- `_backfill_cross_type` re-embeds both source and target with `common_template_text` (`graph_densifier.py:300-303, 388-390`).
- Cosine gate compares `source_embedding` (re-embedded common-template) to `target_embedding` (also re-embedded), not the orphan's stored embedding.
- The clean precision proof above doesn't compose. Cross-type extension is F052.4.

---

## Eval methodology (gate before code)

This section ships **before** implementation. Numbers below are the success criteria; if eval doesn't hit them, F052 doesn't merge.

### Determinism guarantee

Haiku is non-deterministic at default `temperature=0.4`. For density_eval re-runs to produce reproducible numbers:

- `density_eval.py` overrides `Settings.query_expansion_temperature = 0.0` for the duration of the run.
- Eval cache (`heart.query_expansions`) is **not** truncated between configs — variants for the same orphan_content are deterministic across runs after the first.
- For paranoid noise check: harness supports `--n-runs 3` flag; report `mean ± stddev` per metric and gate on `mean - stddev` (lower-bound CI).

(`temperature=0.0` requires adding `Settings.query_expansion_temperature: float = Field(default=0.4)` if not already present; F050's expander currently inherits Anthropic defaults. Plan handles this.)

### Pre-condition: zero-edge state for BOTH configs

Before *any* config runs, the harness asserts a clean baseline:

1. `SELECT count(*) FROM brain.graph_edges WHERE agent_id = $eval_agent_id` must equal 0. If not, harness DELETEs and creates `eval_baseline_edges_snapshot` — a transient table holding the **intentionally empty** zero-edge state. The snapshot is the transactional anchor for step 6's restore on crash; an empty INSERT-from-snapshot is the correct no-op restore back to verified-clean state.
2. Both configs (`baseline`, `f052_on`) start from this snapshot. Without this, the *first* config silently inherits any pre-existing edges and the comparison is meaningless. (Reviewer P1 caught this in v1.)

### New harness mode: `nous_eval/density_eval.py` (~100 LOC)

```bash
uv run python -m nous_eval.density_eval --configs baseline,f052_on [--n-runs 1]
```

Behavior per config:

1. **Restore baseline** — `DELETE FROM brain.graph_edges WHERE agent_id = $eval_agent_id` (no-op if already empty after prior config's restore).
2. **Snapshot pre-state** — record per-relation: edge count = 0, distinct source/target IDs, per-type orphan count via `find_orphans()`. Verified-empty baseline.
3. **Apply config** — set `RuntimeConfig` overrides (`graph_backfill_multi_embedding_enabled`, `query_expansion_temperature=0.0`, etc.) using F051's existing `RuntimeConfig.reset()` between configs.
4. **Run densifier** — invoke `GraphDensifier.run_backfill_cycle()` directly, then **also** invoke `discover_clusters(max_bridges=20)` to mirror prod's full sleep_handler backfill phase (`nous/handlers/sleep_handler.py:886-911`). Without `discover_clusters`, eval measures backfill in isolation while prod runs both. (Reviewer P1.)
5. **Snapshot post-state** — re-run step 2 measurements.
6. **Transactional reset on failure** — wrap each config in `try/except`. If a config crashes mid-cycle (Haiku timeout cascade, OAT 401 burst), `INSERT INTO brain.graph_edges SELECT * FROM eval_baseline_edges_snapshot` to restore zero-edge state and log the failure as a config result, not a harness crash.
7. **Repeat for next config**.
8. **Output** — `reports/density-eval-<timestamp>.md`:

| metric                        | baseline | f052_on | Δ      | gate-eligible |
|-------------------------------|----------|---------|--------|---------------|
| edges_created (total)         | 412      | 489     | +18.7% | ✅            |
| edges_created (fact_fact)     | 218      | 261     | +19.7% | ✅            |
| edges_created (decision_dec.) | 38       | 41      | +7.9%  | ✅ (N=15+)   |
| edges_created (procedure_*)   | 4        | 5       | +25%   | ⚠ underpowered (N<15) |
| orphans_remaining (fact)      | 47       | 31      | −34.0% | ✅            |
| orphans_remaining (procedure) | 3        | 2       | −33.3% | ⚠ underpowered |
| ce_pruned_total               | 134      | 218     | +62.7% | (info)        |
| union_size_p95                | 10       | 22      | +120%  | (info)        |
| haiku_calls                   | 0        | 117     | +∞     | (cost)        |
| openai_embed_calls            | 0        | 117     | +∞     | (cost)        |
| wall_seconds                  | 22       | 47      | +114%  | (cost)        |

(Numbers above are *target shape*, not predictions.)

### Edge-precision audit (semi-manual)

Density lift without precision is worthless. After each eval run:

1. Sample 30 newly-created edges per relation type (only types where new ≥ 30; otherwise sample all new edges of that type).
2. Render each edge as `(source_content, target_content, relation, weight)`.
3. LLM-judge via `nous_eval/edge_judge.py` (NEW, ~60 LOC) — single Sonnet call per batch with cached prompt template `nous_eval/templates/edge_precision_prompt.md`. Returns YES/WEAK/NO per edge.
4. Compute precision = `YES / (YES + WEAK + NO)` per type.

(Reviewer P3-2 flagged that "manual LLM-judge" was hand-waved. v2 commits to a real module.)

### Gate criteria

F052 ships to prod-flippable status only if **all** are true:

1. **Density lift** — `(orphans_remaining_baseline − orphans_remaining_f052) / orphans_remaining_baseline ≥ 0.10` for at least one **gate-eligible** entity type, no per-type regression worse than `−0.02` on any gate-eligible type.
   - **Gate-eligible** = baseline orphan count for that type ≥ 15. Below that, ratios are dominated by noise (procedure_* often has N=2-5 in eval corpus). Underpowered types are reported but excluded from the gate verdict.
2. **Edge precision floor** — ≥ 0.75 LLM-judged YES rate per type. Justification: F045 baseline empirically validated 80% on fact↔fact at 0.65 threshold (decision `7d6fdce9`); F052 widens candidate gen so a 5-percentage-point precision give-back is the explicit acceptable trade-off for the density lift. If user wants zero-regression, set `NOUS_F052_GATE_PRECISION_FLOOR=0.80`.
3. **CE truncation safety** — `union_size_p95 < cross_encoder_max_candidates - 2`. If the 95th-percentile union size hits the CE cap (default 30), eval is measuring CE truncation behavior under wider input distribution, not the multi-embedding effect. Re-run with `NOUS_CROSS_ENCODER_MAX_CANDIDATES=50` and compare. (Reviewer P2-3.)
4. **Cost ceiling** — `haiku_calls + openai_embed_calls` per cycle stays within the orphan-cap budget (`NOUS_GRAPH_BACKFILL_MAX_FACTS + decisions + episodes + procedures = 130`). No surprise multiplication.
5. **Wall time** — sleep cycle stays under existing tolerances (currently bounded by `NOUS_SUBTASK_DEFAULT_TIMEOUT=600s` for the sleep handler subtask).

If gate misses on a single criterion: spec marks F052 abandoned with the eval-run report attached. No prod deploy.

### Cost estimate (eval run, not prod)

- Eval corpus has ~50-130 max orphans/cycle (varies by snapshot).
- F050 expansion: 1 Haiku call per orphan that passes the min_words gate. Assume 80% pass → ~80 Haiku calls.
- Embedding: `embed_batch(3 variants)` per Haiku-expanded orphan → ~80 OpenAI calls.
- Per-run total: ~$0.04 with Haiku 4.5 + text-embedding-3-small. Bounded.

---

## Implementation surface (after eval gate passes)

### Files to modify

| File | LOC | Change |
|---|---|---|
| `nous/brain/graph_densifier.py` | ~30 | `_backfill_same_type` only: replace `hybrid_search(...)` call (line 195) with `queries = await heart.expand_query_pairs(orphan_content)` followed by `hybrid_search_multi(queries=queries, ...)`. Add `import` of `hybrid_search_multi` from `nous.heart.search`. Densifier needs a `Heart` reference passed at construction; today it has `_embedder` + `_settings` + `_linker`. Plan handles wiring. **`_backfill_cross_type` UNCHANGED in Phase 1.** |
| `nous/heart/heart.py` | ~30 | Extract reusable `async def expand_query_pairs(self, query: str) -> list[tuple[str, list[float] \| None]]` from `_recall:809-835` inline block. Drop `agent_id` from signature — block uses `self.agent_id`. **Returns `[(query, None)]` (single-pair fallback) on any failure, NEVER None** — caller never has to check. Refactor `_recall` to call `self.expand_query_pairs(query)` so existing behavior is preserved (the _recall body still treats single-pair-with-None-embedding as the "skip expansion" branch). |
| `nous/config.py` | ~5 | One new `Field`: `graph_backfill_multi_embedding_enabled: bool = Field(default=False, description="F052 master switch...")`. Use F050's `query_expansion_max_variants=3` directly — no new variant-count knob (Reviewer P2-1). |
| `nous/config.py` | ~3 | One new `Field`: `query_expansion_temperature: float = Field(default=0.4, description="F050/F052 — Haiku temp...")`. Allows density_eval to override to 0.0. (Today F050 inherits Anthropic default — making it explicit + tunable.) |
| `nous/heart/query_expansion.py` | ~5 | Read `settings.query_expansion_temperature` instead of inheriting default. Mechanical change. |
| `nous_eval/retrieval.py` | ~10 | New `f052_on` `RetrievalConfig` entry for paired A/B in the existing retrieval harness (optional — does multi-embedding seed at backfill time also help recall via richer graph? Curiosity test). |
| `nous_eval/density_eval.py` | ~100 | NEW. Snapshot/zero-edge-precondition/run/snapshot loop + transactional restore + markdown writer. |
| `nous_eval/edge_judge.py` | ~60 | NEW. Sonnet-judge wrapper for §Edge-precision audit. |
| `nous_eval/templates/edge_precision_prompt.md` | ~40 | NEW. Persisted prompt template with cache_control hint. |
| `nous_eval/_build_densifier_for_eval.py` | ~30 | NEW helper — mirrors `_build_heart_for_eval` pattern. Constructs `Heart` (with QueryExpander wired) + `GraphDensifier` (with the Heart reference passed). Eliminates the wiring gap reviewer P2-1 flagged. |
| `tests/test_f052_multi_embedding_seed.py` | ~180 | Unit + integration. **Required cases** (Reviewer P1-4): (1) single-pair short-circuit byte-identity vs today's hybrid_search; (2) multi-pair candidate-set strictly widens (or equals on full overlap); (3) original-embedding still gates cosine — assert weight = orig-embedding-cosine even when candidate came from variant; (4) expander failure returns single pair (not None) — densifier never sees None; (5) all-variants-return-same-candidates — RRF doesn't double-count; (6) empty union → return 0 cleanly; (7) union > CE cap (30) — assert candidates truncated and behavior matches CE-only path; (8) CancelledError propagates through densifier wedge (does NOT get swallowed by F050 fail-open). |
| `docs/features/INDEX.md` | +1 row | F052 entry (added when impl ships, not now). |

### Files NOT touched

- `nous/heart/query_expansion.py` interface — F050's expander used as-is (only the temperature read changes).
- `nous/heart/search.py` — `hybrid_search_multi` already does N-way RRF via `queries=`; nothing to add.
- `nous/brain/backfill_rerank.py` — F043 CE adapter unchanged.
- `nous/brain/_entity_config.py` — relation/threshold mapping unchanged.
- `nous/brain/graph_densifier.py::_backfill_cross_type` — explicitly out of Phase 1 scope (deferred to F052.4).
- `sql/migrations/` — no schema change. Variant embeddings are ephemeral.
- `nous/handlers/sleep_handler.py` — backfill is invoked via the densifier, which transparently picks up the new path.

### Total: ~250 LOC impl + ~180 LOC tests + ~230 LOC harness mode + judge = **~660 LOC**, ~2-3 day spike.

---

## Risks

1. **Wider candidate net → more wrong edges.** Mitigated by: CE rerank unchanged (F043), cosine verification unchanged (F045 thresholds), per-cycle orphan cap unchanged. Eval gate's edge-precision audit (≥0.75) catches this empirically before any prod flip.
2. **Eval corpus orphan distribution may not match prod.** Eval corpus is ~50-130 orphans across 4 types vs prod's variable backlog. Necessary but not sufficient — *if eval shows lift and we eventually do prod-flip, monitor `/dashboard/density` orphan-rate trend for 1-2 sleep cycles before declaring success*.
3. **F050 cache pollution.** F050's `heart.query_expansions` cache keys on `canonical_input_hash(query)`. Orphan content fed through `expand()` populates the same cache as user queries. Mitigated by F050's existing TTL (`NOUS_QUERY_EXPANSION_CACHE_TTL_DAYS=30`) + the F050.2 sweep handler when it lands.
4. **Haiku budget burn during sleep — shared bucket starvation.** F050 enforces `query_expansion_max_per_hour=500` *process-wide*. Backfill cycle of ≤130 orphans + concurrent interactive recall could starve one or the other. Eval (cold cache, idle DB) will never see this — prod will. Mitigation options for prod-flip phase: (a) `NOUS_QUERY_EXPANSION_MAX_PER_HOUR=1000` raise, (b) document as known cross-feature interaction in §Rollout Phase 4 monitoring, (c) F052.5 separate backfill bucket. Phase 1 picks (b); spec flags it explicitly.
5. **CE rerank candidate-pool growth.** Today CE sees up to 10 candidates per orphan. With 3-variant union, count can grow to 30. Cap is `cross_encoder_max_candidates=30`. Density_eval emits `union_size_p95`; gate criterion 3 explicitly invalidates eval results when p95 hits 28+ and requires a re-run with raised cap. (Reviewer P2-3 operationalized.)
6. **Async error contract** — densifier wedge **must propagate `asyncio.CancelledError`**; only fail open on `Exception`. F050's expander already does this correctly (`query_expansion.py:221-224`). The new `expand_query_pairs` helper preserves this. Test #8 enforces.
7. **Edge cleanup on rollback** — F052 creates graph edges. If a bad rollout creates wrong edges, flipping the flag off doesn't delete them. Asymmetric "create" vs "delete" lifecycle. Low-precision edges get filtered by F022 spreading-activation density gates and F045 thresholds at read time, so impact is bounded — but worth flagging. If a serious post-flip regression emerges, manual `DELETE FROM brain.graph_edges WHERE created_at >= '<flip-timestamp>' AND <quality criteria>` may be needed.

---

## Rollout

| Phase | Trigger | Action |
|---|---|---|
| **0 — Spec lock (this doc)** | This v2 doc + 3-agent re-review confirm deltas | Spec frozen, impl plan can be written |
| **1 — Impl + harness mode** | Plan approval | Branch `feat/F052-multi-embedding-backfill`, ship code default-off + density_eval harness mode + tests + edge_judge + prompt template. PR opens with eval-pending status. |
| **2 — Eval gate** | Branch CI green | Run `uv run python -m nous_eval.density_eval --configs baseline,f052_on --n-runs 1` on eval DB. If borderline, re-run with `--n-runs 3`. Manual edge-precision audit on 30 edges per type via `edge_judge.py`. Attach report to PR. |
| **3a — Pass** | Eval meets all 5 gate criteria | Merge PR with prod flag still off. Open follow-up issue for prod-flip decision (separate from merge). |
| **3b — Fail** | Eval misses any gate | Mark F052 status `❌ Abandoned (eval-fail-yyyy-mm-dd)` with the report linked. PR closes without merge. Spec stays as decision record. |
| **4 — Prod flip (separate decision)** | 3a only | Set `NOUS_GRAPH_BACKFILL_MULTI_EMBEDDING_ENABLED=true` in prod `.env`, restart. **Hard gate**: monitor `/dashboard/density` for 2 sleep cycles AND `query_expansions_per_hour` metric for budget contention. Roll back via env-var flip if either trends wrong. |

---

## Open questions

1. **density_eval harness mode — separate CLI entry or sub-mode of existing `retrieval` CLI?** Cleaner as separate (different metric set, different output format). Lean separate. *Plan answers.*
2. **Should `discover_clusters(max_bridges=20)` parameters in eval mirror prod exactly, or use a smaller `max_bridges` to keep eval fast?** Plan answers.
3. **Future cross-type extension (F052.4)** — should it apply variants pre-template (variant text → common-template wrap → embed) or post-template (orphan_content → common-template wrap → variant embeddings of the templated text)? Affects what semantic neighborhood is sampled. Out of Phase 1 scope; documented for future spec author.

These are answered in the impl plan, not here.

---

## Spec review history

- **v1 (2026-04-26 morning):** Initial draft.
- **v1 → v2 (this doc) review fixes (3-agent: arch / devil / python-pro):**
  - **P1-fix:** Phase 1 scoped to `_backfill_same_type` only — cross-type cosine invariant doesn't compose under multi-embedding seed; deferred to F052.4 (devil P1-1, P1-2; arch P1-2).
  - **P1-fix:** `hybrid_search_multi` parameter renamed throughout: `variant_pairs=` → `queries=` (arch P1-1).
  - **P1-fix:** `expand_query_pairs(self, query)` returns single-pair `[(query, None)]` on failure, NEVER `None` — eliminates 4-site fallback duplication (devil P1-4).
  - **P1-fix:** Helper signature drops `agent_id` parameter (uses `self.agent_id` like the source block at `heart.py:813-835`) (python-pro P1-1).
  - **P1-fix:** Determinism — `query_expansion_temperature: float` field added; density_eval forces `0.0` for reproducibility (devil P1-3).
  - **P1-fix:** Eval pre-condition — zero-edge state asserted before BOTH configs run, not just within the loop (devil P1-5 + arch P2-2 elevated).
  - **P1-fix:** Eval invokes `discover_clusters` to mirror prod's full sleep_handler backfill phase (devil P1-5 second part).
  - **P1-fix:** Test plan bumped from ~120 LOC / 4 cases to ~180 LOC / 8 cases including CE truncation, cosine-gate invariant, RRF double-count, empty union, CancelledError propagation (python-pro P1-4).
  - **P1-fix:** Singleton expander reuse note added to §Goals #6 (python-pro P1-2).
  - **P2-fix:** Per-type N-floor (≥15) for gate eligibility — procedure_* often has N<15 in eval corpus (devil P2-2).
  - **P2-fix:** CE truncation gate criterion (#3) — invalidates eval if `union_size_p95 ≥ cross_encoder_max_candidates - 2` (devil P2-3).
  - **P2-fix:** Transactional reset — try/except + restore from snapshot table on config-crash (devil P2-4).
  - **P2-fix:** Shared budget bucket starvation flagged in §Risks #4 with prod-flip mitigation options (devil P2-1).
  - **P2-fix:** `_build_densifier_for_eval` helper added to impl surface (arch P2-1).
  - **P2-fix:** CancelledError propagation explicit in §Risks #6 + test #8 (python-pro P2-2).
  - **P2-fix:** Drop TBD "default-bind to query_expansion_max_variants" — just default to F050's value at call site (python-pro P2-1).
  - **P2-fix:** `edge_judge.py` + persisted prompt template — no more hand-wave (devil P3-2 elevated).
