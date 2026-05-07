# 2026-05-03 — Memory tuning session

**Branch:** `tune/memory-recall-and-sleep` → `eval/spreading-rrf-configs` (PR #413)
**Final commit:** `132992e` (eval configs) + `ea9b040` (F052/F053/F054 impl)
**Eval DB:** `nous-eval-scratch` (Docker on `127.0.0.1:5433`)
**Agents:** `nous-prod-snapshot` (2027 facts), `nous-lme-corpus` (933 facts + 200 episodes)
**Qrel sources:** `nous_prod` (40q), `nous_prod_procedures` (50q), `longmemeval` (20q)

## Session goal

Add tunable configs to the F051 retrieval eval harness for previously
untested knobs (spreading activation, RRF parameters), measure their
effect, and ship anything that demonstrably improves retrieval quality.

## TL;DR

| Subsystem | Result | Action |
|-----------|--------|--------|
| Spreading activation gate (force-on, threshold sweep) | 0% metric movement on both corpora; gate fires correctly but reorderings happen below gold position | No change to defaults |
| RRF parameter tuning (`vector_weight`, `rrf_k`) | Max ±1.6% MRR with extreme settings; corpus-noise | No change to defaults |
| Eval harness routing of `vector_weight`/`rrf_k` | Bug: flags silently ignored (resolver bypassed eval Settings) | **Fixed** (`6d0eb27`) |
| CE rerank (force off) | Codebase: -2.2% MRR (CE helps); Personal-Q&A: **+5.2% MRR / +14.2% nDCG** (CE hurts) | F052 attempted to gate by episode-share — **dropped**, heuristic falsified |
| MMR stacking (`ce_mmr_*` lambdas) | All variants ≤ baseline on both corpora | F030.1 default `mmr_skip_after_ce=True` validated |
| Channel isolation (`vector_only`, `keyword_only`) | `vector_only` ties default RRF byte-for-byte on lme, -0.2% on prod; `keyword_only` collapses (MRR 0.07/0.35) | **F054 shipped**: `hybrid_search_keyword_enabled` opt-out flag |
| F031 MERGE orphan supersede chain | Bug fixed in PR #412; orphan edges remain in graph | **F053 shipped**: sleep-cycle dead-edge pruning |

## 1. Spreading activation gate

### Hypothesis

The `auto` gate at density >= 3.0 is the default. Forcing it on (or
lowering the threshold) may surface graph-hop lift the gate currently
suppresses.

### Configs

```python
"spread_force_on":      {"spreading_activation_enabled": "true"}
"spread_force_off":     {"spreading_activation_enabled": "false"}
"spread_low_threshold": {"spreading_activation_density_threshold": 1.0}
```

### Results (90 qrels, nous_prod + nous_prod_procedures)

| config | spreading_used | queries diverged | MRR |
|---|---:|---:|---:|
| baseline | 0/90 | — | 0.828 |
| spread_force_on | 10/90 | 9/90 | 0.828 |
| spread_force_off | 0/90 | 0/90 | 0.828 |
| spread_low_threshold | 10/90 | 9/90 | 0.828 |

### Finding

Spreading fires when expected (`spreading_activation_used` counter
increments correctly), reorders 9/90 queries' results — but the
reorderings happen *below* the gold position. CE rerank dominates the
top of the list; spreading-pulled neighbors don't outrank what's
already there.

### Decision

**No change.** Defaults are correct for these corpora. Spreading is a
no-op in measurable terms; doesn't hurt either, so leave it.

### Raw report

`reports/spread_rrf_sweep/2026-05-03T22-06-36_baseline-spread_force_on-spread_force_off-spread_low_threshold-rrf_vector_heavy-rrf_balanced-rrf_keyword_heavy-rrf_k_low-rrf_k_high.{md,json}`

## 2. RRF parameter tuning (initial — silently broken)

### Hypothesis

`vector_weight` (default 0.7) and `rrf_k` (default 60) are tunable.
Different settings should produce different rank orders.

### Configs

```python
"rrf_vector_heavy":   {"vector_weight": 0.9}
"rrf_balanced":       {"vector_weight": 0.5}
"rrf_keyword_heavy":  {"vector_weight": 0.3}
"rrf_k_low":          {"rrf_k": 10}
"rrf_k_high":         {"rrf_k": 200}
```

### Results

All 5 configs produced **byte-identical** retrieved IDs on every query.

### Finding (root cause)

Eval-harness bug. `_resolve_vector_weight()` and `_resolve_rrf_k()` in
`nous/heart/search.py` construct a fresh `Settings()` from env vars,
ignoring the eval-modified Settings instance. Eval `RetrievalConfig.flags`
were silently dropped.

### Decision

**Fixed.** Added two-line patch in
`nous_eval/retrieval_runner.py::_apply_config_flags` to push these
specific keys into `RuntimeConfig` (which the resolver does check):

```python
if "vector_weight" in update:
    RuntimeConfig.get().set_vector_weight(float(update["vector_weight"]))
if "rrf_k" in update:
    RuntimeConfig.get().set_rrf_k(int(update["rrf_k"]))
```

Commit `6d0eb27`. Verification: `rrf_vector_heavy=0.9` now diverges
from baseline on Q34 at rank 5 (was 0/90 before).

### Raw report

`reports/rrf_sweep_v2/2026-05-03T22-18-41_baseline-rrf_vector_heavy-rrf_balanced-rrf_keyword_heavy-rrf_k_low-rrf_k_high.{md,json}`

## 3. RRF parameter tuning (post-fix)

### Hypothesis (revised)

With the resolver bug fixed, RRF tuning may surface lift in CE-on or
CE-off configurations.

### Configs

Same as §2 plus CE-off variants:

```python
"ce_off_rrf_vector_heavy":  {"cross_encoder_enabled": False, "vector_weight": 0.9}
"ce_off_rrf_balanced":      {"cross_encoder_enabled": False, "vector_weight": 0.5}
"ce_off_rrf_keyword_heavy": {"cross_encoder_enabled": False, "vector_weight": 0.3}
"ce_off_rrf_k_low":         {"cross_encoder_enabled": False, "rrf_k": 10}
"ce_off_rrf_k_high":        {"cross_encoder_enabled": False, "rrf_k": 200}
```

### Results (90 qrels, nous_prod + nous_prod_procedures)

CE-on RRF tuning: all configs tied baseline at MRR=0.828. CE rerank
dominates top-K so RRF reorderings get washed out.

CE-off RRF tuning:

| config | MRR | Δ vs ce_off |
|---|---:|---:|
| ce_off | 0.810 | — |
| ce_off_rrf_k_low (k=10) | **0.816** | **+0.7%** |
| ce_off_rrf_balanced | 0.808 | -0.2% |
| ce_off_rrf_vector_heavy (0.9) | 0.805 | -0.6% |
| ce_off_rrf_keyword_heavy (0.3) | 0.797 | -1.6% |
| ce_off_rrf_k_high (k=200) | 0.733 | -9.5% |

### Finding

RRF tuning is mostly corpus-noise. Only `rrf_k=10` shows a marginal
+0.7% lift; `rrf_k=200` is a clear loser at -9.5%. Within the
operating range no setting meaningfully beats default.

### Decision

**No change to defaults.** `vector_weight=0.7` and `rrf_k=60` stand.
Configs remain in the eval matrix as measurement instruments.

### Raw report

`reports/exp1_ce_off_rrf/2026-05-03T22-33-28_ce_off-ce_off_rrf_vector_heavy-ce_off_rrf_balanced-ce_off_rrf_keyword_heavy-ce_off_rrf_k_low-ce_off_rrf_k_high.{md,json}`

## 4. Cross-encoder rerank — corpus split (the headline finding)

### Hypothesis

CE rerank (F042, currently default-off in env) helps recall on these
corpora. Toggle `ce_off`/`ce_on` to measure.

### Configs

```python
"ce_off": {"cross_encoder_enabled": False}
"ce_on":  {"cross_encoder_enabled": True}
```

### Results

| corpus | ce_on (baseline) | ce_off | Δ |
|---|---:|---:|---:|
| **nous_prod (90q)** | **MRR 0.828** | 0.810 | CE **helps** +2.2% |
| **longmemeval (20q)** | MRR 0.892 | **0.938** | CE **hurts** -5.2% (and -14.2% nDCG, +12.9% R@10) |

### Finding

CE rerank's value is **corpus-dependent**. CE uses MS-MARCO-trained
MiniLM, which models "informational relevance" — works on codebase
queries (`nous_prod` short/conceptual) but down-ranks correctly-retrieved
personal facts on dialogue queries (`longmemeval`: "What is my preferred
gin ratio?").

This is a structural limitation of the model, not a tuning artifact.

### Decision

This is the strongest signal of the session. Surfaces the F052 design
question: **make CE conditional per-query**. See §6 for the attempt
and why it failed.

### Raw report

`reports/exp_longmemeval/2026-05-03T22-37-28_baseline-ce_off-ce_off_rrf_k_low-spread_force_on-rrf_k_low.{md,json}`

## 5. MMR stacking on top of CE

### Hypothesis

F030.1 default `mmr_skip_after_ce=True` skips MMR when CE just
reordered the head, on the prior finding that chained CE→MMR
neutralizes CE's lift. Re-validate by forcing MMR back on at
varying λ.

### Configs

```python
"ce_mmr_on_lambda_0.7":  {"cross_encoder_enabled": True, "mmr_enabled": True, "mmr_skip_after_ce": False, "mmr_diversity_weight": 0.7}
"ce_mmr_on_lambda_0.85": ... lambda=0.85
"ce_mmr_on_lambda_0.95": ... lambda=0.95
```

### Results

**nous_prod (90q):**

| config | MRR |
|---|---:|
| baseline (CE+skip-MMR) | **0.828** |
| ce_mmr_λ=0.7 | 0.797 (-3.7%) |
| ce_mmr_λ=0.85 | 0.810 (-2.2%) |
| ce_mmr_λ=0.95 | 0.812 (-1.9%) |

**longmemeval (20q):**

| config | MRR |
|---|---:|
| baseline (CE+skip-MMR) | 0.892 |
| ce_off | **0.938** |
| ce_mmr_λ=0.7 | 0.931 (-0.7%) |
| ce_mmr_λ=0.85 | 0.933 (-0.4%) |
| ce_mmr_λ=0.95 | 0.935 (-0.3%) |

### Finding

F030.1 default `mmr_skip_after_ce=True` is correct on both corpora.
Forcing MMR back on after CE never improves; only at `λ=0.95`
(near-pure relevance, MMR almost a no-op) does it approach baseline.

### Decision

**No change.** F030.1 stays as default. Configs remain as measurement
instruments.

### Raw report

`reports/exp2_mmr_combos_prod/2026-05-03T22-45-40_*.md`
`reports/exp2_mmr_combos_lme/2026-05-03T22-48-57_*.md`

## 6. F052 — Conditional CE rerank by memory-type dominance (FAILED)

### Goal

Recover the +5.2% MRR longmemeval lift (§4) without losing the +2.2%
nous_prod lift. Make CE rerank conditional per-query so it runs only
when it helps.

### Hypothesis

Personal-Q&A queries surface episode-shaped candidates; CE down-ranks
correctly-retrieved personal facts on those queries. Episode-share in
the candidate set should be a deterministic, zero-latency proxy for
"this is dialogue recall — skip CE."

### Implementation

```python
# heart.py — gate inserted before CE invocation
if (ce_enabled and self.settings.cross_encoder_episode_skip_enabled and len(merged) > 0):
    head_size = min(self.settings.cross_encoder_max_candidates, len(merged))
    head_sorted = sorted(merged, key=lambda r: r.score, reverse=True)
    head_slice = head_sorted[:head_size]
    episode_count = sum(1 for r in head_slice if r.type == "episode")
    episode_share = episode_count / head_size if head_size else 0.0
    if episode_share >= self.settings.cross_encoder_episode_skip_threshold:
        ce_skipped_by_episode_gate = True
```

Settings: `cross_encoder_episode_skip_enabled=True`,
`cross_encoder_episode_skip_threshold=0.5`.

### Configs

```python
"f052_on":                   {"cross_encoder_enabled": True, "cross_encoder_episode_skip_enabled": True, "cross_encoder_episode_skip_threshold": 0.5}
"f052_off_explicit":         ... skip_enabled=False
"f052_low_threshold":        ... threshold=0.15
"f052_very_low_threshold":   ... threshold=0.05
```

### Results

| corpus | baseline MRR | f052_on | f052_low | f052_very_low |
|---|---:|---:|---:|---:|
| nous_prod (90q) | 0.828 | 0.828 | — | — |
| longmemeval (20q) | 0.892 | 0.892 | 0.892 | 0.892 |

**Episode-share in retrieved top-K:**

| corpus | type distribution | queries with episode_share ≥ 0.5 |
|---|---|---|
| nous_prod | 52% procedure / 31% fact / 14% decision / **3% episode** | 5/90 |
| longmemeval | **100% fact** / 0% episode | 0/20 |

### Finding

The heuristic doesn't fire on either eval corpus.

- **longmemeval** ingests dialogue as **facts** (not episodes); recall
  returns 100% facts. Episode-share = 0 always.
- **nous_prod** has procedures and facts dominating; episodes are 3%
  of retrieved results. Only 5/90 queries hit the threshold; for those
  5 the gate skip was harmless (no metric change).

The +5.2% longmemeval lift from `ce_off` doesn't correlate with
episode-share. It correlates with *something else* about query/data
shape — query-text features, score-distribution, or a learned signal —
that this heuristic doesn't capture.

### Decision

**Drop F052.** Mechanism is wrong; shipping a flag that demonstrably
does nothing on the eval corpora is dead weight.

The underlying problem (per-query CE gating) remains worth solving.
Open follow-up:

- Profile what specifically about longmemeval queries makes CE wrong
  (query length? embedding cluster? personal pronouns?)
- Build a small classifier (heuristic or Haiku call) mapping
  query → "use CE? yes/no"
- Re-eval

### Raw reports

- `reports/validate_lme/2026-05-03T23-42-04_*.md` (initial f052_on, 0% lift)
- `reports/validate_lme_v2/2026-05-03T23-45-14_*.md` (threshold sweep, still 0%)
- `reports/validate_prod/2026-05-03T23-39-47_*.md` (nous_prod, identical)

## 7. F053 — Orphan-edge sleep cleanup phase (SHIPPED)

### Motivation

PR #412 fixed the F031 MERGE supersede-chain bug (Codex P1) but
existing orphan supersede chains remained — facts deactivated by
F031/F027 with edges in `brain.graph_edges` still pointing at them.
Spreading activation walks edges without an `active` filter, wasting
per-hop activation budget on dead nodes.

### Implementation

New sleep phase `_phase_prune_dead_edges` runs after
`graph_densification`:

```sql
WITH inactive_nodes AS (
    SELECT id, 'fact' AS node_type FROM heart.facts WHERE agent_id=:agent_id AND active=false
    UNION ALL
    SELECT id, 'episode' FROM heart.episodes WHERE agent_id=:agent_id AND active=false
    UNION ALL
    SELECT id, 'procedure' FROM heart.procedures WHERE agent_id=:agent_id AND active=false
), victim_edges AS (
    SELECT e.id FROM brain.graph_edges e
    WHERE e.agent_id=:agent_id
      AND ((e.source_type, e.source_id) IN (SELECT node_type, id FROM inactive_nodes)
           OR (e.target_type, e.target_id) IN (SELECT node_type, id FROM inactive_nodes))
    LIMIT :max_per_cycle
)
DELETE FROM brain.graph_edges WHERE id IN (SELECT id FROM victim_edges) RETURNING id;
```

Bounded by `dead_edge_pruning_max_per_cycle` (default 1000) so the
exclusive lock never holds longer than ~1s of pruning.

Settings: `dead_edge_pruning_enabled=True`,
`dead_edge_pruning_max_per_cycle=1000`.

### Code review findings (addressed inline)

- **P1 — `brain.decisions` has no `active` column.** Initial SQL would
  have crashed every sleep cycle. Fixed: dropped the `decisions` UNION
  ALL branch (decisions are append-only and reviewed-not-deleted). Comment
  added for when soft-delete lands.
- **P2 — Silent-failure surface.** Added
  `sleep_stats["dead_edges_prune_error"] = type(exc).__name__` so
  observability dashboards can detect regressions.
- **P3 — Missing test for SQL drift.** Added
  `test_sql_does_not_reference_decisions_active` to catch the regression.

### Tests

`tests/test_f053_dead_edge_prune.py` — 10 tests, all pass:
- defaults, flag-off no-op, max=0/negative no-op, success records count,
  zero-result records zero, db-exception records error type and returns
  False, query bindings, SQL doesn't reference decisions.

### Decision

**Ship F053.** Real bug fix, contained risk (sleep-cycle housekeeping,
not in retrieval hot path), well-tested.

### Validation note

Not measurable by retrieval eval — affects future graph density and
spreading activation budget, not retrieval order. Will show up over
time in `/dashboard/density` orphan-rate metric.

## 8. F054 — Keyword channel toggle in hybrid_search (SHIPPED)

### Motivation

Channel-isolation eval showed `vector_only` ties default RRF byte-for-byte
on longmemeval and within -0.2% on nous_prod, while `keyword_only`
collapses (MRR 0.07/0.35). Keyword channel adds compute (one FTS query
per recall) for ~zero recall benefit on these workloads.

### Implementation

```python
# heart/search.py
keyword_enabled = _resolve_keyword_enabled() or embedding is None
if keyword_enabled:
    # ... existing FTS query
    keyword_results = [(row.id, float(row.score)) for row in result.all()]
# else: keyword_results stays []
# _rrf_merge handles empty list correctly — degenerates to vector-only
```

`embedding is None` force-on preserves the keyword-only fallback path.

Settings: `hybrid_search_keyword_enabled=True` (default; preserves
current behavior).

### Configs

```python
"f054_keyword_off": {"cross_encoder_enabled": True, "hybrid_search_keyword_enabled": False}
```

### Results

| corpus | baseline | f054_keyword_off | Δ |
|---|---:|---:|---:|
| nous_prod (90q) | 0.828 | 0.828 | identical |
| longmemeval (20q) | 0.892 | 0.892 | identical |

### Finding

Predicted: ties baseline. Validates flag wires to production hot path.
Operator opt-out is real and lossless on these corpora.

### Caveat documented

Empty keyword channel uniformly suppresses the keyword half of the RRF
score (penalty rank), preserving order but shifting absolute scores.
May interact with `relevance_floor` / `staleness_penalty` consumers
if flipped off in prod.

### Tests

`tests/test_f054_keyword_toggle.py` — 6 tests, all pass:
- defaults, flag-on runs both channels, flag-off skips keyword SQL,
  embedding=None forces keyword on, vector-only RRF degeneration,
  resolver fallback to True on Settings exception.

### Decision

**Ship F054.** Default True (no behavior change). Operator opt-out
documented in env-var help text.

### Raw report

`reports/exp3_channel_iso_lme/2026-05-03T23-06-37_ce_off-vector_only-keyword_only.{md,json}`
`reports/exp3_channel_iso_prod/2026-05-03T23-05-33_ce_off-vector_only-keyword_only.{md,json}`

## Decisions log

| Decision | Outcome | Branch / commit |
|----------|---------|-----------------|
| Fix RRF resolver bypass in eval harness | **Shipped** | `6d0eb27` (PR #413) |
| Add 8 spreading + RRF tuning configs | **Shipped** | `e5b1ca9` (PR #413) |
| Add 7 CE-off + channel-isolation configs | **Shipped** | `631d9d0` (PR #413) |
| F052 conditional CE rerank by episode-share | **Dropped** — heuristic falsified by eval | (unstaged on `tune/memory-recall-and-sleep`) |
| F053 orphan-edge sleep cleanup | **Shipped** to branch | `ea9b040` |
| F054 keyword channel toggle | **Shipped** to branch | `ea9b040` |
| Per-query CE gating (proper design) | **Open follow-up** — needs profiling + classifier | TBD |

## Next steps

1. **PR #413** (eval harness configs): merge after review.
2. **`tune/memory-recall-and-sleep` branch**: drop F052 hunks
   (`nous/heart/heart.py`, `nous/config.py` F052 settings,
   `tests/test_f052_conditional_ce.py`), open PR for F053 + F054 only.
3. **Open issue: "Per-query CE gating"** — link this doc, document the
   F052 falsification, propose Haiku-classifier or query-text-feature
   approach.
4. **Production deployment** of F053: ship after PR review. Expect
   `dead_edges_pruned` counter in `sleep_completed` events.
5. **Production deployment** of F054: default-True is no-op; flip to
   False per-deployment for vector-dominant corpora after operator A/B.
