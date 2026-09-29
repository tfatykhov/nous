# Implementation Plan: Cognitive-Substrate Fault Detector

**Date:** 2026-09-26
**Decision:** 28f021a0 — "fault-detector first" for the retrieval / graph / forgetting
self-improvement track.

## Problem Statement

The motivating incident: the sleep-cycle `stale_scan` phase failed silently for 14
consecutive cycles with nobody noticing (it ran, examined zero facts, produced zero
deactivations). A second known case: `sleep_reflection` emitted provenance edges for
only 131/397 facts — invisible until a manual audit.

The existing `nous_eval/probes/sleep_cycle_health.py` is an *offline* diagnostic
tool that a human must remember to run. This plan builds a *runtime* fault detector:
a heartbeat check that surfaces findings automatically through the existing
`FindingStore` / heartbeat pipeline.

## Scope

### Included
1. **`process_run_log` table** (migration 077) — lightweight heartbeat row per
   sleep-phase execution: process name, started_at, finished_at, status
   (started / finished / error / skipped), items_examined, items_changed, error_message.
2. **`ProcessRecorder`** (`nous/observability/process_recorder.py`) — thin async
   helper that writes to the table; fail-open on any DB error.
3. **SleepHandler instrumentation** — adds `_recorder: ProcessRecorder | None`
   (default None = no-op). When set, each phase in `_run_sleep` is wrapped to
   record start/finish/error. stale_scan specifically records items_examined and
   items_changed.
4. **`ProcessFaultCheck`** (`nous/heartbeat/fault_detector.py`) — heartbeat
   `BaseCheck` subclass that detects:
   - **Missed run**: no `finished` row for a sleep phase in the last
     `fault_detector_sleep_max_gap_hours` (default 48h).
   - **Consecutive errors**: last N runs for a phase all have `status='error'`.
   - **Zero-change collapse** (stale_scan): last M finished runs all have
     `items_changed=0` AND the DB count of age-eligible facts is above a floor.
   - **Output/input ratio collapse**: items_changed/items_examined < 30% of the
     20-run trailing baseline (when both are non-NULL).
5. **`RetrievalCanaryCheck`** (`nous/heartbeat/fault_detector.py`) — reads a
   configurable JSONL canary set; for each query calls `heart.search_facts` and
   checks whether at least one gold_id appears in top-K results; emits a finding
   if recall drops below the configured floor. Ships as a no-op when
   `fault_detector_canary_path` is empty.
6. **Config flags** (all default OFF/empty — merging is inert).
7. **`main.py` wiring** — creates recorder + registers checks when
   `fault_detector_enabled=True`.

### Explicitly Out of Scope
- Any graph / density self-tuner (Goodhart-circular per F044 design).
- Any retrieval auto-tuning (deferred to later phase).
- Any forgetting changes.
- Retrieval canary auto-seeding (must be populated by operator; see docs in canary
  check class).

## Process Inventory

All periodic memory processes, verified against HEAD `nous/handlers/sleep_handler.py`:

| Phase | Type | Activity field (sleep_stats) | Failure mode |
|-------|------|------------------------------|--------------|
| review | DB | — | returns False |
| prune | DB | — | returns False |
| compress | LLM | episodes_compacted | LLM error or no stub |
| reflect | LLM | facts_created | LLM error, no episodes |
| resolve_contradictions | LLM | contradictions_resolved | LLM error |
| sweep_key_conflicts | LLM | — | flag disabled |
| stale_scan | DB | stale_deactivated | MOTIVATING INCIDENT |
| cluster_consolidation | LLM | clusters_merged | LLM refuses all |
| recover_abandoned_episodes | LLM | episodes_recovered | no orphans found |
| graph_densification | embed | orphan_edges_created | no orphans |
| relink_open_episodes | DB | episodes_relinked | no orphans |
| stc_consolidation | DB | f044_promoted | flag disabled |
| prune_dead_edges | DB | dead_edges_pruned | no dead edges |
| prune_hub_snapshots | DB | — | always succeeds |
| generalize | LLM | procedures_created | LLM refuses |
| evolve_rubric | LLM | rubric_evolved | flag disabled |

Background retrievals (retrieval_log, migration 070) — covered by canary check.

## File Plan

