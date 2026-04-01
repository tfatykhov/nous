# F031 — Consolidation Orient & Resolve Phase

> **Status:** Draft v1  
> **Priority:** P1  
> **Depends on:** F002 (Heart Module), F023 (A-MAC)  
> **Complements:** F027 (Supersession Detection — write-time conflict)  
> **Inspired by:** Claude Code autoDream Phase 1 (Orient) + Phase 3 (Consolidate with contradiction deletion)  
> **Created:** 2026-04-01  

---

## Problem Statement

All three Nous fact extraction pipelines operate **blind** — they don't know what facts already exist before extracting new ones. This causes two problems:

### Problem 1: Blind Extraction → Semantic Near-Duplicates

**Current flow (all three extractors):**
1. Get content (episode summary / compaction snapshot / sleep episodes)
2. Send to LLM: "extract facts worth remembering"
3. LLM returns candidates
4. Dedup check: >0.85 hybrid score in handler, >0.95 cosine in `FactManager._learn()`
5. Store survivors

**The gap:** The LLM prompt never includes "here's what we already know." So the LLM extracts facts we already have, just phrased differently. The dedup thresholds catch exact matches but let through rephrased duplicates:
- Existing: "Tim prefers Celsius for temperature measurements"
- Extracted: "Temperature values should be displayed in Celsius per Tim's preference"  
- Cosine similarity: ~0.88 (below 0.95 dedup threshold) → stored as duplicate

**Evidence:** The admission dashboard shows clusters of 3-5 facts about the same topic with slightly different wording, all active.

### Problem 2: Contradiction Warnings Without Resolution

**Current state of contradiction handling:**
- `_supersede_by_subject()` — auto-supersedes when BOTH same subject (exact match) AND >0.80 cosine ✅
- `_find_contradiction()` — detects contradictions (0.85-0.95 cosine range) → returns `ContradictionWarning` ⚠️
- But `ContradictionWarning` is **attached to FactDetail and never acted upon**
- No handler reads `fact.contradiction_warning` to resolve it
- Sleep cycle has no contradiction resolution phase
- F027 Phase 3 (Consolidation Sweep) would address this but is unimplemented

**Result:** Contradictions accumulate silently. Recall returns both "Tim's timezone is EST" and a hypothetically stale "Tim's timezone is PST" and lets the context window sort it out.

---

## Solution: Orient-Before-Extract + Consolidation Resolve Phase

Two additions to the sleep cycle, inspired by Claude Code's autoDream structure:

### Addition 1: Orient Context in Extraction Prompts

**What:** Before asking the LLM to extract facts, inject existing relevant facts into the prompt so the LLM knows what we already know.

**Where:** `SleepHandler._phase_reflect()` (primary), and optionally `FactExtractor` and `KnowledgeExtractor`.

**How:**

```python
# In _phase_reflect(), before calling LLM:
# 1. Identify topic areas from episode summaries
topics = extract_topic_keywords(episodes_text)  # simple keyword extraction

# 2. Recall existing facts in those areas
existing_facts = []
for topic in topics[:5]:  # max 5 topic queries
    results = await self._heart.search_facts(topic, limit=5)
    existing_facts.extend(results)

# Deduplicate by ID
existing_facts = list({f.id: f for f in existing_facts}.values())

# 3. Include in prompt
existing_facts_text = "\n".join(
    f"- [{f.category}] {f.content}" for f in existing_facts[:20]
)
```

**Modified reflection prompt addition:**
```
EXISTING KNOWLEDGE (do NOT re-extract these — only extract genuinely NEW information):
{existing_facts_text}

If you discover information that UPDATES or CONTRADICTS an existing fact above,
include it with a note: "UPDATES: <existing fact content>" in the fact's subject field.
```

**Expected impact:**
- Reduces duplicate fact extraction by ~60-70%
- LLM explicitly told "don't re-extract these" with concrete examples
- Cost: 1-5 additional `search_facts` calls per sleep cycle (~50ms each)
- Prompt grows by ~500-1000 tokens (20 existing facts × 50 tokens avg)

### Addition 2: Contradiction Resolution Phase in Sleep Cycle

**What:** New Phase 4.5 (between reflect and generalize) that actively resolves accumulated contradictions.

**Where:** New method `SleepHandler._phase_resolve_contradictions()`

**How:**

