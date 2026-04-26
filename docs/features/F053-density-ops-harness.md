# F053 — Density-Eval Harness + Edge Precision Audit

**Status:** 📝 Proposed (2026-04-26 — salvaged from F052 abandonment)
**Proposed by:** Tim
**Date:** 2026-04-26
**Depends on:** F040 (graph densification — shipped), F042/F043/F045 (CE rerank + thresholds — shipped), F051 (eval harness infrastructure — shipped)
**Blocks:** F054 (selective CE-threshold relaxation — separate spec)
**Related:** F052 (abandoned 2026-04-26 — `❌ eval-fail`)

---

## Problem

F040 graph densification shipped without a way to A/B alternate parameter choices (CE thresholds, content-length guards, candidate-pool caps) against a stable corpus. The only feedback loop today is "ship to prod, watch `/dashboard/density` for two sleep cycles, hope the trend is right." That's slow, expensive, and doesn't surface threshold-bound precision regressions until they've already polluted the graph.

The F052 work (multi-embedding seed for backfill, abandoned) revealed two things during its eval gate run:
1. The F051 eval-harness infrastructure could be wired to drive `GraphDensifier.run_backfill_cycle()` directly against the eval DB and snapshot edge/orphan deltas across configs.
2. The bigger F040 win wasn't candidate generation at all — it was relaxed CE thresholds (`baseline_loose_ce` produced +59.6% more edges than baseline at identical precision). That finding was only visible because the harness instrumented the cycle.

F053 ships the harness as standalone F040 ops infrastructure, separated from the abandoned F052 wedge. F054 will then use F053 to validate the selective CE-threshold relaxation hypothesis empirically before merge.

### What this is not

- Not a F040 algorithm change — `GraphDensifier`, `_backfill_same_type`, `_backfill_cross_type`, `discover_clusters` are untouched.
- Not a new retrieval metric — F051's `nous_eval.retrieval` matrix (MRR/P@K/R@K/nDCG) stays unchanged.
- Not a F052 resurrection — the abandoned multi-embedding wedge is NOT in this PR.

---

## Goals

1. **Reproducible density measurement** — `uv run python -m nous_eval.density_eval --configs <list>` runs each config against the F051 eval DB and emits a markdown report with edge counts, orphan counts, ce_pruned, wall time, and per-relation breakdown. Re-runnable on the same corpus.
2. **Eval-before-deploy gate for F040 tuning** — any PR that touches F040 thresholds, candidate caps, or content-length guards must demonstrate density delta + precision delta on the eval corpus before merge. F053 is the gate mechanism.
3. **LLM-judged edge-precision audit** — `uv run python -m nous_eval.run_edge_audit` samples N newly-created edges per relation type, fetches source/target content, asks Sonnet for YES/WEAK/NO verdicts, computes per-relation precision with PASS/FAIL/UNDERPOWERED gate markers (≥15 N-floor + ≥0.75 precision floor).
4. **Diagnostic baseline_loose_ce config out of the box** — ship one diagnostic `RetrievalConfig` (`baseline_loose_ce`) so future F040 tuning specs (F054, F055, ...) start with a working comparison point.

## Non-goals

