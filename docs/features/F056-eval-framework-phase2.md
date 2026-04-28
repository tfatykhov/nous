# F056 — Eval Framework Phase 2: Handler-Level Evals

**Status:** 📝 Draft v2 (2026-04-28, post-3-agent-review)
**Proposed by:** Tim
**Date:** 2026-04-27
**Depends on:** F051 (retrieval eval harness — shipped), F051.4 (multi-turn replay — shipped), F051 Phase 1 finish (eval_runs to eval DB + regression CLI — PR #368 shipped)
**Blocks:** F051 Phase 3 adoption of `nous-longMemEval` (gated on Phase 2 stabilizing the write-path signal first)
**Related issues:** #367 (Phase 2 expansion meta-issue), #365 (F042 CE regression — discovered by F051.4), #366 (F055 neutralization — discovered by F051.4)

**Review history:**
- v1 (2026-04-27): initial draft, surfaced via PR #370
- v2 (2026-04-28): amended after 3-agent review (architect / devil / python-pro). All P1 entry-point names, regression-CLI extensibility, LLM determinism, tear-down ordering, and cost numbers verified against the actual code.

---

## Problem

The F051 framework (retrieval IR metrics) plus F051.5 ingest (LongMemEval write-path partial coverage) cannot answer:

1. **Is admission control (F023) admitting the right facts and rejecting the wrong ones?** The bug that caused 999/1000 candidate facts to be silently rejected during F051.5 ingest (issue #354 + sibling fix in PR #363) shipped because nothing measured admission F1.
2. **Is dedup catching paraphrases?** The hybrid-search RRF-vs-cosine pre-check that broke ingest (PR #364) shipped because no test measured paraphrase recall against an existing fact corpus.
3. **Is sleep-cycle graph backfill (F040 / F043 / F045 / F054) actually densifying the graph or just adding noise?** F053 added density-eval as ops tooling but it requires manual interpretation; we want a regression-gated pass/fail.
4. **Are episode summaries capturing the right key points?** No measurement exists; LLM-judge against ground-truth would catch silent quality drops.

Today every regression in these areas is found *in production*, weeks after merge, by users noticing memory misbehavior. Phase 2 closes the feedback loop with mechanical metrics that fail CI.

### What this is not

- **Not an end-to-end agent task eval.** That's Phase 3, addressed by adopting `nous-longMemEval` (separate repo at `E:\Projects\nous-longMemEval`, already-built 8-stage pipeline with GPT-4o judge).
- **Not retrieval quality eval** — that's Phase 1 (F051 / F051.4).
- **Not exhaustive coverage of every handler.** Phase 2 covers the 4 handlers where regressions have actually bitten us in the last 6 months. Calibration eval, latency-regression, and per-tool eval are deferred.

---

## Goals

1. **Per-handler mechanical regression gate.** Each handler eval exits non-zero when its primary metric drops beyond a per-handler threshold vs the most recent baseline in `nous_system.eval_runs`.
2. **Reuse F051 infrastructure** — but extend `regression.py` + `run_history.py` to support per-handler metric schemas (today both are hard-coded to retrieval metrics; see §"Phase 1 infra extensions" below).
3. **Deterministic fixtures + deterministic judges.** JSONL fixtures in `tests/fixtures/handlers/` with version-pinned content. All LLM-judge calls use `temperature=0` (Haiku does not yet expose `seed`, so prompt + temp is the determinism floor).
4. **Same DB-isolation discipline as F051.** Handler evals run against the eval DB. Each handler eval scopes its writes to a dedicated `agent_id` (e.g. `nous-eval-handler-admission`) so re-runs are idempotent without cross-handler interference.
5. **One handler per PR.** Avoid the F048 mega-PR pattern. Sequence: admission → dedup → backfill → summary.

## Non-goals

- **No CI integration in this spec.** Solo-dev regression gates run locally before PR creation, identical to F051's stance. Weekly cron is a F056.1 follow-up.
- **No new REST endpoints, no dashboard tab.** Phase 2 is CLI-only.
- **No changes to production handler code.** Each handler eval is purely an external observer — feed input → run handler unchanged → measure output.
- **No new schema migrations.** Reuses `nous_system.eval_runs` with `harness=<handler-name>` tag. Schema is JSONB-shape-agnostic; only the *reader* (`regression.py`) needs extension.

---

## Design

### Phase 1 infra extensions (PRE-handler PR — must land first)

The 3-agent review surfaced that `nous_eval/regression.py` has two hard-coded constraints that block Phase 2:

1. `_TRACKED_METRICS` (`regression.py:48-53`) only knows `mrr/r_at_10/p_at_1/ndcg_at_10`. Handler metrics (`admission_f1`, `dedup_f1`, `orphan_resolution_rate`, `edge_precision`, `summary_quality`) would silently get `0.0` and never gate.
2. `--harness` is `choices=["retrieval", "multi_turn_eval"]` (`regression.py:267`). Adding handlers requires a per-PR edit, plus the metrics-comparison path still won't recognize handler metrics.

**Fix (lands as PR #0 of the F056 sequence):**

- Replace `--harness` `choices=` with free-form `str` (validated only as "non-empty"; existing rows in `eval_runs` define the valid set).
- Replace `_TRACKED_METRICS` constant with a `_PRIMARY_METRIC_BY_HARNESS` dict registry:
  ```python
  _PRIMARY_METRIC_BY_HARNESS = {
      "retrieval": "mrr",
      "multi_turn_eval": "mrr",
      "admission": "admission_f1",
      "dedup": "dedup_f1",
      "backfill": "edge_precision",
      "summary": "summary_quality",
  }
  _ALL_REPORTED_METRICS_BY_HARNESS = {  # what to display in the report (informational deltas)
      "retrieval": ("mrr", "r_at_10", "p_at_1", "ndcg_at_10"),
      "admission": ("admission_f1", "admission_precision", "admission_recall"),
      ...
  }
  ```
- `_compare_bucket` reads `primary_metric` from the registry by `harness`, not from a single `--primary-metric` CLI override.
- The `--primary-metric` CLI override still exists but defaults to "auto" (use registry).

This PR also adds 5-7 unit tests in `tests/eval/test_regression.py` covering the registry lookup + missing-metric fallback behavior.

LOC estimate: ~80 LOC change in `regression.py` + ~120 LOC tests.

### Architecture

```
nous_eval/
├── handlers/                           # NEW
│   ├── __init__.py
│   ├── _cli_base.py                    # NEW — shared argparse + run_handler_eval(name, async_fn) wrapper
│   ├── _jsonl.py                       # NEW — generic _load_jsonl(path, model_cls) helper
│   ├── _models.py                      # NEW — pydantic BaseModels: AdmissionRow, DedupPair, BackfillEntity, SummaryRow
│   ├── admission.py                    # `python -m nous_eval.handlers.admission`
│   ├── dedup.py                        # `python -m nous_eval.handlers.dedup`
│   ├── backfill.py                     # `python -m nous_eval.handlers.backfill` (reuses density_eval.py snapshot logic)
│   └── summary.py                      # `python -m nous_eval.handlers.summary`
├── density_eval.py                     # F053 — exists; backfill handler reuses _snapshot_density() etc.
├── run_history.py                      # Phase 1 finish — reused as-is (JSONB schema is shape-agnostic)
├── regression.py                       # Phase 1 finish — extended per §"Phase 1 infra extensions" above
└── ... (existing F051/F051.4 modules)

tests/eval/handlers/                    # NEW (note: tests/eval/handlers/, NOT tests/handlers/ — matches existing tests/eval/ convention)
├── conftest.py                         # mini-fixture literals (5-row inline dicts) per handler
├── test_admission.py                   # ~12 unit tests
├── test_dedup.py                       # ~14 unit tests
├── test_backfill.py                    # ~16 unit tests
└── test_summary.py                     # ~12 unit tests

tests/integration/
├── test_handler_eval_admission.py      # 1 end-to-end test per handler against test DB
├── test_handler_eval_dedup.py
├── test_handler_eval_backfill.py
└── test_handler_eval_summary.py

tests/fixtures/handlers/                # NEW
├── admission_labeled.jsonl             # ~50 facts with admit/reject labels + reviewed_by field
├── dedup_paraphrases.jsonl             # ~30 (anchor, paraphrase) pairs + reviewed_by field
├── backfill_corpus.jsonl               # ~100 facts/decisions/episodes seed
└── summary_transcripts.jsonl           # ~80 transcripts with gold key-points (raised from 20, see §D below)
```

### Per-handler eval lifecycle (shared across all 4)

Every handler CLI follows the same shape, codified in `nous_eval/handlers/_cli_base.py::run_handler_eval(name, async_fn)`:

1. Parse args (shared `--log-level`, `--report-only`, `--no-history`, `--threshold`, `--notes`, `--fixture-path`).
2. Build `EvalSettings()` + `Settings()`. Apply per-handler Settings overrides explicitly (e.g. `admission_shadow_mode=False`, `admission_control_enabled=True`, `graph_backfill_enabled=True`).
3. Use a dedicated per-handler `agent_id` (`nous-eval-handler-<name>`) so writes are idempotent and isolated from other handlers.
4. Build the eval DB connection via `_settings_for_eval_db(eval_settings, main_overridden_settings)` → `Database(...)`.
5. Inside `async with _build_heart_for_eval(eval_db, eval_scoped) as heart`: load fixture → invoke production handler → measure → compute primary metric.
6. **Exit `async with` block (this disposes the asyncpg pool).** ONLY THEN: optional truncate of handler-specific `agent_id` rows for next-run hygiene. The `pg_try_advisory_xact_lock` pattern from F049 working_memory sweep keyed on a SHA-256 hash of `<handler-name>:<agent_id>` serializes concurrent CLI invocations.
7. Write markdown report under `Path(eval_settings.report_dir) / "handlers" / f"{name}-<timestamp>.md"`.
8. Persist to `eval_runs` via `persist_run_history(harness=name, ...)`.
9. Exit 0 on pass / 4 on regression (matching `regression.py` exit code).

Order matters: step 6 (engine dispose) MUST precede any TRUNCATE, or asyncpg connections will deadlock against the lock TRUNCATE acquires.

### Per-handler specs

#### A. Admission control (`handlers/admission.py`)

**Production code under test:** `nous/heart/admission.py::AdmissionController.score()` (line 135) plus `nous/heart/heart.py::Heart.learn()` (line 282) admit/reject decision logic.

**Fixture:** `tests/fixtures/handlers/admission_labeled.jsonl` — 50 candidate facts:
- 25 positive (high-quality, novel, well-grounded — should admit)
- 25 negative (vague, redundant, ungrounded — should reject)
- Each row: `{content, subject, category, source_text, label: "admit"|"reject", rationale, reviewed_by}`
- `reviewed_by`-pattern matches `qrels_loader.py:80-85`. Hand-labeled fixtures have `reviewed_by="tim"`. AI-drafted-only rows are flagged unreviewed and skipped from the gate unless `--include-unreviewed` is passed (mirrors `retrieval.py:301`).

**Procedure:**
1. Build Settings with explicit overrides: `admission_control_enabled=True`, `admission_shadow_mode=False`. (Note: `admission_shadow_mode` defaults to **True** in production per `nous/config.py:413` — without this override, every fact admits and the gate is useless.)
2. For each candidate: call `Heart.learn(FactInput(content=row.content, subject=row.subject, category=row.category, source="admission_eval", source_text=row.source_text))`.
3. Determine outcome **purely from the return type**: `isinstance(result, FactRejected)` → rejected; else → admitted. Do NOT check `active=true` — that flag is overloaded with supersession (`nous/heart/facts.py:489, 527`).
4. Compare `(rejected XOR label=="admit")` against the gold label.

**Metrics (all persisted to `eval_runs`):**
- `admission_f1` (primary, gated)
- `admission_precision` (informational)
- `admission_recall` (informational)
- `confusion_matrix: {tp, fp, tn, fn}` (informational, supports debugging)

**Gating threshold:** `admission_f1` drop > 5pp → regression.

**Why F1 (not accuracy):** label balance can drift; F1 catches asymmetric regressions (admitting too many junk facts vs rejecting good ones).

**Fixture size justification:** N=50 is small but stratified; per-class N=25 puts the 95% Wilson interval at ±18% on F1 in the unpaired case. Because regression.py uses paired comparison (same fixture rows latest vs baseline), the effective sensitivity is dramatically tighter — comfortably catches 5pp regressions. Bumping to N=200 is F056.1.

**LOC est:** ~280 (eval CLI ~150 + helpers ~80 + fixture seeder ~50). Tests: ~12.

---

#### B. Dedup (`handlers/dedup.py`)

**Production code under test:** Two distinct dedup legs that both ship today and both have shipped bugs in the last quarter:
- **Leg 1 (hybrid-search pre-check):** `nous/handlers/fact_extractor.py:160-167` (`_dedup_via_search` flag) — RRF-scored search before learn.
- **Leg 2 (native cosine):** `nous/heart/facts.py::FactManager.learn` cosine `>0.95` check inside `Heart.learn`.

The eval measures **both legs separately** so a regression in either is attributable.

**Fixture:** `tests/fixtures/handlers/dedup_paraphrases.jsonl` — 30 paraphrase pairs:
- 20 should dedup (semantic paraphrases), 10 should be distinct (similar wording, different meaning).
- Each row: `{anchor, paraphrase, expected: "dedup"|"distinct", reviewed_by}`.
- Example dedup pair: `{"anchor": "Tim prefers FastAPI for new services", "paraphrase": "For new microservices, Tim's choice is FastAPI", "expected": "dedup", "reviewed_by": "tim"}`
- Example distinct pair: `{"anchor": "PostgreSQL 17 is the production database", "paraphrase": "PostgreSQL 17 was deprecated last quarter", "expected": "distinct", "reviewed_by": "tim"}`

**Procedure for Leg 1 (hybrid-search):**
1. Seed eval DB with 50 unrelated background facts under handler-scoped `agent_id` (so search isn't returning trivially-empty results).
2. For each pair: insert anchor via `Heart.learn` → call `Heart.learn(FactInput(content=paraphrase))` directly (NOT `extract_and_store`, which adds an LLM call to the metric path and silently caps at 5 candidates per `fact_extractor.py:151`).
3. Read result.id; if it equals anchor_id → dedup fired (Leg 1 caught it). If new id → no dedup.

**Procedure for Leg 2 (native cosine):**
1. Same seed.
2. For each pair: insert anchor via `Heart.learn` → call `Heart.learn(FactInput(content=paraphrase))` with `dedup_via_search=False` setting (to bypass Leg 1 and isolate Leg 2's behavior).
3. Same read-back.

**Metrics:**
- `dedup_f1_leg1`, `dedup_precision_leg1`, `dedup_recall_leg1` (Leg 1 hybrid-search)
- `dedup_f1_leg2`, `dedup_precision_leg2`, `dedup_recall_leg2` (Leg 2 native cosine)
- `dedup_f1` = mean(leg1_f1, leg2_f1) (primary, gated)

**Gating threshold:** `dedup_f1` drop > 5pp → regression. Either leg dropping >10pp also fires (early warning).

**Why both legs:** PR #364 lowered Leg 1 precision specifically (the over-dedup against near-empty corpus). A combined-only metric wouldn't have caught it.

**LOC est:** ~320 (eval CLI ~180, helpers ~80, fixture seeder ~60). Tests: ~14.

---

#### C. Graph backfill (`handlers/backfill.py`)

**Production code under test:** `nous/brain/graph_densifier.py::GraphDensifier.run_backfill_cycle()` (line 534) — called by sleep handler at `sleep_handler.py:892`. Exercises F040 backfill + F043 cross-encoder gate + F045 CE-aware thresholds + F054 selective relaxation, end-to-end.

**Fixture:** `tests/fixtures/handlers/backfill_corpus.jsonl` — 100 mixed entities:
- 60 facts, 25 decisions, 10 episodes, 5 procedures.
- Designed so ~30% are intentional orphans (no graph edges) — gives the backfill something to do.
- Content drawn from a real Nous dev-loop transcript snippet (deterministic, sanitized). `reviewed_by="tim"` on the orphan-vs-non-orphan classification.

**Procedure:**
1. Build Settings with `graph_backfill_enabled=True` + current F045/F054 thresholds. (`graph_backfill_enabled` defaults True in prod, but per-PR review noted it's gated at 4 sites in `graph_densifier.py:429,456,483,510` — explicit override removes ambiguity.)
2. Truncate eval DB graph tables under handler `agent_id`; load fixture (entities + their existing edges).
3. Snapshot initial state via existing `nous_eval/density_eval.py::_snapshot_density()` — DO NOT re-implement this; F053 already shipped it.
4. Construct `GraphDensifier(embedding_provider, db, agent_id, settings)` — embedding provider built same way as `_build_heart_for_eval`. DB MUST come from `_settings_for_eval_db` (the eval DB), not a raw `Database(Settings())` (which would target main DB).
5. Call `await densifier.run_backfill_cycle()`.
6. Snapshot final state via the same density_eval helper.
7. Compute deltas: new edges per relation type, orphans resolved, cross-type vs same-type breakdown.
8. **Sample 20 new edges** (deterministic seed via `random.Random(42).sample(...)`) → call Haiku LLM-judge with `temperature=0` to score each (`semantically_related: true|false|borderline`).

**Metrics:**
- `density_delta` (new edges total) — informational, no gate.
- `orphan_resolution_rate` = `orphans_resolved / initial_orphans` — informational.
- `edge_precision` (LLM-judged "true" share of sampled new edges) — primary, gated.

**Gating threshold:** `edge_precision` drop > 10pp → regression. (Single-metric gate; `orphan_resolution_rate` is informational because the F052 failure pattern was high resolution at low precision — precision IS the right gate.)

**LLM-judge cost (corrected after review):** 20 edges × Haiku at ~2 entities of content per edge (~1500 input tokens) + ~50 output tokens. Haiku 4.5: ~$1/MTok in, ~$5/MTok out. → 20 × 1500 = 30K input tokens ≈ $0.03; 20 × 50 = 1K output ≈ $0.005. Total: **~$0.035/run** (was claimed $0.005 in v1; off by 7×).

**LOC est:** ~420 (eval CLI ~200 + LLM judge wrapper ~80 + density_eval reuse glue ~60 + fixture seeder ~80). Tests: ~16.

---

#### D. Episode summary (`handlers/summary.py`)

**Production code under test:** `nous/handlers/episode_summarizer.py::EpisodeSummarizer.summarize_episode()` (line 104). Real signature: `summarize_episode(self, episode_id: UUID, transcript: str, agent_id: str | None = None) -> dict | None` — note `episode_id` is required and an `Episode` row must exist (line 126 early-returns if not found OR if already summarized).

**Fixture:** `tests/fixtures/handlers/summary_transcripts.jsonl` — **80 transcripts** (raised from v1's 20 — see §"Why N=80" below):
- Each row: `{transcript: "<300-1500 word conversation>", gold_key_points: ["...", "...", ...], gold_summary_themes: ["..."], reviewed_by}`
- Transcripts cover the 6 LongMemEval question types (knowledge-update, multi-session, single-session-{user,assistant,preference}, temporal-reasoning).
- Gold key-points are 3-7 short factual claims the summary MUST surface.
- Mixed provenance: AI-drafted gold key-points reviewed by Tim → `reviewed_by="tim+ai-draft"` (matches `qrels_loader.py:80-85` reviewed_by pattern; gate-eligible only when `reviewed_by` is set).

**Procedure:**
1. For each transcript row: INSERT a stub `Episode` row into `heart.episodes` under handler `agent_id` (this is the seeding step v1 omitted). Capture the new `episode_id`.
2. Call `await summarizer.summarize_episode(episode_id=new_id, transcript=row.transcript, agent_id=eval_agent_id)`.
3. **Handle `None` return path explicitly**: if returned, the summarizer skipped this transcript (already-summarized or LLM error) — count as a `null_returns` informational metric, not a failure.
4. For non-None returns, call Haiku LLM-judge with `temperature=0` twice per row:
   - `key_point_coverage`: of N gold key-points, how many appear in the produced `key_points` list (sub-string OR semantic match)? Returns 0..1.
   - `summary_faithfulness`: does the produced `summary` contain any claim NOT supported by the transcript? Returns 0..1.

**Metrics:**
- `summary_quality` = `mean(key_point_coverage)` × `mean(summary_faithfulness)` (primary, gated)
- `mean_key_point_coverage` (informational)
- `mean_summary_faithfulness` (informational)
- `null_returns` count (informational; non-zero values warrant investigation but don't gate)

**Gating threshold:** `summary_quality` drop > 5pp → regression. Sensitivity justified by N=80 + paired comparison (see below).

**Why N=80 (raised from v1's N=20):** Reviewer's empirical math: Wilson 95% CI for baseline 0.85 at N=20 is ~30pp wide — a 5pp gate sits inside the noise floor and would either thrash with false positives or never fire. At N=80 the Wilson CI tightens to ~16pp; with paired comparison (`regression.py` uses same fixture rows latest vs baseline), the effective sensitivity comfortably catches 5pp drift. N=80 also gives ~13 transcripts per LongMemEval question type, supporting per-type informational breakdown.

**LLM-judge cost (corrected after review):** 80 transcripts × 2 judge calls × Haiku. Each judge call carries the full transcript (300-1500 words ≈ 1500 input tokens) + produced summary (~200 tokens) + prompt (~200 tokens) = ~2K input tokens, ~50 output. Total: 160 calls × 2K = 320K input ≈ $0.32; 160 × 50 = 8K output ≈ $0.04. **~$0.36/run** (was claimed $0.02 in v1; off by 18× — the cost driver is N going up + transcript size in input being non-trivial).

This bumps Phase 2 total eval cost to **~$0.40/run** — still cheap for a weekly cron, but materially different from v1's "$0.025/run" claim.

**LOC est:** ~360 (eval CLI ~180 + Episode-row seeder ~60 + judge wrapper ~80 + fixture seeder ~40). Tests: ~12.

---

### Wiring into existing infra (corrected after review)

1. **`run_history.py`** — reused as-is. JSONB `metrics` column accepts arbitrary handler-specific shapes (`sql/migrations/037_eval_runs.sql:18` confirmed shape-agnostic).

2. **`regression.py`** — extended in PR #0 of the F056 sequence (see §"Phase 1 infra extensions" above). After that PR, each handler-impl PR is genuinely a one-line addition to `_PRIMARY_METRIC_BY_HARNESS` + `_ALL_REPORTED_METRICS_BY_HARNESS`.

3. **`_settings_for_eval_db`** — reused as-is.

4. **`_build_heart_for_eval` (`retrieval_runner.py:303-382`)** — reused as-is for handlers A, B, D. For handler C (backfill), `GraphDensifier` is constructed AFTER the `async with _build_heart_for_eval` opens, sharing the heart's embedding provider + the same eval DB.

5. **`density_eval.py` (F053)** — reused for backfill snapshot/delta. The backfill handler imports `_snapshot_density()` directly; do NOT re-implement.

### LLM client injection (matches existing pattern)

Handlers C (backfill) and D (summary) call Haiku for judging. Following the existing pattern from `EpisodeSummarizer.__init__` and `FactExtractor.__init__`:

```python
async def run_handler_eval(
    eval_settings: EvalSettings,
    *,
    llm_client: LLMClient | None = None,  # injected for tests; otherwise built from settings
    fixture_path: Path,
    ...
) -> int:
    if llm_client is None:
        from nous.api.anthropic_client import create_client
        llm_client = create_client(main_settings)
        await llm_client.start()
    try:
        # ... handler-specific logic
    finally:
        await llm_client.aclose()
```

Tests inject a `FakeJudge` (mirroring `nous-longMemEval` pattern). Production runs use `HttpxAnthropicClient` (default per `create_client`).

### Determinism (mandatory)

- **Every Haiku judge call:** `temperature=0`. Haiku 4.5 does not yet expose `seed`; document this explicitly in the spec so future versions can lock seed when supported.
- **Sampling for backfill 20-edge LLM-judge:** seeded `random.Random(42).sample(...)` over the new-edges list. Re-runs against identical fixture+code MUST produce byte-identical sample.
- **Fixture loaders:** sort rows by a stable key (`row_id` field added to all fixtures). Avoids order-dependent admission/dedup interactions.
- **Without these guarantees:** N=20 fixture sizes (admission, dedup) would generate 1-2 false-positive regressions per week per handler in a weekly cron.

### Test coverage

Per project convention (matching F051 plan §D): each handler ships with:
- ~12-16 unit tests in `tests/eval/handlers/test_<name>.py` (note: under `tests/eval/handlers/`, NOT `tests/handlers/` — matches existing `tests/eval/` structure and avoids confusion with production `nous/handlers/` package).
- 1 integration test in `tests/integration/test_handler_eval_<name>.py` running end-to-end against the eval test DB on a 5-row mini-fixture defined as a literal in `tests/eval/handlers/conftest.py` (no external JSONL file — keeps tests hermetic).

Total Phase 2 test count target: **54 tests** across 4 PRs (12 + 14 + 16 + 12 — corrected from v1's inconsistent "~80 / table=54" mismatch).

---

## Sequencing & cost (corrected after review)

| PR | Handler | LOC est | Tests | Eval cost/run |
|---|---|---|---|---|
| 0 | regression.py + run_history.py extensions | ~80 + ~120 tests | ~7 | $0 |
| 1 | admission | ~280 | ~12 | $0 |
| 2 | dedup | ~320 | ~14 | $0 |
| 3 | backfill | ~420 | ~16 | $0.035 (Haiku) |
| 4 | summary | ~360 | ~12 | $0.36 (Haiku) |
| **Total** | — | **~1460** | **~61** | **~$0.40/run** |

Each handler PR (1-4) follows the project's standard cycle: spec section above → 3-agent review → impl → review → merge.

Estimated wall time: **2-3 weeks** at 1 PR / 3 days assuming review iteration. PR #0 is a 1-day prerequisite that unblocks 1-4.

---

## Closed questions (was open in v1)

1. **Backfill eval running F040's full sleep-cycle prerequisites?** **No, isolation only.** Correction extraction and supersession are independent handlers — testing them inside backfill violates the "purely external observer, no monkeypatching" principle. Each handler eval exercises its own code path; cross-handler interactions are out of scope for Phase 2.

2. **Fixture provenance for gold labels?** **Use the `reviewed_by` field pattern from `qrels_loader.py:80-85`.** Tim hand-labels admission + dedup (`reviewed_by="tim"`), AI-drafts the summary gold key-points then Tim reviews (`reviewed_by="tim+ai-draft"`). Backfill orphan classification: Tim reviews (`reviewed_by="tim"`). All gate-eligible by default; `--include-unreviewed` allows AI-only rows during fixture iteration but never gates merges. Mirrors `retrieval.py:301`.

3. **CI integration (weekly cron)?** **Defer to F056.1 follow-up issue.** File the F056.1 stub when Phase 2 PR #1 (admission) merges, so we have actual run data to calibrate the cron threshold against.

---

## Rollout

**Phase A (this spec):** PR #0 (regression.py extensions) → 4 handler PRs in order.
**Phase B (F056.1):** CI integration — weekly cron runs all four + retrieval eval, posts regression report to GitHub issue if any gate fails. File F056.1 stub when handler #1 merges.
**Phase C (F056.2):** 5th handler eval covering cognitive-layer frame selection precision. After Phase A stabilizes (3-month observation window).

If Phase A completes cleanly, Phase 3 (`nous-longMemEval` adoption) gets unblocked — we then have BOTH fast white-box write-path coverage AND slow black-box agent-task coverage.

---

## Appendix: 3-agent review summary (for PR conversation)

v2 of this spec amends the following from v1, all empirically verified:

- **Wrong production entry points:** `AdmissionScorer` → `AdmissionController`; `nous/sleep/graph_backfill.py::GraphBackfillRunner.run_one_pass()` → `nous/brain/graph_densifier.py::GraphDensifier.run_backfill_cycle()`; `summarize_episode(transcript=...)` → real signature with required `episode_id` and `dict | None` return.
- **Missing Settings overrides:** `admission_shadow_mode=False` (defaults to True in prod); `graph_backfill_enabled=True` explicit (gated at 4 sites).
- **Wrong admit signal:** `active=true` is overloaded with supersession — must use `isinstance(result, FactRejected)` only.
- **Missing Episode-row seeding:** summarize_episode requires a pre-existing `Episode` row.
- **Cost numbers off by 7-18×:** corrected backfill ($0.005 → $0.035), summary ($0.02 → $0.36), total ($0.025 → $0.40).
- **regression.py "one-line edit" claim was false:** PR #0 added to sequence; introduces per-handler primary-metric registry.
- **Tear-down ordering risk:** TRUNCATE before engine dispose deadlocks against densifier pool. Fixed by mandating step ordering (engine dispose THEN truncate, with `pg_try_advisory_xact_lock` for concurrent-CLI safety).
- **LLM-judge non-determinism:** mandated `temperature=0` everywhere; documented Haiku-no-seed limitation.
- **N=20 summary fixture statistically inadequate:** raised to N=80; cost recalculated.
- **Dedup eval was routing through extract_and_store** (LLM in metric path) — corrected to call `Heart.learn` directly; both legs measured separately.
- **Test layout:** `tests/handlers/` → `tests/eval/handlers/` (matches existing convention; avoids confusion with production `nous/handlers/`).
- **Closed open questions 1-3** with rationale.
- **Reused F053 `density_eval.py`** instead of re-implementing.
- **Added `_cli_base.py`, `_jsonl.py`, `_models.py`** shared helpers (~80 LOC saved across 4 handlers; prevents drift like F051.4 dropping `--log-level`).
