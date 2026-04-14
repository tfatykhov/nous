# F045: CE-Aware Cosine Thresholds + Content-Length Guard for Graph Backfill

**Status:** Shipped
**Proposed by:** Tim + Nous
**Date:** 2026-04-14
**Depends on:** F042 (CE Reranking — shipped), F043 (CE Rerank in Sleep-Cycle Backfill — shipped)
**Blocks:** None (additive, feature-flagged)
**Supplements:** F040 (graph densification), F043 (upstream CE pre-filter)

---

## Empirical motivation

On `192.168.1.141` at `2026-04-14` we ran a 4-way A/B across three sleep cycles of F043 (all with `NOUS_CROSS_ENCODER_ENABLED=true`, `NOUS_CE_BACKFILL_ENABLED=true`) while varying only `NOUS_GRAPH_THRESHOLD_FACT_FACT`:

| Run | Threshold | CE survived | CE pruned | Prune rate | Edges created |
|---|---|---|---|---|---|
| 20:54 | 0.82 | 199 | 825 | 80.6% | **0** |
| 21:39 | 0.82 | 197 | 828 | 80.8% | **0** |
| 21:50 | 0.78 | 190 | 835 | 81.5% | **0** |
| **22:01** | **0.65** | 199 | 825 | 80.6% | **48** |

