# Eval run registry

**Append-only chronological log.** One row per eval run. Never edit
a prior row — corrections go in a footnote.

For F051 retrieval runs, the auto-recorded entry in
`nous_system.eval_runs` is the source-of-truth for the harness; this
file is the human-readable index that surfaces findings.

**Workflow:** see [README.md §workflow](README.md#workflow---after-every-eval-run).
After every eval run: copy headline metrics + git_sha from the
report, append one row here.

## Format

| date_utc | type | git_sha | agent | sources | n | configs | result | report | decision |

**type enum:** `retrieval` (F051), `sleep_action`, `sleep_health`,
`sleep_filter_dryrun`, `compaction`, `anti_hallucination`,
`calibration`, `f027_supersession`, `f031_resolution`, `f026_gate`,
`context_packing`, `density`, `edge_audit`, `working_memory`,
`frame_classifier`, `intent_classifier`, `other`.

**status flags in `decision`:**
- `shipped` / `dropped` / `exploratory` / `superseded` / `INVALID:<reason>`

---

## Active runs (chronological)

| date_utc | type | git_sha | agent | sources | n | configs | result | report | decision |
|---|---|---|---|---|--:|---|---|---|---|
| 2026-04-26 22:23 | density | unknown | nous-prod-snapshot | F040 corpus | 3 cfg | f045_strict, f054_proposed, baseline_loose_ce | 240 / 355 / 383 edges | reports/density-eval-20260426-222310.md | informed F054 |
| 2026-04-26 22:28 | edge_audit | unknown | nous-prod-snapshot | 30/relation | — | aggregate gate (≥0.75) | FAIL: evidence_for 0.53, informed_by 0.69, related_to 0.83 | reports/edge-audit-20260426-222846.md | exploratory |
| 2026-04-29 12:53 | retrieval | 024178b | nous-prod-snapshot | nous_prod | 40 | ce_off, ce_on | MRR 0.777 → 0.808 (+4.0%) | reports/2026-04-29T12-53-08_ce_off-ce_on.md | exploratory (early CE eval) |
| 2026-04-29 23:16 | retrieval | 024178b | nous-prod-snapshot | nous_prod | 50 | ce_off, ce_on | MRR 0.766 → 0.789 (+3.1%) | reports/2026-04-29T23-16-48_ce_off-ce_on.md | exploratory (n=50, P@5 dipped) |
| 2026-04-29 — | calibration | — | nous-prod-snapshot | reviewed decisions | — | global_scale=0.7627 vs raw | Brier 0.252 → 0.215, ECE 0.199 → 0.033 | reports/calibration_simulation.md | informed F058 |
| 2026-04-30 03:26 | edge_audit | unknown | nous-prod-snapshot | 30/relation | — | aggregate gate | FAIL: discussed_in 0.96, evidence_for 0.75 ↑, informed_by 0.70, related_to 0.70 ↓ | reports/edge-audit-20260430-032612.md | regression: related_to fell |
| 2026-05-02 — | frame_classifier | unknown | — | 30 scenarios | 30 | FrameEngine.select | 26/30 (86.7%) | reports/eval_frame_selection.md | exploratory; 4 confusions decision↔question, task↔debug |
| 2026-05-02 — | intent_classifier | unknown | — | 30 scenarios | 30 | IntentClassifier | 30/30 (100%) | reports/eval_intent_classifier.md | shipped (no action needed) |
| 2026-05-02 — | working_memory | unknown | — | 5 scenarios | 5 | threshold=0.7 | precision 58%, recall 83% | reports/eval_working_memory.md | exploratory |
| 2026-05-02 — | compaction | unknown | — | 15 scenarios | 45 facts | sonnet-judge | 33/45 (73.3%) preserved; 26.7% hallucination/contradiction | reports/eval_compaction_fidelity.md | **P2:** add hallucination guard |
| 2026-05-02 — | f026_gate | unknown | — | synthetic | 32 | ClaimVerifier + ActionGate | 18/20 (90%) + 9/12 (75%); 3 dup-detection fails | reports/f026_eval.md | **P2:** re-run post-#285 |
| 2026-05-02 — | f027_supersession | unknown | — | 30 facts, 120 pairs | 120 | haiku-judge (self-consistency) | 77.5% overall, UPDATE 53.3% | reports/f027_supersession_eval.md | superseded by sonnet-judge |
| 2026-05-02 — | f027_supersession | unknown | — | 30 facts, 120 pairs | 120 | sonnet-judge | 74.2% overall, UPDATE **33.3%** | reports/f027_supersession_eval_sonnet.md | **CRITICAL:** UPDATE 20pp swing under stronger judge |
| 2026-05-02 — | f027_supersession | unknown | — | 30 facts, 120 pairs | 120 | haiku-judge v2 | 83.3% overall, UPDATE 86.7% | reports/f027_supersession_eval_v2.md | self-consistency proxy |
| 2026-05-02 — | f027_supersession | unknown | — | 30 facts, 120 pairs | 120 | haiku-judge v3 | 82.5% overall, UPDATE 90% | reports/f027_supersession_eval_v3.md | self-consistency proxy |
| 2026-05-02 — | f027_supersession | unknown | — | 30 facts seeded | 120 | haiku-judge v3 seeded | **86.7%** overall, UPDATE 90% | reports/f027_supersession_eval_v3_seeded.md | best haiku-judge result; sonnet number is canonical |
| 2026-05-02 — | f031_resolution | unknown | — | 30 pairs | 30 | sonnet-resolver | 33% overall; REMOVE_A 0%, REMOVE_B 20% | reports/f031_resolution_eval.md | superseded by v3 |
| 2026-05-02 — | f031_resolution | unknown | — | 30 pairs | 30 | sonnet-resolver v2 | 37% overall; REMOVE_A 0%, REMOVE_B 0% | reports/f031_resolution_eval_v2.md | superseded by v3 |
| 2026-05-02 — | f031_resolution | unknown | — | 30 pairs | 30 | sonnet-resolver v3 | 53% overall; REMOVE_A 20%, REMOVE_B 20%, MERGE 20% | reports/f031_resolution_eval_v3.md | **CRITICAL:** REMOVE actions broken |
| 2026-05-02 — | context_packing | unknown | — | 8 scenarios | 8 | strict judge, top-k baseline | 0/8 (0%) | reports/eval_context_packing.md | **CRITICAL:** all scenarios fail |
| 2026-05-02 — | context_packing | unknown | — | 8 scenarios | 8 | strict, baseline tweaks | 2/8 (25%) | reports/eval_context_packing_baseline.md | exploratory |
| 2026-05-02 — | context_packing | unknown | — | 8 scenarios | 8 | loose judge | 4/8 (50%) | reports/eval_context_packing_loose.md | exploratory; judge accounts for ~50% of fail |
| 2026-05-02 — | context_packing | unknown | — | 8 scenarios | 8 | strict + MMR enabled | 2/8 (25%) | reports/eval_context_packing_mmr.md | exploratory; gate suppresses MMR |
| 2026-05-02 — | context_packing | unknown | — | 8 scenarios | 8 | strict + MMR FORCED | **6/8 (75%)** | reports/eval_context_packing_mmr_forced.md | **CRITICAL:** gate fix would lift +50pp |
| 2026-05-02 18:05 | retrieval | b7691e5 | nous-prod-snapshot | nous_prod | 90 | baseline, ce_on_mmr_off, ce_mmr_lambda_0.7 | baseline=mmr_off=0.828; lambda_0.7 -3.8% | reports/2026-05-02T18-05-41_baseline-ce_on_mmr_off-ce_mmr_on_lambda_0.7.md | exploratory; baseline jumped from Apr 29 |
| 2026-05-03 02:12 | retrieval | b7691e5 | nous-prod-snapshot | nous_prod | 90 | baseline (vector_weight=0.85 attempted) | MRR 0.828 (no comparison) | reports/_archived/2026-05-03-rrf-pre-wiring-fix/2026-05-03T02-12-53_baseline.md | **INVALID:** pre-fix, vector_weight flag silently dropped |
| 2026-05-03 02:14 | retrieval | b7691e5 | nous-prod-snapshot | nous_prod | 90 | baseline (ce_max_candidates=100 attempted) | MRR 0.828 | reports/_archived/2026-05-03-rrf-pre-wiring-fix/2026-05-03T02-14-27_baseline.md | **INVALID:** pre-fix, ce_max_candidates flag silently dropped |
| 2026-05-03 — | calibration | — | nous-prod-snapshot | reviewed decisions | — | observe vs predicted | Brier 0.252, ECE 0.199, overconfident +19.8% | reports/calibration_eval.md | poor; F058 fix in progress |
| 2026-05-03 — | calibration | — | nous-prod-snapshot | post-F058 reviewed | — | F058 counterfactual | Δ Brier -0.038, Δ ECE -0.166 | reports/eval_f058_counterfactual.md | F058 verified shipping |
| 2026-05-03 — | sleep_action | — | nous-default | 1-day lookback | 10 | f031_contradiction, f027_cluster_merge | f031 5/5 (100%), **f027 2/5 (50%)** | reports/eval_sleep_action_audit.md | **CRITICAL:** f027 cluster_merge has 40% error rate |
| 2026-05-03 — | sleep_health | — | — | 14 cycles | 14 | per-phase status | RED: stale_scan, cluster_consolidation; YELLOW: procedures; GREEN rest | reports/eval_sleep_cycle_health.md | **CRITICAL:** 2 phases broken 14 cycles |
| 2026-05-03 — | sleep_filter_dryrun | — | nous-prod-snapshot | filter selection | — | dry-run | stale_scan GREEN (0 candidates), cluster_consolidation GREEN (13 eligible) | reports/eval_sleep_filter_dryrun.md | **CONTRADICTS** sleep_health |
| 2026-05-03 — | anti_hallucination | — | sonnet-4-6 | 15 scenarios | 15 | flag off vs on | 1/15 (6.7%) → 0/15 (0%); +7pp prompt benefit | reports/eval_anti_hallucination.md | exploratory |
| 2026-05-03 — | anti_hallucination | — | haiku-4-5 | 12 scenarios | 12 | flag off vs on | 0/12 → 0/12 (no hallucinations either way) | reports/eval_anti_hallucination_haiku.md | exploratory; haiku doesn't hallucinate on these |
| 2026-05-03 22:02 | retrieval | e5b1ca9 | nous-prod-snapshot | nous_prod | 40 | baseline, spread_force_on | all 0.833 (0% delta) | reports/_archived/2026-05-03-rrf-pre-wiring-fix/spread_smoke/2026-05-03T22-02-01_baseline-spread_force_on.md | **INVALID:** pre-fix wiring bug |
| 2026-05-03 22:06 | retrieval | e5b1ca9 | nous-prod-snapshot | nous_prod | 40 | 9-config spread+RRF sweep | all 0.833 (0% delta across 9) | reports/_archived/2026-05-03-rrf-pre-wiring-fix/spread_rrf_sweep/2026-05-03T22-06-36_*.md | **INVALID:** pre-fix wiring bug |
| 2026-05-03 22:18 | retrieval | e5b1ca9 | nous-prod-snapshot | nous_prod | 40 | 6 RRF configs | all 0.833 (0% delta) | reports/_archived/2026-05-03-rrf-pre-wiring-fix/rrf_sweep_v2/2026-05-03T22-18-41_*.md | **INVALID:** pre-fix wiring bug |
| 2026-05-03 22:24 | retrieval | e5b1ca9 | nous-prod-snapshot | nous_prod + procedures | 90 | 6 spread+RRF configs | all 0.828 (0% delta across 6) | reports/_archived/2026-05-03-rrf-pre-wiring-fix/spread_rrf_combined/2026-05-03T22-24-42_*.md | **INVALID:** pre-fix wiring bug |
| 2026-05-03 22:33 | retrieval | 6d0eb27 | nous-prod-snapshot | nous_prod + procedures | 90 | ce_off + 5 RRF variants | ce_off 0.810; rrf_k_low **0.816 (+0.7%)**; rrf_k_high 0.733 (-9.5%) | reports/exp1_ce_off_rrf/2026-05-03T22-33-28_*.md | shipped (RRF marginal); wiring fix verified |
| 2026-05-03 22:37 | retrieval | 6d0eb27 | nous-lme-corpus | longmemeval | 20 | baseline, ce_off, ce_off_rrf_k_low, spread_force_on, rrf_k_low | baseline 0.892; **ce_off 0.938 (+5.2%)**; spread/rrf no effect | reports/exp_longmemeval/2026-05-03T22-37-28_*.md | **HEADLINE:** CE rerank hurts personal-Q&A |
| 2026-05-03 22:45 | retrieval | 6d0eb27 | nous-prod-snapshot | nous_prod + procedures | 90 | 7 MMR + spread combos | baseline 0.828 best; ce_mmr_lambda_0.7 -3.8%; ce_mmr_lambda_0.95 -1.9% | reports/exp2_mmr_combos_prod/2026-05-03T22-45-40_*.md | F030.1 default validated |
| 2026-05-03 22:48 | retrieval | 6d0eb27 | nous-lme-corpus | longmemeval | 20 | 7 MMR + spread combos | ce_off 0.938 best; MMR variants 0.931-0.935 | reports/exp2_mmr_combos_lme/2026-05-03T22-48-57_*.md | F030.1 default validated on lme too |
| 2026-05-03 23:05 | retrieval | 6d0eb27 | nous-prod-snapshot | nous_prod + procedures | 90 | ce_off, vector_only, keyword_only | ce_off 0.810; vector_only 0.808; keyword_only 0.066 | reports/exp3_channel_iso_prod/2026-05-03T23-05-33_*.md | informed F054 |
| 2026-05-03 23:06 | retrieval | 6d0eb27 | nous-lme-corpus | longmemeval | 20 | ce_off, vector_only, keyword_only | ce_off 0.938 = vector_only; keyword_only 0.348 | reports/exp3_channel_iso_lme/2026-05-03T23-06-37_*.md | informed F054; vector-only ties default |
| 2026-05-03 23:39 | retrieval | 132992e | nous-prod-snapshot | nous_prod + procedures | 90 | baseline, f052_on, f052_off_explicit, f054_keyword_off, f052+f054 | all 0.828 (no movement) | reports/validate_prod/2026-05-03T23-39-47_*.md | F052 dropped (heuristic doesn't fire); F054 validated |
| 2026-05-03 23:42 | retrieval | 132992e | nous-lme-corpus | longmemeval | 20 | baseline, f052_on, f052_off_explicit, f054_keyword_off, f052+f054 | all 0.892 (no movement) | reports/validate_lme/2026-05-03T23-42-04_*.md | F052 falsified on lme; F054 ties baseline |
| 2026-05-03 23:45 | retrieval | 132992e | nous-lme-corpus | longmemeval | 20 | baseline, ce_off, f052_on, f052_low_threshold, f052_very_low_threshold | ce_off 0.938; **all f052 variants 0.892 (no movement at any threshold)** | reports/validate_lme_v2/2026-05-03T23-45-14_*.md | F052 dropped — gate doesn't fire even at threshold=0.05 |
| 2026-05-04 — | f027_supersession | a42d2c1 | nous-prod-snapshot | 30 facts seeded | 120 | sonnet-4-6 judge, post-prompt-fix v3 | **85.00% overall**; CONTRADICTION 80%, UPDATE **90%**, REFINEMENT 100%, UNRELATED 70% | reports/f027_supersession_eval_v4_sonnet_canonical.md | **CANONICAL** — closes EXEC-PLAN 1.3. Sonnet confirms v3 prompt fix; UPDATE recovered from pre-fix 33.3% to 90% |
| 2026-05-04 — | retrieval | a42d2c1 | nous-prod-snapshot | nous_prod | 40 | baseline ×3 (determinism) | **PASS** — all 40 qrels byte-identical across 3 runs. fixture sha256=e4c61d12e7ea055b | `python -m nous_eval.determinism_check` (no md report; CLI exit code only) | closes EXEC-PLAN 1.4. Within-boot determinism confirmed |
| 2026-05-04 — | retrieval | a42d2c1 | nous-lme-corpus | longmemeval | 20 | baseline ×3 (determinism) | **PASS** — all 20 qrels byte-identical across 3 runs | `python -m nous_eval.determinism_check --sources longmemeval` | confirms determinism on lme corpus |
| 2026-05-04 — | f026_gate | 8cb649a | — | synthetic (post-#285) | 32 | ClaimVerifier + ActionGate (skip Tier 3 — rate-limited) | ClaimVerifier 18/20 (90%); ActionGate 9/12 (75%) — same 3 dup fails, all `under-threshold` (1/3, 1/6) | reports/f026_eval_v2_post285.md | **no-fix-eval-issue** — by-design repetition semantics; see [batch doc](2026-05-04-batch-1.2-2.2.md) |
| 2026-05-04 — | f031_resolution | 8cb649a | — | analysis-only (no re-run) | 30 (existing v3) | inspect REMOVE_A/B failures in v3 JSON | resolver makes defensible calls; gen prompt produced mutable-property facts mislabeled REMOVE | reports/f031_resolution_eval_v3.json (analysis) | **no-fix-eval-issue** — gen-prompt bug, not classifier bug; see [batch doc](2026-05-04-batch-1.2-2.2.md) |
| 2026-05-04 — | edge_audit | c65de85 | — | (no live re-run) | tooling-only | added per-relation regression check + JSON output + auto-baseline + backfilled JSON for Apr 30 baseline | 13/13 unit tests PASS; CLI gains `--baseline-json`, `--max-regression`, `--exit-on-regression` (exit 3) | nous_eval/run_edge_audit.py + tests/test_edge_audit_regression.py | **closes EXEC-PLAN 2.3** — next live audit run will auto-detect Apr 30 as baseline |
| 2026-05-04 — | retrieval | 5fb2754 | nous-prod-snapshot | nous_prod + procedures | 90 | baseline/ce_on/ce_off, mmr_enabled=true + skip_after_ce=false | MRR 0.828 → **0.797** (-3.7%), nDCG 0.851 → 0.816, R@10 0.922 → 0.878 | reports/post_mmr_after_ce_prod/2026-05-04T12-52-10_*.md | EXEC-PLAN 1.5 measurement |
| 2026-05-04 — | retrieval | 5fb2754 | nous-lme-corpus | longmemeval | 20 | baseline/ce_on/ce_off, mmr_enabled=true + skip_after_ce=false | MRR 0.892 → **0.931** (+4.4%), but R@10 0.389 → 0.338 (-13%), nDCG -5.4% | reports/post_mmr_after_ce_lme/2026-05-04T12-54-02_*.md | EXEC-PLAN 1.5 measurement — mixed signal |
| 2026-05-04 — | context_packing | 5fb2754 | nous-prod-snapshot | 8 scenarios | 8 | mmr_enabled=true + skip_after_ce=false | **6/6 memory bucket (100%)**, 0/2 docs | reports/eval_context_packing_mmr_after_ce.md | EXEC-PLAN 1.5 measurement — confirms MMR-after-CE unblocks context packing |
| 2026-05-04 — | context_packing | 5fb2754 | nous-prod-snapshot | 8 scenarios | 8 | mmr_enabled=true + skip_after_ce=true (kept) | **2/6 memory (33%)**, 0/2 docs | reports/eval_context_packing_mmr_on_v2.md | proves skip_after_ce gates the context-packing lift |
| 2026-05-04 — | retrieval | 5fb2754 | nous-prod-snapshot | nous_prod + procedures | 90 | baseline/ce_on/ce_off, mmr_enabled=true (skip_after_ce=true default) | identical to prior baseline (MRR 0.828) — MMR skipped due to CE+skip | reports/post_mmr_default_prod/2026-05-04T12-40-56_*.md | retrieval unaffected when only mmr_enabled flipped |
| 2026-05-04 — | EXEC-PLAN 1.5 | 5fb2754 | — | meta | — | tried mmr_enabled=True default | rolled back. Real fix is per-consumer MMR (context packers force on, retrievers keep off); requires plumbing | nous/config.py reverted | **closes 1.5 with finding** — global flip is wrong; needs per-call-site control |
| 2026-05-04 — | sleep_health | 1.1-probe-fix | — | code-only | — | added REFUSED verdict to sleep_cycle_health probe | 18/18 tests pass; phases that emit events on failure get REFUSED instead of RED, distinguishing "fired but refused" from "didn't fire" | nous_eval/probes/sleep_cycle_health.py + tests | **closes EXEC-PLAN 1.1** — no code bug; measurement gap fixed |
| 2026-05-04 — | density | 0ab8dc7 | nous-prod-snapshot | eval-scratch DB | 3137 edges | F053 SQL (BEGIN…ROLLBACK) | DELETE 884 (28% of edges incident to inactive endpoints); within 1000/cycle cap | docker exec psql (no md report; transactional dry-run) | **F053 SQL validated end-to-end** — production form matches production code path |
| 2026-05-04 — | density | 0ab8dc7 | f053-it-* | eval-scratch DB | 4 edges (2 alive + 2 dead fixture) | F053 integration test against real Postgres | PASS — handler deleted exactly the 2 dead edges; 2 alive survived; sleep_stats[dead_edges_pruned]=2 | tests/test_f053_dead_edge_prune.py::TestF053Integration | **F053 behavioral validation complete** — 11/11 unit+integration pass |
| 2026-05-05 — | compaction | feat/F059 | — | prod-flavored 15 + longmemeval 20 | 35 | F059 hallucination guard | prod 3/15 fired (all TPs); lme 1/20 above threshold (likely TP); 1/35 actionable (2.9%) | reports/eval_f059_guard_prod_flavored.md, reports/eval_f059_guard_longmemeval.md | **F059 shipped** PR #418 — warn-only + persist to nous_system.events |
| 2026-05-05 19:00 | retrieval | feat/F030.2 | nous-prod-snapshot | nous_prod | 40 | baseline/ce_off/ce_on (apply_mmr=None default) | all 0.833 (no movement vs prior baseline; backward-compat verified) | reports/2026-05-05T19-00-12_baseline-ce_off-ce_on.md | F030.2 default path unchanged |
| 2026-05-05 — | context_packing | feat/F030.2 | nous-prod-snapshot | 5 scenarios (3 memory + 2 docs) | 5 | apply_mmr=None default | **0/3 memory (0%)**, 0/2 docs | reports/eval_context_packing_f030_2_default.md | F030.2 default reproduces prior baseline |
| 2026-05-05 — | context_packing | feat/F030.2 | nous-prod-snapshot | 5 scenarios (3 memory + 2 docs) | 5 | **apply_mmr=force_on (F030.2 opt-in)** | **2/3 memory (67%)**, 0/2 docs (1 fail was judge parse error, not retrieval) | reports/eval_context_packing_f030_2_force_on.md | **F030.2 validated** — +67pp memory bucket vs default |

---

## Backfill TODO

These reports exist on disk but aren't yet captured above with full
metadata. Backfill on demand when revisiting the topic.

- (None at time of seed — all 47 reports are above. Future runs append below.)

---

## Footnotes / corrections

(None yet. Add `[^N]` footnote markers in row + footnote text here
if a row needs correction without rewriting history.)
