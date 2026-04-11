# F040 — Graph Densification: Orphan Recovery & Edge Coverage

> **Status:** Draft  
> **Priority:** P1  
> **Depends on:** F022 (Graph-Augmented Recall — shipped), F027 (Supersession Detection — shipped)  
> **Related:** F031 (Sleep Consolidation), F022 Phase 4 (Spreading Activation)  
> **Author:** Nous  
> **Created:** 2026-04-11

---

## Problem Statement

The Nous knowledge graph is critically sparse. Graph-augmented recall (F022) and spreading activation are architecturally sound, but they operate on a graph where **most nodes have no edges at all**.

### Measured Graph State (2026-04-11)

| Entity Type | Total Active | Linked | Orphan | Orphan % |
|-------------|-------------|--------|--------|----------|
| Facts       | 1,487       | 759    | 728    | **49%**  |
| Decisions   | 369         | 156    | 213    | **58%**  |
| Episodes    | 414         | 146    | 268    | **65%**  |
| Procedures  | 55          | 2      | 53     | **96%**  |
| **TOTAL**   | **2,325**   | **1,063** | **1,262** | **54%** |

**1,262 orphan nodes** — more than half of all knowledge has zero graph connectivity.

### Edge Distribution

- 1,277 total edges across 1,133 connected nodes
- Average degree: 2.3 (median: 2, p90: 5)
- 47% of connected nodes are leaf nodes (degree 1 — one edge, dead end)
- Even the best-connected hub only reaches 31 nodes in 4 hops (2.7% of graph)

### Edge Type Breakdown

| Source→Target | Relation | Count | Notes |
|---------------|----------|-------|-------|
| fact→fact | related_to | 695 | 54% of all edges — working well |
| fact→decision | evidence_for | 252 | Cross-type linking — working |
| fact→episode | extracted_from | 218 | Deterministic linking — working |
| fact→episode | discussed_in | 64 | Deterministic linking — working |
| decision→decision | related_to | 33 | Only 9% of decisions linked |
| episode→episode | related_to | 6 | Almost no episode connectivity |
| fact→fact | supersedes | 4 | F027 working but rare |
| procedure→fact | informed_by | 3 | Procedures nearly disconnected |
| episode→decision | discussed_in | 2 | Should be much higher |

### Root Causes

1. **Pre-linking legacy data:** 578 facts (39%) were created before cross-type linking deployed on 2026-03-20. These never got linked. But even post-linking: 406 of 909 post-linking facts (45%) are still orphans.

2. **Threshold too high:** `cross_type_threshold=0.80` for fact→decision and `cross_type_same_threshold=0.90` for fact→fact. The average edge weight is 0.813 for related_to, meaning many candidate pairs score 0.70-0.79 and get rejected despite genuine semantic relationships.

3. **Linking only happens at creation time:** `FactGraphLinker` runs when a fact is created, but only compares against decisions from the last 30 days. Facts created before a related decision have no way to get linked retroactively.

4. **Decision→decision linking is too conservative:** `auto_link_decisions()` uses cosine > 0.85 and max 3 links. Only 33 edges across 369 decisions.

5. **Episode linking only captures deterministic relationships:** `link_episode_deterministic()` links episodes to decisions/facts mentioned in that episode. But episodes about similar topics are never connected to each other (only 6 episode→episode edges).

6. **Procedures are graph-invisible:** 96% of procedures have zero edges. No linking logic exists for procedures at creation time. Only 3 edges total (procedure→fact, informed_by).

7. **No backward linking:** When a new decision is created, it gets linked to similar decisions. But it never checks if existing orphan facts should be linked to it. Linking is strictly one-directional.

### Why This Matters

- **Spreading activation is useless on a sparse graph.** F022 Phase 4 exists but `should_use_spreading_activation()` checks density — with avg degree 2.3, it falls below any useful threshold.
- **Graph-augmented recall adds noise, not signal.** With most nodes being leaves, 1-hop expansion mostly hits dead ends or unrelated nodes.
- **Multi-hop reasoning chains don't exist.** "What facts informed decision X?" requires fact→decision edges. With 58% of decisions orphaned, this query fails >50% of the time.
- **Sleep consolidation can't see patterns.** The reflect phase finds facts by vector search but misses structural clusters that would emerge from proper graph connectivity.

