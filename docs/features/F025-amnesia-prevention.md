# F025 — Amnesia Prevention Spec

**Author:** Nous (self-diagnosed) + Tim  
**Date:** 2026-03-23  
**Updated:** 2026-04-04  
**Status:** Phase 1 Complete, Phase 2–3 Open  
**Priority:** High  
**Relates to:** F023 (Admission Protocol), F030 (MMR Diversity), Context Assembly, Fact Extraction

---

## Problem Statement

Nous suffers from structural amnesia — not because memories aren't stored, but because the retrieval and extraction pipeline systematically loses them. Seven compounding failure modes were identified through code review of `context.py`, `facts.py`, `heart.py`, `episode_summarizer.py`, and `fact_extractor.py`.

The result: Nous forgets conversations, loses context mid-session, and fails to surface relevant facts even when they exist in storage.

---

## Root Cause Status Summary

| RC | Description | Status | Notes |
|----|-------------|--------|-------|
| RC-1 | Staleness penalty | ❌ Open | Still applied to all 4 types, half-life=20d |
| RC-2 | Tiny retrieval limits | ✅ Fixed | Limits raised to 15/8/5/8, caps at 12/7/5/6 |
| RC-3 | Naive diversity grouping | ✅ Fixed | Full string grouping. max_per_subject=2 (not 3) |
| RC-4 | User Profile budget too small | ❌ Open | Still 200 tokens, NOT scaled by `_scaled_budget()` |
| RC-5 | Transcript truncation | ❌ Open | Still 8000 chars |
| RC-6 | Fact extractor dedup too aggressive | ❌ Open | Still 0.85 threshold |
| RC-7 | Admission grounding against summary | ❌ Open | Returns `episode.summary`, no transcript stored |

---

## Root Causes — Detail

### RC-1: Staleness penalty still active (❌ Open)
**File:** `nous/cognitive/context.py` lines 337, 383, 475, 571  
**Severity:** Critical  
**Env:** `NOUS_STALENESS_PENALTY_ENABLED=true`, `NOUS_STALENESS_HALF_LIFE_DAYS=20`

The staleness penalty applies exponential decay (`0.5^(age/20)`) to all four memory types: decisions (L337), facts (L383), procedures (L475), episodes (L571). A fact that's 40 days old loses 75% of its relevance score regardless of content quality.

This systematically penalizes stable, long-lived facts like "Tim lives in Silver Spring" or "FORGE uses Minsky's Society of Mind." These are the *highest-value* facts — penalizing them is backwards.

**Current pipeline:** retrieve → staleness → frame boost → diversity → dedup → usage boost → relevance floor  
**Problem:** Staleness and relevance floor are redundant — both remove low-scoring items. Staleness specifically hurts old-but-correct facts.

### RC-2: Retrieval limits (✅ Fixed)
**File:** `nous/cognitive/context.py` lines 48–52 (DEFAULT_FETCH_LIMITS), 40–42 (RELEVANCE_MAX_RESULTS)

**Current values (validated against code):**
- Fetch limits: `fact: 15, decision: 8, procedure: 5, episode: 8`
- Relevance caps: `fact: 12, decision: 7, procedure: 5, episode: 6`

These match or exceed the original spec targets. Combined with MMR (F030) and budget scaling, retrieval coverage is adequate.

### RC-3: Diversity filter grouping (✅ Fixed)
**File:** `nous/cognitive/context.py` lines 748–771 (`_enforce_diversity`)

Now uses full `raw.strip().lower()` for string attributes (L764). "Nous architecture" and "Nous version" are separate groups.

**Remaining minor gap:** `max_per_subject` is still 2 for facts and episodes (L388, L576), not 3 as originally proposed. Decisions use 3 (L339). This is acceptable — MMR (F030) provides additional diversity at a different layer.

### RC-4: User Profile budget not scaled (❌ Open)
**File:** `nous/cognitive/schemas.py` line 103, `nous/cognitive/context.py` line 249  
**Severity:** Medium-High

`user_profile: int = 200` — hardcoded at 200 tokens.

Every other dynamic section passes through `_scaled_budget()` which applies 2.5× multiplier for context windows ≥500K:
- `budget.decisions` → `self._scaled_budget(budget.decisions)` (L355)
- `budget.facts` → `self._scaled_budget(budget.facts)` (L410)
- `budget.procedures` → `self._scaled_budget(budget.procedures)` (L513)
- `budget.episodes` → `self._scaled_budget(budget.episodes)` (L593)

But user_profile is used raw: `self._truncate_to_budget(profile_text, budget.user_profile)` (L249) — **no scaling applied.**

