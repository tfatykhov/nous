# F022 Graph-Augmented Recall — Design

> **Date:** 2026-03-08
> **Status:** Approved
> **Feature:** F022 — Graph-Augmented Recall
> **Decision ID:** 0a24c6b2

## Context

F022 spec proposes wiring the existing but unused `brain.graph_edges` table into `recall_deep`. A thorough spec-vs-code analysis identified 7 issues (2 P1, 4 P2, 1 P3) that must be resolved before implementation. This design addresses all of them.

### Issues Found

| # | Severity | Finding |
|---|----------|---------|
| 1 | **P1** | Phase 2 FK drop loses cascade deletes + needs ORM changes |
| 2 | **P1** | Phase 3 duplicates existing `facts.contradict()` system |
| 3 | **P2** | Phase 4 CTE incompatible with Phase 2's polymorphic edges |
| 4 | **P2** | Cross-type embedding comparability unvalidated |
| 5 | **P2** | `graph_edges` has no `agent_id` column |
| 6 | **P2** | Phase 2 LOC estimate ~200 is optimistic |
| 7 | **P3** | Embedding comparability flagged but no solution proposed |

## Design Decisions

### 1. Polymorphic Edges (resolves P1 #1)

**Choice:** Drop FK constraints from `graph_edges`, add `source_type`/`target_type` columns.

**Rationale:** The graph's value is unified traversal. Splitting edges across multiple tables means every graph operation (`neighbors()`, spreading activation, path queries) becomes a multi-table UNION. At Nous's scale (hundreds of nodes), the FK loss is manageable:
- Nous uses soft deletes (`active = false`), not hard `DELETE`. Cascades rarely fire.
- Orphaned edges are harmless noise — a periodic cleanup query handles them.
- One query pattern everywhere: `neighbors(node_id, node_type)` regardless of type.

**Alternatives rejected:**
- Separate link tables (preserves FKs but fragments the graph API)
- Hybrid approach (two tables, complex traversal logic)

### 2. Contradiction Bridge (resolves P1 #2)

**Choice:** When `facts.contradict()` fires, it also creates a `contradicts` graph edge. Same for `facts.supersede()` → `supersedes` edge.

**Rationale:** The fact-level FKs (`contradiction_of`, `superseded_by`) are fast lookups. The graph edges enable traversal. Both systems stay in sync — the existing methods become the write path that always creates graph edges as a side effect.

Phase 3 LLM-based detection on new facts calls the existing `facts.contradict()`/`facts.supersede()` methods, which now also create graph edges. Single write path, no duplication.

### 3. Common-Template Re-Embedding (resolves P2 #4, P3 #7)

**Choice:** Before cross-type similarity comparison, re-embed both items using a common template:

```
"{type}: {core_content}"
```

Where `core_content` is:
- Decision: `description`
- Fact: `content`
- Episode: `summary`
- Procedure: `description`

**Rationale:** Facts embed `content + subject`, decisions embed `description + context + pattern`, episodes embed `title + summary`. These are different semantic spaces — cosine similarity between them is not directly comparable. Re-embedding with a common template eliminates the comparability problem at the source.

**Cost:** One extra `text-embedding-3-small` call per candidate item during auto-linking. At ~5 items/day, cost is effectively zero (~$0.0001/day).

### 4. Agent ID on Graph Edges (resolves P2 #5)

**Choice:** Add `agent_id VARCHAR(100) NOT NULL` to `graph_edges`.

**Rationale:** Every other table in the schema has `agent_id`. Without it, graph queries can't be self-contained — they'd need joins through node tables for agent scoping. One column makes every query clean and future-proofs for multi-agent.

### 5. Density-Gated Spreading Activation (resolves P2 #3)

**Choice:** Type-agnostic CTE that traverses by ID only, resolves node objects after. Auto-enables via density gate.

**Density gate:** `avg_edges_per_node` calculated once per session start from `graph_edges` scoped by `agent_id`. When >= 3.0, spreading activation replaces 1-hop expansion.

**Kill switch:** `NOUS_SPREADING_ACTIVATION_ENABLED`:
- `auto` (default) — density gate controls
- `true` — force on regardless of density
- `false` — force off, always use 1-hop