---

## Design Principles

1. **Backfill before build** — Fix the orphan problem for existing data before adding new linking paths.
2. **Lower the bar, raise the signal** — Reduce thresholds but add relation-type specificity to maintain precision.
3. **Every entity type gets linked** — Decisions, episodes, and procedures need first-class linking parity with facts.
4. **Batch operations for efficiency** — Don't embed one-at-a-time. Process orphans in bulk during sleep.
5. **Measure density as a health metric** — Track orphan rate and average degree over time. Alert when density drops.

---

## Phase 1: Orphan Backfill Engine (Sleep Phase)

**Goal:** Process all 1,262 orphan nodes during sleep cycles, creating edges to semantically related existing nodes.

### New Sleep Phase: `_phase_graph_densification`

Add after `_phase_cluster_consolidation` in `SleepHandler._run_sleep()`. This phase:

1. Queries for orphan nodes (nodes with zero edges in `graph_edges`)
2. For each orphan, finds candidates via vector similarity
3. Creates edges above a reduced threshold
4. Processes up to N orphans per sleep cycle to control cost

```python
async def _phase_graph_densification(self, sleep_stats: dict) -> bool:
    """Phase 7: Connect orphan nodes to the knowledge graph."""
    if not self._graph_linker:
        return True
    try:
        edges_created = 0
        
        # Process each entity type
        for entity_type, backfill_fn, max_per_cycle in [
            ("fact", self._backfill_orphan_facts, 50),
            ("decision", self._backfill_orphan_decisions, 30),
            ("episode", self._backfill_orphan_episodes, 30),
            ("procedure", self._backfill_orphan_procedures, 20),
        ]:
            if self._interrupted:
                break
            count = await backfill_fn(max_per_cycle)
            edges_created += count
            
        sleep_stats["orphan_edges_created"] = edges_created
        logger.info("Graph densification: created %d edges", edges_created)
        return True
    except Exception:
        logger.warning("Graph densification phase failed", exc_info=True)
        return False
```

### Orphan Detection Query

Common query pattern for finding orphan nodes:

```sql
-- Find orphan facts (not in any edge as source or target where type='fact')
SELECT f.id, f.content, f.embedding, f.category, f.subject
FROM heart.facts f
WHERE f.agent_id = :agent_id
  AND f.active = true
  AND f.embedding IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM brain.graph_edges e
    WHERE (e.source_id = f.id AND e.source_type = 'fact')
       OR (e.target_id = f.id AND e.target_type = 'fact')
  )
ORDER BY f.created_at DESC
LIMIT :max_per_cycle;
```

Equivalent patterns for decisions (`brain.decisions`), episodes (`heart.episodes`), and procedures (`heart.procedures`).

### Backfill Logic: Orphan Facts

For each orphan fact:

1. **Fact→Fact linking** (threshold: 0.82, reduced from 0.90):
   - Use the fact's existing embedding (no re-embedding needed for same-type)
   - Vector search against other active facts
   - Create `related_to` edges for matches above threshold
   - Max 5 edges per fact

2. **Fact→Decision linking** (threshold: 0.72, reduced from 0.80):
   - Use common-template re-embedding: `"fact: {content}"` vs `"decision: {description}"`
   - Search recent + semantically similar decisions (no 30-day cutoff during backfill)
   - Create `evidence_for` edges
   - Max 3 edges per fact

3. **Fact→Episode linking** (threshold: 0.70):
   - Search episodes whose summaries semantically match the fact
   - Create `discussed_in` edges
   - Max 2 edges per fact

### Backfill Logic: Orphan Decisions

For each orphan decision:

1. **Decision→Decision linking** (threshold: 0.78, reduced from 0.85):
   - Use existing decision embedding
   - Search against other decisions
   - Create `related_to` edges
   - Max 5 edges per decision

2. **Decision→Fact linking** (reverse direction — threshold: 0.72):
   - Search facts that semantically relate to this decision
   - Create `evidence_for` edges (fact→decision direction)
   - Max 3 edges per decision

3. **Decision→Episode linking** (threshold: 0.70):
   - Search episodes where this decision's topic was discussed
   - Create `discussed_in` edges (episode→decision direction)
   - Max 2 edges per decision

