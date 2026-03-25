# Implementation Plan: RRF Score Normalization + Adaptive Relevance Filtering

**Decision ID:** `1d53fd29`
**Status:** Reviewed and approved (3-agent architecture review)

## Problem

RRF scores with `k=60` max at ~0.033 (when a doc is ranked #1 in both lists). The existing relevance floors (fact=0.45, decision=0.40, procedure=0.25, episode=0.35) and TIER3 thresholds (0.20-0.30) all expect scores in the 0-1 range. Result: **everything gets filtered**, context fills to ~10%.

Secondary bug: spreading activation seeds (tools.py:401) use raw RRF scores (~0.03) but the activation threshold is 0.1 (tools.py:409), so graph expansion never triggers.

## Root Cause

`_rrf_merge()` returns raw RRF scores: `score = vector_weight/(k+rank) + keyword_weight/(k+rank)`. With k=60, the theoretical max is `1/60 ≈ 0.0167` per component, so the best possible score is `~0.033`. No downstream filter can work with these values.

## Solution: 3 Layers

### Layer 1: Normalize RRF scores to 0-1 (search.py)

**File:** `nous/heart/search.py`
**Function:** `_rrf_merge()` (line 40-73)

Add normalization at the end of `_rrf_merge()`, before the final `return scored[:limit]`:

```python
# Normalize to 0-1 range relative to theoretical max
# Max RRF = vector_weight/(k+0) + keyword_weight/(k+0) = 1/k
# (since vector_weight + keyword_weight = 1.0 by construction)
max_score = 1.0 / k
if max_score > 0 and scored:
    scored = [(doc_id, score / max_score) for doc_id, score in scored]
```

**Score distribution after normalization** (k=60, vector_weight=0.7, limit=10, penalty_rank=11):

| Scenario | Raw RRF | Normalized |
|----------|---------|------------|
| #1 in both lists (rank 0/0) | 0.01667 | **1.000** |
| #1 vector only (rank 0/penalty) | 0.01589 | **0.953** |
| #1 keyword only (penalty/rank 0) | 0.01486 | **0.891** |
| #5 in both (rank 4/4) | 0.01563 | **0.937** |
| #10 in both (rank 9/9) | 0.01449 | **0.869** |
| Worst case (penalty/penalty) | 0.01408 | **0.845** |

**Score compression note:** With k=60, all RRF-normalized scores fall in ~0.845-1.0. This is acceptable because downstream pipeline stages (staleness penalty, frame boost, usage boost) create real spread. Example: a 60-day-old result at 0.95 × staleness(0.3) = 0.285, while a fresh result stays at 0.92. Gap detection operates on these post-pipeline scores.

**Callers automatically covered** (normalization is inside `_rrf_merge`):
- `search.py:168` — `hybrid_search()`
- `brain.py:702` — `Brain.query()`
- `facts.py:868` — `FactManager._search_all()` (used for contradiction detection — verified: only uses ranked IDs, not absolute scores)

**Keyword-only fallback:** When embeddings are None, `hybrid_search()` returns `ts_rank_cd/(1+ts_rank_cd)` scores directly (0-1 range, typically 0.01-0.15). These bypass `_rrf_merge` and are NOT normalized. This is acceptable because the new adaptive filter uses gap detection (scale-agnostic), not absolute thresholds.

### Layer 2: Replace absolute floors with adaptive top-K + gap detection (context.py)

**File:** `nous/cognitive/context.py`

Replace `_apply_relevance_floor()` (line 621-631) and `_apply_diminishing_returns_cutoff()` (line 633-643) with a single `_apply_relevance_filter()`. Move constants to module level:

```python
# Per-type result count bounds (configurable via Settings)
RELEVANCE_MIN_RESULTS: dict[str, int] = {
    "fact": 3, "decision": 2, "procedure": 2, "episode": 2,
}
RELEVANCE_MAX_RESULTS: dict[str, int] = {
    "fact": 8, "decision": 5, "procedure": 3, "episode": 4,
}

def _apply_relevance_filter(self, results: list, memory_type: str) -> list:
    """RRF-aware relevance filtering.

    Strategy: Keep top-K results, then cut at score gaps.
    - Always keep at least min_results (don't return empty)
    - Always keep at most max_results (don't flood context)
    - Between min and max, cut at sharp score drops
    - Items from exempt sources bypass gap detection
    """
    if not results:
        return results

    # Merge defaults with config overrides
    min_k = {**RELEVANCE_MIN_RESULTS, **self._settings.relevance_min_results}.get(memory_type, 2)
    max_k = {**RELEVANCE_MAX_RESULTS, **self._settings.relevance_max_results}.get(memory_type, 5)

    # Always keep at least min_k
    if len(results) <= min_k:
        return results

    # Cap at max_k
    results = results[:max_k]

    # Within [min_k, max_k], cut at sharp score drops
    # Skip exempt-source items in gap calculation
    drop_ratio = self._settings.relevance_drop_ratio  # default 0.6
    for i in range(min_k, len(results)):
        # Preserve FLOOR_EXEMPT_SOURCES items (e.g. pre_prune_extraction)
        if getattr(results[i], "source", None) in FILTER_EXEMPT_SOURCES:
            continue
        score = getattr(results[i], "score", 0) or 0
        prev = getattr(results[i - 1], "score", 0) or 0
        if prev > 0 and score < prev * drop_ratio:
            # Keep any exempt items beyond the cut point
            tail_exempt = [
                r for r in results[i:]
                if getattr(r, "source", None) in FILTER_EXEMPT_SOURCES
            ]
            return results[:i] + tail_exempt

    return results
```

**Callers:** Replace all 8 call sites (2 calls × 4 memory types) with single `_apply_relevance_filter(items, type)`:
- Line 334-335 (decisions): `_apply_relevance_floor` + `_apply_diminishing_returns_cutoff` → `_apply_relevance_filter`
- Line 394-395 (facts): same
- Line 442-444 (procedures): same
- Line 527-528 (episodes): same

**Config additions (config.py):**
```python
relevance_min_results: dict[str, int] = Field(default_factory=dict)  # override RELEVANCE_MIN_RESULTS
relevance_max_results: dict[str, int] = Field(default_factory=dict)  # override RELEVANCE_MAX_RESULTS
```

**Keep `relevance_floor_enabled`** as the config gate (backward compat — no rename):
```python
def _apply_relevance_filter(self, results, memory_type):
    if not self._settings.relevance_floor_enabled:
        return results
    ...
```

### Layer 3: Remove legacy TIER3 logic (context.py)

**Delete from context.py:**
- `TIER3_THRESHOLDS` dict (line 34-39)
- All 4 TIER3 conditional blocks:
  - Line 323-327 (decisions): `if decisions and self._has_embeddings and not self._settings.relevance_floor_enabled:`
  - Line 375-378 (facts): same pattern
  - Line 427-430 (procedures): same pattern
  - Line 510-513 (episodes): same pattern

**Rename in context.py:**
- `FLOOR_EXEMPT_SOURCES` → `FILTER_EXEMPT_SOURCES` (keep set, same values)

**Delete from schemas.py:**
- `RELEVANCE_FLOORS` dict (line 29-36) — replaced by min/max K
- `FLOOR_EXEMPT_SOURCES` set (line 39-41) — moved to context.py as `FILTER_EXEMPT_SOURCES`

### Layer 4: Test Updates

**File:** `tests/test_rrf_search.py`
- Update `TestRRFMerge` — all 6 tests check exact raw scores, update to normalized values (multiply expected by `k`)
- Add `test_normalization_range` — verify rank-0-in-both → 1.0
- Add `test_normalization_preserves_order` — ranking unchanged

**File:** `tests/test_relevance_floor.py` → rename to `tests/test_relevance_filter.py`
- Replace `TestRelevanceFloor` with `TestRelevanceFilter`:
  - `test_min_k_guarantee` — ≤min_k items → all kept
  - `test_max_k_cap` — >max_k items → capped
  - `test_gap_detection_within_range` — cuts at sharp drop between min_k and max_k
  - `test_disabled_passes_all` — `relevance_floor_enabled=False` → no filtering
  - `test_empty_input` — empty → empty
  - `test_exempt_source_preserved` — exempt items survive gap detection
  - `test_config_overrides_defaults` — Settings min/max override module-level defaults

**File:** `tests/test_diminishing_cutoff.py` → delete (merged into relevance_filter)

## Implementation Order

1. **Layer 1** (search.py normalization) + test updates for `test_rrf_search.py`
2. **Layer 2+3** (context.py filter replacement + TIER3 removal + schemas.py cleanup) + new `test_relevance_filter.py`
3. **Config** (config.py new fields) — included in Layer 2 commit
4. Delete `test_diminishing_cutoff.py` and old `test_relevance_floor.py`

Each layer is a separate commit. Tests included with code changes, not separate.

## Review Findings Addressed

| # | Finding | Source | Resolution |
|---|---------|--------|------------|
| P1-1 | brain.py and facts.py also call _rrf_merge | architect | Covered: normalization is inside _rrf_merge, all callers get it |
| P1-1 | Score compression 0.845-1.0 with k=60 | search-specialist | Accepted: downstream staleness/boosts create real spread |
| P1-2 | Example scores wrong (claimed ~0.5) | architect | Fixed: corrected table with actual values |
| P1-2 | Min-K forces garbage into context | devil's advocate | Mitigated: min_k is configurable, gap detection still cuts within range |
| P1-3 | Boosts push scores above 1.0 | architect | Documented: expected behavior, gap detection uses ratios not absolutes |
| P2-1 | Keyword-only fallback not normalized | all three | Documented: gap detection is scale-agnostic, asymmetry is acceptable |
| P2-1 | Spreading activation threshold broken | devil's advocate | Bonus fix: normalization fixes the 0.1 threshold gate |
| P2-3 | Config override merge logic missing | architect | Added: `{**DEFAULTS, **settings.overrides}` pattern |
| P2-4 | FLOOR_EXEMPT_SOURCES intent not preserved | devil's advocate | Fixed: exempt-source items skip gap detection in new filter |
| P2-4 | TIER3 removal breaks floor_enabled=False users | devil's advocate | Mitigated: `relevance_floor_enabled` gates new filter too |
| P3-1 | Tests in same commit as code | architect | Fixed: implementation order updated |
| P3-2 | recalled_score_map values change | architect | Noted: score discontinuity in logs, no action needed |
| P3-3 | Config name backward compat | devil's advocate | Fixed: keep `relevance_floor_enabled` name |

## Risk Assessment

- **Low risk on Layer 1:** Normalization is pure math, doesn't change ranking order. All callers benefit automatically.
- **Medium risk on Layer 2+3:** Replacing two filters with one changes behavior. Min/max K bounds + exempt sources provide safety net. Gated by existing config flag.
- **Bonus fix:** Spreading activation graph expansion will start working (was silently broken by low RRF scores vs 0.1 threshold).
- **Backward compatibility:** `relevance_floor_enabled` config still works. Drop ratio reused. No API changes. No config renames.
