# Execution plan — derived from 2026-05-03 audit

**Status:** active
**Owner:** Tim (PM/exec) + Claude (impl)
**Generated:** 2026-05-04
**Source:** [audit-2026-05-03.md](audit-2026-05-03.md)

## Goals

1. **Resolve 5 critical anomalies** (week 1)
2. **Address 5 P2 anomalies** (weeks 1–2)
3. **Close 6 coverage gaps** (weeks 2–4)
4. **Implement 4 process changes** (parallel, ongoing)
5. **Establish run-documentation discipline** — no more lost stats (today)

Each work item below has: **what**, **why**, **how to verify**,
**dependencies**, and **report path** for the eval that proves it
landed. Items marked 🟢 are "ready to start" — no upstream blockers.

## Phase 0 — Run-documentation discipline (TODAY) 🟢

The audit surfaced 47 reports with informal naming and inconsistent
metadata. We can't reproduce or compare runs without it. Fix by
adding two things and a workflow.

### 0.1 Create the run registry

**File:** [`RUNS.md`](RUNS.md) — append-only chronological log,
one row per eval run. Every new run appends a row; never edit
prior rows (errata go in a footnote).

**Row format:**
```
| date_utc | type | git_sha | agent | sources | n | configs | result | report | decision |
```

`type` enum: `retrieval` (F051), `sleep_action`, `sleep_health`,
`compaction`, `anti_hallucination`, `calibration`, `f027_supersession`,
`f031_resolution`, `f026_gate`, `context_packing`, `density`,
`edge_audit`, `working_memory`, `frame_classifier`, `intent_classifier`,
`other`.

### 0.2 Standardize report frontmatter

Every new `reports/*.md` and every report inside `reports/<dir>/` MUST
start with a YAML frontmatter block:

```yaml
---
date_utc: 2026-05-04T12:00:00Z
type: retrieval
git_sha: <commit>
agent_id: <agent>
sources: [nous_prod, longmemeval]
configs: [baseline, ce_off]
n_qrels: 90
fixture_version: v2026-Q2
notes: <one-sentence purpose>
---
```

Existing F051 markdown reports already have header fields close to
this; just enforce the standard going forward. Backfill is
best-effort (see 0.4).

### 0.3 Workflow — "after every eval run"

Add this 3-step ritual to `evaluations/README.md`:

1. Eval CLI writes the report to `reports/<descriptive-name>/`.
2. Open the report, copy the headline metrics + git_sha.
3. Append one row to `RUNS.md` with the result + decision (or
   "exploratory — no decision yet").

For F051 runs, the auto-recorded row in `nous_system.eval_runs`
(per F051 spec) is the source of truth for the harness — but the
human-readable `RUNS.md` row is what makes findings discoverable.

### 0.4 Backfill seed (PARTIAL)

Seed `RUNS.md` with the 18 F051 retrieval rows + the 4 critical
non-retrieval evals (sleep_action, F031 v3, F027 sonnet, calibration).
Mark the remaining ~25 with a "TODO backfill" stub at the bottom of
RUNS.md so they're not forgotten but also not blocking.

### 0.5 (Optional) Helper script