### Backfill Logic: Orphan Episodes

For each orphan episode:

1. **Episode→Episode linking** (threshold: 0.75):
   - Compare episode summaries via embedding
   - Create `related_to` edges between topically similar episodes
   - Max 3 edges per episode

2. **Episode→Fact linking** (threshold: 0.70):
   - Search facts that relate to episode summary content
   - Create `discussed_in` edges
   - Max 3 edges per episode

3. **Episode→Decision linking** (threshold: 0.70):
   - Already handled by deterministic linking, but catch cases where
     decisions were made outside the episode's direct transcript
   - Create `discussed_in` edges
   - Max 2 edges per episode

### Backfill Logic: Orphan Procedures

For each orphan procedure:

1. **Procedure→Fact linking** (threshold: 0.70):
   - Compare procedure trigger patterns + body against fact embeddings
   - Create `informed_by` edges (procedure→fact)
   - Max 3 edges per procedure

2. **Procedure→Decision linking** (threshold: 0.70):
   - Search decisions that led to creating this procedure
   - Create `caused_by` edges (procedure→decision)
   - Max 2 edges per procedure

3. **Procedure→Episode linking** (threshold: 0.70):
   - Search episodes where this skill was discussed or created
   - Create `discussed_in` edges
   - Max 2 edges per procedure

### Embedding Strategy for Backfill

**Key optimization:** Most nodes already have embeddings stored. The expensive part is cross-type comparison, which requires common-template re-embedding.

Strategy:
- **Same-type linking** (fact↔fact, decision↔decision, episode↔episode): Use existing stored embeddings directly. No re-embedding needed. Compare via pgvector `<=>` operator.
- **Cross-type linking** (fact↔decision, etc.): Re-embed using common template format. Cache the re-embedded vectors in a temporary batch to avoid redundant API calls.

```python
# Batch re-embedding for cross-type comparison
# For 50 orphan facts: embed all 50 in one batch call, then compare against
# a pre-cached set of decision template embeddings
template_texts = [f"fact: {f.content}" for f in orphan_facts]
template_embeddings = await embedder.embed_batch(template_texts)
```

### Rate Limiting & Cost Control

- **Per-cycle cap:** 50 facts + 30 decisions + 30 episodes + 20 procedures = 130 nodes max
- **Embedding calls:** Batch embedding reduces API calls. Same-type uses stored embeddings (0 cost).
- **Edge creation cap:** Max 10 edges per node x 130 nodes = 1,300 edges max per cycle
- **Interruptible:** Check `self._interrupted` between each entity type batch
- **Progress tracking:** Store last-processed ID so backfill resumes where it left off

### Configuration

```python
# New settings in nous/config.py
graph_backfill_enabled: bool = True
graph_backfill_max_facts: int = 50
graph_backfill_max_decisions: int = 30
graph_backfill_max_episodes: int = 30
graph_backfill_max_procedures: int = 20

# Backfill uses lower thresholds than real-time linking
graph_backfill_fact_fact_threshold: float = 0.82
graph_backfill_fact_decision_threshold: float = 0.72
graph_backfill_fact_episode_threshold: float = 0.70
graph_backfill_decision_decision_threshold: float = 0.78
graph_backfill_episode_episode_threshold: float = 0.75
graph_backfill_procedure_threshold: float = 0.70
```

### Estimated LOC: ~350

---

## Phase 2: Bidirectional Reverse Linking at Creation Time

**Goal:** When a new decision/episode/procedure is created, search backward for orphan facts and entities that should link to it. Currently linking is strictly forward (fact→existing decisions on fact creation). This adds the reverse direction.

### Problem

Current flow:
```
New fact created → search existing decisions → link fact→decision ✓
New decision created → auto_link to similar decisions only → ignores all facts ✗
New episode created → deterministic links only → misses similar episodes ✗
New procedure created → no linking at all ✗
```

Missing flow:
```
New decision created → search existing facts → link fact→decision (reverse)
New decision created → search existing episodes → link episode→decision (reverse)
New episode created → search similar episodes → link episode→episode
New procedure created → search related facts/decisions → link procedure→fact/decision
```

