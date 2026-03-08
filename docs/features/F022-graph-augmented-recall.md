# F022 — Graph-Augmented Recall

> **Status:** Planned
> **Priority:** P1
> **Depends on:** F001 (Brain Module — `graph_edges`, `auto_link_decisions`, `neighbors`), F002 (Heart Module)
> **Research:** 016 — Agent Memory Synthesis, Gap G5 (Graph Overlay for Relational Retrieval)
> **Papers:** A-MEM (Zettelkasten linking), SYNAPSE (spreading activation), Mem0 (graph variant)

---

## Problem Statement

Nous has a graph infrastructure that nobody uses.

**What exists today:**
- `brain.graph_edges` table — `supports`, `contradicts`, `supersedes`, `related_to`, `caused_by`
- `auto_link_decisions()` — creates `related_to` edges between similar decisions (cosine > 0.85, max 3)
- `neighbors()` — traverses edges bidirectionally, returns connected decisions

**What doesn't work:**
- `recall_deep` does pure vector + keyword hybrid search. It never checks graph edges.
- `neighbors()` is never called from any recall path.
- Edges connect only decision→decision. Facts, episodes, and procedures are graph-isolated.
- `contradicts` and `supersedes` edge types exist in the schema but nothing writes them.
- No cross-type linking: a fact that informed a decision has no structural connection to it.

**Result:** Multi-hop queries fail. "What decisions did I make based on security research?" requires traversing fact→decision edges that don't exist. Vector similarity might catch some by keyword overlap, but it's probabilistic where the answer should be deterministic.

---

## Research Backing

Three papers from our 016 synthesis directly support this:

**A-MEM (Zettelkasten):** When a new memory is encoded, the agent generates keywords and tags, then actively searches historical memories for semantic connections. Linked memories are updated bidirectionally — a new fact triggers re-evaluation of older ones.

**SYNAPSE (Spreading Activation):** Memory items are graph nodes; semantic, temporal, and causal relationships are edges. Retrieval activates a seed node and propagates activation through the graph. Lateral inhibition suppresses competing activations. Outperforms SOTA on temporal coherence and multi-hop reasoning.

**Key insight from 016:** "Relevance is not a static property of a memory item — it is a relational property that depends on context, recency, and causal structure. Vector similarity alone cannot model this."

---

## Design Principles

1. **Use what exists** — the table, the auto-linker, and `neighbors()` are already built. Wire them in, don't rebuild.
2. **Additive to vector search** — graph traversal expands results after vector search, never replaces it.
3. **Cross-type edges** — facts, episodes, decisions, and procedures should all be linkable.
4. **Lightweight** — no graph database (Neo4j, Apache AGE). Postgres + the existing `graph_edges` table is sufficient at Nous's scale.
5. **Phased** — each phase is independently valuable and shippable.

---

## Phase 1: Wire Graph into Recall

**Goal:** When `recall_deep` returns results, expand them by 1 hop through existing graph edges.

### Changes to `recall_deep` (in `nous/api/tools.py`)

After the current vector+keyword search returns top-N results:

1. Collect decision IDs from Brain results
2. Call `brain.neighbors()` for each, 1-hop only
3. Deduplicate against already-returned results
4. Append neighbor decisions with a `[graph]` tag and a decayed score:
   - `neighbor_score = original_edge_weight × 0.7` (graph results rank below direct hits)
5. Re-sort merged results by score
6. Trim to requested `limit`

```python
# Pseudocode — recall_deep after Brain search
if decision_results:
    decision_ids = {d.id for d in decision_results}
    graph_expanded = []
    for dec in decision_results[:5]:  # Only expand top 5 to limit fan-out
        neighbors = await brain.neighbors(dec.id, limit=3)
        for neighbor in neighbors:
            if neighbor.id not in decision_ids:
                neighbor.score = neighbor.edge_weight * 0.7
                neighbor.source = "graph"
                graph_expanded.append(neighbor)
                decision_ids.add(neighbor.id)
    decision_results.extend(graph_expanded)
    decision_results.sort(key=lambda d: d.score or 0, reverse=True)
    decision_results = decision_results[:limit]
```

### Changes to `neighbors()` (in `nous/brain/brain.py`)

