# F027 — Supersession Detection & Principled Forgetting

> **Status:** Draft v1
> **Priority:** P1
> **Depends on:** F002 (Heart Module), F022 (Graph-Augmented Recall), F023 (A-MAC)
> **Research:** SleepGate (arXiv:2603.14517), CraniMem (arXiv:2603.15642), Memory Survey (arXiv:2603.07670), Doc 016 Gap G4
> **Fills:** Gap G4 (Forgetting/Lifecycle), Gap G1 (Memory Evolution)

---

## Problem Statement

Nous stores facts but never forgets them. When a fact is updated — "Tim lives in DC" followed by "Tim lives in Silver Spring" — both persist with equal retrieval weight. The older fact is never marked stale. Over time, this creates **proactive interference**: recall_deep returns outdated facts that compete with current ones for context window space.

**Current state:**
- `FactManager._learn()` has dedup (>0.95 cosine) and subject supersession (same subject + >0.80 cosine → retire older)
- But supersession only fires on exact subject match during `learn_fact()` — it doesn't detect conflicts across different extraction pipelines
- No mechanism detects that "Tim's timezone is EST" supersedes "Tim's timezone is PST" if the subjects aren't identical strings
- No periodic scan for accumulated contradictions
- No soft forgetting — facts are either active or inactive, nothing in between

**What SleepGate proved:** Without conflict detection, all forgetting policies (sliding window, attention-based, decay-only) perform at <18% retrieval accuracy under proactive interference. With conflict-aware gating, accuracy reaches 99.5% at moderate interference depths.

**The cost:** Each stale fact in recall_deep results consumes ~50-200 context tokens per turn. With 5 stale facts surfacing per query × 20 turns per session × 100 tokens average = 10,000 wasted tokens per session. At scale, this is the single largest source of retrieval noise.

---

## Solution: Three-Layer Supersession Detection

### Layer 1: Write-Time Conflict Detection

**When:** Every `learn_fact()` call (both user-initiated and automated extraction)

**How:**
1. Compute semantic signature of the new fact: `sig = embed(subject + " " + category)`
2. Search existing facts with same subject OR high signature similarity (cosine > 0.80)
3. For each candidate match, classify the relationship:
   - **Update** (same entity, same property, different value) → supersede old fact
   - **Contradiction** (same entity, conflicting claims) → flag for review, store both with conflict edge
   - **Refinement** (same entity, more specific claim) → keep both, link via graph edge
   - **Unrelated** (false positive from embedding similarity) → no action

**Classification method:** LLM micro-call with structured output:
```
Given existing fact: "{old_fact}"
And new fact: "{new_fact}"
Classify: UPDATE | CONTRADICTION | REFINEMENT | UNRELATED
If UPDATE: which fact is current?
```

**Cost:** One embedding lookup (existing) + one LLM micro-call per conflict candidate (~50 tokens). Expected 0-2 candidates per write = negligible overhead.

**Action on UPDATE:**
- Set `old_fact.superseded_by = new_fact.id`
- Set `old_fact.confidence *= 0.3` (soft penalty, not deletion)
- Create graph edge: `new_fact --supersedes--> old_fact`
- Log supersession event for dashboard

### Layer 2: Retrieval-Time Suppression

**When:** Every `recall_deep()` result set assembly

**How:** After vector search returns candidates, apply supersession filter:

```python
def apply_supersession_filter(results: list[Fact]) -> list[Fact]:
    """Suppress superseded facts in retrieval results."""
    filtered = []
    for fact in results:
        if fact.superseded_by is not None:
            # Check if the superseding fact is also in results
            superseder = next((f for f in results if f.id == fact.superseded_by), None)
            if superseder:
                # Superseder present — suppress this one entirely
                continue
            else:
                # Superseder not in results — apply soft penalty
                fact.score *= 0.3
        filtered.append(fact)
    return filtered
```

**Why soft penalty instead of hard filter:** SleepGate's key finding — soft attention biasing (`b = β·log(r)`) outperforms hard eviction because it degrades gracefully. If the supersession classification was wrong, the old fact still has a chance to surface.

### Layer 3: Scheduled Consolidation Sweep

**When:** Triggered by:
1. Session end (conversation close) — lightweight sweep
2. Daily maintenance tick — full sweep
3. Conflict density threshold — when >15% of facts for any subject have conflicts

