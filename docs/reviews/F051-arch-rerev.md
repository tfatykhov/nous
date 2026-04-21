# F051 Architecture Re-Review (v2) — nous-eval-arch-rerev

**Verdict:** APPROVE WITH MINOR
**Decision ID:** 2a008453
**Reviewer:** nous-eval-arch-rerev
**Date:** 2026-04-20
**Original review:** `docs/reviews/F051-arch-review.md` (decision `fd69ffe1`)
**Artifacts checked:**
- `docs/superpowers/plans/2026-04-20-f051-retrieval-eval-harness.md` (v2)
- `docs/features/F051-retrieval-eval-harness.md` (spec line 275 — paired-delta math)

## Original P1s status

- **P1-1** ✅ — `nous/api/retrieval_pipeline.py::run_recall_pipeline` introduced as a sequenced PREREQ subagent (`nous-eval-impl-refactor`) that must complete before Core/Infra/Tests start. Refactor contract specifies byte-identical text output, snapshot test, and all 10 existing `recall_deep` consumers must pass unchanged. Runner calls the pipeline directly — exactly what the original P1 demanded.
- **P1-2** ✅ — `_run_one` calls `run_recall_pipeline(query=..., heart=, brain=, settings=, limit=, memory_types=...)`. The phantom `session_id` kwarg is gone; Heart.recall is now invoked from inside the pipeline with proper `types`/`session` params.
- **P1-3** ⚠️ — `RuntimeConfig.reset()` is called at startup AND between configs (plan §B.6 line 328). This solves the `_overrides`-bleed half of the problem. It does **not** solve the original sub-finding that `_resolve_vector_weight`/`_resolve_rrf_k` in `nous/heart/search.py:34-55` build a fresh `Settings()` from `os.environ` internally — so per-config Settings overrides for `vector_weight` or `rrf_k` will silently NOT propagate. The Phase 1 config matrix (`baseline / f050_on / ce_off / mmr_off / graph_off`) does not touch those two knobs, so the gap is non-blocking, but it should be **documented as out-of-scope** (any future `vector_weight` A/B requires an upstream search.py refactor).
- **P1-4** ✅ — Standardized on `agent_id="nous-eval-corpus"`. `EvalSettings.agent_id` defaults to it, `_settings_for_eval_db` propagates it, and spec §15 silent-failure adds a startup `SELECT DISTINCT agent_id` warning.
- **P1-5** ✅ — Spec line 275 verified: variance claim removed, replaced with the correct linearity-of-expectation statement and a note that paired analysis tightens CIs (deferred to Phase 1.5). `metrics.py compute_delta` docstring repeats the same correction. Both spec and plan match.
- **P1-6** ✅ — `EvalSettings.db_url` is a `@property` (not `dsn()`). Required `db_pool_size`, `db_max_overflow`, `log_level` fields all present.

## New concerns from v2 design

- **`_settings_for_eval_db` defense-in-depth (minor):** Disables 9 background flags. For full belt-and-suspenders also disable `rubric_outcome_detection_enabled` and `correction_extraction_enabled` — they only fire on episode close, which the eval harness shouldn't trigger, but the explicit disable removes any ambiguity.
- **`require_majority_positive` at N=2 sources (informational):** With only `longmemeval` + `probes` gate-eligible, "majority positive" reduces to "both must be net-positive (>0%)". A single noise-flat source can fail the gate even when aggregate clears +7%. The threshold is `>0%` (not `>3%`), so genuinely-flat sources still pass. Acceptable, but worth one sentence in the report explaining the rule at N=2.
- **Heart sub-search exception suppression (minor quality gap):** `nous/heart/heart.py:783-799` catches per-type search exceptions and emits a `logger.warning`, then drops the type silently. The pipeline's `QrelResult.error` only populates when the OUTER call raises, so a fact-subsearch failure manifests as a quiet MRR drop, not as a flagged error. Recommend `PipelineStats` carry `n_per_type_errors` and the report surface it. Not blocking — Heart's existing log line is the audit trail.

## Verdict justification

All six original P1s are addressed in spec/plan v2; five fully and one (P1-3) partially with the remaining gap correctly scoped out of Phase 1. The pipeline refactor — the heaviest lift the original review demanded — is sequenced as a dedicated PREREQ subagent with a byte-identical-output contract and 10 regression tests. The three new concerns are incremental improvements, not blockers. **APPROVE WITH MINOR**: implementation can proceed; fold the three minor items into impl-review checklists.

**Decision ID: `2a008453`**