Currently returns `DecisionSummary` objects. Add `edge_weight` and `edge_relation` to the return so the caller can use them for scoring.

```python
class NeighborResult(BaseModel):
    decision: DecisionSummary
    edge_relation: str
    edge_weight: float
```

### Display Format

Graph-expanded results are annotated in the output:

```
=== Brain Decisions ===
1. Use pgvector for embedding storage | architecture | medium | confidence: 0.85 (score: 0.782)
2. [via graph: related_to] Choose Postgres over MongoDB | architecture | high | confidence: 0.90 (score: 0.547)
```

### Configuration

```env
NOUS_GRAPH_RECALL_ENABLED=true          # Feature flag
NOUS_GRAPH_RECALL_HOPS=1                # Max traversal depth (1 or 2)
NOUS_GRAPH_RECALL_MAX_EXPAND=5          # Max seed results to expand
NOUS_GRAPH_RECALL_DECAY=0.7             # Score decay per hop
NOUS_GRAPH_RECALL_MAX_NEIGHBORS=3       # Max neighbors per seed
```

### Estimated LOC: ~80

---

## Phase 2: Cross-Type Edges

**Goal:** Allow edges between any memory types, not just decision→decision.

### Schema Change

The current `graph_edges` table has foreign keys to `brain.decisions` only:

```sql
source_id UUID REFERENCES brain.decisions(id)
target_id UUID REFERENCES brain.decisions(id)
```

**New approach — polymorphic edges:**

```sql
ALTER TABLE brain.graph_edges
    DROP CONSTRAINT graph_edges_source_id_fkey,
    DROP CONSTRAINT graph_edges_target_id_fkey,
    ADD COLUMN source_type VARCHAR(20) NOT NULL DEFAULT 'decision',
    ADD COLUMN target_type VARCHAR(20) NOT NULL DEFAULT 'decision';

-- source_type / target_type: 'decision', 'fact', 'episode', 'procedure'
-- No FK constraints — validated at application level
-- Index for cross-type traversal
CREATE INDEX idx_graph_edges_source ON brain.graph_edges(source_id, source_type);
CREATE INDEX idx_graph_edges_target ON brain.graph_edges(target_id, target_type);
```

**Valid type values:** `decision`, `fact`, `episode`, `procedure`

### New Relation Types

Extend the check constraint to add cross-type relations:

```sql
ALTER TABLE brain.graph_edges
    DROP CONSTRAINT ck_edges_relation,
    ADD CONSTRAINT ck_edges_relation CHECK (
        relation IN (
            -- Existing
            'supports', 'contradicts', 'supersedes', 'related_to', 'caused_by',
            -- New cross-type
            'informed_by',   -- decision was informed by this fact
            'evidence_for',  -- fact provides evidence for this decision
            'discussed_in',  -- fact/decision was discussed in this episode
            'extracted_from' -- fact was extracted from this episode
        )
    );
```

### Auto-Linking: Cross-Type

Extend `auto_link_decisions()` → `auto_link()`:

**On new fact creation:**
1. Embed the fact content
2. Compare against recent decisions (last 30 days) by cosine similarity
3. If similarity > 0.80, create `evidence_for` edge (fact → decision)
4. Compare against other facts — if similarity > 0.90, create `related_to` edge

**On episode summary creation:**
1. Link episode to all decisions made during it: `discussed_in` edges
2. Link episode to all facts extracted from it: `extracted_from` edges
3. These are deterministic (from existing `episode_decisions` and fact extraction), not similarity-based

### Changes to `neighbors()`

Accept a `source_type` parameter to traverse from any node type:

```python
async def neighbors(
    self,
    node_id: UUID,
    node_type: str = "decision",  # NEW
    relation: str | None = None,
    limit: int = 10,
) -> list[NeighborResult]:
```

### Changes to `recall_deep`

After Heart results are returned, check if any facts/episodes have graph edges to decisions (or vice versa). This surfaces structurally connected results that vector search missed.

```python
# After Heart search returns facts
for fact in fact_results[:3]:
    # Check if this fact has edges to decisions
    edges = await brain.neighbors(fact.id, node_type="fact", limit=2)
    # Add connected decisions to brain results (if not already there)
```