```python
async def _phase_resolve_contradictions(self, sleep_stats: dict) -> bool:
    """Phase 4.5: Find and resolve contradictory facts."""
    
    # Step 1: Find facts with unresolved contradiction_of references
    # Step 2: Find fact clusters with same subject but different content
    # Step 3: For each conflict pair, ask LLM to classify:
    #   - SUPERSEDE: newer fact replaces older → call heart.supersede_fact()
    #   - MERGE: combine into single authoritative fact → supersede both, create new
    #   - KEEP_BOTH: genuinely different facts → no action
    #   - REMOVE_ONE: one is wrong → deactivate the wrong one
    # Step 4: Track resolutions in sleep_stats
```

**Conflict discovery query:**
```sql
-- Find active facts with same subject and high embedding similarity
-- These are contradiction candidates that slipped past write-time detection
SELECT f1.id AS fact1_id, f2.id AS fact2_id,
       f1.content AS content1, f2.content AS content2,
       1 - (f1.embedding <=> f2.embedding) AS similarity
FROM heart.facts f1
JOIN heart.facts f2 ON f1.agent_id = f2.agent_id
  AND f1.id < f2.id  -- avoid duplicates
  AND f1.active = true AND f2.active = true
  AND LOWER(f1.subject) = LOWER(f2.subject)
  AND 1 - (f1.embedding <=> f2.embedding) > 0.75
WHERE f1.agent_id = :agent_id
ORDER BY similarity DESC
LIMIT 10;
```

**Resolution LLM prompt:**
```
Two facts exist in memory about the same subject. Determine the correct action:

Fact A (stored {date_a}): {content_a}
Fact B (stored {date_b}): {content_b}

Actions:
- SUPERSEDE_A: Fact B is the current/correct version, retire Fact A
- SUPERSEDE_B: Fact A is the current/correct version, retire Fact B  
- MERGE: Both contain partial truth, merge into single fact
- KEEP_BOTH: Genuinely different information, both valid
- REMOVE_A: Fact A is wrong/stale, remove it
- REMOVE_B: Fact B is wrong/stale, remove it

Return ONLY valid JSON:
{
  "action": "<ACTION>",
  "reason": "<brief explanation>",
  "merged_content": "<only if action is MERGE>"
}
```

**Cost:** ~100-200 tokens per conflict pair × max 10 pairs per sleep = ~1500-2000 tokens. Negligible.

**Actions taken:**
- `SUPERSEDE_A/B` → call `heart.supersede_fact(old_id, winning_fact_input)`
- `MERGE` → create new fact, supersede both old ones
- `KEEP_BOTH` → no action (log for audit)
- `REMOVE_A/B` → call `heart.facts.deactivate(fact_id)`

---

## Implementation Plan

### Phase 1: Orient in Sleep Reflection (~3h)

**Files modified:**
- `nous/handlers/sleep_handler.py` — modify `_phase_reflect()`

**Changes:**
1. Add `_extract_topic_keywords(episodes_text: str) -> list[str]` helper
   - Simple: split episodes into sentences, extract nouns/proper nouns
   - Or simpler: use the episode subjects/tags if available
2. Before building the reflection prompt, call `heart.search_facts()` for each topic
3. Inject existing facts into `_REFLECTION_PROMPT` with "do NOT re-extract" instruction
4. Add `"UPDATES:"` prefix parsing in fact storage loop — when an extracted fact subject starts with "UPDATES:", find the referenced existing fact and call `supersede_fact()` instead of `learn()`

**Testing:**
- Unit test: mock heart.search_facts, verify prompt includes existing facts
- Unit test: verify "UPDATES:" prefix triggers supersession path
- Integration test: run sleep cycle twice on same episodes, verify no new duplicates on second run

### Phase 2: Contradiction Resolution Phase (~4h)

**Files modified:**
- `nous/handlers/sleep_handler.py` — add `_phase_resolve_contradictions()`
- `nous/heart/facts.py` — add `find_contradiction_candidates()` query method

**Changes:**
1. Add `FactManager.find_contradiction_candidates(limit=10)` — the SQL query above
2. Add `_phase_resolve_contradictions()` to SleepHandler between reflect and generalize
3. Wire into `_run_sleep()` phase ordering
4. Add resolution tracking to `sleep_stats`: `contradictions_found`, `contradictions_resolved`
5. Emit `sleep_contradiction_resolved` event for dashboard tracking

