# 2026-04-28 → 2026-05-05 — Memory features tested

Single-page index of every nous memory feature/mechanism evaluated in
the past week, grouped by mechanism (not by eval run). For per-run
detail and exact metrics, see [`RUNS.md`](RUNS.md).

This document complements [`audit-2026-05-03.md`](audit-2026-05-03.md)
(which audits all 47 reports for anomalies) by giving the systems-level
view: *what was tested, what shipped, what's open*.

## Retrieval mechanisms (memory recall)

| Mechanism | Eval verdict |
|---|---|
| Vector search (pgvector cosine) | baseline |
| Keyword search (PG FTS / tsvector) | collapses standalone (MRR 0.07/0.35); ties baseline as part of RRF |
| RRF hybrid fusion | `rrf_k_low` +0.7% MRR; `rrf_k_high` -9.5%; F054 keyword-toggle validated |
| Cross-encoder rerank (F042) | corpus-dependent: +4% MRR on prod, **-5% on LongMemEval** (CE off wins on personal-Q&A) |
| MMR diversity (F030) | global-flip falsified; per-consumer needed |
| MMR skip-after-CE (F030.1) | validated as default — disabling regresses both corpora |
| **MMR per-consumer override (F030.2)** | **NEW — `apply_mmr` plumbed through `Heart.recall` + `run_recall_pipeline`. Default unchanged (retrieval matrix byte-identical). `apply_mmr=True` opt-in: context_packing memory bucket 0/3 → 2/3 (+67pp on current 5-scenario set)** |
| Spreading activation | no movement on either corpus at any threshold |
| Channel isolation | vector-only ties default; keyword-only collapses |
| F052 multi-embedding backfill | **falsified** — gate never fires even at threshold=0.05; reverted |
| Determinism | byte-identical across 3 runs on both corpora |

## Sleep cycle phases

| Phase | Eval verdict |
|---|---|
| F031 contradiction resolution | sonnet-resolver v3: 53% (REMOVE 0/20%, MERGE 20%) → root-caused to gen-prompt bug, not classifier (`no-fix-eval-issue`) |
| F027 supersession | sonnet-judge canonical: **85% overall, UPDATE 90%** (post v3 prompt fix) |
| F027 cluster_merge | **40% error rate** in prod (sleep_action audit) |
| stale_scan | RED then GREEN after Codex fix #409 |
| F040 graph densification | precision-validated; F045 thresholds + F054 same-type relaxations |
| F043 CE-aware backfill | precision pre-filter validated |
| F053 dead-edge pruning | **NEW** — 884/3137 edges pruned in dry-run; shipped |
| F057 episode re-linker | **NEW** — backfills F022 missed links; shipped |
| F058 graph densifier summary fallback | **NEW** — unblocks F040 for episodes without `structured_summary`; shipped; verified on prod (orphans 487→456 first cycle, backfill 162 edges/day vs prior 7-57) |
| prune_dead_edges, relink_open_episodes, generalize, evolve_rubric | covered by sleep_health probe (RED/YELLOW/GREEN/REFUSED) |

## Sleep observability

- Sleep cycle health probe — REFUSED verdict added so phases that fired-and-refused are distinct from didn't-fire
- Sleep filter dry-run probe — contradicted the health probe; both surfaced gaps the other missed
- Sleep action audit — F031 + F027 verdict accuracy on real prod cycles

## Compaction (history)

| Mechanism | Status |
|---|---|
| Compaction fidelity (judge eval) | 33/45 (73.3%) preserved → 26.7% silent fact loss |
| F058 structured tool-use (fact ledger array) | shipped, eased loss but didn't fully close gap |
| F059 hallucination guard | **NEW** — regex entity check; 1/35 above-threshold fire (2.9%) across both corpora; PR #418 |
| F059 event persistence | **NEW** — fires saved to `nous_system.events` so Docker log rotation can't lose evidence |
| Tool result pruning (4-tier) | unchanged |

## Context engine

- Context packing (memory bucket vs docs split): MMR-after-CE force-on unblocks 0→6/6 memory
- Token budget scaling, relevance floor, staleness penalty: validated
- Frame classifier: 26/30 (86.7%); 4 confusions decision↔question, task↔debug
- Intent classifier: 30/30 (100%) — shipped

## Decision quality

- F026 action gating synthetic eval: 9/12 (75%); 3 dup fails are by-design repetition semantics (`no-fix-eval-issue`)
- F026 claim verification: 18/20 (90%)
- F058 confidence calibration (temperature 0.7627): Brier 0.252→0.215, ECE 0.199→0.033

## Memory quality / write-side

- F022 episode-graph linker: missed-link audit drove F057
- F022 live linker content guard: shipped (40-char min)
- F023 admission control: 5-dim scoring; bypassed for cluster/contradiction merges
- F056 dedup: hybrid threshold 0.92 / native cosine 0.95 — env-tunable
- Anti-hallucination prompt: Sonnet +7pp benefit; Haiku doesn't hallucinate on these scenarios

## Edge precision

- Edge audit by relation: `related_to` 0.83→0.70 regression caught; per-relation regression check shipped
- F045 CE-aware thresholds + content-length guard
- F054 same-type threshold relaxations

## Eval framework itself (PR-B / #417)

- RRF resolver bypass fix (was silently testing same config across rows)
- Determinism check CLI
- Edge-audit regression check with auto-baseline + `--exit-on-regression`
- 17+ new validation configs in `_DEFAULT_CONFIGS`
- `evaluations/` registry (README, EXECUTION-PLAN, RUNS.md, audit-2026-05-03, per-session logs, HTML dashboard)

---

## Status of headline findings

| Finding | Status |
|---|---|
| F022 missed episode→fact links | **Fixed (F057, shipped)** |
| F040 excludes episodes without structured_summary | **Fixed (F058 densifier, shipped)** |
| Dead edges (28% of incidence) waste spreading hops | **Fixed (F053, shipped)** |
| Compaction silent fact loss / substitution | **Mitigated (F058 structured + F059 guard, F059 in PR #418)** |
| F027 cluster_merge 40% error | Open — by-design repetition semantics, `no-fix-eval-issue` |
| F031 REMOVE 0% | Open — gen-prompt bug not classifier; `no-fix-eval-issue` |
| Calibration overconfident +19.8% | **Fixed (F058 confidence, shipped)** |
| MMR-after-CE skip globally wrong for context packing | **Fixed (F030.2 `apply_mmr` override, validated +67pp memory bucket)** |
| Keyword channel collapses standalone | Operational toggle (F054) shipped |
| Cross-encoder corpus-dependent | Documented, default off; on for prod-shaped via env |