Future enhancement: `scripts/eval_register_run.py <report_path>`
that parses the report's frontmatter + JSON sibling, extracts
metrics, and appends to RUNS.md. Defer until manual rhythm is
established (don't optimize too early).

**Owner:** Claude implements 0.1–0.4 in this session.
**Verify:** RUNS.md exists, has ≥22 seeded rows, README references it.

## Phase 1 — 5 critical anomalies (this week)

### 1.1 Sleep phases silently broken — `stale_scan` + `cluster_consolidation`

**Anomaly:** 14 consecutive sleep cycles produced 0 work for both
phases. `eval_sleep_filter_dryrun` reports both phases GREEN with
eligible candidates (13 clusters in cluster_consolidation). Filters
work in dry-run; cycles produce nothing.

**Root-cause hypotheses (in priority):**
1. Settings flag silently disabling the phase body (env mismatch).
2. Admission filter rejecting all candidates after dry-run select.
3. Unhandled exception caught + logged but missing from `sleep_stats`.
4. Sleep cycle not actually invoking the phase methods (wiring drift).

**How to verify the fix:**
- Re-run `eval_sleep_cycle_health` after 3 consecutive sleep cycles;
  expect non-zero `stale_facts_deactivated` and `clusters_merged`.
- Add to RUNS.md as `type=sleep_health` with the result.

**Eval to add (alongside the fix):** A unit-style probe that asserts
each sleep phase invokes its body at least once per cycle, even on
empty corpus. Catches "phase silently no-op'd" forever.

🟢 Ready to start. No dependencies.

### 1.2 F031 REMOVE actions broken (20% accuracy, 3 iterations)

**Anomaly:** REMOVE_A and REMOVE_B both at 20% across `f031_resolution_eval_v1/v2/v3`. Best overall is 53% (v3). MERGE collapsed in v3 (40% → 20%) without explanation.

**Approach options (pick one):**
- **A.** Restrict action vocabulary to `{SUPERSEDE, KEEP_BOTH}` until
  REMOVE quality recovers. Simplest; loses some coverage but is honest.
- **B.** Rework the F031 prompt with action-specific 1-shot examples
  for each of the 6 actions. Tests will surface the prompt change.
- **C.** Train a small classifier per-action (4-6 prompts, vote).
  Heavier; defer unless A/B fail.

**Recommended:** Start with A (1-line config change to the resolver).
Re-run f031_resolution_eval against the restricted vocabulary; if
overall accuracy lifts >70% with no REMOVE actions, ship A. If still
weak, escalate to B.

**How to verify:** New `f031_resolution_eval_v4_restricted.md` showing
overall ≥70% with no REMOVE rows.

**Dependencies:** None. The 0.7-confidence floor in
`sleep_handler.py:668` already downgrades low-conf to KEEP_BOTH; the
restricted vocab just removes 4 of 6 options.

### 1.3 F027 sonnet-judge re-eval (canonical metric switch)

**Anomaly:** UPDATE category 33% under sonnet judge vs 90% under
haiku-on-haiku self-consistency. The 86.7% number on the dashboard is
inflated.

**Action:**
1. Re-run F027 supersession eval against sonnet-4-6 judge as canonical.
2. Update the dashboard / README with the sonnet number.
3. Mark prior haiku-judge results as "self-consistency proxy."

**How to verify:** New `f027_supersession_eval_v4_sonnet_canonical.md`
with overall + per-category accuracy. Append to RUNS.md with
`type=f027_supersession` and `decision: adopt sonnet judge`.

**Cost:** ~$0.05 per re-run (120 pairs × sonnet calls). Trivial.

### 1.4 F051 baseline drift — pin fixtures by hash, add determinism test

**Anomaly:** Same git_sha producing 0.810 vs 0.828 baseline MRR on
May 3. Plus +8.1% jump between Apr 29 and May 2.

**Two fixes:**

**a)** Pin the qrel fixture by SHA256 hash, not date:
- Add `fixture_sha256` field to `RetrievalConfig` and reports
- Compute hash at load time; warn if changed since prior run
- Investigate the 0.810 vs 0.828 gap by re-running both with explicit
  hashes — find which qrel changed

**b)** Add a determinism test to `nous_eval/`:
- New CLI: `python -m nous_eval.determinism_check --runs 3`
- Runs the same config 3 times against the same source
- Asserts byte-identical retrieved IDs across runs
- Fails loudly if not — surfaces RuntimeConfig leaks or sampling drift

**How to verify:** `determinism_check` exits 0 on baseline×3 against
nous_prod. Append result to RUNS.md.

### 1.5 Context-packing MMR gate investigation

**Anomaly:** `eval_context_packing_mmr_forced` shows 6/8 (75%) pass
vs 2/8 (25%) for the production per-scenario gate.

**Investigation steps:**
1. Read `nous/cognitive/context.py` to identify the per-scenario MMR
   gate logic. What conditions does it check?
2. Determine which 4 scenarios are blocked by the gate that the
   forced variant unlocks (cognitive_loop, skill_management,
   subtask_workers, telegram_email/heartbeat_overview/rubric_evolution).
3. Hypothesize: is the gate gating on memory_type? candidate_count?
   query length? Identify the rule that's wrong.