With `NOUS_CONTEXT_WINDOW=700000` and `NOUS_BUDGET_SCALE_ENABLED=true`, all other sections get 2.5× but user_profile stays at 200. This is the highest-priority section (Tier 1: preferences, person, rules) getting the smallest budget.

**Fix options:**
1. Pass through `_scaled_budget()` (200 → 500 at 700K window) — consistent with other sections
2. Add `user_profile` to `NOUS_CONTEXT_BUDGET_OVERRIDES` env — immediate workaround
3. Raise default from 200 → 400 — simple but doesn't scale

### RC-5: Transcript truncation before extraction (❌ Open)
**File:** `nous/handlers/episode_summarizer.py` lines 183, 205–241  
**Severity:** High

`_truncate_transcript()` caps at 8000 chars (L205). Uses smart truncation (keep first + last portions with middle gap) but still loses 60–80% of long technical sessions.

The fact extractor (`fact_extractor.py` L194–206) then works from `summary_text = summary.get("summary", "")` — it extracts facts from the *already-summarized* output, not the transcript. So the information loss is:

**Raw transcript** → (truncate to 8K) → **LLM summarization** → (extract facts from summary) → **Facts**

Two lossy compressions in series.

### RC-6: Fact extractor dedup too aggressive (❌ Open)
**File:** `nous/handlers/fact_extractor.py` lines 115–119, 170  
**Severity:** Medium

Dedup threshold hardcoded at 0.85 (hybrid search score). Comment at L115–117 explains history:
```python
# P0-7 fix: use .score not .similarity, threshold 0.85 for hybrid search
# Raised from 0.65 -> 0.85 (#45): 0.65 was too aggressive, blocking
```

The storage layer's `supersede_by_subject` in `facts.py` handles updates correctly at 0.95 threshold — but facts blocked at 0.85 in the extractor never reach it. Updated values like price changes or version bumps get killed as "duplicates" before the smarter supersession logic can evaluate them.

### RC-7: Admission grounding against summary, not transcript (❌ Open)
**File:** `nous/heart/facts.py` lines 483–500 (`_get_source_text`)  
**Severity:** Low-Medium

```python
async def _get_source_text(self, fact_input, session) -> str | None:
    """Retrieve original source text for ROUGE-L grounding check.
    
    Fetches episode.content by PK if source_episode_id present.
    Episode content includes tool call outputs (web_search, bash, etc.).
    """
    episode = await session.get(Episode, fact_input.source_episode_id)
    if episode and episode.summary:
        return episode.summary  # <-- returns summary, not content/transcript
    return None
```

**Note:** The docstring is misleading — it references `episode.content` but the Episode model (`storage/models.py` L306–336) has no `content` or `transcript` column. Only `summary` (L319) and `structured_summary` (L336). The raw transcript is not persisted on the Episode model.

This means ROUGE-L grounding (in `admission.py` L155) compares facts against the lossy summary, not the source conversation. With F023 now active (`NOUS_ADMISSION_SHADOW_MODE=false`, `NOUS_ADMISSION_THRESHOLD=0.60`), facts that came from truncated/lost portions of transcripts will score low on grounding and may be rejected.

**Architectural note:** Fixing this properly requires either (a) storing transcript on Episode, or (b) passing transcript through the extraction→admission pipeline without persisting it. Option (b) is lighter.

---

## Implementation Plan (Revised)

### Phase 1 — Quick Wins ✅ COMPLETE
- ~~Raise retrieval limits~~ → Done (DEFAULT_FETCH_LIMITS: 15/8/5/8)
- ~~Fix diversity grouping~~ → Done (full string, strip+lower)
- ~~MMR diversity reranking~~ → Done (F030, NOUS_MMR_ENABLED=true)
- ~~Activate admission control~~ → Done (F023, shadow_mode=false)

### Phase 2 — Remaining High-Impact Fixes (5 items)

**P2-A: Disable staleness penalty for facts (RC-1, partial)**
- Target: `context.py` — skip staleness for fact type only
- Rationale: Decisions and episodes have natural temporal relevance. Facts don't — "Tim lives in Silver Spring" is equally true at day 1 and day 100
- Alternative: Make staleness per-type configurable (`NOUS_STALENESS_TYPES=decision,episode,procedure`)
- Risk: Low
- Effort: ~20 lines

**P2-B: Scale user_profile budget (RC-4)**
- Target: `context.py` L249 — wrap in `_scaled_budget()`
- One-line fix: `self._truncate_to_budget(profile_text, self._scaled_budget(budget.user_profile))`
- Effect: 200 → 500 tokens at 700K context window
- Risk: None
- Effort: 1 line + test