- **No automatic gate enforcement at PR-time** — manual operator invocation, manual report attachment to PRs (matches F051's no-CI posture).
- **No per-edge embedding cost analysis** — wall time + ce_pruned counters only; finer instrumentation deferred.
- **No prompt-cache for edge_judge** — single Sonnet call per BATCH_SIZE=30 edges, no caching. Audit is rare (~once per F040 tuning PR).
- **No support for cross-corpus comparison** — each density_eval run is scoped to the F051 eval corpus's `agent_id`. Prod-corpus runs are out of scope (F051 already enforces "eval DB only at runtime").

## Deferred (with rationale)

- **F053.1 — Per-config edge_judge integration in `density_eval.py`.** Today the audit is a separate `run_edge_audit` invocation against whatever edges currently live in the eval DB (typically the LAST config that ran). A future enhancement: snapshot edges-per-config and audit each set independently. Useful when comparing >2 configs' precision side-by-side.
- **F053.2 — Cost telemetry per config** — track Haiku/Sonnet/embed token usage per config, surface in markdown report.
- **F053.3 — `--n-runs` averaging** — repeat each config N times, report mean ± stddev. Useful when configs invoke non-deterministic LLM calls (F052-style expansion); not needed for pure-threshold configs.

---

## Mechanism

### `nous_eval/density_eval.py` — the harness mode

```bash
uv run python -m nous_eval.density_eval --configs baseline,baseline_loose_ce
```

Per config (mirrors `nous_eval.retrieval_runner.run_matrix`):

1. `RuntimeConfig.reset()` — clear flag leakage from prior config.
2. `_apply_config_flags(template, cfg)` — Settings overlay (NOT `RuntimeConfig.set` — that's the F051 lesson).
3. `_settings_for_eval_db(eval_settings, overridden)` — redirect Settings DB connection to the eval DB.
4. `eval_scoped.model_copy(update={"graph_backfill_enabled": True})` — F051's `_settings_for_eval_db` forces this False to suppress prod handlers; density_eval invokes the densifier *directly* so we re-enable. Without this every config silently returns 0 edges.
5. `_ensure_zero_edge_baseline(db, agent_id)` — DELETE current edges; create persistent `brain.eval_baseline_edges_snapshot` table (REAL, not TEMP — survives connection-pool churn); TRUNCATE.
6. Snapshot pre-state — orphan + edge counts per type/relation.
7. Build Heart + densifier via `_build_densifier_for_eval(settings, db, agent_id)`. Raises loud if `OPENAI_API_KEY` is unset (no silent collapse to baseline-vs-baseline).
8. Run `densifier.run_backfill_cycle()` + `densifier.discover_clusters(max_bridges=20)`. On any exception: `_restore_baseline` → record failure string in `DensityRunResult.failure`.
9. Snapshot post-state.
10. `RuntimeConfig.reset()` for the next config.

Output: `reports/density-eval-<timestamp>.md` markdown table + per-config breakdown.

### `nous_eval/edge_judge.py` — the LLM judge

- Loads operator-editable prompt template from `nous_eval/templates/edge_precision_prompt.md`.
- Batches edges in chunks of `BATCH_SIZE=30`.
- Calls Sonnet via the existing OAT-supporting `AnthropicClient` with the **Claude Code preamble as system block 0** (without it, OAT returns 429 — per project memory).
- Parses JSON response with `json5`-style trailing-comma tolerance + code-fence stripping.
- Returns one `EdgeJudgment` per input edge in input order; uses `zip_longest` + `verdict="PARSE_ERROR"` padding when Sonnet returns short responses (max_tokens cutoff, ambiguous-skip).
- `max_tokens=8192` (empirically necessary — 2048 truncates mid-string with 30 edges of reasoning).

### `nous_eval/run_edge_audit.py` — the audit driver

```bash
uv run python -m nous_eval.run_edge_audit --since "$NOW" --limit-per-type 30
```

- Samples N edges per relation type from `brain.graph_edges` for the eval `agent_id`.
- Joins source + target content via the right per-type table (`heart.facts.content`, `brain.decisions.context`, `heart.episodes.summary`, `heart.procedures.name || ': ' || description`).
- Skips edges where either side has missing content (logs DEBUG; counts toward N).
- Calls `edge_judge.judge_edges` per relation; computes per-relation precision = `YES / (YES + WEAK + NO)`.
- Writes `reports/edge-audit-<timestamp>.md` with per-relation gate markers:
  - **PASS** = N ≥ 15 AND precision ≥ 0.75
  - **UNDERPOWERED** = N < 15 (excluded from gate verdict)
  - **FAIL** = N ≥ 15 AND precision < 0.75
- Spot-check section lists up to 10 NO + WEAK verdicts with the model's reasoning so operators can audit the audit.

### `baseline_loose_ce` diagnostic config

Lives in `nous_eval/retrieval.py::_DEFAULT_CONFIGS`. Six-knob CE-threshold relaxation:

| threshold | strict | loose |
|---|---|---|
| `ce_backfill_threshold_fact_fact` | 0.65 | 0.55 |
| `ce_backfill_threshold_decision_decision` | 0.60 | 0.50 |
| `ce_backfill_threshold_fact_decision` | 0.55 | 0.45 |
| `ce_backfill_threshold_fact_episode` | 0.55 | 0.45 |
| `ce_backfill_threshold_episode_episode` | 0.58 | 0.50 |
| `ce_backfill_threshold_procedure_any` | 0.55 | 0.45 |

**Note**: this is a *diagnostic* config, not a recommended deployment. The 2026-04-26 audit on the F051 eval corpus showed `evidence_for` precision regresses from 0.57 → 0.47 with this blanket relaxation. F054 will propose a **selective** relaxation (loosen same-type only).

---

## Code surface

### Files added
| File | LOC | Purpose |
|---|---|---|
| `nous_eval/density_eval.py` | ~490 | F053 harness mode CLI + per-config snapshot/run/restore loop + markdown writer |
| `nous_eval/edge_judge.py` | ~190 | Sonnet edge-precision judge with OAT preamble + JSON tolerance |
| `nous_eval/run_edge_audit.py` | ~290 | Audit CLI driver + per-relation precision report |
| `nous_eval/templates/edge_precision_prompt.md` | ~30 | Operator-editable judge prompt |

### Files modified
| File | LOC change | Change |
|---|---|---|
| `nous_eval/retrieval_runner.py` | +44 | Add `_build_densifier_for_eval(settings, db, agent_id)` helper |
| `nous_eval/retrieval.py` | +25 | Add `baseline_loose_ce` diagnostic `RetrievalConfig` |
| `docs/features/INDEX.md` | +1 row | F053 entry (added when impl ships) |

### Files NOT touched
- `nous/` — no production code changes. F040/F042/F043/F045 algorithms unchanged.
- `nous_eval/retrieval_runner.py::run_matrix` — F051 retrieval harness unchanged.
- `sql/migrations/` — no schema. The `brain.eval_baseline_edges_snapshot` table is created idempotently by the harness itself.

**Total**: ~1070 LOC (mostly new module code, minimal touch on existing files).

---

## Tests

This is operator tooling, not production code. Per F051's no-CI posture, F053 ships with smoke-test invocations rather than a unit test suite:

- `uv run python -c "from nous_eval.density_eval import main; main(['--help'])"` — CLI parses cleanly.
- `uv run python -c "from nous_eval.edge_judge import judge_edges; print('OK')"` — module imports.
- Live smoke validated 2026-04-26 against `nous_eval_scratch` DB:
  - `density_eval --configs baseline` → 240 edges, 105.8s wall.
  - `density_eval --configs baseline,baseline_loose_ce,...` → 6-config matrix completed.
  - `run_edge_audit --limit-per-type 30` → precision table for 3 relations.

Future regression-prevention tests can land as F053.1 alongside the per-config edge_judge integration.

---

## Risks

1. **Mutates the eval DB.** Each config DELETEs all edges before running. The snapshot table is empty by design (anchor for restore-on-crash), so a crash leaves zero-edge state. Operators must understand this is *not* a read-only harness — it modifies graph state.
2. **OAT rate limits during edge_judge.** Heavy concurrent Sonnet usage on a single OAT can 429. The `Claude Code` preamble as system block 0 mitigates per project memory, but back-to-back audit runs may still hit limits.
3. **JSON parser tolerance window.** The audit uses surgical regex to strip code fences + trailing commas before strict `json.loads`. Pathological Sonnet responses (deeply nested unquoted keys, unicode escapes) could still fail with `verdict="PARSE_ERROR"`. Logged as WARN; doesn't crash the audit.
4. **`max_tokens=8192` cap.** 30 edges × ~250 chars reasoning = ~7500 chars ≈ 1900 tokens. Comfortable margin, but extreme verbosity could still truncate. Mitigated by zip_longest padding (truncated tail gets `PARSE_ERROR`).
5. **Embedder requirement.** `_build_densifier_for_eval` raises `RuntimeError` if `OPENAI_API_KEY` is unset. This is intentional (silent collapse to baseline-vs-baseline would invalidate any A/B), but operators must export the key before running.

---

## Rollout

| Phase | Trigger | Action |
|---|---|---|
| **0 — Spec lock (this doc)** | 3-agent review optional (low-risk operator tooling, no production code change). Plan-author judgment call. | Spec frozen. |
| **1 — Ship as F053 PR** | Branch `feat/F053-density-ops-harness` with the files in §Code surface. Default-on (no flag — these are ad-hoc CLIs, not request-path code). | PR opens; merge after smoke-test confirmation. |
| **2 — F054 spec uses F053** | F053 merged | F054 (selective CE-threshold relaxation) declares `Depends on: F053` and uses `density_eval` + `run_edge_audit` as its gate mechanism. |
| **3 — Future tuning** | Any F040 PR | New convention: F040 changes touching thresholds, caps, or content guards must run `density_eval` + `run_edge_audit` against the eval corpus, attach reports to PR, demonstrate density Δ + precision Δ before merge. |

---

## Provenance

This module was built during F052 implementation (PR #350) and validated by surfacing F052's negative result on 2026-04-26 (multi-embedding seed produced 0% density lift). When F052 was abandoned per its `§Rollout 3b`, the harness code was salvaged into this F053 spec because it's reusable infrastructure that paid for itself by enabling the abandonment decision.

Concretely:
- The diagnostic matrix (baseline / baseline_loose_ce / f052_on / f052_on_low_minwords / f052_on_loose_ce / f052_on_combo) revealed that F040's CE-mode cosine thresholds were the binding constraint, NOT candidate generation. This insight directly motivates F054.
- The edge_judge precision audit revealed asymmetry between same-type relations (`related_to` precision 0.83 stable under loose CE) and cross-type relations (`evidence_for` precision degrades 0.57 → 0.47 under loose CE). This shapes F054's *selective* relaxation design.
- Three SFH-grade bugs were caught and fixed during the live gate run: `SdkAnthropicClient.call()` payload-dict signature, OAT Claude-Code-preamble requirement, and JSON5 trailing-comma tolerance. Future Sonnet-judge code can reuse these patterns.

The F052 spec stays at `docs/features/F052-multi-embedding-backfill-seed.md` as the abandonment decision record.