**Testing:**
- Seed two contradictory facts (same subject, different content), run sleep, verify one is superseded
- Seed two genuinely-different facts (same subject, different aspects), verify KEEP_BOTH
- Verify phase is interruptible (checks `self._interrupted`)

### Phase 3: Extend to Real-Time Extractors (~2h, optional)

**Files modified:**
- `nous/handlers/fact_extractor.py` — add orient context
- `nous/handlers/knowledge_extractor.py` — add orient context

**Changes:**
- Before LLM extraction call, search for existing facts related to the episode topic
- Inject into extraction prompt as "already known" context
- Lighter touch than sleep — max 5 existing facts, max 1 search query

**Trade-off:** Each episode extraction adds ~50ms latency + ~200 prompt tokens. May not be worth it if sleep orient catches most duplicates. Implement only if sleep-only approach doesn't reduce duplication enough.

---

## Modified Sleep Phase Order

```
Phase 1: Review decisions     (free — DB only)
Phase 2: Prune stale censors  (free — DB only)
Phase 3: Compress old episodes (LLM)
Phase 4: Reflect with orient  (LLM — MODIFIED: includes existing facts in prompt)
Phase 4.5: Resolve contradictions (LLM — NEW)
Phase 5: Generalize / K-line  (LLM)
Phase 6: Evolve rubric        (LLM)
```

---

## Relationship to F027 (Supersession Detection)

F027 focuses on **write-time** conflict detection — catching supersessions as they happen during `learn_fact()`. F031 is complementary:

| Aspect | F027 (Write-Time) | F031 (Consolidation-Time) |
|--------|-------------------|---------------------------|
| When | Every `learn_fact()` call | Once per sleep cycle |
| Catches | Conflicts at moment of storage | Accumulated conflicts that slipped past write-time |
| Cost | Per-write LLM micro-call | Batch during idle time |
| Scope | Single new fact vs. existing | All active facts vs. each other |
| Prevention | Stops duplicates entering | Cleans up duplicates that entered |

**F031 is the safety net for F027.** Even with perfect write-time detection, facts extracted by different pipelines at different times can create conflicts that are only visible in aggregate. The consolidation sweep catches these.

**Implementation order:** F031 first (lower risk, immediate value), then F027 (higher complexity, blocks write path).

---

## Success Metrics

- **Duplicate fact clusters reduced by 50%+** — measure by counting same-subject active facts with >0.75 cosine similarity
- **Contradiction warnings resolved to 0** — currently accumulated silently
- **Sleep cycle produces fewer redundant facts** — track `sleep_stats["facts_created"]` before/after; expect 30% fewer with orient phase
- **No regression in fact quality** — monitor admission scores of sleep-extracted facts
- **Context token waste reduction** — fewer duplicate facts in recall_deep results = fewer wasted tokens

---

## Risks & Mitigations

- **False merges in contradiction resolution** → LLM classifier with KEEP_BOTH option; start with logging-only mode for first week; all supersessions are soft (old fact still queryable by ID)
- **Orient phase increases sleep LLM token usage** → ~500-1000 extra prompt tokens per sleep cycle; offset by fewer facts created (net token savings)
- **Topic keyword extraction is noisy** → Use episode subjects/tags first; fall back to simple noun extraction; worst case: a few irrelevant existing facts in prompt (LLM ignores them)
- **Contradiction resolution LLM makes wrong call** → All actions are reversible (superseded facts retain data); log every resolution decision as an event for audit; cap at 10 resolutions per sleep cycle

---

## Connection to Research

**Claude Code autoDream:** Directly inspired by their 4-phase consolidation structure:
- Phase 1 (Orient): "Read existing memory files before extracting" → our orient context
- Phase 3 (Consolidate): "Delete contradicted facts at the source" → our contradiction resolution
- Phase 4 (Prune): "Remove stale/wrong pointers" → natural extension of this spec

**SleepGate (arXiv:2603.14517):** Conflict-aware temporal tagging achieves 99.3% accuracy on supersession. Our LLM classifier for SUPERSEDE/MERGE/KEEP is a simplified version of their three-way gating.

**CraniMem (arXiv:2603.15642):** Scheduled consolidation (every N turns) keeps memory bounded. Our sleep-cycle-based consolidation is the same pattern — periodic batch cleanup rather than continuous.

**A-MEM (arXiv:2502.12110):** Self-organizing memory with structured notes that link to existing knowledge. Our orient phase is the retrieval step that enables this linking.