**Possible outcomes:**
- The gate is pure overhead → remove it, MMR always-on for context
  packing. Re-validate that retrieval-MMR-off + context-MMR-on
  doesn't regress retrieval MRR.
- The gate is correct in some cases but too aggressive → loosen the
  threshold.

**This contradicts today's "MMR hurts retrieval" finding.** Resolution:
retrieval-MMR ≠ context-packing-MMR; the optimization is different
because the consumers are different (LLM ranks vs human-perceived
context completeness).

**How to verify:** New `eval_context_packing_post_fix.md` showing
≥6/8 pass under default settings (not forced).

## Phase 2 — 5 P2 anomalies (next 2 weeks)

### 2.1 Archive 6 pre-fix RRF reports

**What:** Move to `reports/_archived/2026-05-03-rrf-pre-wiring-fix/`
with a README explaining the wiring bug (commit 6d0eb27 fix). Update
report parsers to skip this dir.

**Why:** The reports show 0% deltas not because RRF tuning has no
effect, but because the flag was silently dropped. Keeping them in
the active set risks misinterpretation.

🟢 Trivial. ~10 min.

### 2.2 F026 ActionGate post-#285 re-eval

**What:** Re-run `f026_eval` against the current ActionGate (which
includes PR #285 dedup tightening). 3 of 12 fails were
duplicate-detection misses.

**Verify:** ActionGate score ≥10/12 (currently 9/12). If still <10,
surface the specific exact-duplicate scenarios that pass and add
short-circuit dedup before the LLM tier.

### 2.3 Edge audit — per-relation precision regression test

**What:** Currently `edge_audit` reports aggregate precision per
relation but has no regression assertion. Between Apr 26 and Apr 30,
evidence_for jumped 0.53 → 0.75 (good) while related_to fell
0.83 → 0.70 (bad). Net: still failing.

**Add:** assertion that no relation's precision drops more than
0.05 absolute between consecutive eval runs. Fail loudly if so.

**Verify:** Re-run edge_audit on current code; ensure all 4
relations meet 0.75 precision floor. New report:
`edge_audit_post_regression_test.md`.

### 2.4 Compaction hallucination guard

**What:** Add an entity-substring check to `ConversationCompactor`:
- Extract named entities from the source via simple regex/NER
- For each entity in the summary, verify it's a substring of source
- If not, mark `compaction_hallucinations` counter +1 and log

**Verify:** Re-run `eval_compaction_fidelity`; expect hallucination
rate to drop from 26.7% to <5%. The remaining are real omissions,
not fabrications.

### 2.5 Calibration rolling window post-F058

**What:** F058 scaling is verified working but calibration was poor
pre-fix (Brier 0.252, ECE 0.199). Set up a rolling 30-day calibration
eval that runs weekly.

**Verify:** Cron entry or scheduled job that runs
`eval_f058_counterfactual` against the prior 30 days of decisions
and writes to `reports/calibration_<date>.md`. Add to RUNS.md.

## Phase 3 — 6 coverage gaps (weeks 2–4)

### 3.1 sleep_ingestion eval

**What:** Eval the memory intake during sleep — when fact_extractor
runs after end_conversation, what fraction of intended facts make it
into `heart.facts`? Are any duplicated, dropped, or modified?

**Eval shape:** 30 ground-truth conversations with annotated
"intended facts." Run sleep cycle, query `heart.facts` for
extracted facts, compute precision + recall.

### 3.2 deliberation chain-of-thought

**What:** When `record_thought` is called during pre_action, are the
thoughts actually informing the final decision? Audit by sampling 50
decisions, check if the recorded thoughts logically support the
final decision.

### 3.3 decision closure & outcome feedback

**What:** When `review_outcome` is called, does the calibration
update happen? Sample 20 reviewed decisions, verify the brain's
calibration shift matches expectations.

### 3.4 skill auto-learning effectiveness

**What:** F011 skill discovery + auto-activation. Sample skills
created in the last 30 days, ask Sonnet judge "is this skill
useful?" Compute pass rate.

### 3.5 retrieval pre-RRF tuning ablation

**What:** Today's RRF tuning showed ±1.6% MRR; channel isolation
showed vector_only suffices. But we never tested raw cosine vs RRF
fusion vs alternative fusion (e.g., score multiplication, max-merge).
Add 3-4 fusion variant configs to the matrix.

### 3.6 fact-graph edge recall

**What:** Currently `edge_audit` measures precision (of created
edges, what % are correct?). It doesn't measure recall (of
should-have edges, what % did we create?). Build a synthetic
ground-truth corpus of 50 fact pairs with known relationships,
run graph_linker, compute recall.

