# F027 — Supersession Detection & Principled Forgetting

> **Status:** Draft v3 (spec-reviewed 2026-04-09)  
> **Priority:** P1 (Tier 2)  
> **Depends on:** F002 (Heart Module), F022 (Graph-Augmented Recall), F023 (A-MAC)  
> **Complements:** F031 (Consolidation Orient & Resolve — sleep-time contradiction resolution)  
> **Research:** SleepGate (arXiv:2603.14517), CraniMem (arXiv:2603.15642), Memory Survey (arXiv:2603.07670), Doc 016 Gap G4  
> **Fills:** Gap G4 (Forgetting/Lifecycle), Gap G1 (Memory Evolution)  
> **Revised v2:** Audit against codebase as of 2026-04-06. Removed already-implemented items, corrected schema assumptions, refocused scope.  
> **Revised v3:** Spec-review audit 2026-04-09 — corrected line numbers, FactSummary gap, edge constraint, rowcount bug, session pattern, phase ordering. See notes below each section.

---

## Problem Statement

Nous stores facts but never forgets them. When a fact is updated — "Tim lives in DC" followed by "Tim lives in Silver Spring" — both can persist with equal retrieval weight. The current write-time detection catches **only** same-subject + high-cosine matches (>0.80), missing conflicts across different subjects, phrasing, or extraction pipelines. There is no retrieval-time suppression gradient — a fact is either fully visible (`active=true`) or completely invisible (`active=false`).

**What SleepGate proved:** Without conflict detection, all forgetting policies (sliding window, attention-based, decay-only) perform at <18% retrieval accuracy under proactive interference. With conflict-aware gating, accuracy reaches 99.5% at moderate interference depths.

**The cost:** Each stale fact in recall_deep results consumes ~50-200 context tokens per turn. With 5 stale facts surfacing per query × 20 turns per session × 100 tokens average = 10,000 wasted tokens per session. At scale, this is the single largest source of retrieval noise.

---

## What Already Exists (Built by F022/F023/F031)

Before defining what F027 adds, here's the current state as of 2026-04-06:

### Write-Time Detection (in `FactManager`)
- **Subject-based supersession** (`_supersede_by_subject()`, `facts.py:366-399`): Same subject (case-insensitive) + cosine > 0.80 → sets `superseded_by`, `active = False` on old fact. **Note:** does NOT create a graph edge — only the explicit `supersede()` API does.
- **Contradiction detection** (`_find_contradiction()`, `facts.py:271-328`): Cosine 0.85–0.95 range → returns `ContradictionWarning` (logged, not auto-resolved)
- **Deduplication** (`_find_duplicate()`, `facts.py:411-452`): Cosine > 0.95 → reject as duplicate
- **Explicit supersede/contradict APIs**: `supersede()` (`facts.py:560-610`) and `contradict()` (`facts.py:616-665`) for programmatic use, creating graph edges and adjusting confidence

### Schema Columns (on `heart.facts`)
- `superseded_by` (UUID FK) — ✅ exists, used by `_supersede_by_subject()` and `supersede()`
- `contradiction_of` (UUID FK) — ✅ exists, used by `contradict()`
- `recall_count` (Integer) — ✅ exists in model (`models.py:405`), **but never incremented anywhere in code**
- `last_recalled_at` (DateTime) — ✅ exists in model (`models.py:406`), **but never updated anywhere in code**
- `admission_score` / `admission_scores` (Float/JSONB) — ✅ exists, populated by F023

### Graph Edges (F022)
- `supersedes` and `contradicts` edge types created via `_create_graph_edge()` in `facts.py:65-99` — ✅ working
- Full set of allowed relation types in `ck_edges_relation` CheckConstraint (`models.py:237-239`): `'supports', 'contradicts', 'supersedes', 'related_to', 'caused_by', 'informed_by', 'evidence_for', 'discussed_in', 'extracted_from'`

