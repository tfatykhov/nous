# F017 — Context Quality Gate & Relevance-Aware Memory Assembly

**Status:** Draft (v3 — all P1/P2/P3 review findings from v2 analysis addressed)
**Author:** Emerson (spec), Tim (requirements)
**Created:** 2026-03-07
**Revised:** 2026-03-07
**Priority:** High
**Depends on:** F016 (context pruning), 008 (tiered context model)
**Trigger:** With 1M context models, budget ceilings can scale up — but more space without quality control leads to context pollution. Marginally relevant facts, stale decisions, and low-score episodes dilute the signal the model needs.
**Reviews:** F016/F017 joint review — 4 P1s, 8 P2s. All addressed in v2. v3: deep codebase analysis — 4 P1s, 8 P2s, 2 P3s. All addressed.

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

> **Note:** The pipeline below describes the full actual pipeline from `context.py` lines 302+. "D3" refers to the usage feedback tracker (design decision 3 from spec 005.1). "D7" refers to per-frame conversation dedup windows (design decision 7).

```
retrieve → tier3_threshold → apply_frame_boost → diversity_filter → dedup → usage_boost → truncate
```

| Step | What it does | Quality control? |
|------|-------------|-----------------|
| retrieve | `heart.search_facts(query, limit=N)` | No score floor |
| tier3_threshold | Filter by `TIER3_THRESHOLDS` (0.20-0.30) when embeddings available | Removes very low scores |
| apply_frame_boost | Re-rank by frame/censor match (1.0-1.56x multiplier) | Helps relevance ordering |
| diversity_filter | Cap per-subject results (`_enforce_diversity`) | Prevents topic domination |
| dedup | Filter overlapping content via `ConversationDeduplicator` | Removes duplicates |
| usage_boost | Re-rank by usage tracker reference rate (D3) | Learns over time |
| truncate_to_budget | Hard cut at token limit | No quality check |

**Gap:** No step filters by relevance quality after all ranking is done. The existing `TIER3_THRESHOLDS` (`context.py:30-38`) provide a basic floor (0.20-0.30) but these are too low to prevent noise — they just filter out completely irrelevant results.

**Critical implementation detail:** `apply_frame_boost` and `_apply_usage_boost` currently sort by boost factors but **discard the factor values** — they return re-ordered items without modifying `.score`. This means `.score` on items reflects raw search scores only, not boosted scores. F017 changes this (see Phase 1 note below).

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

# Sources exempt from relevance floor (F016 interaction)
FLOOR_EXEMPT_SOURCES: set[str] = {
    "pre_prune_extraction",  # F016: facts extracted before hard-clear
}
```

> **v3 fix (P1-1 — boost/score mismatch):** Currently `apply_frame_boost` and `_apply_usage_boost` sort by boost factors but discard them — `.score` on items is never modified. For the floor to work on "effective" scores, **boosts must write back to `.score`**. This requires modifying both functions to apply their multipliers to the item's score attribute (via shallow wrapper — see Phase 5 note on ORM safety). The floor then checks the actual effective score after all multipliers have been applied.
>
> **Replaces `TIER3_THRESHOLDS`:** The existing tier3 thresholds (0.20-0.30, `context.py:30-38`) are superseded by these higher floors. When F017 is enabled, remove the `TIER3_THRESHOLDS` filtering and let the relevance floor handle it. When F017 is disabled, `TIER3_THRESHOLDS` remain as fallback.

**Modified boost functions** (required prerequisite for floor to work correctly):

```python
# In search.py — apply_frame_boost must write back effective scores
def apply_frame_boost(results: list, current_frame: str | None = None,
                      current_censors: list[str] | None = None) -> list:
    """Re-rank results with frame and censor boost (003.2).

    v3: Boost factors are now applied to .score so downstream
    consumers (relevance floor, staleness penalty) see effective scores.
    Uses _ScoredWrapper to avoid mutating ORM objects.
    """
    if not current_frame and not current_censors:
        return results

    boosted = []
    for item in results:
        boost = 1.0
        encoded_frame = getattr(item, "encoded_frame", None)
        if encoded_frame and current_frame and encoded_frame == current_frame:
            boost *= 1.3
        enc_censors = set(getattr(item, "encoded_censors", None) or [])
        cur_censors = set(current_censors or [])
        if enc_censors and cur_censors:
            union = enc_censors | cur_censors
            if union:
                jaccard = len(enc_censors & cur_censors) / len(union)
                boost *= 1.0 + 0.2 * jaccard
        # Apply boost to score via wrapper
        wrapped = _wrap_with_score(item, (getattr(item, "score", 0) or 0) * boost)
        boosted.append((wrapped, boost))

    boosted.sort(key=lambda x: x[1], reverse=True)
    return [item for item, _ in boosted]