### New Files
| File | Purpose |
|------|---------|
| `sql/migrations/077_process_run_log.sql` | Table + index |
| `nous/observability/process_recorder.py` | ProcessRecorder + context manager |
| `nous/heartbeat/fault_detector.py` | ProcessFaultCheck + RetrievalCanaryCheck |
| `tests/test_fault_detector.py` | Focused unit tests with mutation evidence |

### Modified Files
| File | Changes |
|------|---------|
| `nous/storage/models.py` | Add ProcessRunLog ORM model |
| `nous/config.py` | 9 new fault_detector_* settings |
| `nous/handlers/sleep_handler.py` | Add _recorder attribute + _run_phase helper + instrumentation |
| `nous/main.py` | Create recorder, register checks when enabled |

## Design Decisions

### Why a separate table instead of reusing nous_system.events?
`sleep_completed` events carry aggregate stats but cannot distinguish "phase errored"
from "phase was skipped due to wake interrupt". We need per-phase error status to
detect consecutive failures.

### Why not extend consolidation_audit?
`consolidation_audit_enabled` defaults False; extending it would make the fault
detector dependent on a separate optional system. The process_run_log is a
**lightweight** append-only log — one row per phase, no full changelog.

### Why fail-open on DB errors in the recorder?
The recorder must never interfere with the sleep cycle. All recorder calls are
wrapped in try/except; on any error the sleep phase proceeds normally.

### Canary check design
- JSONL format: `{"query": str, "gold_ids": [str], "min_recall_at_k": float}`
- Uses `heart.search_facts(query, limit=top_k)` — pure vector + FTS, no LLM judge
- Empty `fault_detector_canary_path` → no-op (check skips, no findings)
- To seed: run `scripts/diag/seed_retrieval_canary.py` against a known-good DB
  (not shipped in this PR; documented here for the operator)

## Config Flags

```
NOUS_FAULT_DETECTOR_ENABLED=false          # master switch
NOUS_FAULT_DETECTOR_CHECK_INTERVAL=3600    # seconds between process fault check runs
NOUS_FAULT_DETECTOR_SLEEP_MAX_GAP_HOURS=48 # flag if any sleep phase hasn't run in N hours
NOUS_FAULT_DETECTOR_CONSECUTIVE_ERROR_THRESHOLD=3   # N consecutive errors → finding
NOUS_FAULT_DETECTOR_ZERO_CHANGE_THRESHOLD=5  # stale_scan: M zero-change runs → check population
NOUS_FAULT_DETECTOR_RATIO_COLLAPSE_THRESHOLD=0.30   # <30% of baseline → finding
NOUS_FAULT_DETECTOR_RATIO_BASELINE_WINDOW=20         # runs for trailing baseline
NOUS_FAULT_DETECTOR_CANARY_PATH=""          # path to canary JSONL (empty = disabled)
NOUS_FAULT_DETECTOR_CANARY_TOP_K=10        # top-K for canary recall check
NOUS_FAULT_DETECTOR_CANARY_INTERVAL=3600   # seconds between canary check runs
NOUS_FAULT_DETECTOR_PROCESS_LOG_RETENTION_DAYS=90  # prune old rows
```

## Implementation Steps

1. Migration 077 + ORM model
2. ProcessRecorder
3. SleepHandler instrumentation (add _recorder + _run_phase helper)
4. ProcessFaultCheck + RetrievalCanaryCheck
5. Config flags
6. main.py wiring
7. Tests

## Open Questions for Tim

1. **Which phases should get items_examined/changed in V1?** This plan
   instruments stale_scan fully (it's the motivating incident). Other phases
   record only start/finish/error (NULL items counts). Should reflect and
   graph_densification also get counts in this PR?

2. **Canary seeding script**: A `scripts/diag/seed_retrieval_canary.py` that
   queries the prod DB and generates a canary JSONL from the highest-confidence
   facts is useful but out of scope here. Should it ship in the same PR?

3. **Retention policy**: `fault_detector_process_log_retention_days=90` means
   ~90 * 3 sleeps/day * ~16 phases = ~4,300 rows/agent/day at most. At that
   rate 90 days = ~387k rows. Acceptable? Or should we prune to e.g. 14 days?

4. **Zero-change threshold for phases other than stale_scan**: Currently only
   stale_scan has the "zero change + non-empty population" check because only
   stale_scan records items_changed. Add reflect (facts_created=0 but many
   episodes exist) in V2?

5. **Surfacing via REST**: Should process_run_log be surfaced in the dashboard
   (e.g., a new tab or an extension of the consolidation tab)? Deferred to a
   later phase.