### Sleep-Time Resolution (F031)
- `_phase_resolve_contradictions()` in `SleepHandler` (`sleep_handler.py:586-707`): Finds contradiction candidates (same subject, cosine 0.75–0.95), runs LLM judgment with SUPERSEDE_A/B, MERGE, REMOVE_A/B, KEEP_BOTH actions. Confidence < 0.7 → downgrades to KEEP_BOTH. — ✅ working
- Current sleep phase order (`_run_sleep()`, `sleep_handler.py:242-288`): `review_decisions → prune → compress → reflect → resolve_contradictions → generalize → evolve_rubric`

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
   - **REFINEMENT**: Keep both active, create `refines` graph edge (new edge type — **requires migration**, see Database Changes)
   - **UNRELATED**: No action (false positive from embedding similarity)

**Cost:** One LLM micro-call per conflict candidate (~50–100 tokens). Expected 0–2 candidates per write. Uses `call_background_llm_structured()` from `nous/handlers/__init__.py:86` (same as F031 contradiction resolution).

**Key difference from current `_supersede_by_subject()`:** The LLM call enables cross-subject detection. "Tim lives in DC" and "Tim moved to Silver Spring, MD" have different subjects but the LLM recognizes the update relationship.

### Addition 2: Retrieval-Time Soft Suppression

**When:** After `hybrid_search()` returns results, before they enter the context window.

**Why:** Currently, `hybrid_search()` filters `active = true` (hardcoded at `search.py:158`). This creates a binary cliff — a fact is either fully visible or gone. SleepGate's key finding: soft attention biasing (`b = β·log(r)`) outperforms hard eviction because it degrades gracefully. If supersession classification was wrong, the old fact still has a chance to surface.

**Implementation:** The filter must be applied **inside `facts._search()`** (at `facts.py:806-824`), not in `HeartManager.recall()` — because `FactSummary` (returned by `facts.search()`) does NOT have a `superseded_by` field. The ORM `Fact` objects fetched at `facts.py:809-810` DO have `superseded_by`, making that the natural insertion point before conversion to `FactSummary`.

Two options for implementation:

**Option A (minimal):** After fetching ORM facts at line 809-810, filter results where `superseded_by is not None`:
```python
# After: fact_result = await session.execute(select(Fact).where(Fact.id.in_(ids)))
# facts = {f.id: f for f in fact_result.scalars().all()}
# Identify superseded facts and suppress/penalize them

suppressed_ids: set[UUID] = set()
all_fact_ids = set(facts.keys())
for f in facts.values():
    if f.superseded_by is not None and f.superseded_by in all_fact_ids:
        # Superseder is also in results — suppress the old one
        suppressed_ids.add(f.id)

return [
    FactSummary(
        id=f.id,
        content=f.content,
        category=f.category,
        subject=f.subject,
        confidence=f.confidence or 1.0,
        active=f.active if f.active is not None else True,
        score=(scores.get(f.id) or 0.0) * (0.3 if f.superseded_by is not None and f.superseded_by not in all_fact_ids else 1.0),
    )
    for fid in ids
    if (f := facts.get(fid)) is not None and f.id not in suppressed_ids
]
```

**Option B (richer):** Also query low-confidence facts (`confidence < 0.5, active=true`) and apply graduated penalties based on confidence. This turns confidence into a retrieval signal, not just a storage flag.

**Recommended:** Option B — it makes confidence *mean something* at retrieval time, which is currently not the case.

**Note on `HeartManager._recall()` location:** `recall()` is at `heart.py:717`, `_recall()` at `heart.py:737`. The fact search call that feeds into this pipeline is at `heart.py:769` (`await self.facts.search(...)`). Adding the filter inside `facts._search()` is preferable to adding it in `_recall()` because that's where the ORM objects (with `superseded_by`) are accessible.

### Addition 3: Access Tracking & Stale Scan

**Part A: Wire up access tracking**

The columns exist (`models.py:405-406`). They just need writes.

```python
# In facts._search(), after returning FactSummary results:
async def _track_access(self, fact_ids: list[UUID], session: AsyncSession) -> None:
    """Update recall_count and last_recalled_at for accessed facts (fire-and-forget)."""
    from sqlalchemy import update
    await session.execute(
        update(Fact)
        .where(Fact.id.in_(fact_ids))
        .values(
            recall_count=Fact.recall_count + 1,
            last_recalled_at=datetime.now(UTC),
        )
    )
```