## Phase 4 — Process changes (ongoing)

### 4.1 Sample-size minimum (n ≥ 50)

**What:** Shipping decisions require n ≥ 50 with bootstrap CIs.
Current eval has many n=20 (longmemeval) and n=12-15 (memory
ingestion) runs.

**How:** Update `nous_eval/qrels_loader.py` to surface a warning
when source has <50 reviewed qrels. Update the qrel set for
longmemeval to grow beyond 20.

### 4.2 Judge sensitivity ritual

**What:** Any LLM-judged metric must be re-run with a stronger judge
model and the delta reported. Standardize before new metrics ship.

**How:** Add a `--judge-models <m1,m2>` flag to LLM-judged eval CLIs.
Output table includes per-judge results.

### 4.3 Pin qrel fixtures by hash, not date

**What:** See 1.4(a). Same git_sha + same fixture hash must produce
byte-identical metrics.

### 4.4 Per-relation regression assertions

**What:** See 2.3. Fail any relation that drops >0.05 precision vs
the prior run.

## Risk register

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Fix for sleep phases (1.1) breaks F051 baseline further | Medium | Run determinism test (1.4b) before and after |
| F031 vocabulary restriction (1.2.A) loses real REMOVE cases | Low | Eval shows REMOVE is 20% accurate anyway — losing 20% of nothing |
| F027 sonnet judge cost balloons | Low | $0.05/run; budget cap if it becomes daily |
| Context-packing fix (1.5) regresses retrieval MRR | Medium | Re-run §3 retrieval matrix after the fix; gate on no MRR regression |
| Backfill of RUNS.md (~25 entries) takes hours | Low | Stub-only seeding, fill rest as we touch each report |

## Sequencing & owner table

| # | Task | Phase | Owner | Eta | Blocked by |
|---|------|-------|-------|-----|------------|
| 0.1-0.4 | Run-doc discipline + RUNS.md seed | 0 | Claude | today | — |
| 1.1 | Sleep phases broken — investigate + fix | 1 | Together | 2-3d | — |
| 1.2 | F031 REMOVE actions — restrict or rework | 1 | Together | 2d | — |
| 1.3 | F027 sonnet judge re-eval | 1 | Claude | 1d | — |
| 1.4 | F051 baseline determinism + fixture pin | 1 | Claude | 1-2d | — |
| 1.5 | Context-packing MMR gate fix | 1 | Together | 2-3d | — |
| 2.1 | Archive 6 pre-fix RRF reports | 2 | Claude | 10min | — |
| 2.2 | F026 ActionGate re-eval | 2 | Claude | 1d | — |
| 2.3 | Edge audit per-relation regression | 2 | Claude | 1d | — |
| 2.4 | Compaction hallucination guard | 2 | Claude | 2-3d | — |
| 2.5 | Calibration rolling window | 2 | Together | 1d setup | — |
| 3.x | Coverage backfill (6 gaps) | 3 | Claude | 1-2 weeks | — |
| 4.x | Process changes | 4 | Claude | parallel | — |

## Success criteria for this plan

After 2 weeks:

- [ ] All 5 critical anomalies have a closing eval report linked in RUNS.md
- [ ] All 5 P2 anomalies are either fixed or have a "won't fix — accepted risk" entry with rationale
- [ ] RUNS.md has ≥40 entries (today's seed + 2 weeks of new runs)
- [ ] No new "lost stat" — every eval run since today is in RUNS.md
- [ ] At least 2 of 6 coverage gaps have a first eval landed
- [ ] At least 2 of 4 process changes are in code

## Updates

This file is the single source of truth for the plan. Update it as
items complete. Don't delete — add a `**done:** <link to PR>` line
under each item.