### Implementation

#### 2a: Decision Creation Reverse Linking

Add a `DecisionGraphLinker` handler on the `decision_recorded` event (emitted by Brain).

> **Source code note:** `Brain._record()` currently emits `decision_recorded` with only
> `{"decision_id": str, "category": str}`. The event payload does **not** include
> `description` or `tags`. The handler must therefore fetch the full decision record
> by ID before performing similarity searches.

**Option A (recommended):** Fetch decision inside the handler — zero changes to Brain:

```python
class DecisionGraphLinker:
    """Reverse-link: when a decision is created, find related orphan facts and episodes."""
    
    def __init__(self, brain, graph_linker, settings, bus):
        self._brain = brain        # Brain instance — for fetching full decision
        self._linker = graph_linker
        self._settings = settings
        bus.on("decision_recorded", self.handle)
    
    async def handle(self, event: Event):
        decision_id = event.data.get("decision_id")
        
        # Fetch full decision record (description, tags, embedding)
        decision = await self._brain.get(decision_id)
        if not decision:
            return
        
        description = decision.description
        tags = decision.tags or []
        
        # 1. Find facts that relate to this decision
        # 2. Find episodes that discussed this topic
        # 3. Create edges using description for similarity search
```

**Option B (cleaner long-term):** Extend the event payload in `Brain._record()`:

```python
# In brain.py _record(), change the emit from:
await self._emit_event(
    session,
    "decision_recorded",
    {"decision_id": str(decision.id), "category": input.category},
)

# To:
await self._emit_event(
    session,
    "decision_recorded",
    {
        "decision_id": str(decision.id),
        "category": input.category,
        "description": input.description,
        "tags": input.tags,
    },
)
```

**Recommendation:** Implement Option A first (no Brain changes needed), then migrate to
Option B when other handlers also need richer event data.

**Key change:** This ensures that when a decision is recorded _after_ related facts already exist, the edges still get created.

#### 2b: Episode Creation Semantic Linking

Extend `EpisodeSummarizer.handle()` — after deterministic linking, add semantic search for similar episodes:

```python
# After deterministic linking (existing code)
# Add: semantic episode↔episode linking
if self._graph_linker and episode.structured_summary:
    similar_episodes = await self._search_similar_episodes(
        episode_id, episode.structured_summary.get("summary", "")
    )
    for similar in similar_episodes:
        await self._graph_linker.create_edge(
            source_id=episode_id, target_id=similar.id,
            source_type="episode", target_type="episode",
            relation="related_to", weight=similar.score,
        )
```

#### 2c: Procedure Creation Linking

> **Source code note:** `ProcedureManager.store()` currently does **not** emit any event.
> The only procedure events today are `procedure_activated` (from `activate()`) and
> `procedure_outcome` (from `record_outcome()`). A new `procedure_stored` event must
> be added before this handler can work.

**Step 1 — Add `procedure_stored` event to `ProcedureManager._store()`:**

```python
# In procedures.py _store(), after session.flush():
procedure = Procedure(...)
session.add(procedure)
await session.flush()

# ADD THIS:
await self._emit_event(
    session,
    "procedure_stored",
    {
        "procedure_id": str(procedure.id),
        "name": input.name,
        "domain": input.domain,
        "description": input.description,
        "tags": input.tags or [],
    },
)

return self._to_detail(procedure)
```

**Step 2 — Add `ProcedureGraphLinker` handler:**

```python
class ProcedureGraphLinker:
    """Link new procedures to related facts and decisions."""
    
    def __init__(self, procedure_manager, graph_linker, settings, bus):
        self._procedures = procedure_manager
        self._linker = graph_linker
        self._settings = settings
        bus.on("procedure_stored", self.handle)
    
    async def handle(self, event: Event):
        proc_id = event.data.get("procedure_id")
        description = event.data.get("description", "")
        domain = event.data.get("domain", "")
        
        # Use description + domain for similarity search
        # (embedding already stored by _store(), can also fetch it)
        
        # 1. Find facts related to this procedure's domain
        # 2. Find decisions that motivated this procedure
        # 3. Create informed_by and caused_by edges
```