**Where:** Call after `hybrid_search()` returns in `_search()`, fire-and-forget (don't block the recall response). Use `asyncio.create_task()` with a separate session to avoid blocking or session contention.

**Part B: Stale fact scan (sleep-time)**

Add a new phase to `SleepHandler` after `_phase_resolve_contradictions()` and before `_phase_generalize()` (see current phase order above).

```python
async def _phase_stale_scan(self, sleep_stats: dict) -> bool:
    """Deactivate facts that are superseded, rarely accessed, and low confidence."""
    try:
        from sqlalchemy import update as sa_update
        stale_count = 0
        async with self._heart.db.session() as session:
            result = await session.execute(
                select(Fact).where(
                    Fact.agent_id == self._heart.agent_id,
                    Fact.active == True,  # noqa: E712
                    Fact.superseded_by.isnot(None),
                    Fact.confidence < 0.5,
                    Fact.last_recalled_at < datetime.now(UTC) - timedelta(days=30),
                )
            )
            for fact in result.scalars().all():
                fact.active = False
                stale_count += 1
            await session.commit()
        sleep_stats["stale_deactivated"] = stale_count
        logger.info("Stale scan: deactivated %d facts", stale_count)
        return True
    except Exception:
        logger.warning("Stale scan phase failed", exc_info=True)
        return False
```

**Note:** Uses `self._heart.db.session()` (not `self.db`) consistent with how `SleepHandler` accesses the database through the `Heart` instance. Also note that `rowcount` is NOT available after a SELECT — use a counter variable (the pseudocode above does this correctly).

**Part C: Cluster consolidation (sleep-time)**

Add another sleep phase after `_phase_stale_scan()`:

```python
async def _phase_cluster_consolidation(self, sleep_stats: dict) -> bool:
    """Merge 3+ active facts with same subject into one consolidated fact."""
    if not self._llm:
        return True
    try:
        async with self._heart.db.session() as session:
            clusters = await session.execute(
                select(Fact.subject, func.count(Fact.id).label("cnt"))
                .where(
                    Fact.agent_id == self._heart.agent_id,
                    Fact.active == True,  # noqa: E712
                    Fact.subject.isnot(None),
                )
                .group_by(Fact.subject)
                .having(func.count(Fact.id) >= 3)
            )
            for subject, count in clusters.all():
                facts_result = await session.execute(
                    select(Fact).where(
                        Fact.agent_id == self._heart.agent_id,
                        Fact.active == True,  # noqa: E712
                        Fact.subject == subject,
                    ).order_by(Fact.created_at.desc())
                )
                fact_list = facts_result.scalars().all()
                # LLM micro-call: "Merge these N facts about {subject} into one"
                merged_content = await self._merge_facts(fact_list)
                if merged_content:
                    # Create merged fact, deactivate originals
                    ...
            await session.commit()
        return True
    except Exception:
        logger.warning("Cluster consolidation phase failed", exc_info=True)
        return False
```

---

## Database Changes

### No new columns needed.

All required columns already exist:
- `superseded_by` (UUID FK) ✅
- `contradiction_of` (UUID FK) ✅
- `recall_count` (Integer) ✅
- `last_recalled_at` (DateTime) ✅

### New graph edge type: `refines` — **requires migration**

The `ck_edges_relation` CheckConstraint in `graph_edges` (`models.py:236-240`) must be updated. PostgreSQL requires dropping and re-adding check constraints. Create `sql/migrations/031_supersession_detection.sql`:

```sql
-- F027: Add 'refines' relation type to graph_edges
ALTER TABLE nous_system.graph_edges
    DROP CONSTRAINT IF EXISTS ck_edges_relation;

ALTER TABLE nous_system.graph_edges
    ADD CONSTRAINT ck_edges_relation CHECK (
        relation IN (
            'supports', 'contradicts', 'supersedes', 'related_to', 'caused_by',
            'informed_by', 'evidence_for', 'discussed_in', 'extracted_from', 'refines'
        )
    );
```

Also update the CheckConstraint in `nous/storage/models.py` (`GraphEdge.__table_args__`) to include `'refines'`.

### Optional index for stale scan:
```sql
-- Composite partial index for stale scan query (Addition 3B)
-- Go in the same migration file above
CREATE INDEX idx_facts_stale_candidates
ON heart.facts (agent_id, confidence, last_recalled_at)
WHERE active = true AND superseded_by IS NOT NULL;
```

---

## Implementation Plan

### Phase 1: Access Tracking (~1h)
- Wire `recall_count` and `last_recalled_at` updates into `facts._search()` (fire-and-forget via `asyncio.create_task()` with fresh session)
- Verify with test: recall a fact, check columns are updated

### Phase 2: LLM Write-Time Classifier (~3h)
- Add LLM micro-call to `_find_contradiction()` result path in `facts._learn()` (`facts.py:253-257`)
- Define structured output schema: `{relation: UPDATE|CONTRADICTION|REFINEMENT|UNRELATED, current_fact: "new"|"old", confidence: float}`
- Route UPDATE → `supersede()` with soft penalty (confidence × 0.3)
- Route CONTRADICTION → existing `contradict()` flow
- Route REFINEMENT → new `refines` graph edge, both stay active (requires Phase 2 migration first)
- Route UNRELATED → no action
- Add to `_supersede_by_subject()` for ambiguous-range matches (0.80–0.95)
- Create migration `031_supersession_detection.sql` with `refines` constraint update

### Phase 3: Retrieval Soft Suppression (~2h)
- Implement suppression logic inside `facts._search()` at line 806-824 (after ORM fetch, before `FactSummary` construction)
- Apply graduated confidence-based penalties (Option B)
- No changes needed to `hybrid_search()` — ORM fetch already has `superseded_by`

### Phase 4: Stale Scan & Cluster Consolidation (~3h)
- Add `_phase_stale_scan()` to `SleepHandler` phase list after `resolve_contradictions`, before `generalize`
- Add `_phase_cluster_consolidation()` to `SleepHandler` phase list after `stale_scan`
- LLM micro-call for fact merging (reuse `call_background_llm_structured()` from `nous/handlers/__init__.py:86`)
- Update `sleep_stats` dict to include `stale_deactivated` and `clusters_merged` keys

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

---

## Spec Review Notes (v3, 2026-04-09)

The following issues were found in v2 and corrected above:

1. **Wrong line number for recall insertion point** (v2 line 118): Said "around line 340 in `heart.py`". Actual: `recall()` is at `heart.py:717`, `_recall()` at `heart.py:737`. The fact search call is at `heart.py:769`.

2. **`FactSummary` lacks `superseded_by` field** (v2 lines 97-116): The `apply_supersession_filter()` pseudocode showed `result.superseded_by` but `FactSummary` (in `heart/schemas.py:144-153`) has no such field. Corrected: the filter must be implemented inside `facts._search()` after the ORM object fetch at `facts.py:809-810`, where `Fact` ORM objects with `superseded_by` are available before conversion to `FactSummary`.

3. **Incomplete edge type list** (v2 Database Changes): Said "Existing types: 'supersedes', 'contradicts'". Actual constraint has 9 types — full list now in "What Already Exists" section.

4. **`refines` edge type requires migration** (v2 was silent on this): The `ck_edges_relation` CheckConstraint in `models.py:236-240` must be dropped and re-added. Migration `031_supersession_detection.sql` added to Implementation Plan and Database Changes.

5. **`stale.rowcount` bug** (v2 stale scan pseudocode): `rowcount` is valid for DML statements (INSERT/UPDATE/DELETE), not SELECT results. Fixed: use a counter variable incremented during iteration.

6. **Missing session management in `_phase_stale_scan`** (v2): The pseudocode referenced `session` without showing how it was acquired. Fixed: use `async with self._heart.db.session() as session:` consistent with how `SleepHandler` accesses the Heart instance.

7. **Phase ordering now explicit**: New phases go after `resolve_contradictions` and before `generalize` (current order: `review → prune → compress → reflect → resolve_contradictions → [stale_scan → cluster_consolidation] → generalize → evolve_rubric`).

8. **`_supersede_by_subject()` does not create graph edges**: Added note that this method (unlike the explicit `supersede()` API) only sets the FK and flips `active=False` — no graph edge is created. This is a subtle consistency gap worth noting for Phase 2 implementation.