```

```python
# In context.py — _apply_usage_boost must also write back
def _apply_usage_boost(self, items: list, usage_tracker: UsageTracker | None) -> list:
    if not usage_tracker or not items:
        return items
    boosted = []
    for item in items:
        mid = str(getattr(item, "id", ""))
        boost = usage_tracker.get_boost_factor(mid) if mid else 1.0
        wrapped = _wrap_with_score(item, (getattr(item, "score", 0) or 0) * boost)
        boosted.append((wrapped, boost))
    boosted.sort(key=lambda x: x[1], reverse=True)
    return [item for item, _ in boosted]
```

Applied **after** all boosts — immediately before truncation:

```python
def _apply_relevance_floor(
    self, results: list[T], memory_type: str, score_attr: str = "score"
) -> list[T]:
    """Remove results below the relevance floor for this memory type.

    Applied AFTER all boosts (frame, usage) which now write back
    effective scores to .score via _ScoredWrapper.
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

**Applied after relevance floor (which is itself after all boosts).** The floor removes absolute noise; the cutoff finds the natural boundary within the remaining results. Both operate on effective boosted scores.

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
    - half_life_days configurable via NOUS_STALENESS_HALF_LIFE_DAYS

    v3 fixes:
    - Uses datetime.now(timezone.utc) instead of deprecated datetime.utcnow()
    - Uses _ScoredWrapper instead of copy() to avoid SQLAlchemy identity map
      corruption from shallow-copying session-bound ORM instances
    """
    if half_life_days is None:
        half_life_days = 14  # Default; override via config
    now = datetime.now(timezone.utc)
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
            adjusted.append(
                _wrap_with_score(r, score * max(decay, 0.3))
            )
        else:
            adjusted.append(r)
    return adjusted
```

**`_ScoredWrapper` — ORM-safe score override** (shared by staleness penalty and boost writeback):

```python
class _ScoredWrapper:
    """Lightweight proxy that overrides .score without mutating the ORM object.

    Delegates all attribute access to the wrapped item except 'score',
    which returns the overridden value. This avoids SQLAlchemy identity
    map corruption from shallow-copying session-bound model instances.
    """
    __slots__ = ("_item", "_score")

    def __init__(self, item, score: float) -> None:
        object.__setattr__(self, "_item", item)
        object.__setattr__(self, "_score", score)

    def __getattr__(self, name: str):
        if name == "score":
            return object.__getattribute__(self, "_score")
        return getattr(object.__getattribute__(self, "_item"), name)


def _wrap_with_score(item, score: float):
    """Wrap an item with an overridden score, or update if already wrapped."""
    return _ScoredWrapper(item, score)
```

**Rationale:** A decision from 2 months ago with score 0.65 is less useful than a decision from yesterday with score 0.60. Without staleness penalty, the older one wins on raw score alone. The 14-day half-life means:
- 1 week old: ~71% of original score (0.5^(7/14) = 0.707)
- 2 weeks: 50% (0.5^(14/14) = 0.5)
- 1 month: ~25% (0.5^(30/14) = 0.228)
- Exempt: rules and preferences (they don't go stale)

### Phase 6: Enhanced Context Usage Tracking

> **Source:** ACE (Automated Context Engineering, Stanford/ICLR 2026) showed +10.6% on benchmarks by evolving playbooks based on what actually works.

**Existing capability (D3 — design decision 3 from spec 005.1):** The current usage tracker in `layer.py:510-535` already tracks both retrieval AND reference. After each turn, it computes `UsageTracker.compute_overlap(content, response_text)` using containment coefficient and calls `record_retrieval(was_referenced=overlap >= 0.15, overlap_score=overlap)`. This is NOT binary "retrieved or not" — it already has strength-weighted detection.

**What Phase 6 adds on top of D3:**

| | D3 (current) | Phase 6 enhancement |
|---|---|---|
| **Detection** | Containment coefficient (word overlap) | Same, but with tunable threshold |
| **Granularity** | Binary: overlap >= 0.15 → referenced | Multi-level: 1.0 direct quote, 0.5 paraphrase, 0.2 topic overlap |
| **Penalty signal** | Implicit via low reference rate | Explicit: items assembled but unused across N turns get score penalty |
| **Feedback target** | `get_boost_factor()` range [0.5, 1.5] | Extended range [0.3, 2.0] for stronger signal |

#### 6.1 Enhanced Reference Detection

Extend the existing post-turn tracking in `layer.py:post_turn()` (NOT in `runner.py`):

```python
# In layer.py post_turn(), replace the existing usage tracking block (lines 510-535)
if self._usage_tracker and turn_context.recalled_content_map:
    response_text = turn_result.response_text
    for mem_type, id_list in [
        ("decision", turn_context.recalled_decision_ids),
        ("fact", turn_context.recalled_fact_ids),
        ("procedure", turn_context.recalled_procedure_ids),
        ("episode", turn_context.recalled_episode_ids),
    ]:
        for mid in id_list:
            content = turn_context.recalled_content_map.get(mid, "")
            if content:
                overlap = UsageTracker.compute_overlap(content, response_text)
                # Multi-level strength (v3: replaces binary threshold)
                if overlap >= 0.5:
                    strength = 1.0  # Direct reference
                elif overlap >= 0.25:
                    strength = 0.5  # Paraphrase
                elif overlap >= 0.10:
                    strength = 0.2  # Topic overlap
                else:
                    strength = 0.0  # Not referenced
                self._usage_tracker.record_retrieval(
                    memory_id=mid,
                    memory_type=mem_type,
                    was_referenced=strength > 0,
                    overlap_score=overlap,
                )
