# F017 — Context Quality Gate & Relevance-Aware Memory Assembly

**Status:** Draft (v2 — all P1/P2 review findings addressed)
**Author:** Emerson (spec), Tim (requirements)
**Created:** 2026-03-07
**Revised:** 2026-03-07
**Priority:** High
**Depends on:** F016 (context pruning), 008 (tiered context model)
**Trigger:** With 1M context models, budget ceilings can scale up — but more space without quality control leads to context pollution. Marginally relevant facts, stale decisions, and low-score episodes dilute the signal the model needs.
**Reviews:** F016/F017 joint review — 4 P1s, 8 P2s. All addressed in v2.

---

## Problem

The current context assembly (`cognitive/context.py`) fills memory budgets greedily:

1. Semantic search returns N results ranked by score
2. Results are truncated to fit the token budget
3. **There is no relevance floor** — if the budget says 1500 tokens of facts, it fills 1500 tokens regardless of whether result #8 is actually relevant or just the least-irrelevant thing in the database

This works at small budgets (8K total) because there's no room for noise. But with 1M context models enabling 2-3x larger budgets (F016 Phase 2), the greedy fill becomes a liability:

- Low-relevance facts dilute high-relevance ones
- The model processes 20K of context when only 8K is useful
- Stale decisions and episodes consume budget that should stay empty
- More tokens = more cost with zero benefit

**The goal:** Budget is a ceiling, not a target. Empty space is better than noise.

---

## Current Architecture

### Context Assembly Pipeline (per memory type)

```
retrieve → apply_frame_boost → dedup → usage_boost → truncate_to_budget
```

From `context.py` line 116+:

| Step | What it does | Quality control? |
|------|-------------|-----------------|
| retrieve | `heart.search_facts(query, limit=N)` | ❌ No score floor |
| apply_frame_boost | Boost scores for frame-relevant categories | ✅ Helps relevance |
| dedup | Filter overlapping content (PR #101) | ✅ Removes duplicates |
| usage_boost | Feedback from usage tracker (D3) | ✅ Learns over time |
| truncate_to_budget | Hard cut at token limit | ❌ No quality check |

**Gap:** No step filters by relevance quality. Steps 2-4 re-rank, but a low-relevance result that gets boosted is still low-relevance — boosting doesn't create signal that isn't there.

### Current Budget Defaults

| Frame | Total | Decisions | Facts | Procedures | Episodes |
|-------|-------|-----------|-------|------------|----------|
| conversation | 3,000 | 500 | 500 | 0 | 0 |
| question | 6,000 | 1,000 | 1,500 | 500 | 500 |
| task | 8,000 | 2,000 | 1,500 | 1,500 | 1,000 |
| decision | 12,000 | 3,000 | 2,000 | 2,000 | 1,000 |
| debug | 10,000 | 1,500 | 1,000 | 2,500 | 1,000 |
| creative | 6,000 | 1,000 | 1,500 | 500 | 500 |

---

## Proposed Changes

### Phase 1: Relevance Score Floor

Don't include any memory item below a minimum relevance score, regardless of budget.

```python
RELEVANCE_FLOORS: dict[str, float] = {
    "fact": 0.45,       # Facts must be clearly relevant
    "decision": 0.40,   # Decisions can be looser (patterns transfer across domains)
    "procedure": 0.50,  # Procedures must match closely (wrong procedure = wrong action)
    "episode": 0.35,    # Episodes provide broad context, looser threshold
}

# Sources exempt from relevance floor (v2 fix P1 #2 — F016 interaction)
FLOOR_EXEMPT_SOURCES: set[str] = {
    "pre_prune_extraction",  # F016: facts extracted before hard-clear
}
```

> **v2 fix (P1 #1 + P2 #5):** Floor is applied AFTER frame_boost and usage_boost, not before. The original ordering would discard items that frame boost or usage feedback would have rescued. The floor checks final scores after all boosts have been applied.

Applied **after** frame_boost, dedup, and usage_boost — immediately before truncation:

```python
def _apply_relevance_floor(
    self, results: list[T], memory_type: str, score_attr: str = "score"
) -> list[T]:
    """Remove results below the relevance floor for this memory type.

    Applied AFTER all boosts (frame, usage) so boosted items get a
    fair chance. The floor checks final effective scores.
    """
    floor = RELEVANCE_FLOORS.get(memory_type, 0.40)
    return [
        r for r in results
        if getattr(r, score_attr, 0) >= floor
        or getattr(r, "source", None) in FLOOR_EXEMPT_SOURCES
    ]
```

**Effect:** If only 3 facts score above 0.45 after all boosts, you get 3 facts — not 10 padded with noise. Budget is a ceiling, not a target. Items that were boosted by frame relevance or usage feedback are evaluated at their boosted score.

### Phase 2: Diminishing Returns Cutoff

Detect sharp relevance drops between consecutive results and stop there:

```python
def _apply_diminishing_returns_cutoff(
    self,
    results: list[T],
    score_attr: str = "score",
    drop_ratio: float | None = None,  # Uses config setting if None
) -> list[T]:
    """Cut results at sharp score drops.

    If result[i].score < result[i-1].score * drop_ratio, stop at i.
    This finds natural relevance boundaries.

    Example: scores [0.82, 0.79, 0.75, 0.31, 0.28]
    → drop from 0.75 to 0.31 (0.31 < 0.75 * 0.6 = 0.45) → cut at position 3
    """
    if drop_ratio is None:
        drop_ratio = 0.6  # Default; override via NOUS_RELEVANCE_DROP_RATIO
    if len(results) < 2:
        return results
    for i in range(1, len(results)):
        score = getattr(results[i], score_attr, 0)
        prev_score = getattr(results[i - 1], score_attr, 0)
        if prev_score > 0 and score < prev_score * drop_ratio:
            return results[:i]
    return results
```

**Applied after relevance floor (which is itself after all boosts).** The floor removes absolute noise; the cutoff finds the natural boundary within the remaining results. Both operate on final boosted scores.

### Phase 3: Model-Aware Budget Scaling

Scale budget ceilings based on context window size (cross-ref F016 Phase 2), but only as a ceiling — Phases 1-2 prevent filling with noise.

```python
def _scaled_budget(self, base_budget: int, context_window: int) -> int:
    """Scale budget ceiling for larger context windows.

    The ceiling grows, but relevance floor + cutoff ensure
    only high-quality content fills the extra space.
    """
    if context_window >= 1_000_000:
        return int(base_budget * 2.5)
    elif context_window >= 200_000:
        return int(base_budget * 1.5)
    return base_budget
```

**Scaled budgets (1M context models):**

| Frame | Total | Decisions | Facts | Procedures | Episodes |
|-------|-------|-----------|-------|------------|----------|
| conversation | 7,500 | 1,250 | 1,250 | 0 | 0 |
| question | 15,000 | 2,500 | 3,750 | 1,250 | 1,250 |
| task | 20,000 | 5,000 | 3,750 | 3,750 | 2,500 |
| decision | 30,000 | 7,500 | 5,000 | 5,000 | 2,500 |
| debug | 25,000 | 3,750 | 2,500 | 6,250 | 2,500 |
| creative | 15,000 | 2,500 | 3,750 | 1,250 | 1,250 |

**In practice:** Most queries won't fill these budgets. A `task` frame with a 20K ceiling might only use 10K because only 10K of content passes the relevance floor. That's the intended behavior — the extra 10K stays empty rather than filled with noise.

### Phase 4: Context Fill Ratio Logging

Track how much of the budget is actually used vs available:

```python
logger.info(
    "Context assembly: frame=%s, budget=%d, used=%d, fill_ratio=%.1f%%, "
    "facts=%d/%d, decisions=%d/%d, procedures=%d/%d, episodes=%d/%d, "
    "floor_filtered=%d, cutoff_filtered=%d",
    frame.frame_id, total_budget, total_used,
    (total_used / total_budget * 100) if total_budget > 0 else 0,
    facts_used, facts_retrieved,
    decisions_used, decisions_retrieved,
    procedures_used, procedures_retrieved,
    episodes_used, episodes_retrieved,
    floor_filtered_count, cutoff_filtered_count,
)
```

**Key metrics:**
- **Fill ratio** — how much of the budget is used. Healthy range: 30-80%. Consistently >90% suggests the floor is too low. Consistently <20% suggests retrieval quality issues.
- **Floor filtered count** — items removed by relevance floor. If high, retrieval is returning too much noise.
- **Cutoff filtered count** — items removed by diminishing returns. Shows where the natural relevance boundary falls.

### Phase 5: Staleness Penalty

Decay relevance scores for older content:

```python
def _apply_staleness_penalty(
    self, results: list[T], half_life_days: int | None = None
) -> list[T]:
    """Apply time-decay penalty to relevance scores.

    Content older than half_life_days has its score halved.
    This prevents stale decisions and facts from filling budget
    when fresher, equally relevant content exists.

    Exemptions: facts with category 'rule' or 'preference' (timeless).

    v2 fixes:
    - Guards against None scores (keyword-only retrieval returns None)
    - Returns new list with adjusted scores instead of mutating ORM objects
      (avoids SQLAlchemy dirty-tracking side effects)
    - half_life_days configurable via NOUS_STALENESS_HALF_LIFE_DAYS
    """
    if half_life_days is None:
        half_life_days = 14  # Default; override via config
    now = datetime.utcnow()
    adjusted: list[T] = []
    for r in results:
        score = getattr(r, "score", None)
        if score is None:
            adjusted.append(r)
            continue
        created = getattr(r, "created_at", None)
        if not created:
            adjusted.append(r)
            continue
        # Exempt timeless categories
        category = getattr(r, "category", "")
        if category in {"rule", "preference"}:
            adjusted.append(r)
            continue
        age_days = (now - created).days
        if age_days > 0:
            decay = 0.5 ** (age_days / half_life_days)
            # Create a shallow copy to avoid mutating ORM objects
            from copy import copy
            r_copy = copy(r)
            r_copy.score = score * max(decay, 0.3)
            adjusted.append(r_copy)
        else:
            adjusted.append(r)
    return adjusted
```

**Rationale:** A decision from 2 months ago with score 0.65 is less useful than a decision from yesterday with score 0.60. Without staleness penalty, the older one wins on raw score alone. The 14-day half-life means:
- 1 week old: ~71% of original score (0.5^(7/14) = 0.707)
- 2 weeks: 50% (0.5^(14/14) = 0.5)
- 1 month: ~25% (0.5^(30/14) = 0.228)
- Exempt: rules and preferences (they don't go stale)

---

## Updated Pipeline

> **v2 fix:** Floor and cutoff moved AFTER all boosts to prevent discarding items that would have been rescued by frame_boost or usage_boost.

```
retrieve
  → apply_staleness_penalty     (Phase 5 — decay old content scores)
  → apply_frame_boost           (existing — boost frame-relevant categories)
  → dedup                       (existing, PR #101 — remove overlaps)
  → usage_boost                 (existing, D3 — learn from feedback)
  → apply_relevance_floor       (Phase 1 — remove noise on FINAL scores)
  → apply_diminishing_cutoff    (Phase 2 — find natural boundary)
  → truncate_to_scaled_budget   (Phase 3 — ceiling, not target)
  → log_fill_ratio              (Phase 4 — observability)
```

**Why staleness is early:** Staleness adjusts raw scores before boosting. A stale item can still be rescued by frame_boost if it's relevant to the current frame. But it starts from a lower base, so equally relevant fresh content wins.

**Why floor is late:** The floor checks final effective scores. An item with raw score 0.38 that gets frame-boosted to 0.52 should pass a 0.45 floor. The old ordering would have killed it before the boost.

---

## Affected Files

| File | Change | Phase |
|------|--------|-------|
| `nous/cognitive/context.py` | `_apply_relevance_floor()`, `_apply_diminishing_returns_cutoff()`, `_apply_staleness_penalty()`, `_scaled_budget()`, fill ratio logging, updated `build()` pipeline ordering | 1-5 |
| `nous/cognitive/schemas.py` | `RELEVANCE_FLOORS`, `BUDGET_SCALE_FACTORS` constants, scaled budget calculation in `ContextBudget` | 1, 3 |
| `nous/config.py` | New settings (all with env var overrides): | 1-5 |

**New config settings (v2 — P2 #4, #8):**

```python
# Phase 1: Relevance floor
relevance_floor_enabled: bool = Field(
    default=True, validation_alias="NOUS_RELEVANCE_FLOOR_ENABLED"
)
# Phase 2: Diminishing returns
relevance_drop_ratio: float = Field(
    default=0.6, validation_alias="NOUS_RELEVANCE_DROP_RATIO"
)
# Phase 3: Budget scaling
budget_scale_enabled: bool = Field(
    default=True, validation_alias="NOUS_BUDGET_SCALE_ENABLED"
)
# Phase 5: Staleness
staleness_penalty_enabled: bool = Field(
    default=True, validation_alias="NOUS_STALENESS_PENALTY_ENABLED"
)
staleness_half_life_days: int = Field(
    default=14, validation_alias="NOUS_STALENESS_HALF_LIFE_DAYS"
)
```

> **v2 note (P2 #4):** All constants (`RELEVANCE_FLOORS`, `BUDGET_SCALE_FACTORS`) go in `schemas.py`. All runtime toggles go in `config.py`. Implementation code in `context.py` imports from both. No config dicts scattered across modules.

---

## Implementation Priority

1. **Relevance score floor** — stop including noise (highest impact, simplest change)
2. **Diminishing returns cutoff** — find natural relevance boundaries
3. **Context fill ratio logging** — observability before scaling budgets
4. **Model-aware budget scaling** — increase ceilings on 1M models
5. **Staleness penalty** — time-decay for older content

> **Recommendation:** Ship Phases 1-3 first, observe fill ratios for a week, then tune floors/cutoffs based on data before enabling Phase 4 scaling.

---

## Token Cost Analysis

| Change | Impact |
|--------|--------|
| Relevance floor | **Negative** — fewer tokens in system prompt. Saves cost. |
| Diminishing returns cutoff | **Negative** — fewer tokens. Saves cost. |
| Scaled budgets (1M models) | **Positive** — up to 2.5x more context tokens, but only if high-quality content exists |
| Staleness penalty | **Neutral** — same retrieval count, just re-ranked |
| Fill ratio logging | Zero token cost (server-side only) |
| **Net effect** | **Likely net decrease** in most sessions — floor filters more than scaling adds |

---

## Success Metrics

- Fill ratio between 30-80% across frames (not consistently >90%)
- Zero low-relevance items (<0.35 score) in assembled context
- Model references assembled context accurately (no confusion from irrelevant facts)
- Context assembly time unchanged (<100ms p95)
- Scaled budgets on 1M models don't increase cost >20% vs current

---

## Risk Assessment

| Change | Risk | Mitigation |
|--------|------|------------|
| Relevance floor too high | Important but low-scoring content gets filtered | Start conservative (0.40 default), tune based on fill ratio logs. Per-type floors allow precision. |
| Diminishing returns false positive | Sharp drop in scores within a cluster of relevant results | `drop_ratio=0.6` is conservative; only triggers on >40% drops. Adjustable per deployment. |
| Staleness penalty on valid old content | A foundational decision from month 1 gets penalized | Exempt `rule`/`preference` categories. 30% floor prevents complete disappearance. |
| Budget scaling + poor floor tuning | Scaled budgets fill with medium-relevance content | Ship logging (Phase 3) before scaling (Phase 4). Data-driven tuning. |

---

## Migration Path (v2 — P2 #7)

**Upgrading existing deployments:**

1. **All features default ON** but are individually toggleable via env vars. To disable everything and match current behavior: set `NOUS_RELEVANCE_FLOOR_ENABLED=false`, `NOUS_STALENESS_PENALTY_ENABLED=false`, `NOUS_BUDGET_SCALE_ENABLED=false`.

2. **No database changes required.** All changes are in the assembly pipeline logic — no schema migrations, no new tables.

3. **Active sessions:** Changes take effect on the next turn. No session restart needed. The pipeline is stateless — it runs fresh each turn based on current config.

4. **Recommended rollout:**
   - Week 1: Enable floor + logging only (`NOUS_STALENESS_PENALTY_ENABLED=false`, `NOUS_BUDGET_SCALE_ENABLED=false`)
   - Week 2: Review fill ratio logs, tune floors if needed
   - Week 3: Enable staleness penalty
   - Week 4: Enable budget scaling (only matters on 1M models)

5. **Rollback:** Set any feature flag to `false`. Immediate effect, no restart needed (config is read per-turn).

---

## Open Questions

1. Should relevance floors be per-frame as well as per-type? A `debug` frame might want a lower floor for procedures (cast a wider net for solutions) while a `conversation` frame wants a higher floor (only clearly relevant context).

2. Should the staleness penalty half-life be configurable per memory type? Decisions might age faster than facts (decisions are situational, facts are reference).

3. ~~How should the quality gate interact with the usage tracker (D3)?~~ **RESOLVED (v2):** Floor now applies AFTER usage_boost. Items boosted by usage feedback are evaluated at their boosted score. A low raw-score item that gets usage-boosted above the floor will pass.

4. Should there be a minimum result count guarantee? e.g., "always include at least 2 facts regardless of floor" to prevent completely empty context sections. Risk: defeats the purpose of the floor for sparse queries.