> **Note:** Unlike the decision handler (2a), we control the full event payload here
> since we're adding the event from scratch. Include all fields the linker needs
> directly in the event to avoid a round-trip fetch.

### Event Requirements

Verified against source code (2026-04-11):

| Event | Required Data | Status | Action Needed |
|-------|--------------|--------|---------------|
| `decision_recorded` | decision_id, description, tags | ⚠️ **Partial** — only emits `decision_id` + `category` | Handler fetches full record by ID (Option A), or extend payload (Option B) |
| `procedure_stored` | procedure_id, name, domain, description, tags | ❌ **Does not exist** — `store()` emits no event | Add `procedure_stored` emit to `ProcedureManager._store()` |
| `session_ended` | episode_id, transcript | ✅ Exists and sufficient | None |

### Estimated LOC: ~250

---

## Phase 3: Threshold Tuning & Relation Specificity

**Goal:** Replace the current one-size-fits-all thresholds with per-relation-type thresholds, and add edge quality scoring.

### Problem

Current thresholds:
- `cross_type_threshold = 0.80` — for all cross-type links
- `cross_type_same_threshold = 0.90` — for all same-type links
- `auto_link threshold = 0.85` — for decision→decision

These are too high. The average existing edge weight is:
- `related_to` (fact→fact): avg 0.813 — threshold 0.90 rejects the median match
- `evidence_for` (fact→decision): avg 0.770 — threshold 0.80 rejects most matches
- `extracted_from` (fact→episode): avg 0.996 — deterministic, no threshold issue
- `discussed_in` (fact→episode): avg 0.736 — below 0.80, only passes because deterministic

### New Threshold Matrix

```python
# Per-relation thresholds in config
EDGE_THRESHOLDS = {
    # Same-type
    ("fact", "fact", "related_to"): 0.82,          # Was 0.90
    ("decision", "decision", "related_to"): 0.78,  # Was 0.85
    ("episode", "episode", "related_to"): 0.75,    # New
    
    # Cross-type
    ("fact", "decision", "evidence_for"): 0.72,     # Was 0.80
    ("fact", "episode", "discussed_in"): 0.70,      # New
    ("episode", "decision", "discussed_in"): 0.70,  # New
    ("procedure", "fact", "informed_by"): 0.70,     # New
    ("procedure", "decision", "caused_by"): 0.70,   # New
}
```

### Edge Confidence Scoring

Not all edges above threshold are equally useful. Add a composite confidence score:

```python
def edge_confidence(
    similarity: float,
    shared_tags: int,
    shared_subject: bool,
    temporal_proximity_days: float,
) -> float:
    """Score an edge candidate beyond raw similarity."""
    score = similarity * 0.6                          # Embedding similarity
    score += min(shared_tags * 0.05, 0.15)           # Tag overlap (max 0.15)
    score += 0.10 if shared_subject else 0.0         # Same subject bonus
    score += max(0, 0.15 - temporal_proximity_days * 0.001)  # Recency bonus
    return min(score, 1.0)
```

This means a fact with similarity 0.73 to a decision gets boosted to ~0.85 if they share tags and a subject, making it a strong `evidence_for` edge. Conversely, a 0.82 similarity between unrelated subjects with no tag overlap stays at ~0.50 effective score and might be rejected.

### Weight Differentiation

Currently all edges are weighted by raw cosine similarity. Add semantic weighting:

| Relation | Base Weight Multiplier | Rationale |
|----------|----------------------|-----------|
| `evidence_for` | 1.0x | Direct supporting evidence — full weight |
| `related_to` | 0.8x | Topical similarity — slightly less traversal priority |
| `discussed_in` | 0.7x | Contextual — useful for provenance, not reasoning |
| `extracted_from` | 1.0x | Deterministic — always full weight |
| `supersedes` | 1.0x | Critical for version tracking |
| `contradicts` | 1.0x | Critical for consistency |
| `informed_by` | 0.9x | Strong causal link |
| `caused_by` | 0.9x | Strong causal link |

### Real-Time Threshold Adaptation (Stretch)

After backfill completes, compute density metrics per entity type. If a type is still >30% orphaned, automatically lower that type's threshold by 0.05 for the next cycle.