```

#### 6.2 Extended Boost Range

Update `UsageTracker.get_boost_factor()` in `usage_tracker.py`:

```python
def get_boost_factor(self, memory_id: str) -> float:
    """Get retrieval boost factor based on usage history.

    v3: Extended range [0.3, 2.0] for stronger signal.
    Items consistently referenced get up to 2x boost.
    Items consistently ignored drop to 0.3x.
    """
    stats = self._stats.get(memory_id)
    if stats is None or stats.times_retrieved < 2:
        return 1.0
    ref_rate = stats.times_referenced / stats.times_retrieved
    # Scale: 0% referenced -> 0.3x, 50% -> 1.0x, 100% -> 2.0x
    return 0.3 + ref_rate * 1.7
```

**Over time:** Items that are consistently assembled but never used will have their effective scores lowered. Items that are consistently referenced will rise. This is the ACE principle — the context strategy evolves based on outcomes, not just initial relevance scores.

---

## Updated Pipeline

> **v3 fix:** Pipeline now reflects all actual stages. Boosts write back effective scores to `.score` via `_ScoredWrapper`. When F017 is enabled, `TIER3_THRESHOLDS` filtering is removed (superseded by relevance floor on effective scores).

```
retrieve
  → apply_staleness_penalty     (Phase 5 — decay old content scores)
  → apply_frame_boost           (existing — re-rank AND write back boosted scores)
  → diversity_filter            (existing — cap per-subject results)
  → dedup                       (existing — remove conversation overlaps)
  → usage_boost                 (existing D3 — re-rank AND write back boosted scores)
  → apply_relevance_floor       (Phase 1 — remove noise on EFFECTIVE scores)
  → apply_diminishing_cutoff    (Phase 2 — find natural boundary)
  → truncate_to_scaled_budget   (Phase 3 — ceiling, not target)
  → log_fill_ratio              (Phase 4 — observability)

# Post-response (Phase 6 — in layer.py post_turn(), enhancing existing D3 tracking):
  → compute_overlap             (existing — containment coefficient)
  → multi_level_strength        (Phase 6 — graduated reference detection)
  → record_retrieval            (existing API — extended boost range)