**Fallback:** If density drops below threshold, reverts to Phase 1 simple 1-hop expansion. No behavioral cliff.

---

## Schema Changes

### Migration: `graph_edges` table

```sql
-- 1. Drop FK constraints
ALTER TABLE brain.graph_edges
    DROP CONSTRAINT graph_edges_source_id_fkey,
    DROP CONSTRAINT graph_edges_target_id_fkey;

-- 2. Add new columns
ALTER TABLE brain.graph_edges
    ADD COLUMN source_type VARCHAR(20) NOT NULL DEFAULT 'decision',
    ADD COLUMN target_type VARCHAR(20) NOT NULL DEFAULT 'decision',
    ADD COLUMN agent_id VARCHAR(100);

-- 3. Backfill agent_id from source decisions
UPDATE brain.graph_edges e
SET agent_id = d.agent_id
FROM brain.decisions d
WHERE e.source_id = d.id;

-- 4. Make agent_id NOT NULL after backfill
ALTER TABLE brain.graph_edges
    ALTER COLUMN agent_id SET NOT NULL;

-- 5. Add type check constraints
ALTER TABLE brain.graph_edges
    ADD CONSTRAINT ck_edges_source_type CHECK (
        source_type IN ('decision', 'fact', 'episode', 'procedure')
    ),
    ADD CONSTRAINT ck_edges_target_type CHECK (
        target_type IN ('decision', 'fact', 'episode', 'procedure')
    );

-- 6. Extend relation check constraint
ALTER TABLE brain.graph_edges
    DROP CONSTRAINT ck_edges_relation,
    ADD CONSTRAINT ck_edges_relation CHECK (
        relation IN (
            'supports', 'contradicts', 'supersedes', 'related_to', 'caused_by',
            'informed_by', 'evidence_for', 'discussed_in', 'extracted_from'
        )
    );

-- 7. New indexes for cross-type traversal
CREATE INDEX idx_graph_edges_source_type ON brain.graph_edges(source_id, source_type);
CREATE INDEX idx_graph_edges_target_type ON brain.graph_edges(target_id, target_type);
CREATE INDEX idx_graph_edges_agent ON brain.graph_edges(agent_id);
```

### ORM Model Update (`models.py`)

Remove `ForeignKey` declarations on `source_id`/`target_id`. Add `source_type`, `target_type`, `agent_id` mapped columns. Update check constraint to include new relation types.

### init.sql Update

Update the `brain.graph_edges` CREATE TABLE to reflect the new schema (for fresh installs).

---

## Phase 1: Wire Graph into Recall

**Goal:** When `recall_deep` returns results, expand them by 1 hop through existing graph edges.

### Changes

**`brain/schemas.py`** — New schema:
```python
class NeighborResult(BaseModel):
    decision: DecisionSummary
    edge_relation: str
    edge_weight: float
    source_type: str
    target_type: str
```

**`brain/brain.py`** — Update `neighbors()`:
- Add `node_type: str = "decision"` parameter
- Query filters by `source_type`/`target_type` alongside ID matching
- Return `list[NeighborResult]` instead of `list[DecisionSummary]`

**`api/tools.py`** — Update `recall_deep`:
1. After `brain.query()` returns decisions, take top 5 by score
2. Call `brain.neighbors()` for each (1-hop, max 3 neighbors per seed)
3. Deduplicate against already-returned results
4. Decay scores: `neighbor_score = edge_weight × 0.7`
5. Tag graph results with `[via graph: {relation}]`
6. Merge, re-sort, trim to limit

### Config

```
NOUS_GRAPH_RECALL_ENABLED=true
NOUS_GRAPH_RECALL_HOPS=1
NOUS_GRAPH_RECALL_MAX_EXPAND=5
NOUS_GRAPH_RECALL_DECAY=0.7
NOUS_GRAPH_RECALL_MAX_NEIGHBORS=3
```

### Estimated LOC: ~100

---

## Phase 2: Cross-Type Edges & Auto-Linking

**Goal:** Allow edges between any memory types with common-template embedding comparison.

### Common Embedding Template

