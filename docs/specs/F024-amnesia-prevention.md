# F024 — Amnesia Prevention Spec

**Author:** Nous (self-diagnosed) + Tim  
**Date:** 2026-03-23  
**Status:** Draft  
**Priority:** High  
**Relates to:** F023 (Admission Protocol), Context Assembly, Fact Extraction

---

## Problem Statement

Nous suffers from structural amnesia — not because memories aren't stored, but because the retrieval and extraction pipeline systematically loses them. Seven compounding failure modes were identified through code review of `context.py`, `facts.py`, `heart.py`, `episode_summarizer.py`, and `fact_extractor.py`.

The result: Nous forgets conversations, loses context mid-session, and fails to surface relevant facts even when they exist in storage.

---

## Root Causes

### RC-1: Over-filtered retrieval pipeline
**File:** `nous/cognitive/context.py`  
**Severity:** Critical

The recall pipeline applies 6 sequential filters:
1. Staleness penalty (age-based decay)
2. Frame boost (cognitive frame relevance)
3. Diversity filter (max_per_subject)
4. Dedup filter
5. Usage boost
6. Relevance floor + diminishing returns cutoff

Each stage independently removes items. A fact scoring 0.50 relevance can be staleness-penalized to 0.42, then killed by the 0.45 relevance floor. Filters compound multiplicatively — survival probability drops at each stage.

### RC-2: Tiny retrieval limits
**File:** `nous/cognitive/context.py`  
**Severity:** High

Default retrieval fetches 5 items per memory type (facts, decisions, episodes, procedures). With 762+ facts, this is a 0.7% sample. After 6-stage filtering, 1-2 items per type may survive.

### RC-3: Diversity filter uses naive subject grouping
**File:** `nous/cognitive/context.py`  
**Severity:** Medium

`max_per_subject=2` with grouping by first word of `subject`. "Nous architecture", "Nous version", "Nous memory" all map to "Nous" — only 2 survive. This disproportionately drops memories about the primary project.

### RC-4: User Profile token budget too small
**File:** `nous/cognitive/context.py` (or `schemas.py`)  
**Severity:** Medium

Tier 1 facts (preferences, person, rules) capped at 200 tokens (~50 words). This is ALL preferences + ALL person facts + ALL rules combined. Critical identity and preference data gets truncated.

### RC-5: Transcript truncation before extraction
**File:** `nous/handlers/episode_summarizer.py`  
**Severity:** High

Episode summarizer truncates transcripts to 8000 chars before LLM summarization. Long technical sessions (common for Tim) lose 60-80% of content. Facts never extracted from truncated portions can never be recalled.

### RC-6: Fact extractor dedup too aggressive
**File:** `nous/handlers/fact_extractor.py`  
**Severity:** Medium

Dedup threshold of 0.85 hybrid score blocks updates to existing facts. "Gold at $4,800" and "Gold at $4,492" are seen as duplicates. The `supersede_by_subject` mechanism in `facts.py` (threshold 0.95) handles this correctly — but facts are blocked before reaching it.

### RC-7: Admission scores against summary, not transcript
**File:** `nous/handlers/admission.py` (inferred from `_get_source_text()`)  
**Severity:** Low

Admission controller evaluates grounding against `episode.summary`, which is already a lossy compression of the conversation. Cannot properly verify claims that were lost in summarization.

---

## Proposed Changes

### Change 1: Reduce filter stages
**Target:** `context.py`  
**Risk:** Low  
**Effort:** Small

Remove the staleness penalty from the main pipeline. Staleness should be an optional re-ranker, not a filter. The relevance floor already handles low-quality items.

**Before:** retrieve → staleness → frame boost → diversity → dedup → usage → floor → diminishing  
**After:** retrieve → frame boost → diversity → dedup → usage boost → floor

Rationale: Staleness and relevance floor are redundant — both remove low-scoring items. Staleness penalizes old-but-relevant facts (e.g., "Tim lives in Silver Spring") that should persist.

### Change 2: Raise retrieval limits
**Target:** `context.py`  
**Risk:** Low (token budgets already cap output)  
**Effort:** Small

