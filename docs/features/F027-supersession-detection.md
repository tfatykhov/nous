# F027 — Supersession Detection & Principled Forgetting

> **Status:** Draft v2 (revised 2026-04-06)  
> **Priority:** P1 (Tier 2)  
> **Depends on:** F002 (Heart Module), F022 (Graph-Augmented Recall), F023 (A-MAC)  
> **Complements:** F031 (Consolidation Orient & Resolve — sleep-time contradiction resolution)  
> **Research:** SleepGate (arXiv:2603.14517), CraniMem (arXiv:2603.15642), Memory Survey (arXiv:2603.07670), Doc 016 Gap G4  
> **Fills:** Gap G4 (Forgetting/Lifecycle), Gap G1 (Memory Evolution)  
> **Revised:** Audit against codebase as of 2026-04-06. Removed already-implemented items, corrected schema assumptions, refocused scope.

---

## Problem Statement

Nous stores facts but never forgets them. When a fact is updated — "Tim lives in DC" followed by "Tim lives in Silver Spring" — both can persist with equal retrieval weight. The current write-time detection catches **only** same-subject + high-cosine matches (>0.80), missing conflicts across different subjects, phrasing, or extraction pipelines. There is no retrieval-time suppression gradient — a fact is either fully visible (`active=true`) or completely invisible (`active=false`).

**What SleepGate proved:** Without conflict detection, all forgetting policies (sliding window, attention-based, decay-only) perform at <18% retrieval accuracy under proactive interference. With conflict-aware gating, accuracy reaches 99.5% at moderate interference depths.

**The cost:** Each stale fact in recall_deep results consumes ~50-200 context tokens per turn. With 5 stale facts surfacing per query × 20 turns per session × 100 tokens average = 10,000 wasted tokens per session. At scale, this is the single largest source of retrieval noise.

---

## What Already Exists (Built by F022/F023/F031)

Before defining what F027 adds, here's the current state as of 2026-04-06:

### Write-Time Detection (in `FactManager`)
- **Subject-based supersession** (`_supersede_by_subject()`): Same subject (case-insensitive) + cosine > 0.80 → sets `superseded_by`, `active = False` on old fact
- **Contradiction detection** (`_find_contradiction()`): Cosine 0.85–0.95 range → returns `ContradictionWarning` (logged, not auto-resolved)
- **Deduplication** (`_find_duplicate()`): Cosine > 0.95 → reject as duplicate
- **Explicit supersede/contradict APIs**: `supersede()` and `contradict()` methods for programmatic use, creating graph edges and adjusting confidence

### Schema Columns (on `heart.facts`)
- `superseded_by` (UUID FK) — ✅ exists, used by `_supersede_by_subject()` and `supersede()`
- `contradiction_of` (UUID FK) — ✅ exists, used by `contradict()`
- `recall_count` (Integer) — ✅ exists in model, **but never incremented anywhere in code**
- `last_recalled_at` (DateTime) — ✅ exists in model, **but never updated anywhere in code**
- `admission_score` / `admission_scores` (Float/JSONB) — ✅ exists, populated by F023

### Graph Edges (F022)
- `supersedes` and `contradicts` edge types created via `_create_graph_edge()` — ✅ working

### Sleep-Time Resolution (F031)
- `_phase_resolve_contradictions()` in `SleepHandler`: Finds contradiction candidates (same subject, cosine 0.75–0.95), runs LLM judgment with SUPERSEDE_A/B, MERGE, REMOVE_A/B, KEEP_BOTH actions. Confidence < 0.7 → downgrades to KEEP_BOTH. — ✅ working

### What Does NOT Exist (F027 Scope)
1. **LLM micro-call classifier at write-time** — current detection is purely cosine-based, no semantic understanding of UPDATE vs CONTRADICTION vs REFINEMENT vs UNRELATED
2. **Retrieval-time soft suppression** — no gradient between `active=true` (full visibility) and `active=false` (invisible). No `apply_supersession_filter()` in the recall pipeline
3. **Access tracking** — `recall_count` and `last_recalled_at` columns exist but are dead code
4. **Stale fact scan** — no periodic cleanup of old, low-confidence, superseded facts
5. **Cluster consolidation** — no merging of 3+ same-subject facts into one