Before cross-type similarity comparison, re-embed using:
```
"{type}: {core_content}"
```

Cached in memory during auto-link batch. No DB storage of normalized embeddings.

### Auto-Linking Extensions

**On new fact creation (`facts.learn()`):**
1. Re-embed fact with common template
2. Compare against recent decisions (last 30 days) using common-template embeddings
3. If similarity > 0.80, create `evidence_for` edge (fact → decision)
4. Compare against other facts — if similarity > 0.90, create `related_to` edge

**On episode summary creation (event bus handler):**
1. Deterministic links (no embedding needed):
   - Episode → decisions made during it: `discussed_in` edges (from `episode_decisions` table)
   - Episode → facts extracted from it: `extracted_from` edges (from `source_episode_id` FK on facts)

### Recall Expansion

After Heart search returns facts/episodes, check for graph edges to decisions:
```python
for fact in fact_results[:3]:
    edges = await brain.neighbors(fact.id, node_type="fact", limit=2)
    # Add connected decisions to results if not already present
```

### Config

```
NOUS_CROSS_TYPE_LINKING_ENABLED=true
NOUS_CROSS_TYPE_THRESHOLD=0.80
NOUS_CROSS_TYPE_SAME_THRESHOLD=0.90
```

### Estimated LOC: ~350

---

## Phase 3: Contradiction & Supersession Bridge

**Goal:** Unify fact-level contradiction/supersession with graph edges.

### Bridge Implementation

When `facts.contradict()` fires:
1. Creates new fact with `contradiction_of` FK (existing behavior)
2. Also creates `contradicts` graph edge (new_fact → old_fact, source_type=fact, target_type=fact)

When `facts.supersede()` fires:
1. Creates new fact with `superseded_by` FK (existing behavior)
2. Also creates `supersedes` graph edge (new_fact → old_fact)

### LLM-Based Detection on New Facts

When `learn_fact()` stores a new fact and finds high-similarity matches (> 0.85):
1. LLM classifies: SAME, SUPERSEDES, CONTRADICTS, RELATED, UNRELATED
2. For CONTRADICTS → calls `facts.contradict()` (which now also creates graph edge)
3. For SUPERSEDES → calls `facts.supersede()` (which now also creates graph edge)
4. For RELATED → creates `related_to` graph edge directly

### Contradiction Surfacing

When `recall_deep` results contain nodes connected by `contradicts` edges:
```
⚠️ Contradiction: "Tim prefers Celsius" ↔ "Tim uses Fahrenheit" (auto-detected)
```

### Config

```
NOUS_CONTRADICTION_DETECTION=true
NOUS_CONTRADICTION_SIMILARITY_THRESHOLD=0.85
NOUS_CONTRADICTION_MODEL=claude-haiku-4-5-20241022
```

### Estimated LOC: ~180

---

## Phase 4: Spreading Activation with Density Gate

**Goal:** Multi-hop retrieval via spreading activation, auto-enabled when graph is dense enough.

### Density Gate

- Metric: `avg_edges_per_node` from `graph_edges` scoped by `agent_id`
- Calculated once per session start, cached in cognitive layer state
- Threshold: >= 3.0 enables spreading activation
- Kill switch: `NOUS_SPREADING_ACTIVATION_ENABLED` (`auto`/`true`/`false`)

### Algorithm

1. **Seed:** Vector search results = seed nodes, activation = search score
2. **Spread:** Type-agnostic CTE traverses `graph_edges` by ID, accumulates `activation × weight × decay` per hop
3. **Inhibition:** Nodes sharing `contradicts` edges — suppress the lower-activation one
4. **Resolve:** CTE returns (id, node_type, total_activation). Fetch actual objects by type in batch.
5. **Rank:** `α × vector_score + β × graph_activation + γ × recency` (default 0.5/0.3/0.2)
6. **Return:** Top-N by final rank

### Type-Agnostic CTE