```python
async def adaptive_threshold(entity_type: str, base_threshold: float) -> float:
    orphan_rate = await compute_orphan_rate(entity_type)
    if orphan_rate > 0.30:
        return max(base_threshold - 0.05, 0.60)  # Floor at 0.60
    return base_threshold
```

### Estimated LOC: ~150

---

## Phase 4: Graph Health Dashboard & Monitoring

**Goal:** Continuous monitoring of graph density to prevent regression and measure improvement.

### Health Metrics

Compute during each sleep cycle and store as events:

```python
graph_health = {
    "total_nodes": 2325,
    "total_edges": 1277,
    "orphan_count": 1262,
    "orphan_rate": 0.54,
    "avg_degree": 2.3,
    "median_degree": 2,
    "p90_degree": 5,
    "max_degree": 15,
    "connected_components": 47,  # Number of disconnected subgraphs
    "largest_component_size": 890,
    "density_by_type": {
        "fact": {"total": 1487, "orphan": 728, "orphan_rate": 0.49, "avg_degree": 2.1},
        "decision": {"total": 369, "orphan": 213, "orphan_rate": 0.58, "avg_degree": 1.8},
        "episode": {"total": 414, "orphan": 268, "orphan_rate": 0.65, "avg_degree": 1.5},
        "procedure": {"total": 55, "orphan": 53, "orphan_rate": 0.96, "avg_degree": 0.1},
    },
    "edge_distribution": {
        "related_to": 734,
        "evidence_for": 252,
        "extracted_from": 218,
        "discussed_in": 66,
        "supersedes": 4,
        "informed_by": 3,
    },
}
```

### Alerting

Emit warning events when:
- Overall orphan rate exceeds 40% (currently 54%)
- Any entity type orphan rate exceeds 60%
- Average degree drops below 2.0
- New entities are created without any edges for 24+ hours (linking pipeline broken)

```python
if graph_health["orphan_rate"] > 0.40:
    await bus.emit(Event(
        type="graph_health_warning",
        data={
            "warning": "orphan_rate_high",
            "orphan_rate": graph_health["orphan_rate"],
            "threshold": 0.40,
        },
    ))
```

### Dashboard Query Endpoint

Add to `nous/api/dashboard_queries.py`:

```python
async def get_graph_health(db: Database, agent_id: str) -> dict:
    """Compute current graph health metrics for dashboard display."""
    # Uses the same queries from the analysis above
    # Returns structured health dict
```

### Target Metrics (Success Criteria)

After full F040 deployment:

| Metric | Current | Phase 1 Target | Phase 2 Target | Final Target |
|--------|---------|---------------|---------------|-------------|
| Overall orphan rate | 54% | 25% | 15% | <10% |
| Fact orphan rate | 49% | 20% | 10% | <5% |
| Decision orphan rate | 58% | 25% | 15% | <10% |
| Episode orphan rate | 65% | 35% | 20% | <15% |
| Procedure orphan rate | 96% | 50% | 25% | <15% |
| Average degree | 2.3 | 4.0 | 5.5 | >6.0 |
| Spreading activation useful | No | Maybe | Yes | Yes |

### Estimated LOC: ~120

---

## Phase 5: Semantic Cluster Discovery

**Goal:** Identify natural clusters in the knowledge graph and create hub nodes that connect related subgraphs.

### Problem

Even after backfill, the graph may remain fragmented — many small connected components rather than one cohesive graph. This phase identifies semantic clusters and creates bridge edges between them.

### Algorithm

1. **Component detection:** Use a UNION-FIND query to identify connected components in `graph_edges`
2. **Cluster embedding:** For each component with 3+ nodes, compute a centroid embedding (average of member embeddings)
3. **Cross-cluster bridging:** Compare centroids between components. If similarity > 0.65, create a `related_to` edge between the two nodes closest to each other across components
4. **Hub identification:** Nodes in the top 5% by degree become "hub nodes" — their edges get a weight boost of 1.1x to encourage traversal through them

### Bridge Edge Creation

```sql
-- Find pairs of disconnected components with semantically similar content
-- Component centroids computed in application layer, then:
-- For the two closest nodes across components, create a bridge edge
INSERT INTO brain.graph_edges (source_id, target_id, source_type, target_type, relation, weight, auto_linked, agent_id)
VALUES (:node_a, :node_b, :type_a, :type_b, 'related_to', :similarity, true, :agent_id)
ON CONFLICT DO NOTHING;
```