The first three runs produced zero edges — F043 was running, CE was pruning consistently (~80%), but every survivor fell below the cosine gate at both 0.82 and 0.78. At 0.65 the gate finally let the CE-approved candidates through, producing **48 orphan edges + 8 bridge edges** in a single cycle (nearly 4× the entire preceding day's backfill output).

### Histogram confirms: 0.82 was cutting the modal peak

Weight distribution across all 828 existing fact-fact auto-linked edges:

```
  [0.50,0.60)  n=  54   # pre-existing, from non-F040 linker paths
  [0.60,0.65)  n=   2
  [0.65,0.70)  n=   0
  [0.70,0.75)  n=   0
  [0.75,0.80)  n= 419   # ←  MODAL PEAK — 51% of all fact-fact edges live here
  [0.80,0.82)  n=  92
  [0.82,0.85)  n=  98
  [0.85,0.90)  n= 116
  [0.90,1.01)  n=  47
```

**511 of 828 (62%) fact-fact edges cluster in [0.75, 0.82)** — exactly the range the old 0.82 floor was rejecting. The bi-encoder's "similar" distribution for the Nous fact corpus peaks below 0.82, not above. The threshold wasn't protecting against noise — it was blocking the dominant cluster.

### LLM-as-judge: 80% precision, 2 structural failures

Random sample of 10 edges from the [0.75, 0.82) bucket, judged for semantic relatedness:

| # | w | Verdict | Topic |
|---|---|---|---|
| 1 | 0.757 | ✅ | Iran geopolitical snapshots, same timeframe |
| 2 | 0.763 | ✅ | Minsky SoM Chapter 5 (same chapter) |
| 3 | 0.763 | ✅ | FORGE brand hierarchy (near-duplicate) |
| 4 | 0.766 | ✅ | Self-improvement skill-execution gates |
| 5 | 0.773 | ❌ | `https://arxiv.org/abs/2511.17673,` + `/arxiv.org/abs/2505.03434` — URL-only facts |
| 6 | 0.774 | ✅ | Emotion Machine mapping doc (near-duplicate) |
| 7 | 0.791 | ✅ | Heartbeat false-alarm pattern |
| 8 | 0.798 | ❌ | Another pair of URL-only facts |
| 9 | 0.809 | ✅ | F023 admission protocol 77% NULL |
| 10 | 0.811 | ✅ | "Nous" naming collision with Nous Research |

**8/10 YES, 2/10 NO → 80% precision.** Both failures are the same structural bug: facts whose content is *literally just an arxiv URL string* (~25-35 chars, no prose). The bi-encoder scores URL-shape strings highly against each other because they share token patterns, and the cross-encoder has nothing else to disagree with. This failure mode is independent of threshold — it shows up at any threshold where URL-only facts co-exist with prose facts.

---

## Solution

Two orthogonal changes. Both feature-flagged, both backwards-compatible.

### Part 1: CE-aware cosine thresholds

Add a second set of per-relation thresholds that apply **only when `ce_backfill_enabled=True`**. When CE is doing the precision filtering upstream, we can safely relax the downstream cosine gate because CE has already pruned ~80% of candidates. When CE is disabled, the conservative strict thresholds still apply as before.

**New settings in `nous/config.py`** (all ~17 percentage points below the corresponding strict default, derived from the A/B experiment and the histogram modal-peak analysis):

```python
# F045: CE-aware relaxed thresholds (apply only when ce_backfill_enabled=True)
ce_backfill_threshold_fact_fact: float = 0.65
ce_backfill_threshold_fact_decision: float = 0.55
ce_backfill_threshold_fact_episode: float = 0.55
ce_backfill_threshold_decision_decision: float = 0.60
ce_backfill_threshold_episode_episode: float = 0.58
ce_backfill_threshold_procedure_any: float = 0.55
```

Env vars follow the standard `NOUS_` prefix.

**Threshold resolver in `nous/brain/graph_densifier.py`** becomes:

```python
def _get_threshold(settings, source_type: str, target_type: str) -> float:
    if settings.ce_backfill_enabled:
        return _get_ce_mode_threshold(settings, source_type, target_type)
    return _get_strict_threshold(settings, source_type, target_type)
```

`_get_strict_threshold` is the existing lookup renamed. `_get_ce_mode_threshold` is a new mirror that reads the `ce_backfill_threshold_*` fields.

### Part 2: MIN_CONTENT_CHARS guard in the reranker adapter

Drop candidates whose content is shorter than a minimum character floor **before** CE inference. URL-only facts never make it to scoring — they're filtered out during wrapping.

**New setting in `nous/config.py`:**

```python
ce_backfill_min_content_chars: int = 80  # drops URL-only / boilerplate facts
```

**Change in `nous/brain/backfill_rerank.py` `ce_rerank_backfill_candidates`:**

```python
min_chars = int(getattr(settings, "ce_backfill_min_content_chars", 80))

wrapped: list[RerankCandidate] = []
for cand_id, rrf in candidate_rows:
    content = content_map.get(cand_id, "")
    if not content or len(content.strip()) < min_chars:
        continue  # skip URL-only / boilerplate facts
    wrapped.append(RerankCandidate(id=cand_id, content=content, score=float(rrf)))
```

The guard sits after the existing `if not content: continue` short-circuit, so candidates that pass the length floor also pass the non-empty check.

---

## Why not just drop `NOUS_GRAPH_THRESHOLD_FACT_FACT` globally to 0.65?

That's a valid workaround — and in fact it's what we did on the live instance to run the experiment. But:

1. **It's a footgun for operators without CE.** An operator who runs Nous with `ce_backfill_enabled=False` (the default) gets raw hybrid-search candidates hitting the cosine gate directly. At 0.65, the cosine gate alone would let in noise from non-CE-filtered candidates. We need the strict floor to remain the default when CE isn't in front of it.
2. **It silently widens the gate for every other code path.** The `NOUS_GRAPH_THRESHOLD_FACT_FACT` setting is also consumed by downstream spreading-activation tuning and future graph-quality checks. Changing its value changes more than just F043 behavior.
3. **It makes the intent invisible.** Config grep for "why is this low?" wouldn't surface that the low value only makes sense because F043 is upstream. Two named settings — `graph_threshold_fact_fact` vs `ce_backfill_threshold_fact_fact` — document the coupling explicitly.

---

## Default behavior matrix

| `ce_backfill_enabled` | Thresholds used | Content guard | Behavior |
|---|---|---|---|
| `False` (default) | `graph_threshold_*` (strict) | off | Pre-F045 behavior, unchanged |
| `True` | `ce_backfill_threshold_*` (relaxed) | `≥80 chars` | F045 behavior |

No migration needed. Existing deployments with CE off see zero change.

---

## Tests

**`tests/test_backfill_rerank.py`** — new cases:
1. `test_content_guard_drops_short` — candidate with 40-char content is dropped before CE; longer content proceeds.
2. `test_content_guard_respects_whitespace` — candidate with `"   short text   "` (10 chars after strip) is dropped.
3. `test_content_guard_configurable` — `ce_backfill_min_content_chars=200` drops medium-length content that passes at default.

**`tests/test_graph_densifier.py`** — new cases:
1. `test_get_threshold_ce_mode` — with `ce_backfill_enabled=True`, `_get_threshold` returns the `ce_backfill_threshold_*` value for each relation pair.
2. `test_get_threshold_strict_mode` — with `ce_backfill_enabled=False`, `_get_threshold` returns the existing `graph_threshold_*` value (regression guard).

All unit-test-only; no Postgres needed.

---

## Risk Assessment

| Risk | Likelihood | Mitigation |
|---|---|---|
| Relaxed thresholds let noise through when CE's precision is lower than assumed | LOW | 80% precision empirically validated at fact↔fact 0.65. Other relation defaults are conservative (not tuned). Operators can override via env var. |
| Content guard drops legitimate short facts | LOW | 80-char floor is below the median fact length (~200 chars). Configurable. Only affects the CE pipeline, not ingestion. |
| Deploy without CE accidentally picks relaxed thresholds | NONE | `_get_threshold` gates on `ce_backfill_enabled`; default is `False`. |
| Per-relation thresholds other than fact↔fact aren't empirically calibrated | MEDIUM | Explicitly noted as "derived from histogram modal-peak estimate, tunable via env var." Phase 2 follow-up: repeat A/B for each relation type. |
| Existing `graph_threshold_*` env overrides stop taking effect when CE is on | LOW | Documented in CLAUDE.md — when enabling CE, operators must set `ce_backfill_threshold_*` if they want non-default values. |

---

## Metrics

Post-deployment, monitor in sleep_stats:

- `ce_backfill_survived` / `ce_backfill_pruned` — continues from F043
- **New expectation:** `orphan_edges_created` should climb from 0 → ~20-60 per cycle on a moderately-populated graph
- **Precision proxy:** LLM-as-judge sample of new edges (manual, periodic — not automated in MVP)

If `orphan_edges_created` stays at 0 after F045 ships, the bi-encoder is genuinely placing CE-survivor pairs below the 0.65 floor — indicates a deeper embedding-model mismatch (F042 Phase 3 territory: knowledge distillation).

---

## Out of scope (future)

- Per-relation A/B calibration (decision↔decision, episode↔episode, etc.) — Phase 2
- Runtime config override via `RuntimeConfig` for live threshold adjustment — follows F042 pattern
- Re-embedding facts with a CE-distilled model — F042 Phase 3
- Automated LLM-as-judge precision pipeline in sleep_stats — follow-up observability feature

---

## Implementation estimate

| Area | LOC |
|---|---|
| `nous/config.py` | 7 |
| `nous/brain/graph_densifier.py` | 15 |
| `nous/brain/backfill_rerank.py` | 5 |
| `tests/test_backfill_rerank.py` | 40 |
| `tests/test_graph_densifier.py` | 30 |
| docs (CLAUDE.md, INDEX.md, spec) | 15 |
| **Total** | **~112 LOC** |

No new dependencies. No schema changes. No runtime migration.