| Memory Type | Current | Proposed |
|-------------|---------|----------|
| Facts       | 5       | 15       |
| Decisions   | 5       | 10       |
| Episodes    | 5       | 10       |
| Procedures  | 5       | 8        |

The token budget per section (1500-2000) already prevents context overflow. Retrieving more candidates gives the filter pipeline better material to work with.

### Change 3: Fix diversity filter grouping
**Target:** `context.py`  
**Risk:** Low  
**Effort:** Small

Replace first-word grouping with full subject string matching. Raise `max_per_subject` from 2 to 3.

```python
# Before
group_key = item.subject.split()[0] if item.subject else "unknown"

# After  
group_key = item.subject.strip().lower() if item.subject else "unknown"
```

This means "Nous architecture" and "Nous version" are separate groups, each allowed 3 items.

### Change 4: Raise User Profile budget
**Target:** `schemas.py` (ContextBudget)  
**Risk:** Low  
**Effort:** Small

Raise Tier 1 (user profile) token budget from 200 → 500 tokens.

Rationale: Tier 1 facts are the highest-value memories — user identity, preferences, hard rules. 200 tokens forces critical information to be dropped. 500 tokens ≈ 125 words, sufficient for 15-20 preference/rule entries.

### Change 5: Raise transcript budget for extraction
**Target:** `episode_summarizer.py`  
**Risk:** Medium (cost increase)  
**Effort:** Small

Raise transcript truncation from 8000 → 16000 chars.

Cost impact: ~2x token usage per summarization call. For typical session frequency (5-10/day), this is negligible.

Alternative: Implement chunked summarization — split long transcripts into 8k chunks, summarize each, then merge. Higher quality but more complex.

### Change 6: Relax fact extractor dedup
**Target:** `fact_extractor.py`  
**Risk:** Medium (more near-duplicate facts)  
**Effort:** Small

Raise dedup threshold from 0.85 → 0.92.

The safety net is `facts.py` `learn()` method which has its own dedup at 0.95 with `supersede_by_subject` logic. Let more candidates through the extractor; let the storage layer decide.

### Change 7: Admission grounding against transcript
**Target:** `admission.py`  
**Risk:** Low  
**Effort:** Medium  
**Priority:** Deferred (depends on F023 leaving shadow mode)

Pass raw transcript (truncated to budget) to admission controller instead of episode summary. Only matters once admission is actually rejecting facts.

---

## Implementation Order

**Phase 1 — Quick wins (Changes 1-4)**
- Reduce filter stages
- Raise retrieval limits  
- Fix diversity grouping
- Raise user profile budget
- Expected impact: ~40% improvement in recall coverage
- Effort: 1-2 hours
- Risk: Low

**Phase 2 — Extraction quality (Changes 5-6)**
- Raise transcript budget
- Relax extractor dedup
- Expected impact: ~25% improvement in fact capture rate
- Effort: 1 hour
- Risk: Medium (monitor for duplicate explosion)

**Phase 3 — Admission alignment (Change 7)**
- Deferred until F023 exits shadow mode
- Depends on admission threshold tuning (separate issue)

---

## Success Metrics

- **Recall coverage:** % of stored facts that can be surfaced in context. Target: >5% (currently ~0.7%)
- **Amnesia incidents:** Conversations where Nous fails to recall relevant stored facts. Target: <1/week
- **Duplicate rate:** New fact duplicates after Change 6. Target: <5% increase
- **Context utilization:** % of token budget actually used per section. Target: >60% (measure current baseline first)

---

## Testing Plan

1. Before any changes, run 10 test queries and record what surfaces
2. Apply Phase 1 changes
3. Re-run same 10 queries, compare recall
4. Monitor duplicate rate for 48 hours after Phase 2
5. Run self-reflection after 1 week to assess amnesia frequency

---

## Open Questions

1. Should we add a "pinned facts" mechanism — facts Tim explicitly marks as always-surface?
2. Should retrieval limits be frame-dependent? (e.g., task frame gets more facts, conversation gets more episodes)
3. Is the current embedding model producing good enough similarity scores, or is poor embedding quality contributing to low recall?