### Sleep Integration

This runs as a monthly maintenance task (not every sleep cycle):
- Compute connected components
- Find bridge candidates
- Create max 20 bridge edges per run
- Track component count over time (should decrease)

### Estimated LOC: ~200

---

## Implementation Plan

### Priority Order

| Phase | Name | LOC | Value | Cost | Priority |
|-------|------|-----|-------|------|----------|
| 1 | Orphan Backfill Engine | ~350 | **Critical** — fixes 1,262 orphans | Medium (embeddings) | P0 |
| 2 | Bidirectional Reverse Linking | ~250 | **High** — prevents new orphans | Low | P1 |
| 3 | Threshold Tuning | ~150 | **Medium** — improves edge quality | Low | P1 |
| 4 | Graph Health Dashboard | ~120 | **Medium** — visibility & alerting | Low | P2 |
| 5 | Semantic Cluster Discovery | ~200 | **Low** — diminishing returns | Medium | P3 |

**Total estimated LOC:** ~1,070

### Dependencies

```
Phase 1 (backfill) ← standalone, can ship immediately
Phase 2 (reverse linking) ← standalone, can ship in parallel with Phase 1
Phase 3 (thresholds) ← should ship WITH Phase 1 (backfill uses these thresholds)
Phase 4 (dashboard) ← after Phase 1 (needs metrics to be meaningful)
Phase 5 (clusters) ← after Phase 1+2 (needs denser graph to find meaningful clusters)
```

### Recommended MVP: Phases 1 + 3

Ship the backfill engine with tuned thresholds. This alone should:
- Reduce overall orphan rate from 54% to ~25%
- Increase average degree from 2.3 to ~4.0
- Make spreading activation useful for the first time
- Process ~130 orphans per sleep cycle → full backfill in ~10 sleep cycles

---

## Migration

### Database Changes

No schema changes required. The existing `brain.graph_edges` table supports all entity types and relation types needed.

However, consider adding an index for orphan detection efficiency:

```sql
-- Partial index for faster orphan queries
-- (not strictly necessary at current scale, but prevents degradation)
CREATE INDEX CONCURRENTLY idx_facts_active_agent 
ON heart.facts(agent_id) 
WHERE active = true;

-- Already have idx_graph_edges_source_type and idx_graph_edges_target_type
-- These are sufficient for the NOT EXISTS subqueries
```

### Configuration Additions

```python
# Add to nous/config.py Settings class

# Phase 1: Backfill
graph_backfill_enabled: bool = True
graph_backfill_max_facts_per_cycle: int = 50
graph_backfill_max_decisions_per_cycle: int = 30
graph_backfill_max_episodes_per_cycle: int = 30
graph_backfill_max_procedures_per_cycle: int = 20

# Phase 3: Per-relation thresholds
graph_threshold_fact_fact: float = 0.82
graph_threshold_fact_decision: float = 0.72
graph_threshold_fact_episode: float = 0.70
graph_threshold_decision_decision: float = 0.78
graph_threshold_episode_episode: float = 0.75
graph_threshold_procedure_any: float = 0.70

# Phase 4: Health monitoring
graph_health_orphan_warn_threshold: float = 0.40
graph_health_check_enabled: bool = True
```

---

## Risks & Mitigations

### Risk 1: Embedding API Cost During Backfill

**Problem:** 1,262 orphans x cross-type re-embedding = potentially thousands of API calls.

**Mitigation:**
- Same-type linking uses stored embeddings (zero cost)
- Cross-type uses batch embedding (fewer API calls)
- Cap at 130 nodes per sleep cycle — spreads cost over ~10 cycles
- Same-type linking alone will connect many orphans (fact→fact is the densest link type)

### Risk 2: Low-Quality Edges at Reduced Thresholds

**Problem:** Lowering thresholds from 0.90→0.82 might create noisy edges.

**Mitigation:**
- Composite confidence scoring (Phase 3) filters by multiple signals, not just cosine
- Edge weight stores the actual similarity — low-weight edges naturally get less traversal priority
- Backfill can be re-run with higher thresholds if noise is detected
- Dashboard (Phase 4) tracks edge quality distribution