---

## Solution: Three Additions to Existing Infrastructure

### Addition 1: LLM Micro-Call Classifier at Write-Time

**When:** During `learn_fact()`, after the existing `_find_contradiction()` detects a candidate (cosine 0.85–0.95 range) OR when `_supersede_by_subject()` finds a subject match but cosine is in the ambiguous 0.80–0.95 zone.

**Why the current approach isn't enough:**
- `_supersede_by_subject()` requires exact subject match. "Tim's timezone" won't match "User timezone preference"
- `_find_contradiction()` flags candidates but takes no action — it returns a warning that gets logged and ignored
- Cosine similarity alone can't distinguish "update" from "refinement" from "coincidental similarity"

**How:**
1. When `_find_contradiction()` returns a candidate, OR when `_supersede_by_subject()` finds a match in the 0.80–0.95 range, invoke an LLM micro-call:

```
Given existing fact: "{old_fact}"
And new fact: "{new_fact}"
Classify: UPDATE | CONTRADICTION | REFINEMENT | UNRELATED
If UPDATE: which fact is current?
Confidence: 0.0-1.0
```

2. Based on classification:
   - **UPDATE** (high confidence ≥ 0.8): Supersede old fact — set `superseded_by`, apply soft penalty (`confidence *= 0.3`), create `supersedes` graph edge
   - **UPDATE** (low confidence < 0.8): Flag for sleep-time resolution (let F031 handle it)
   - **CONTRADICTION**: Store both, set `contradiction_of` on new fact, create `contradicts` graph edge, reduce old confidence by 0.2 (existing `contradict()` behavior)
   - **REFINEMENT**: Keep both active, create `refines` graph edge (new edge type)
   - **UNRELATED**: No action (false positive from embedding similarity)

**Cost:** One LLM micro-call per conflict candidate (~50–100 tokens). Expected 0–2 candidates per write. Uses `call_background_llm_structured()` (same as F031 contradiction resolution).

**Key difference from current `_supersede_by_subject()`:** The LLM call enables cross-subject detection. "Tim lives in DC" and "Tim moved to Silver Spring, MD" have different subjects but the LLM recognizes the update relationship.

### Addition 2: Retrieval-Time Soft Suppression

**When:** After `hybrid_search()` returns results, before they enter the context window.

**Why:** Currently, `hybrid_search()` filters `active = true`. This creates a binary cliff — a fact is either fully visible or gone. SleepGate's key finding: soft attention biasing (`b = β·log(r)`) outperforms hard eviction because it degrades gracefully. If supersession classification was wrong, the old fact still has a chance to surface.

**Implementation:** Add `apply_supersession_filter()` to the recall pipeline in `HeartManager.recall()`:

```python
def apply_supersession_filter(results: list[ScoredResult]) -> list[ScoredResult]:
    """Apply soft scoring penalties to superseded/low-access facts."""
    superseded_ids = {r.fact_id: r for r in results if r.superseded_by is not None}
    
    filtered = []
    for result in results:
        if result.superseded_by is not None:
            # Check if the superseding fact is also in results
            superseder_present = any(
                r.fact_id == result.superseded_by for r in results
            )
            if superseder_present:
                # Superseder in results — suppress this one entirely
                continue
            else:
                # Superseder not in results — apply soft penalty
                result.score *= 0.3
        filtered.append(result)
    return filtered
```

**Where to wire it:** In `HeartManager.recall()` (around line 340 in `heart.py`), after the `facts.search()` call returns results and before they are assembled into the response.