```

**Why staleness is early:** Staleness adjusts raw scores before boosting. A stale item can still be rescued by frame_boost if it's relevant to the current frame. But it starts from a lower base, so equally relevant fresh content wins.

**Why floor is late:** The floor checks effective scores after boosts have been applied via `_ScoredWrapper`. An item with raw score 0.38 that gets frame-boosted 1.3x to 0.49 will pass a 0.45 floor.

---

## Affected Files

| File | Change | Phase |
|------|--------|-------|
| `nous/cognitive/context.py` | `_apply_relevance_floor()`, `_apply_diminishing_returns_cutoff()`, `_apply_staleness_penalty()`, `_wrap_with_score()`, `_ScoredWrapper`, `_scaled_budget()`, fill ratio logging, updated `build()` pipeline (remove `TIER3_THRESHOLDS` when enabled), modified `_apply_usage_boost()` to write back scores | 1-5 |
| `nous/heart/search.py` | Modified `apply_frame_boost()` to write back boosted scores via `_ScoredWrapper` | 1 (prerequisite) |
| `nous/cognitive/layer.py` | Enhanced post-turn usage tracking with multi-level strength detection (replaces existing block at lines 510-535) | 6 |
| `nous/cognitive/usage_tracker.py` | Extended `get_boost_factor()` range to [0.3, 2.0] | 6 |
| `nous/cognitive/schemas.py` | `RELEVANCE_FLOORS`, `FLOOR_EXEMPT_SOURCES`, `BUDGET_SCALE_FACTORS` constants, scaled budget calculation in `ContextBudget` | 1, 3 |
| `nous/config.py` | New settings (all with NOUS_ env prefix): | 1-5 |

**New config settings (v3 — uses env_prefix, no redundant validation_alias):**

```python
# Phase 1: Relevance floor
relevance_floor_enabled: bool = True
# Phase 2: Diminishing returns
relevance_drop_ratio: float = 0.6
# Phase 3: Budget scaling
budget_scale_enabled: bool = True
# Phase 5: Staleness
staleness_penalty_enabled: bool = True
staleness_half_life_days: int = 14
```

> **v3 note (P2-5):** Settings use the `NOUS_` env_prefix from `SettingsConfigDict` — no `validation_alias` needed. `relevance_floor_enabled` automatically maps to env var `NOUS_RELEVANCE_FLOOR_ENABLED`. The `validation_alias` pattern is reserved for non-prefixed env vars like `DB_HOST` and `ANTHROPIC_API_KEY`.

> **v3 note (P2-3):** Constants (`RELEVANCE_FLOORS`, `FLOOR_EXEMPT_SOURCES`, `BUDGET_SCALE_FACTORS`) go in `schemas.py`. Runtime toggles go in `config.py`. Implementation code in `context.py` and `search.py` imports from both.

---

## Implementation Priority

1. **Prerequisite: Boost score writeback** — modify `apply_frame_boost` and `_apply_usage_boost` to write effective scores via `_ScoredWrapper`
2. **Relevance score floor** — stop including noise (highest impact, simplest change after prerequisite)
3. **Diminishing returns cutoff** — find natural relevance boundaries
4. **Context fill ratio logging** — observability before scaling budgets
5. **Model-aware budget scaling** — increase ceilings on 1M models
6. **Staleness penalty** — time-decay for older content
7. **Enhanced usage tracking** — multi-level strength detection + extended boost range

> **Recommendation:** Ship prerequisite + Phases 1-3 first, observe fill ratios for a week, then tune floors/cutoffs based on data before enabling Phase 4 scaling. Phase 6 enhances existing infrastructure and can ship independently.

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
| Boost writeback changes existing behavior | `apply_frame_boost` and `_apply_usage_boost` currently only reorder; writing back scores changes downstream consumers | Only `_apply_relevance_floor` and `_apply_staleness_penalty` read `.score` — both are new code. Existing `_truncate_to_budget` and format functions don't read `.score`. Risk is minimal. |

---

## Migration Path

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

5. Should staleness half-life be per-memory-type? Decisions are situational (shorter half-life), facts are reference (longer). Current: uniform 14 days for all.

---

## Changelog

**v3** (2026-03-07) — Deep codebase analysis fixes:
- **P1-1:** Boosts don't modify `.score` — added `_ScoredWrapper` proxy and modified `apply_frame_boost`/`_apply_usage_boost` to write back effective scores
- **P1-2:** Replaced non-existent `record_usage()`/`record_unused()` with actual `record_retrieval()` API from `usage_tracker.py`
- **P1-3:** Removed `AssembledItem` references — Phase 6 now uses existing `recalled_content_map`/`recalled_ids` from `TurnContext`
- **P1-4:** Removed undefined `_extract_key_phrases()` — Phase 6 uses existing `UsageTracker.compute_overlap()` containment coefficient
- **P2-1:** Acknowledged existing D3 usage tracking in `layer.py:510-535` — Phase 6 now extends it rather than duplicating
- **P2-2:** Pipeline description updated to include `tier3_threshold` and `diversity_filter` stages
- **P2-3:** Added `TIER3_THRESHOLDS` interaction note — superseded when F017 enabled, kept as fallback when disabled
- **P2-4:** Fixed `datetime.utcnow()` → `datetime.now(timezone.utc)` (deprecated in Python 3.12+)
- **P2-5:** Removed redundant `validation_alias` — `env_prefix="NOUS_"` already handles NOUS_-prefixed env vars
- **P2-6:** Replaced `copy()` of ORM objects with `_ScoredWrapper` proxy to avoid SQLAlchemy identity map corruption
- **P2-7:** Phase 6 placement corrected from `runner.py` to `layer.py:post_turn()` where usage tracking already lives
- **P2-8:** Fixed D3 comparison table — current D3 already does reference detection via overlap score, not binary retrieval tracking
- **P3-1:** Fixed line reference from "context.py line 116+" to "context.py lines 302+"
- **P3-2:** Defined D3/D7 shorthand on first use

**v2** (2026-03-07) — F016/F017 joint review fixes:
- P1 #1 + P2 #5: Floor moved after all boosts
- P1 #2: F016 `pre_prune_extraction` exemption added
- P2 #4: Config/constant placement clarified
- P2 #7: Migration path added
- P2 #8: All settings have env var overrides