```sql
WITH RECURSIVE activation AS (
    SELECT id, node_type, score AS activation, 0 AS depth
    FROM (VALUES (:seeds)) AS seeds(id, node_type, score)

    UNION ALL

    SELECT
        CASE WHEN e.source_id = a.id THEN e.target_id ELSE e.source_id END,
        CASE WHEN e.source_id = a.id THEN e.target_type ELSE e.source_type END,
        a.activation * e.weight * :decay,
        a.depth + 1
    FROM activation a
    JOIN brain.graph_edges e
        ON (e.source_id = a.id OR e.target_id = a.id)
    WHERE a.depth < :max_depth
        AND e.relation != 'contradicts'
        AND e.agent_id = :agent_id
)
SELECT id, node_type, SUM(activation) AS total_activation
FROM activation
GROUP BY id, node_type
ORDER BY total_activation DESC
LIMIT :limit;
```

### Config

```
NOUS_SPREADING_ACTIVATION_ENABLED=auto
NOUS_SPREADING_ACTIVATION_DENSITY_THRESHOLD=3.0
NOUS_SPREADING_ACTIVATION_DECAY=0.5
NOUS_SPREADING_ACTIVATION_MAX_DEPTH=2
NOUS_SPREADING_ACTIVATION_ALPHA=0.5
NOUS_SPREADING_ACTIVATION_BETA=0.3
NOUS_SPREADING_ACTIVATION_GAMMA=0.2
```

### Estimated LOC: ~150

---

## Full Config Summary

| Variable | Default | Phase | Description |
|----------|---------|-------|-------------|
| `NOUS_GRAPH_RECALL_ENABLED` | `true` | 1 | Feature flag for graph expansion in recall |
| `NOUS_GRAPH_RECALL_HOPS` | `1` | 1 | Max traversal depth for simple expansion |
| `NOUS_GRAPH_RECALL_MAX_EXPAND` | `5` | 1 | Max seed results to expand |
| `NOUS_GRAPH_RECALL_DECAY` | `0.7` | 1 | Score decay per hop |
| `NOUS_GRAPH_RECALL_MAX_NEIGHBORS` | `3` | 1 | Max neighbors per seed |
| `NOUS_CROSS_TYPE_LINKING_ENABLED` | `true` | 2 | Enable cross-type auto-linking |
| `NOUS_CROSS_TYPE_THRESHOLD` | `0.80` | 2 | Similarity threshold for cross-type edges |
| `NOUS_CROSS_TYPE_SAME_THRESHOLD` | `0.90` | 2 | Similarity threshold for same-type edges |
| `NOUS_CONTRADICTION_DETECTION` | `true` | 3 | Enable LLM-based contradiction detection |
| `NOUS_CONTRADICTION_SIMILARITY_THRESHOLD` | `0.85` | 3 | Similarity threshold for contradiction candidates |
| `NOUS_CONTRADICTION_MODEL` | `claude-haiku-4-5-20241022` | 3 | Model for contradiction classification |
| `NOUS_SPREADING_ACTIVATION_ENABLED` | `auto` | 4 | Kill switch (auto/true/false) |
| `NOUS_SPREADING_ACTIVATION_DENSITY_THRESHOLD` | `3.0` | 4 | Edges/node threshold for auto-enable |
| `NOUS_SPREADING_ACTIVATION_DECAY` | `0.5` | 4 | Activation decay per hop |
| `NOUS_SPREADING_ACTIVATION_MAX_DEPTH` | `2` | 4 | Max CTE recursion depth |
| `NOUS_SPREADING_ACTIVATION_ALPHA` | `0.5` | 4 | Weight for vector score |
| `NOUS_SPREADING_ACTIVATION_BETA` | `0.3` | 4 | Weight for graph activation |
| `NOUS_SPREADING_ACTIVATION_GAMMA` | `0.2` | 4 | Weight for recency |

## Phasing

| Phase | Component | LOC Est. | Depends On |
|-------|-----------|----------|------------|
| 0 | Schema migration + ORM update | ~60 | Nothing |
| 1 | Wire graph into recall_deep | ~100 | Phase 0 |
| 2 | Cross-type edges + common-template auto-linking | ~350 | Phase 0 |
| 3 | Contradiction bridge + LLM detection | ~180 | Phase 2 |
| 4 | Spreading activation + density gate | ~150 | Phase 2 |

Each phase is independently shippable and behind its own feature flag.
Total estimated LOC: ~840