**Requires:** The `hybrid_search()` query must join or include `superseded_by` in returned columns (currently it filters `active=true` only, so superseded facts with `active=false` are excluded anyway). Two options:
- **Option A (minimal):** Only apply to facts that are `active=true` but have non-null `superseded_by` (edge case: explicit supersession set the FK but didn't flip `active`)
- **Option B (richer):** Also query low-confidence facts (`confidence < 0.5, active=true`) and apply graduated penalties based on confidence. This turns confidence into a retrieval signal, not just a storage flag.

**Recommended:** Option B — it makes confidence *mean something* at retrieval time, which is currently not the case.

### Addition 3: Access Tracking & Stale Scan

**Part A: Wire up access tracking**

The columns exist. They just need writes.

```python
# In HeartManager.recall() or facts.search(), after results are returned:
async def track_access(self, fact_ids: list[UUID]):
    """Update recall_count and last_recalled_at for accessed facts."""
    await self.db.execute(
        update(Fact)
        .where(Fact.id.in_(fact_ids))
        .values(
            recall_count=Fact.recall_count + 1,
            last_recalled_at=utcnow(),
        )
    )
```

**Where:** Call after `hybrid_search()` returns results. Fire-and-forget (don't block the recall response).

**Part B: Stale fact scan (sleep-time)**

Add a new phase to `SleepHandler`, after `_phase_resolve_contradictions()`:

```python
async def _phase_stale_scan(self, sleep_stats: dict) -> bool:
    """Deactivate facts that are superseded, rarely accessed, and low confidence."""
    stale = await session.execute(
        select(Fact).where(
            Fact.agent_id == self.agent_id,
            Fact.active == True,
            Fact.superseded_by.isnot(None),
            Fact.confidence < 0.5,
            Fact.last_recalled_at < utcnow() - timedelta(days=30),
        )
    )
    for fact in stale.scalars():
        fact.active = False
    sleep_stats["stale_deactivated"] = stale.rowcount
    return True
```

**Part C: Cluster consolidation (sleep-time)**

Add another sleep phase:

```python
async def _phase_cluster_consolidation(self, sleep_stats: dict) -> bool:
    """Merge 3+ active facts with same subject into one consolidated fact."""
    # Find subjects with 3+ active facts
    clusters = await session.execute(
        select(Fact.subject, func.count(Fact.id).label("cnt"))
        .where(Fact.agent_id == self.agent_id, Fact.active == True, Fact.subject.isnot(None))
        .group_by(Fact.subject)
        .having(func.count(Fact.id) >= 3)
    )
    for subject, count in clusters:
        facts = await session.execute(
            select(Fact).where(
                Fact.agent_id == self.agent_id,
                Fact.active == True,
                Fact.subject == subject,
            ).order_by(Fact.created_at.desc())
        )
        fact_list = facts.scalars().all()
        # LLM micro-call: "Merge these N facts about {subject} into one"
        merged_content = await self._merge_facts(fact_list)
        # Create merged fact, deactivate originals
        ...
    return True
```

---

## Database Changes

### No new columns needed.

All required columns already exist:
- `superseded_by` (UUID FK) ✅
- `contradiction_of` (UUID FK) ✅
- `recall_count` (Integer) ✅
- `last_recalled_at` (DateTime) ✅

### New graph edge type:
```sql
-- 'refines' edge for refinement relationships (Addition 1)
-- Uses existing graph_edges table from F022
-- Existing types: 'supersedes', 'contradicts'
-- New type: 'refines'
```

### Possible index for stale scan:
```sql
-- Composite index for stale scan query (Addition 3B)
CREATE INDEX idx_facts_stale_candidates
ON heart.facts (agent_id, active, superseded_by, confidence, last_recalled_at)
WHERE active = true AND superseded_by IS NOT NULL;
```

---

## Implementation Plan

### Phase 1: Access Tracking (~1h)
- Wire `recall_count` and `last_recalled_at` updates into the recall pipeline
- Fire-and-forget async update after `hybrid_search()` returns
- Verify with test: recall a fact, check columns are updated

### Phase 2: LLM Write-Time Classifier (~3h)
- Add LLM micro-call to `_find_contradiction()` result path
- Define structured output schema: `{relation: UPDATE|CONTRADICTION|REFINEMENT|UNRELATED, current_fact: "new"|"old", confidence: float}`
- Route UPDATE → `supersede()` with soft penalty (confidence × 0.3)
- Route CONTRADICTION → existing `contradict()` flow
- Route REFINEMENT → new `refines` graph edge, both stay active
- Route UNRELATED → no action
- Add to `_supersede_by_subject()` for ambiguous-range matches (0.80–0.95)

### Phase 3: Retrieval Soft Suppression (~2h)
- Implement `apply_supersession_filter()` 
- Wire into `HeartManager.recall()` after search results
- Apply graduated confidence-based penalties (Option B)
- Modify `hybrid_search()` to include `superseded_by` and `confidence` in returned data

### Phase 4: Stale Scan & Cluster Consolidation (~3h)
- Add `_phase_stale_scan()` to `SleepHandler` phase list
- Add `_phase_cluster_consolidation()` to `SleepHandler` phase list  
- LLM micro-call for fact merging (reuse `call_background_llm_structured()`)
- Dashboard metrics: stale deactivation count, cluster merge count per sleep cycle

### Phase 5: Evaluation (~1h)
- Measure stale retrieval rate: % of recall results that are superseded
- Measure contradiction rate: % of fact pairs with unresolved conflicts
- Compare pre/post retrieval quality on known-superseded facts
- Feed metrics into F024 critic evaluation dimensions

**Total estimated effort: ~10h** (down from ~12h in v1, since schema and contradiction resolution are already built)

---

## Success Metrics

- **Stale retrieval rate < 5%** — currently unmeasured, likely 15-25%
- **Zero contradictory facts in same recall result set**
- **Fact count growth rate reduced by 30%** — consolidation prevents unbounded growth
- **Context token waste from stale facts reduced by 50%+**
- **recall_count and last_recalled_at populated for >95% of active facts** (within 2 weeks of deployment)

---

## Risks & Mitigations

- **False supersession (wrong LLM classification)** — Soft penalty (0.3×) not hard delete; reversible. Low-confidence classifications (<0.8) deferred to sleep-time F031 resolution
- **LLM micro-call latency on write path** — Expected 0-2 candidates per write. Use `call_background_llm_structured()` which is already optimized for small structured outputs. If latency becomes an issue, batch during sleep instead
- **Over-aggressive stale scan** — Only targets facts that are: superseded AND low-confidence AND not recalled in 30 days. All three conditions must be true. Facts remain queryable by direct ID even after deactivation
- **Cluster consolidation losing nuance** — LLM merge with explicit "preserve all unique details" prompt instruction. Original facts kept as inactive (recoverable), not deleted

---

## Connection to Existing Work

- **F023 (A-MAC):** LLM classifier result can feed admission scoring — a fact that would supersede an existing high-confidence fact gets admission priority
- **F022 (Graph-Augmented Recall):** Supersession and refinement edges enable "show me the history of this fact" via edge traversal
- **F031 (Consolidation Orient & Resolve):** F027 handles write-time detection; F031 handles sleep-time resolution. They are complementary — F027 catches conflicts as they arrive, F031 catches what slipped through
- **F024 (Critic Agent):** Stale retrieval rate and contradiction rate become observable metrics for the Critic to monitor

---

## Research Grounding

**SleepGate (arXiv:2603.14517):**
- Conflict-aware temporal tagger achieves 99.3% accuracy on supersession labels
- Soft attention biasing outperforms hard eviction
- O(1) amortized conflict detection via LSH
- Three-way action (keep/compress/evict) > binary keep/delete

**CraniMem (arXiv:2603.15642):**
- Goal-conditioned gating prevents noise at admission
- Scheduled consolidation (every N turns) keeps memory bounded
- FreqBonus in replay scoring reinforces important facts

**Memory Survey (arXiv:2603.07670):**
- Identifies "continual consolidation" as top open challenge
- Five mechanism families all require conflict detection as a primitive