**What it does:**
1. **Stale scan:** Find all facts where `last_accessed < 30 days` AND `confidence < 0.5` AND `superseded_by IS NOT NULL` → mark `active = false`
2. **Contradiction resolution:** Find all fact pairs with contradiction graph edges → run LLM judgment to determine which is current → supersede the stale one
3. **Cluster consolidation:** Find fact clusters (same subject, 3+ facts) → merge into one consolidated fact, retire the fragments
4. **Metrics:** Log supersession count, contradiction count, consolidation count per sweep

---

## Database Changes

### Facts table additions:
```sql
ALTER TABLE heart.facts ADD COLUMN superseded_by UUID REFERENCES heart.facts(id);
ALTER TABLE heart.facts ADD COLUMN supersession_score FLOAT DEFAULT NULL;
ALTER TABLE heart.facts ADD COLUMN last_accessed TIMESTAMPTZ DEFAULT NULL;
CREATE INDEX idx_facts_superseded ON heart.facts(superseded_by) WHERE superseded_by IS NOT NULL;
CREATE INDEX idx_facts_last_accessed ON heart.facts(last_accessed);
```

### Graph edge type addition:
```sql
-- New edge type for supersession relationships
-- Uses existing graph_edges table from F022
INSERT INTO brain.graph_edges (source_type, source_id, target_type, target_id, relation, weight)
VALUES ('fact', :new_id, 'fact', :old_id, 'supersedes', 1.0);
```

### Access tracking:
Update `last_accessed` on every `recall_deep` hit:
```python
async def track_access(self, fact_ids: list[UUID]):
    await self.db.execute(
        update(Fact).where(Fact.id.in_(fact_ids)).values(last_accessed=utcnow())
    )
```

---

## Implementation Plan

### Phase 1: Write-Time Detection (~4h)
- Add `superseded_by`, `supersession_score`, `last_accessed` columns
- Implement conflict candidate search in `FactManager._learn()`
- Add LLM micro-call classifier for UPDATE/CONTRADICTION/REFINEMENT/UNRELATED
- Create supersession graph edges
- Add access tracking to recall_deep

### Phase 2: Retrieval Suppression (~2h)
- Implement `apply_supersession_filter()` in recall pipeline
- Add soft penalty scoring (0.3× multiplier for superseded facts)
- Wire into `HeartManager.recall()` result assembly

### Phase 3: Consolidation Sweep (~4h)
- Implement stale scan (age + confidence + superseded → deactivate)
- Implement contradiction resolution (LLM judgment on conflict pairs)
- Implement cluster consolidation (merge 3+ same-subject facts)
- Add sweep trigger to session end handler and daily tick
- Dashboard metrics for supersession events

### Phase 4: Evaluation (~2h)
- Measure stale retrieval rate: % of recall_deep results that are superseded
- Measure contradiction rate: % of fact pairs with unresolved conflicts
- Compare pre/post retrieval quality on known-superseded facts
- Add to F024 critic evaluation dimensions

---

## Success Metrics

- **Stale retrieval rate < 5%** — currently unmeasured, likely 15-25%
- **Zero contradictory facts in same recall result** — currently possible
- **Fact count growth rate reduced by 30%** — consolidation prevents unbounded growth
- **Context token waste from stale facts reduced by 50%+**

---

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| False supersession (wrong classification) | Soft penalty (0.3×) not hard delete; reversible |
| LLM micro-call latency on write path | Cache classifier results; batch during consolidation sweep |
| Over-aggressive consolidation | Phase 1 runs in shadow mode (log only) before enforcing |
| Loss of historical context | Superseded facts remain queryable via direct ID lookup; only suppressed in ranked results |

---

## Connection to Existing Work

- **F023 (A-MAC):** Supersession score becomes a 6th dimension in admission control. A fact that would supersede an existing high-confidence fact gets admission priority.
- **F022 (Graph-Augmented Recall):** Supersession edges enable graph-based retrieval: "show me the history of this fact" via edge traversal.
- **F008 (Memory Lifecycle):** Supersession is the primary trigger for the CONFIRMED → SUPERSEDED → INACTIVE lifecycle defined in F008 but never implemented.
- **F024 (Critic Agent):** Stale retrieval rate becomes an observable metric for the Critic to monitor.

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
