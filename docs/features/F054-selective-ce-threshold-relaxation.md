# F054 — Selective CE-Threshold Relaxation for F040 Backfill

**Status:** 📝 Proposed (2026-04-26)
**Proposed by:** Tim
**Date:** 2026-04-26
**Depends on:** F045 (CE-aware thresholds — shipped), F053 (density-eval harness — proposed PR #351)
**Blocks:** none
**Related:** F040 (graph densification — shipped), F052 (abandoned — provided the eval data motivating F054)

---

## Problem

F045 ships per-relation CE-mode cosine thresholds for graph densification backfill, calibrated against a 2026-04-14 A/B that found `fact_fact=0.65` produced 80% LLM-judged precision. The other five thresholds (`decision_decision=0.60`, `fact_decision=0.55`, `fact_episode=0.55`, `episode_episode=0.58`, `procedure_any=0.55`) were histogram estimates, not empirically validated.

The 2026-04-26 F053 density-eval harness run revealed:

| relation | strict thresholds (today) | loose thresholds (`baseline_loose_ce`) | Δ edges | precision change |
|---|---|---|---|---|
| `related_to` (mostly same-type) | 159 edges | 272 edges | **+113 (+71%)** | **0.83 → 0.83 (no change)** |
| `informed_by` (decision↔procedure) | 9 edges | 16 edges | +7 | 1.00 (n=9, underpowered) → 0.88 (n=16) |
| `evidence_for` (decision↔fact) | 73 edges | 95 edges | +22 | **0.57 → 0.47 (worse)** |

**Same-type relations are over-filtered.** Loosening their thresholds catches +113 high-precision edges per cycle on the F051 eval corpus at zero precision regression.

**Cross-type `evidence_for` (fact↔decision) is fragile.** Already failing precision (0.57 < 0.75) with strict thresholds, and degrades further when loosened. The cause is corpus quality (~5 of 9 NO/WEAK verdicts cite "source content is empty" — `brain.decisions.context` is often null in the eval corpus), not algorithmic — but the threshold can't fix the corpus, so loosening it just adds more noise.

F054 ships a *selective* relaxation: lower the same-type and one cross-type threshold, **leave fact↔fact at 0.55, fact↔decision at the strict 0.55**, and add a content-length guard for decisions to prevent empty-content edge creation.

### What this is not

- Not a F040 algorithm change — `_backfill_same_type` and `_backfill_cross_type` orchestration is untouched.
- Not a F045 redesign — only the per-relation threshold *defaults* change; the `_get_threshold` routing logic stays the same.
- Not a multi-embedding seed (that was F052 — abandoned). F054 doesn't generate new candidates; it lets more existing candidates clear cosine.

---

## Goals

1. **Increase F040 backfill density on production** by ~50-70% (extrapolated from F051 eval-corpus measurement) at zero LLM-judged precision regression for same-type relations.
2. **Eval-before-deploy gate** — every threshold change validated via F053 density_eval + run_edge_audit before merge. F054 implementation runs the same harness with the new defaults and demonstrates per-relation precision ≥ 0.75 (or unchanged from baseline) on a 30-edge audit per relation.
3. **Add content-length guard for `brain.decisions.context`** mirroring F045's existing `NOUS_CE_BACKFILL_MIN_CONTENT_CHARS=80` for facts. Prevents empty-decision-content edges that the F053 audit identified as the root cause of `evidence_for` precision drops.
4. **Default-on ship** (this is a tuning change, not a new feature). Old thresholds remain available via env-var overrides for operators who want to revert.

## Non-goals

- **No change to fact↔fact threshold** — already empirically validated at 0.65 → 80% precision (decision `7d6fdce9`). Don't fix what isn't broken.
- **No change to `evidence_for` (fact↔decision) threshold** — F053 audit shows it's already under-precision; loosening makes it worse. Address via content guard, not threshold.
- **No new metrics / dashboard panels** — `/dashboard/density` already shows orphan rate; F054 expects the rate to drop.
- **No change to `_backfill_cross_type` candidate generation logic** — out of scope (would be F052.4 territory if anyone resurrects it).
- **No F040 cycle-cap changes** — `NOUS_GRAPH_BACKFILL_MAX_FACTS=50` etc. stay where they are.

## Deferred (with rationale)

- **F054.1 — Per-corpus threshold calibration.** F054 ships defaults validated on the F051 eval corpus (~3000 nodes). Larger prod corpora may have different optimal thresholds. Deferred until F053's eval methodology has 30-day track record on prod density trends.
- **F054.2 — Threshold annealing.** Lower thresholds during early backfill cycles (when graph is sparse), tighten as graph fills. Plausible but adds complexity; defer until baseline lift is measured.
- **F054.3 — Content guards for episodes + procedures.** F045 has `NOUS_CE_BACKFILL_MIN_CONTENT_CHARS=80` for facts. F054 adds it for decisions. Episodes and procedures may also benefit but the F053 audit didn't surface evidence — defer until audit data shows similar empty-content patterns.

---

## Mechanism

### Threshold changes

Default values in `nous/config.py`:

| Field | Current default (F045) | F054 default | Justification |
|---|---|---|---|
| `ce_backfill_threshold_fact_fact` | 0.65 | **0.55** | F053 measured: same-type fact↔fact at 0.55 produces +71% edges at 0.83 precision (no change) |
| `ce_backfill_threshold_decision_decision` | 0.60 | **0.50** | Same lift pattern as fact↔fact (`related_to` precision unchanged) |
| `ce_backfill_threshold_episode_episode` | 0.58 | **0.50** | Same pattern; conservative -8 since N=0 in eval corpus (extrapolated from same-type behavior) |
| `ce_backfill_threshold_procedure_any` | 0.55 | **0.45** | Same pattern; same-type and procedure→fact catches more `related_to` edges |
| `ce_backfill_threshold_fact_decision` | 0.55 | **0.55 (UNCHANGED)** | F053 measured: precision regresses 0.57 → 0.47 if loosened. KEEP STRICT. |
| `ce_backfill_threshold_fact_episode` | 0.55 | **0.55 (UNCHANGED)** | No empirical lift data; default to KEEP STRICT until F053 audit shows otherwise |

Mechanically: the values in `nous/brain/graph_densifier.py::_get_ce_mode_threshold` and `nous/config.py` change. `_get_threshold` routing logic is untouched.

### Content-length guard for decisions

Add a new Settings field:

```python
# F054 — minimum chars in brain.decisions.context for the decision to
# participate in graph densification edges. Mirrors F045's
# ce_backfill_min_content_chars for facts. F053 edge_judge audit showed
# empty/near-empty decision context is the root cause of evidence_for
# precision drops (~5 of 9 NO verdicts cited "source content is empty").
ce_backfill_min_decision_chars: int = Field(
    default=40,
    description="F054 — drop decisions with context shorter than this from graph backfill",
)
```

Apply in `_backfill_same_type` (decision-decision) and `_backfill_cross_type` (decision-fact, decision-episode, decision-procedure) candidate hydration: when fetching `brain.decisions.context`, exclude rows where `length(trim(context)) < min_decision_chars`. Symmetric to how F045's content guard works for `heart.facts.content`.

### Backward compatibility

All changes are pure default value tweaks. Operators who want the old behavior can revert via env vars:
```bash
NOUS_CE_BACKFILL_THRESHOLD_FACT_FACT=0.65 \
NOUS_CE_BACKFILL_THRESHOLD_DECISION_DECISION=0.60 \
NOUS_CE_BACKFILL_THRESHOLD_EPISODE_EPISODE=0.58 \
NOUS_CE_BACKFILL_THRESHOLD_PROCEDURE_ANY=0.55 \
NOUS_CE_BACKFILL_MIN_DECISION_CHARS=0 \
```

---

## Eval methodology (gate before merge)

Per F053's eval-before-deploy convention:

### Required eval matrix

```bash
NOUS_EVAL_DB_NAME=nous_eval_scratch \
  uv run python -m nous_eval.density_eval \
    --configs baseline,f054_proposed,baseline_loose_ce
```

Where `f054_proposed` is added to `nous_eval/retrieval.py::_DEFAULT_CONFIGS`:

```python
"f054_proposed": RetrievalConfig(
    name="f054_proposed",
    flags={
        "ce_backfill_threshold_fact_fact": 0.55,
        "ce_backfill_threshold_decision_decision": 0.50,
        "ce_backfill_threshold_episode_episode": 0.50,
        "ce_backfill_threshold_procedure_any": 0.45,
        # fact_decision, fact_episode UNCHANGED at 0.55
        "ce_backfill_min_decision_chars": 40,
    },
    description=(
        "F054 selective CE relaxation: same-type loosened, cross-type "
        "fact_decision/fact_episode KEPT STRICT, +decision content guard."
    ),
),
```

### Required edge-precision audit

```bash
NOUS_EVAL_DB_NAME=nous_eval_scratch \
  uv run python -m nous_eval.run_edge_audit --limit-per-type 30
```

### Gate criteria (all must pass)

1. **Same-type density lift**: `f054_proposed.related_to_edges - baseline.related_to_edges >= +50`
2. **Same-type precision floor**: `related_to` precision ≥ 0.75 (gate-eligible if N ≥ 15)
3. **Cross-type precision floor (no regression)**: `evidence_for` precision change vs baseline ≤ -0.03 (i.e., max 3-percentage-point drop tolerated)
4. **Cross-type density (informed_by, evidence_for)**: per-type Δ ≥ 0 (no regression)

If any criterion fails: spec marked abandoned, defaults stay at F045 values.

---

## Code surface

| File | LOC | Change |
|---|---|---|
| `nous/config.py` | ~10 | 4 default value tweaks (`Field(default=0.55)` etc.) + 1 new `ce_backfill_min_decision_chars` Field |
| `nous/brain/graph_densifier.py` | ~5 | Apply `ce_backfill_min_decision_chars` filter in decision candidate hydration |
| `nous_eval/retrieval.py` | ~15 | Add `f054_proposed` `RetrievalConfig` |
| `tests/test_f054_threshold_relaxation.py` | ~80 | Unit tests for the threshold defaults + decision content guard |
| `CLAUDE.md` | +5 lines | Update the env-var table with the new defaults + new `NOUS_CE_BACKFILL_MIN_DECISION_CHARS` row |
| `docs/features/INDEX.md` | +1 row | F054 entry on ship |

**Total**: ~115 LOC, ~half-day spike. No migration, no schema change.

---

## Risks

1. **Eval corpus may not represent prod.** F051 eval corpus = ~3000 nodes from prior dev work. Prod corpus shape differs (likely more episodes, different fact/decision ratio). The +71% same-type lift is corpus-specific. Mitigation: monitor `/dashboard/density` orphan rate for 2 sleep cycles after merge; rollback via env-var override if trend is wrong.
2. **Cross-type empty-content edges still possible for episodes/procedures.** F054.3 deferred. If `heart.episodes.summary` or `heart.procedures.description` is empty in prod, those will still create low-precision edges. Watch the next F053 audit cycle.
3. **`fact_decision` precision is already failing (0.57 < 0.75).** F054 doesn't fix this — it's a corpus quality issue (empty `brain.decisions.context`). The content guard helps prevent NEW empty-decision edges but doesn't retroactively delete existing ones. Pre-existing `evidence_for` edges with empty source content will continue to score WEAK/NO in audits until cleaned up.
4. **Threshold drift risk.** Default value changes cascade into any dashboard / report / test that hardcodes the old values. Search for `0.65` / `0.60` / `0.58` literals before merging; F045's existing tests assert specific threshold values and may need updates.

---

## Rollout

| Phase | Trigger | Action |
|---|---|---|
| **0 — Spec lock** | This doc + optional 3-agent review (low-risk default-tweak; reviewer judgment) | Spec frozen; impl plan can be drafted (or skipped — change is small enough that direct PR is reasonable) |
| **1 — Impl + eval** | Plan approval | Branch `feat/F054-selective-ce-relaxation`. Land config defaults + content guard + `f054_proposed` config + tests. Run `density_eval --configs baseline,f054_proposed,baseline_loose_ce` against eval DB. Run `run_edge_audit --limit-per-type 30`. |
| **2 — Gate evaluation** | Branch CI green + eval reports attached to PR | Verify all 4 gate criteria from §Eval methodology pass. |
| **3a — Pass** | All criteria met | Merge PR. Default takes effect on next prod restart. Monitor `/dashboard/density` for 2 sleep cycles. |
| **3b — Fail** | Any gate criterion fails | Mark `❌ Abandoned (eval-fail-yyyy-mm-dd)`. Spec stays as decision record; defaults revert to F045 values. |
| **4 — Post-merge monitoring** | Phase 3a only | Watch density dashboard. If orphan rate doesn't track eval prediction within 2 sleep cycles, env-var override the new thresholds back to F045 values and investigate corpus differences. |

---

## Open questions

1. **Should `episode_episode` (0.58 → 0.50) be more conservative?** F053 corpus has 0 episode orphans, so the lift is extrapolated from same-type behavior. A first-merge could land at 0.55 (-3) instead of 0.50 (-8), then tighten/loosen based on prod data. Decided in impl plan.
2. **Should the content guard apply to existing rows or only new edges?** F054 mechanism filters at backfill time so only NEW edges are gated. Existing low-content-source edges stay until manually cleaned. Decided: don't retroactively delete (F040 doesn't have a delete-low-quality-edges sweep).
3. **Should F054 also raise `NOUS_CROSS_ENCODER_MAX_CANDIDATES` from 30 to 50?** With looser cosine, more candidates may pass CE rerank. If `union_size_p95` consistently hits 28+, raise the cap. Decided in impl plan after first eval run.

These are answered in the impl plan, not here.