**P2-C: Raise transcript truncation limit (RC-5)**
- Target: `episode_summarizer.py` L205 — raise `max_chars` default from 8000 → 16000
- Also make configurable: `NOUS_TRANSCRIPT_MAX_CHARS=16000`
- Cost impact: ~2× tokens per summarization call (~$0.01 more per episode)
- Risk: Low
- Effort: ~10 lines

**P2-D: Relax fact extractor dedup (RC-6)**
- Target: `fact_extractor.py` L119, L170 — raise from 0.85 → 0.92
- Also make configurable: `NOUS_FACT_DEDUP_THRESHOLD=0.92`
- Safety net: `facts.py` supersession at 0.95 catches true duplicates at storage layer
- Risk: Medium (monitor duplicate rate for 48h after deploy)
- Effort: ~5 lines + config

**P2-E: Fix _get_source_text docstring and evaluate transcript passthrough (RC-7)**
- Immediate: Fix misleading docstring (references episode.content which doesn't exist)
- Investigate: Can we pass transcript text through fact extraction pipeline to admission without persisting it?
- If yes: Thread `source_text` parameter through `fact_extractor` → `facts.learn()` → `admission.score()`
- Risk: Medium (plumbing change across 3 files)
- Effort: ~40 lines

### Phase 3 — Structural Improvements (Future)

**P3-A: Per-type staleness configuration**
- Full implementation of type-specific staleness enable/disable
- `NOUS_STALENESS_TYPES=decision,episode` (facts and procedures exempt)

**P3-B: Chunked summarization for long episodes**
- Split transcripts >16K into chunks, summarize each, merge
- Higher quality than single-pass truncation
- Depends on: P2-C deployed and cost baseline established

**P3-C: Transcript persistence on Episode model**
- Add `transcript` column to Episode table (nullable Text)
- Populate during episode close
- Enables RC-7 full fix and future features (search within conversations)
- Depends on: Storage migration, disk budget analysis

---

## Current Environment Settings (Reference)

```
NOUS_STALENESS_PENALTY_ENABLED=true        # RC-1: still on
NOUS_STALENESS_HALF_LIFE_DAYS=20           # RC-1: 20-day half-life
NOUS_BUDGET_SCALE_ENABLED=true             # RC-4: scaling on but user_profile not using it
NOUS_CONTEXT_WINDOW=700000                 # RC-4: would give 2.5x if scaled
NOUS_CONTEXT_BUDGET_OVERRIDES={"total": 12000, "facts": 3000, "decisions": 2500}
NOUS_MMR_ENABLED=true                      # F030: active
NOUS_ADMISSION_SHADOW_MODE=false           # F023: active enforcement
NOUS_ADMISSION_THRESHOLD=0.60             # F023: admission gate
NOUS_RELEVANCE_FLOOR_ENABLED=true
NOUS_RRF_K=30
NOUS_RELEVANCE_DROP_RATIO=0.6
```

---

## Success Metrics

- **Recall coverage:** % of stored facts surfaced in context. Target: >5% (was ~0.7% pre-Phase 1)
- **Amnesia incidents:** Conversations where Nous fails to recall relevant stored facts. Target: <1/week
- **Duplicate rate:** New fact duplicates after P2-D. Target: <5% increase
- **Context utilization:** % of token budget used per section. Target: >60%
- **User profile truncation:** Track how often profile_text exceeds budget (currently 200, proposed 500)

---

## Testing Plan

1. ~~Before Phase 1, run 10 test queries and record what surfaces~~ (Phase 1 complete)
2. Before Phase 2, baseline current recall on 10 representative queries
3. Apply P2-A through P2-D
4. Re-run same 10 queries, compare recall coverage
5. Monitor duplicate rate for 48h after P2-D
6. Monitor admission rejection rate after P2-E (should decrease for legitimate facts)
7. Run self-reflection after 1 week to assess amnesia frequency

---

## Open Questions

1. Should we add a "pinned facts" mechanism — facts Tim explicitly marks as always-surface?
2. Should retrieval limits be frame-dependent? (e.g., task frame gets more facts, conversation gets more episodes)
3. Is the current embedding model producing good enough similarity scores, or is poor embedding quality contributing to low recall?
4. Should `max_per_subject` for facts be raised from 2 → 3 to match decisions? (Low priority given MMR is active)
5. For RC-7: Is the cost of storing raw transcripts justified, or is pipeline passthrough sufficient?