### Estimated LOC: ~200 (schema migration + auto_link extension + neighbors update)

---

## Phase 3: Contradiction & Supersession Detection

**Goal:** Automatically detect when new facts contradict or supersede existing ones.

### On Fact Creation

When `learn_fact()` stores a new fact:

1. Search existing facts by embedding similarity (top 5, threshold > 0.85)
2. For highly similar facts (> 0.85), use an LLM call to classify the relationship:

```
Given these two facts about "{subject}":
OLD: "{old_fact.content}"
NEW: "{new_fact.content}"

Classify their relationship:
- SAME: identical information, no action needed
- SUPERSEDES: new fact updates/replaces the old one
- CONTRADICTS: new fact conflicts with old one
- RELATED: same topic but different information
- UNRELATED: similarity is coincidental
```

3. Create appropriate edges:
   - `SUPERSEDES` → create `supersedes` edge, optionally deactivate old fact
   - `CONTRADICTS` → create `contradicts` edge, flag for user review
   - `RELATED` → create `related_to` edge
   - `SAME` → skip (duplicate detection)

### Contradiction Surfacing

When a `contradicts` edge exists and both facts are recalled in the same query, add a warning:

```
⚠️ Contradiction detected:
  Fact A: "Tim prefers Celsius" (confidence: 1.0, 2025-12-01)
  Fact B: "Tim uses Fahrenheit" (confidence: 0.8, 2026-01-15)
  Edge: contradicts (auto-detected)
```

### Configuration

```env
NOUS_CONTRADICTION_DETECTION=true
NOUS_CONTRADICTION_SIMILARITY_THRESHOLD=0.85
NOUS_CONTRADICTION_MODEL=claude-sonnet     # Cheaper model for classification
NOUS_SUPERSEDE_AUTO_DEACTIVATE=false       # Require user confirmation
```

### Estimated LOC: ~150 (detection logic + LLM call + edge creation + surfacing)

---

## Phase 4: Spreading Activation (Stretch Goal)

**Goal:** Implement SYNAPSE-inspired spreading activation for multi-hop retrieval.

### Algorithm

1. **Seed:** Vector search returns top-K results → these are seed nodes with activation = their search score
2. **Spread:** For each seed node, propagate activation to neighbors:
   - `neighbor_activation = seed_activation × edge_weight × decay_factor`
   - Decay factor per hop: 0.5 (configurable)
3. **Inhibition:** If two neighbors have a `contradicts` edge, the lower-activation one is suppressed
4. **Collect:** All activated nodes above a minimum threshold are collected
5. **Rank:** Final ranking = `α × vector_score + β × graph_activation + γ × recency`
   - Default weights: α=0.5, β=0.3, γ=0.2
6. **Return:** Top-N by final rank

### Differences from Full SYNAPSE

- No pre-computed graph embeddings (use existing pgvector embeddings)
- No iterative convergence (1-2 rounds of spreading, not until convergence)
- No dedicated graph DB (Postgres CTEs for recursive edge traversal)

### Postgres CTE for Spreading

```sql
WITH RECURSIVE activation AS (
    -- Seeds from vector search
    SELECT id, score AS activation, 0 AS depth
    FROM brain.decisions
    WHERE id IN (:seed_ids)

    UNION ALL

    -- Spread through edges
    SELECT
        CASE WHEN e.source_id = a.id THEN e.target_id ELSE e.source_id END AS id,
        a.activation * e.weight * :decay AS activation,
        a.depth + 1 AS depth
    FROM activation a
    JOIN brain.graph_edges e
        ON (e.source_id = a.id OR e.target_id = a.id)
    WHERE a.depth < :max_depth
        AND e.relation != 'contradicts'  -- Don't spread through contradictions
)
SELECT id, SUM(activation) AS total_activation
FROM activation
GROUP BY id
ORDER BY total_activation DESC
LIMIT :limit;
```

### Estimated LOC: ~120

---

## Implementation Priority