### Risk 3: Sleep Cycle Duration Increase

**Problem:** Adding a graph densification phase to sleep could make cycles too long.

**Mitigation:**
- Interruptible — stops between entity type batches
- Per-cycle caps prevent runaway processing
- Same-type linking (the bulk of work) is pure SQL, no LLM calls
- Measured: 50 vector similarity queries take ~2-3 seconds in pgvector

### Risk 4: Backfill Creates Redundant Edges to Already-Dense Nodes

**Problem:** Popular hub nodes might accumulate too many edges during backfill.

**Mitigation:**
- Max edges per node cap (10 per backfill run)
- Skip nodes that already have degree > 10
- Prefer orphan→orphan connections over orphan→hub connections

---

## Non-Goals

- **Graph schema migration** — No new tables, columns, or relation types beyond what F022 already created.
- **Graph database migration** — Postgres + pgvector remains sufficient at current scale (<3K nodes).
- **Real-time linking latency** — Backfill is async/sleep-time. Real-time creation linking (Phase 2) runs in background handlers, not in the request path.
- **Cross-agent graph federation** — Single-agent only. Multi-agent knowledge sharing is out of scope.
- **Edge deletion/pruning** — This spec only creates edges. Edge lifecycle management (pruning low-weight edges, aging out stale connections) is a separate concern.
- **Manual/user-directed linking** — All linking is automatic. A future feature could let the user say "link X to Y" explicitly.

---

## Open Questions

1. **Should backfill prioritize recent orphans or old ones?** Currently ordered by `created_at DESC` (newest first) — these are most likely to have relevant peers. But old orphans are the most disconnected.

2. **Embedding freshness for old nodes.** Facts created 2+ months ago have embeddings from an older model version. Should we re-embed during backfill to ensure consistent similarity scores?

3. **Edge direction conventions.** Currently inconsistent: `evidence_for` goes fact→decision, but `discussed_in` goes both episode→decision and fact→episode. Should we standardize? (e.g., always source = the "smaller" entity type)

4. **Graph density threshold for spreading activation.** `should_use_spreading_activation()` checks density. What density target should trigger the switch? Currently the threshold is configurable but untested. After Phase 1, with avg degree ~4.0, should spreading activation auto-enable?

5. **Procedure embedding quality.** Procedures have triggers + body text. What gets embedded — just triggers? Just body? Both concatenated? This affects cross-type similarity accuracy.

---

## Appendix: Current Linking Pipeline

### What Creates Edges Today

| Trigger | Handler | Edge Types Created | Limitation |
|---------|---------|-------------------|------------|
| `learn_fact()` | `FactGraphLinker` | fact→decision (`evidence_for`), fact→fact (`related_to`) | Only checks decisions from last 30 days. Threshold too high. |
| `Brain.record()` | `Brain.auto_link()` | decision→decision (`related_to`) | Threshold 0.85, max 3 links. Very conservative. |
| `session_ended` | `EpisodeSummarizer` | episode→decision (`discussed_in`), fact→episode (`extracted_from`) | Deterministic only — no semantic episode↔episode linking. |
| Sleep reflection | `SleepHandler._phase_reflect()` | None | Creates facts but doesn't link them (FactGraphLinker handles linking on creation). |
| Sleep consolidation | `SleepHandler._phase_cluster_consolidation()` | Implicitly via supersedes | Merges clusters but doesn't create cross-type edges for the merged fact. |
| Never | — | procedure→anything | No procedure linking exists. |

### What F040 Adds

| Trigger | Handler | Edge Types Created | Improvement |
|---------|---------|-------------------|-------------|
| Sleep cycle | `_phase_graph_densification` | All types → all types | Processes 130 orphans/cycle, backfills entire history |
| `decision_recorded` | `DecisionGraphLinker` | fact→decision, episode→decision | Reverse linking — catches facts that predate the decision |
| `session_ended` | Extended `EpisodeSummarizer` | episode→episode (`related_to`) | Semantic episode similarity, not just deterministic |
| `procedure_learned` | `ProcedureGraphLinker` | procedure→fact, procedure→decision | First-ever procedure graph connectivity |
