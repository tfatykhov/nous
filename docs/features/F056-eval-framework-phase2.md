# F056 — Eval Framework Phase 2: Handler-Level Evals

**Status:** 📝 Draft (2026-04-27)
**Proposed by:** Tim
**Date:** 2026-04-27
**Depends on:** F051 (retrieval eval harness — shipped), F051.4 (multi-turn replay — shipped), F051 Phase 1 finish (eval_runs to eval DB + regression CLI — PR #368 shipped)
**Blocks:** F051 Phase 3 adoption of `nous-longMemEval` (gated on Phase 2 stabilizing the write-path signal first)
**Related issues:** #367 (Phase 2 expansion meta-issue), #365 (F042 CE regression — discovered by F051.4), #366 (F055 neutralization — discovered by F051.4)

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
2. **Reuse F051 infrastructure.** New CLIs live under `nous_eval/handlers/`, share `EvalSettings`, `_settings_for_eval_db`, `persist_run_history`, and `regression.py`.
3. **Deterministic fixtures.** Every handler eval ships a JSONL fixture in `tests/fixtures/handlers/` with version-pinned content. No live-LLM calls in the metric path (LLM-judge eval excepted; see §summary).
4. **Same DB-isolation discipline as F051.** Handler evals run against the eval DB (`nous_eval_scratch`); they MAY mutate it for the duration of one run, but each run starts from a clean schema slice (per-handler tear-down).
5. **One handler per PR.** Avoid the F048 mega-PR pattern. Sequence: admission → dedup → backfill → summary.

## Non-goals

- **No CI integration in this spec.** Solo-dev regression gates run locally before PR creation, identical to F051's stance. Weekly cron is a F056.1 follow-up.
- **No new REST endpoints, no dashboard tab.** Phase 2 is CLI-only.
- **No changes to production handler code.** Each handler eval is purely an external observer — feed input → run handler unchanged → measure output.
- **No new schema migrations.** Reuses `nous_system.eval_runs` with `harness=<handler-name>` tag.

---

## Design

### Architecture

```
nous_eval/
├── handlers/                           # NEW
│   ├── __init__.py
│   ├── admission.py                    # `python -m nous_eval.handlers.admission`
│   ├── dedup.py                        # `python -m nous_eval.handlers.dedup`
│   ├── backfill.py                     # `python -m nous_eval.handlers.backfill`
│   └── summary.py                      # `python -m nous_eval.handlers.summary`
├── run_history.py                      # Phase 1 finish — reused as-is
├── regression.py                       # Phase 1 finish — reused as-is (already gates per harness)
└── ... (existing F051/F051.4 modules)

tests/fixtures/handlers/                # NEW
├── admission_labeled.jsonl             # ~50 facts with admit/reject labels
├── dedup_paraphrases.jsonl             # ~30 (anchor, paraphrase) pairs
├── backfill_corpus.jsonl               # ~100 facts/decisions/episodes seed
└── summary_transcripts.jsonl           # ~20 transcripts with gold key-points
```

Each handler CLI follows the same shape:
1. Load fixture → seed eval DB.
2. Invoke the production handler (no monkeypatching, no shims).
3. Compute mechanical metric.
4. Write markdown report under `reports/handlers/`.
5. Persist to `eval_runs` (via shared `persist_run_history`).
6. Tear down per-run state (truncate the tables it touched).
7. Exit 0 on pass / 4 on regression (matching `regression.py` exit code).

### Per-handler specs

#### A. Admission control (`handlers/admission.py`)

**Production code under test:** `nous/heart/admission.py::AdmissionScorer.score()` plus `nous/heart/heart.py::Heart.learn()` decision logic.

**Fixture:** `admission_labeled.jsonl` — 50 candidate facts:
- 25 positive (high-quality, novel, well-grounded — should admit)
- 25 negative (vague, redundant, ungrounded — should reject)
- Each row: `{content, subject, category, source_text, label: "admit"|"reject", rationale}`
- Labels reviewed by Tim (no AI-only labels in gating fixture).

**Procedure:**
1. Load fixture into a temp `heart.facts` slice on the eval DB.
2. For each candidate: call `Heart.learn(FactInput(...))` with `admission_control_enabled=true`, `admission_shadow_mode=false`.
3. Read back: was the row admitted (`active=true`) or rejected (returned `FactRejected` with reason)?
4. Compare to label.

**Metric:** F1 score of admit/reject classification.

**Gating threshold:** `admission_f1` drop > 5 percentage points → regression.

**Why F1 (not accuracy):** label balance can drift; F1 catches asymmetric regressions (e.g. admitting too many junk facts vs rejecting good ones).

**Fixture size justification:** N=50 is small but stratified; per-class N=25 puts the 95% Wilson interval at ±18% on F1, enough to catch 10%+ regressions reliably. Bumping to N=200 is F056.1.

---

#### B. Dedup (`handlers/dedup.py`)

**Production code under test:** `nous/handlers/fact_extractor.py::_dedup_via_search` + `Heart.learn` native cosine `> 0.95` dedup. Both legs.

**Fixture:** `dedup_paraphrases.jsonl` — 30 paraphrase pairs:
- Each row: `{anchor: "<original fact>", paraphrase: "<rephrased>", expected: "dedup"|"distinct"}`
- 20 should dedup (semantic paraphrases), 10 should be distinct (similar wording, different meaning).
- Example dedup pair: `{"anchor": "Tim prefers FastAPI for new services", "paraphrase": "For new microservices, Tim's choice is FastAPI", "expected": "dedup"}`
- Example distinct pair: `{"anchor": "PostgreSQL 17 is the production database", "paraphrase": "PostgreSQL 17 was deprecated last quarter", "expected": "distinct"}`

**Procedure:**
1. Seed eval DB with 50 unrelated background facts (so search isn't returning trivially-empty results).
2. For each pair: insert anchor → call `fact_extractor.extract_and_store` with paraphrase as candidate → check whether dedup fired (returned UUID matches anchor's UUID, no new row).
3. Tally: did the system dedup paraphrases that should be dedup'd? Did it correctly NOT dedup distinct facts?

**Metric:** `dedup_recall` = correct-dedups / true-dedup-pairs; `dedup_precision` = correct-dedups / all-dedups-fired. Report both, gate on **F1**.

**Gating threshold:** `dedup_f1` drop > 5pp → regression.

**Why both halves:** The PR #364 bug (over-dedup against near-empty corpus) lowered precision. A pure recall metric wouldn't have caught it.

---

#### C. Graph backfill (`handlers/backfill.py`)

**Production code under test:** `nous/sleep/graph_backfill.py` (F040) + F043 cross-encoder gate + F045 CE-aware thresholds + F054 selective relaxation. The full sleep-cycle backfill path.

**Fixture:** `backfill_corpus.jsonl` — 100 mixed entities:
- 60 facts, 25 decisions, 10 episodes, 5 procedures.
- Designed so ~30% are intentional orphans (no graph edges) — gives the backfill something to do.
- Content drawn from a real Nous dev-loop transcript snippet (deterministic, sanitized).

**Procedure:**
1. Truncate eval DB graph tables; load fixture (entities + their existing edges).
2. Snapshot initial state: count edges per relation type, count orphans per entity type.
3. Run `GraphBackfillRunner.run_one_pass()` with current settings (CE on, F045 thresholds).
4. Snapshot final state.
5. Compute deltas: new edges per relation type, orphans resolved, cross-type vs same-type breakdown.
6. **Sample 20 new edges** → call Haiku LLM-judge to score each (`semantically_related: true|false|borderline`).

**Metric:**
- `density_delta` (new edges total) — informational, no gate.
- `orphan_resolution_rate` = `orphans_resolved / initial_orphans` — gate on drop > 10pp.
- `edge_precision` (LLM-judged "true" share of sampled new edges) — gate on drop > 10pp.

**Why two-metric gate:** A change that adds many edges (high density delta) at low precision is exactly the F052 failure pattern — must catch both axes.

**LLM-judge cost:** 20 edges/run × Haiku ≈ $0.005/run. Negligible.

---

#### D. Episode summary (`handlers/summary.py`)

**Production code under test:** `nous/handlers/episode_summarizer.py::EpisodeSummarizer.summarize_episode()`.

**Fixture:** `summary_transcripts.jsonl` — 20 transcripts:
- Each row: `{transcript: "<300-1500 word conversation>", gold_key_points: ["...", "...", ...], gold_summary_themes: ["..."]}`
- Transcripts cover the 6 LongMemEval question types (knowledge-update, multi-session, single-session-{user,assistant,preference}, temporal-reasoning) — ensures we catch summary quality drops in the conversation patterns we care about.
- Gold key-points are 3-7 short factual claims the summary MUST surface.

**Procedure:**
1. For each transcript: call `summarize_episode(transcript=...)` → get `{summary, key_points, candidate_facts}`.
2. **LLM-judge with Haiku** asks two questions per row:
   - `key_point_coverage`: of N gold key-points, how many appear in the produced `key_points` list (sub-string OR semantic match)? Returns 0..1.
   - `summary_faithfulness`: does the produced `summary` contain any claim NOT supported by the transcript? Returns 0..1.

**Metric:** `summary_quality` = `mean(key_point_coverage)` × `mean(summary_faithfulness)`. Gate on drop > 5pp.

**Cost:** 20 transcripts × 2 judge calls × Haiku ≈ $0.02/run.

---

### Wiring into existing infra

Each `handlers/<name>.py` exposes `def main(argv) -> int` and conforms to:

```python
# Pseudo-API (real signature in run_history.py from Phase 1 finish)
async def persist_run_history(
    eval_settings, main_settings, *,
    git_sha, fixture_version,
    configs_payload=[{"name": "<handler-name>", "harness": "<handler-name>", ...}],
    metrics_payload={"<handler-name>": {"metrics": {primary_metric: <value>, ...}, ...}},
    qrel_counts={"<handler-name>": <fixture-N>},
    report_path=str(out_path),
    notes=f"<short context>",
)
```

`regression.py` already supports arbitrary harness tags (filters by `harness=<name>` when `--harness` flag is passed). Add `<handler-name>` to the `--harness` choices in a one-line edit per handler.

### Test coverage

Per project convention (matching F051 plan §D): each handler ships with:
- 5-10 unit tests per metric helper (in `tests/handlers/test_<name>.py`).
- 1 integration test that runs end-to-end against the test DB on a 5-row mini-fixture.

Total Phase 2 test count target: ~80 tests across 4 PRs.

---

## Sequencing & cost

| PR | Handler | LOC est | Fixture LOC | Tests | Eval cost/run |
|---|---|---|---|---|---|
| 1 | admission | ~250 | 50 rows | ~12 | $0 |
| 2 | dedup | ~280 | 30 rows + 50 bg | ~14 | $0 |
| 3 | backfill | ~400 | 100 rows | ~16 | $0.005 (Haiku) |
| 4 | summary | ~300 | 20 rows | ~12 | $0.02 (Haiku) |
| **Total** | — | **~1230** | **~200 rows** | **~54** | **$0.025/run** |

Each PR follows the project's standard cycle: spec section above → 3-agent review (arch / devil / python-pro) → impl → review → merge.

Estimated wall time: **2 weeks** at 1 PR / 3 days assuming review iteration.

---

## Open questions for review

1. **Should backfill eval also run F040's sleep-cycle prerequisites (correction extraction, supersession)?** Tradeoff: cleaner if it does (matches prod state), more brittle if any of those handlers change. Currently spec says no — pure backfill in isolation.
2. **Fixture provenance for the gold labels:** Tim hand-labels the admission + dedup fixtures, AI-drafts the summary gold key-points then Tim reviews. Is that calibration acceptable, or should everything be AI-drafted-then-reviewed for consistency?
3. **Should there be an F056.1 follow-up issue right now to track CI integration (weekly cron)?** Or wait until all 4 handlers ship and we have data on per-run reliability?

---

## Rollout

**Phase A (this spec):** 4 PRs, one per handler, in order.
**Phase B (F056.1):** CI integration — weekly cron runs all four + retrieval eval, posts regression report to GitHub issue if any gate fails.
**Phase C (F056.2):** Add a 5th handler eval covering the cognitive layer (frame selection precision against labeled task descriptions).

If Phase A completes cleanly, Phase 3 (`nous-longMemEval` adoption) gets unblocked — at that point we have both fast white-box write-path coverage AND slow black-box agent-task coverage.