| Phase | Component | LOC Est. | Value | Depends On |
|-------|-----------|----------|-------|------------|
| 1 | Wire `neighbors()` into `recall_deep` | ~80 | High — immediate recall improvement | Nothing (all infra exists) |
| 2 | Cross-type edges + auto-linking | ~200 | High — connects the full knowledge graph | Phase 1 |
| 3 | Contradiction/supersession detection | ~150 | Medium — data integrity, prevents stale facts | Phase 2 |
| 4 | Spreading activation | ~120 | Medium — diminishing returns unless graph is dense | Phase 2 |

**Minimum viable:** Phase 1 alone (~80 LOC). Uses existing infrastructure with zero schema changes.

**Recommended MVP:** Phases 1 + 2 (~280 LOC). This is the "finish the graph you already started" scope.

---

## What This Changes

### Before F022 (current)
```
recall_deep("security decisions")
→ vector search: top 5 decisions by embedding similarity
→ keyword search: top 5 by BM25
→ merge, rank, return
→ MISSES: related decisions connected by graph edges but with different wording
```

### After F022 Phase 1
```
recall_deep("security decisions")
→ vector + keyword search: top 5 decisions
→ expand top 5 by 1 hop through graph_edges
→ find 3 related decisions (e.g., "choose JWT over sessions" linked to "enforce HTTPS")
→ merge, decay graph scores, rank, return
→ CATCHES: structurally related decisions regardless of wording
```

### After F022 Phase 2
```
recall_deep("what informed the JWT decision?")
→ vector search returns the JWT decision
→ cross-type traversal: JWT decision ← evidence_for ← fact("PyJWT CVE discovered")
→ cross-type traversal: JWT decision ← discussed_in ← episode("Security review session")
→ returns decision + the fact that informed it + the episode where it was discussed
```

---

## Metrics

**How to measure success:**

1. **Multi-hop recall rate** — manually test queries that require traversing relationships:
   - "What facts informed decision X?" (requires fact→decision edges)
   - "What did we discuss in the session where we decided Y?" (requires episode→decision edges)
   - Target: >80% of structurally connected items surfaced

2. **Graph density** — edges per node:
   - Phase 1 baseline: only decision→decision `related_to` edges (currently ~2.5 per decision)
   - Phase 2 target: >5 edges per node including cross-type

3. **Contradiction detection rate** — % of conflicting facts that get flagged:
   - No baseline (currently 0%)
   - Phase 3 target: >90% of high-similarity contradictions detected

4. **Recall precision impact** — does graph expansion introduce noise?
   - Track: of graph-expanded results, how many does the model actually reference? (F017 usage tracking)
   - Target: graph results referenced at ≥50% the rate of direct vector results

---

## Non-Goals

- **Full graph database** — No Neo4j, no Apache AGE. Postgres + `graph_edges` table is sufficient at current scale (hundreds of nodes, not millions).
- **Graph-only retrieval** — Graph traversal supplements vector search, never replaces it. Vector search remains the primary retrieval mechanism.
- **Real-time graph visualization** — defer to F021 (Dashboard). The graph can be queried via REST endpoints but won't have a visual UI in this feature.
- **Cross-agent graph sharing** — single-agent graph only. Multi-agent knowledge sharing is a separate concern.
- **Automatic memory consolidation** — merging/generalizing facts based on graph clusters. This belongs in F008 (Memory Lifecycle).

---

## Open Questions

1. **Edge weight decay over time?** Should old edges lose weight, or is creation-time decay sufficient? SYNAPSE uses temporal decay on edges; A-MEM does not.

2. **Fan-out control in dense graphs.** If a central decision has 50 edges, expanding it floods results. Cap at `max_neighbors=3` per seed? Or use edge weight as a natural filter?

3. **Cross-type embedding comparison.** Facts and decisions use different embedding content (fact text vs. decision description). Are cosine similarities between them meaningful, or do we need to embed them in a common format?

4. **LLM cost for contradiction detection.** Phase 3 makes an LLM call on every new fact that has high-similarity matches. At current fact creation rate (~2/day), cost is negligible. But if fact extraction scales up (F016 pre-prune extraction), this could become expensive. Batch processing? Local model?

5. **Phase 4 feasibility in Postgres.** The recursive CTE for spreading activation works but may be slow for graphs with >1000 nodes. Benchmark needed before committing to this approach vs. application-level BFS.
