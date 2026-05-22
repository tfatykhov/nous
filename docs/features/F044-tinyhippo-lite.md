# F044 — tinyHippo-Lite: Algorithmic Sleep Consolidation

> **Status:** Draft
> **Priority:** P2
> **Depends on:** F031-b (Consolidation Orient & Resolve — shipped), F040 (Graph Densification — shipped)
> **Related:** F041 (SNN Sleep Densification — Draft, .h5 ingestion), F053 (Density-Eval Harness — shipped, used by the Run A/B/C harness), tinyHippo (github.com/max-talanov/tinyHippo)
> **Author:** Nous + Tim
> **Created:** 2026-05-08
> **Updated:** 2026-05-08

---

## Thesis

Lift tinyHippo's **biologically-grounded constants and procedure** — Synaptic Tagging and Capture (STC), bidirectional SWR replay, and Vyazovskiy-style multiplicative homeostatic downscaling — into Nous as a **pure-Python algorithm** running on Nous's actual memory graph. No NEST, no spiking simulation, no .h5 file dependency. The algorithm replaces ad-hoc edge decay constants with values validated by Talanov's hippocampal microcircuit work.

This is **not a substitute for F041**. F041 ingests outputs from a real SNN simulation. F044 implements the *algorithmic skeleton* of that simulation directly on Nous's graph. They can coexist; F044 ships first because it has zero external runtime dependencies.

---

## Problem Statement

Nous's sleep cycle currently performs:

1. **F040 graph densification** — cosine-similarity backfill on orphan nodes
2. **Dead-edge pruning** — implicit cleanup of edges incident to inactive nodes (not a numbered feature; spread across the sleep handler)
3. **F031-b contradiction resolution** — supersession via LLM (`Consolidation Orient & Resolve`, PR #232)
4. **Generic edge weight maintenance** — implicit, scattered, no principled decay

What's missing:

- **No multiplicative downscaling pass.** The Vyazovskiy 2008 / Tononi–Cirelli SHY hypothesis says synaptic strengths are globally rescaled during sleep so that net potentiation across the day is renormalized. Nous has no analog. Edge weights drift monotonically upward as activity accumulates.
- **No tag/capture gating.** F040 promotes edges based on cosine similarity at the moment of densification — there is no concept of a *tagged* candidate edge that needs sustained reinforcement (PRP > threshold) before it consolidates into long-term graph structure.
- **No bidirectional replay.** Sleep does not exercise edges in both directions (forward and reverse traversal) the way SWR replay does in CA3, which is what makes tinyHippo's consolidation competitive rather than uniform.
- **No falsification harness.** We cannot answer "does Nous's sleep cycle actually improve retrieval vs a no-op baseline?" — we have no A/B/C rig.

The previously-identified **topology gap** (Nous's graph is not anatomically hippocampal; tinyHippo's graph is a sequential chain) prevents direct simulation. This spec sidesteps the gap by lifting only the *parts that don't depend on neural topology*: the constants, the procedural ordering, and the test methodology.

---

## What We Lift From tinyHippo (Gap-Independent)

| tinyHippo concept | Nous translation | Source |
|---|---|---|
| Multiplicative downscale α=0.75 | Per-sleep-cycle rescale of non-consolidated edge weights | Vyazovskiy 2008 / Tononi–Cirelli SHY; tinyHippo `replay_scaled.py` Phase 4 |
| α-sweep envelope 0.50/0.75/0.90 | Validated sensitivity band; alarm if tuned outside | tinyHippo `alpha_sweep_commands.sh` |
| PRP threshold ≈ 3.5 | Cumulative reinforcement count above which a tagged edge consolidates | tinyHippo STC hook; observed in `replay_12pct_stc.h5` |
| STC tag/capture gate | Two-tier edge state: `tagged` (provisional) → `consolidated` (permanent) | tinyHippo `stc/n_ltp_total`, `stc/w_final` |
| Bidirectional SWR replay | Sleep phase walks tagged-edge chains forward and reverse | tinyHippo `swr_fwd_*` / `swr_rev_*` windows |
| HDF5 schema | Output format for sleep-cycle telemetry, compatible with Max's reader | `figures/make_paper_figure.py` |
| Run A / B / C falsification | Nightly regression harness for sleep-cycle efficacy | tinyHippo `fig_triple_falsification.{png,pdf}` |

What we **do not** lift:

- NEST simulation
- Izhikevich neuron dynamics
- Theta rhythm modulation
- The chain-shaped CA3 topology
- Any claim that Nous "runs a hippocampal simulation"

---

## Design

### Edge State Machine

Every `brain.graph_edges` row gains two columns:

```
consolidation_state  TEXT NOT NULL DEFAULT 'tagged'    -- 'tagged' | 'consolidated'
ltp_count            INTEGER NOT NULL DEFAULT 0        -- cumulative reinforcement events (PRP analog)
```

Transitions:

- **Created** → `tagged`, `ltp_count = 0`, `weight = w_init`
- **Reinforced** (re-traversed during recall, or re-derived during densification) → `ltp_count += 1`
- **Promotion gate** during sleep Phase 8c: if `ltp_count >= NOUS_TINYHIPPO_PRP_THRESHOLD` (default 3) → `consolidation_state = 'consolidated'`
- **Homeostatic downscale** during sleep Phase 8d: if `consolidation_state = 'tagged'` → `weight *= α` (default 0.75); consolidated edges are exempt
- **Mortality** during F053 dead-edge prune: if `weight < NOUS_TINYHIPPO_WEIGHT_FLOOR` (default 0.05) AND `state = 'tagged'` → DELETE

The `consolidated` state is sticky — once promoted, an edge is never downscaled by Phase 8d. It can still be deleted by F053 if its endpoint goes inactive, or by F031 contradiction supersession.

### Sleep Cycle Integration

Three new phases in `nous/handlers/sleep_handler.py`, all guarded by `NOUS_TINYHIPPO_LITE_ENABLED`:

```
Phase 8c — STC Promotion Gate
  Promote tagged edges with ltp_count >= PRP_THRESHOLD to consolidated.
  Idempotent. Single UPDATE per agent_id. Batched by ctid.

Phase 8d — Homeostatic Downscale
  UPDATE brain.graph_edges
     SET weight = weight * :alpha
   WHERE consolidation_state = 'tagged'
     AND agent_id = :agent;

Phase 8e — Bidirectional Replay Telemetry
  For each consolidated edge, sample N forward and N reverse traversals
  via spreading_activation. Record rho_fwd / rho_rev (recall quality
  proxy: fraction of sampled walks that land on a co-tagged node).
  Pure observation — no graph mutation.
```

Order (full sleep cycle):

```
Phase 1-7   : existing (review, prune, compress, reflect, …)
Phase 8a    : F040 graph densification (cosine)
Phase 8b    : F041 SNN densification (when .h5 available)        ← future
Phase 8c    : F044 STC promotion gate                            ← NEW
Phase 8d    : F044 homeostatic downscale                         ← NEW
Phase 8e    : F044 bidirectional replay telemetry                ← NEW
Phase 9     : F053 dead-edge prune (now also drops tagged edges  ← MODIFIED
              below weight floor)
Phase 10+   : cleanup, stats
```

Promotion (8c) runs *before* downscale (8d) so freshly-promoted edges are not penalized in the same cycle.

### Reinforcement Hook (LTP Counter)

`ltp_count` increments at three call sites:

1. **Recall traversal** — `brain/spreading_activation.py` walks an edge → `ltp_count += 1` for every edge actually traversed (not just reachable). Async batched UPDATE at request end to avoid per-hop SQL overhead.
2. **Densification re-derivation** — F040 backfill rediscovers an edge that already exists with same `(source_id, target_id, relation)` → `ltp_count += 1` instead of no-op.
3. **CE survival** — F043 cross-encoder reranking keeps an existing edge → `ltp_count += 1`.

Increment is bounded: max 1 per edge per sleep cycle (debounced via `last_ltp_at` timestamp). This prevents runaway tagging from a single hot recall path.

### Configuration (Settings)

```python
# nous/config/settings.py additions
tinyhippo_lite_enabled: bool = False                  # NOUS_TINYHIPPO_LITE_ENABLED
tinyhippo_alpha: float = 0.75                         # NOUS_TINYHIPPO_ALPHA (range 0.50–0.90)
tinyhippo_prp_threshold: int = 3                      # NOUS_TINYHIPPO_PRP_THRESHOLD
tinyhippo_weight_floor: float = 0.05                  # NOUS_TINYHIPPO_WEIGHT_FLOOR
tinyhippo_replay_samples: int = 16                    # NOUS_TINYHIPPO_REPLAY_SAMPLES
tinyhippo_log_h5: bool = False                        # NOUS_TINYHIPPO_LOG_H5
tinyhippo_log_path: str = "var/tinyhippo/"            # NOUS_TINYHIPPO_LOG_PATH
```

Validation: `tinyhippo_alpha` must be in `[0.50, 0.90]`. Outside that band logs a WARN and clamps. This is the tinyHippo-validated envelope; we do not extrapolate.

### HDF5 Telemetry (Optional)

When `NOUS_TINYHIPPO_LOG_H5=true`, each sleep cycle writes one `.h5` to `var/tinyhippo/cycle_{timestamp}.h5` with the schema below. This makes Nous sleep cycles *directly comparable* to Max's MareNostrum5 outputs — same reader (`figures/make_paper_figure.py`) renders both.

```
ROOT ATTRIBUTES:
  created_utc:        ISO timestamp
  cycle_id:           UUID
  agent_id:           string
  alpha:              float (downscale factor used)
  prp_threshold:      int
  scale:              "nous-graph" (string discriminator vs tinyHippo "12% scale")
  source:             "F044-tinyhippo-lite"

GROUPS:
  /stc/
    n_ltp_total:      int[N_edges]      # ltp_count post-cycle
    w_final:          float[N_edges]    # weight post-cycle
    state:            string[N_edges]   # 'tagged' | 'consolidated'
    promoted_in_cycle: int[N_edges]     # 1 if promoted this cycle
  /homeostasis/
    rho_fwd_post_homeo: float[N_consolidated]  # forward replay quality
    rho_rev_post_homeo: float[N_consolidated]  # reverse replay quality
  /counts/
    n_tagged:         int
    n_consolidated:   int
    n_promoted:       int
    n_pruned:         int
```

This is opt-in — default off. When off, telemetry goes to standard sleep_stats only.

### Falsification Harness (Run A / B / C)

A new test module `tests/test_f044_falsification.py` runs the canonical tinyHippo triple:

```
Run A — control:    full F044 enabled (tag → promote → downscale)
Run B — STC off:    PRP_THRESHOLD = 999 (nothing promotes; everything gets downscaled)
Run C — pruning on: standard A + aggressive weight_floor=0.10
```

Metric: retrieval P@10 on the F051 eval harness held-out set, before and after a synthetic 7-cycle sleep sequence on a fixed corpus.

**Pass criteria:**
- Run A retrieval P@10 ≥ Run B P@10 (consolidation must beat no-consolidation)
- Run A retrieval P@10 ≈ Run C P@10 within ±5pp (consolidated edges survive aggressive pruning)
- Run B graph weight L1 norm at cycle 7 ≤ 0.5× cycle 0 (downscaling without promotion collapses the graph)

If A < B, F044 is net-negative and is reverted. If A − C > 5pp, the weight floor is too aggressive. This is the same logical structure as `figures/fig_triple_falsification.{png,pdf}`.

---

## Implementation Plan

### Phase 1 — Schema + Sleep Phases (~400 LOC, ~3 days)

| Component | LOC | Description |
|---|---|---|
| `sql/migrations/0XX_f044_edge_state.sql` | ~30 | Add `consolidation_state`, `ltp_count`, `last_ltp_at` columns + index |
| `nous/brain/tinyhippo_lite.py` | ~180 | Promote / downscale / replay-sample logic; pure SQL + pure Python |
| `nous/handlers/sleep_handler.py` | ~80 | `_phase_stc_promotion`, `_phase_homeostatic_downscale`, `_phase_replay_telemetry` |
| `nous/config/settings.py` | ~20 | Five env vars + validation clamp |
| Reinforcement hooks | ~60 | Three call sites: spreading_activation, F040, F043 |
| Sleep stats schema | ~10 | New keys: `f044_promoted`, `f044_downscaled`, `f044_rho_fwd_mean`, `f044_rho_rev_mean` |
| Tests | ~120 | Unit tests for state machine, integration test for full cycle |

### Phase 2 — Falsification Harness (~150 LOC, ~2 days)

| Component | LOC | Description |
|---|---|---|
| `tests/test_f044_falsification.py` | ~120 | Run A / B / C on F051 corpus |
| Integration with eval harness | ~30 | Hook into `nous_eval/density_eval.py` |

### Phase 3 — HDF5 Telemetry (~120 LOC, ~1 day)

| Component | LOC | Description |
|---|---|---|
| `nous/brain/tinyhippo_h5_logger.py` | ~100 | Write per-cycle `.h5` matching tinyHippo schema |
| Conditional dependency | ~20 | `h5py` optional import; feature-flag-gated |

**Total:** ~670 LOC across 3 phases. Phase 1 alone ships a working consolidation system. Phase 2 validates it. Phase 3 adds interop with Max's pipeline.

---

## Dependencies

- No new required runtime deps for Phase 1 or 2.
- `h5py` optional for Phase 3 (already required transitively by F041 if that ships).
- F051 eval corpus (already available) for Phase 2.

---

## Risks

| Risk | Probability | Mitigation |
|---|---|---|
| Run A < Run B (consolidation hurts retrieval) | Medium | Spec requires immediate revert; test harness is a hard gate |
| `ltp_count` increment becomes hot path bottleneck | Low | Async batched UPDATE at request end, debounced per cycle |
| α=0.75 drives all weights to zero in long-running agents | Medium | Promotion gate keeps reinforced edges; weight_floor sweeps the rest |
| `consolidation_state` migration breaks existing F040 edges | Low | DEFAULT 'tagged' on existing rows; behaves as no-op until reinforced |
| Schema divergence from F041 if both ship | Low | F044 is a strict subset of F041 telemetry schema; coexistence is read-only at h5 level |
| Tim never validates against Max's outputs | Low | Phase 3 is opt-in; A/B/C harness validates standalone |

---

## Why F044 and Not F041.1

F041 is the **data-ingestion path**: read .h5 files produced by an external NEST simulation, project into Nous's graph. It depends on Max's MareNostrum5 runs being available and on a topology mapping we have not yet built.

F044 is the **algorithmic-skeleton path**: reimplement the procedural core of tinyHippo (tag, promote, downscale, replay) directly on Nous's graph using Nous-native data. No external dependency, no topology mapping, no .h5 file required to operate.

These are different mechanisms with different risk profiles. They share constants and an output format, not code. F044 is numbered as a sibling, not a sub-feature, because it can ship without F041 ever shipping — and vice versa.

---

## Acceptance Criteria

- [ ] Migration applied; `consolidation_state` and `ltp_count` columns visible
- [ ] Three new sleep phases logged in `sleep_stats` with non-zero counts on a populated graph
- [ ] `NOUS_TINYHIPPO_LITE_ENABLED=false` (default) leaves sleep behavior bit-identical to current main
- [ ] α-sweep (0.50, 0.75, 0.90) all produce stable graphs over 7 cycles (no collapse, no explosion)
- [ ] Falsification harness: Run A ≥ Run B on retrieval P@10 with statistical significance (n=50 queries)
- [ ] Promotion gate is idempotent — running Phase 8c twice produces the same row count
- [ ] Optional HDF5 output readable by `figures/make_paper_figure.py` from the tinyHippo repo
- [ ] No regression on F051 multi-turn replay benchmark with feature off
- [ ] Documentation: README section pointing at this spec; INDEX.md entry

---

## Out of Scope

- Spiking simulation of any kind
- Theta rhythm modulation
- CA3 chain-shaped topology induction (the gap)
- Predicting *which specific* memories will consolidate from neural dynamics — F044 only implements *the rule*, not the prediction
- Replacing F040 (cosine densification) — F044 sits downstream and operates on whatever F040 produces

---

## References

- tinyHippo: https://github.com/max-talanov/tinyHippo
- `replay_scaled.py` Phase 4 — homeostatic downscale implementation
- `alpha_sweep_commands.sh` — α envelope validation
- `figures/make_paper_figure.py` — reference HDF5 reader
- `figures/fig_triple_falsification.{png,pdf}` — falsification rig
- Vyazovskiy et al. 2008 — Molecular and electrophysiological evidence for net synaptic potentiation in wake and depression in sleep
- Tononi & Cirelli 2014 — Synaptic Homeostasis Hypothesis (SHY)
- Frey & Morris 1997 — Synaptic Tagging and Capture (STC)
- F031: `docs/features/F031-consolidation-orient-resolve.md`
- F040: `docs/features/F040-graph-densification.md`
- F041: `docs/features/F041-snn-sleep-densification.md`
- F051: `docs/features/F051-retrieval-eval-harness.md`
- F053: `docs/features/F053-density-ops-harness.md`
- Nous sleep handler: `nous/handlers/sleep_handler.py`
- Graph edge schema: `sql/migrations/016_graph_edges_polymorphic.sql`
