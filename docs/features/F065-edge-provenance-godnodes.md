# F065 — Edge Provenance & God-Node Surfacing

> **Status:** Draft
> **Priority:** P2
> **Depends on:** F022 (Graph-Augmented Recall — shipped)
> **Related:** F040 (Graph Densification)
> **Author:** Nous
> **Created:** 2026-05-21

---

## Problem

`brain.graph_edges` has a `weight FLOAT` column but no column that records *how* an edge was created. Every edge looks the same at query time regardless of whether it was extracted deterministically from a known relationship or auto-linked by a heuristic cosine threshold.

**Specific gaps:**

1. **No provenance label.** There is no way to distinguish a `relation='extracted_from'` edge (deterministic — a fact was literally pulled from a specific episode) from a `relation='related_to'` edge created by `Brain.auto_link()` (heuristic — cosine > 0.85, could be spurious). Both arrive in `recall_deep` with equal footing. The only proxy is `auto_linked BOOLEAN`, which is set to `FALSE` for manually-created edges and `TRUE` for machine-created ones, but it doesn't capture the *quality* of the inference.

2. **F027 (Supersession Detection) contradiction edges lack a trust tier.** When the contradiction detector fires, it writes a `relation='contradicts'` edge based on LLM reasoning over two facts that merely have high vector similarity. This is a model-inferred relationship — more uncertain than either deterministic extraction or cosine-threshold heuristics — yet `recall_deep` treats it identically.

3. **No API surfaces hub or high-centrality nodes.** The graph is traversed hop-by-hop during retrieval (F022) and during spreading activation, but there is no function that asks: *which nodes are the most-connected concepts in the graph?* God nodes — the handful of nodes that sit at the intersection of many sub-graphs — are never surfaced proactively. If a core concept becomes a hub, the agent only discovers it by accident.

---

## Inspiration: Graphify

[**Graphify**](https://github.com/safishamsi/graphify) is an open-source knowledge-graph tool for codebases (NetworkX + Leiden + tree-sitter + vis.js). Three ideas from its design directly inform this feature.

### 1. Honest provenance on every edge

Graphify tags each edge with one of three confidence labels: `EXTRACTED` (explicitly stated in source), `INFERRED` (model-reasoned, e.g. call-graph edges via tree-sitter second pass), or `AMBIGUOUS` (low-confidence). From the README:

> *"Every edge is tagged `EXTRACTED`, `INFERRED`, or `AMBIGUOUS` - you always know what was found vs guessed."*

Nous adapts this with three analogous tiers mapped to its existing relation taxonomy: `deterministic`, `heuristic`, and `inferred`.

### 2. God-node / hub analysis via degree centrality

From Graphify's `analyze.py`:

```python
def god_nodes(G: nx.Graph, top_n: int = 10) -> list[dict]:
    sorted_nodes = sorted(degree.items(), key=lambda x: x[1], reverse=True)
    ...
    result.append({"id": node_id, "label": ..., "edges": deg})
```

Graphify surfaces the top-N highest-degree nodes as "god nodes — the most-connected concepts in your project. Everything flows through these." Nous will implement the equivalent as `brain.graph.top_hubs()` using Postgres degree aggregation rather than NetworkX, keeping the zero-dependency posture established by F022.

### 3. Surprising connections (deferred — see Out of Scope)

Graphify also computes `surprising_connections()` using a composite score that weights cross-community, cross-file-type, and peripheral-to-hub edges. Nous records this idea but defers it to F066, where Leiden community detection can be evaluated as an optional dependency.

---

## Design

### A. Edge Provenance

#### Schema addition (migration 047)

Add one column to `brain.graph_edges`:

```sql
ALTER TABLE brain.graph_edges
    ADD COLUMN extraction_method VARCHAR(20)
        CHECK (extraction_method IN ('deterministic', 'heuristic', 'inferred'));
```

The column is **nullable** at addition time to permit a safe migration without locking the table. The backfill (below) populates all existing rows; application code treats a `NULL` value as `'heuristic'` (the conservative default) until backfill completes.

#### Backfill rules

Existing edges are categorised by their `relation` value and `auto_linked` flag:

| Condition | `extraction_method` assigned | Reasoning |
|---|---|---|
| `relation IN ('extracted_from', 'discussed_in', 'supersedes')` | `'deterministic'` | These relation types require an explicit, structural match — a fact lifted from a specific episode, or one decision known to supersede another. No model inference involved. |
| `auto_linked = TRUE AND relation = 'related_to'` | `'heuristic'` | Created by `Brain.auto_link()` (cosine > 0.85). Threshold is meaningful but not infallible. |
| `relation = 'related_to' AND auto_linked = FALSE` | `'heuristic'` | Manually created related_to edges; trust slightly above auto-linked but still non-deterministic. |
| `relation = 'contradicts'` | `'inferred'` | Written by F027 (Supersession Detection) contradiction path via LLM reasoning over high-similarity pairs. Model inference, not structural extraction. |
| All remaining (`supports`, `caused_by`, `informed_by`, `evidence_for`) | `'heuristic'` | Default safe tier; re-classify when provenance can be verified. |

SQL backfill (included in migration 047):

```sql
-- deterministic
UPDATE brain.graph_edges
SET extraction_method = 'deterministic'
WHERE relation IN ('extracted_from', 'discussed_in', 'supersedes');

-- inferred (F027 contradiction detector)
UPDATE brain.graph_edges
SET extraction_method = 'inferred'
WHERE relation = 'contradicts';

-- heuristic catch-all
UPDATE brain.graph_edges
SET extraction_method = 'heuristic'
WHERE extraction_method IS NULL;
```

After backfill, add a `NOT NULL DEFAULT 'heuristic'` constraint:

```sql
ALTER TABLE brain.graph_edges
    ALTER COLUMN extraction_method SET NOT NULL,
    ALTER COLUMN extraction_method SET DEFAULT 'heuristic';
```

#### recall_deep down-weighting

`recall_deep` already applies a decay factor (`graph_recall_decay = 0.7`) when scoring 1-hop graph-expanded results (F022). F065 adds a second, independent multiplier applied to edges whose `extraction_method = 'inferred'`:

```
final_score = base_score × graph_recall_decay × inferred_penalty
```

where `inferred_penalty` defaults to `0.7` for `inferred` edges and `1.0` for `deterministic` and `heuristic`.

New config knob:

```python
# nous/config.py
graph_inferred_edge_penalty: float = 0.7   # F065: down-weight 'inferred' edges in recall_deep
```

Exposed as env var `NOUS_GRAPH_INFERRED_EDGE_PENALTY`. Setting to `1.0` disables the penalty.

The penalty is applied inside `retrieval_pipeline.py` at the same stage as F022's existing decay (Stage 4 — graph-expanded decisions), keeping the scoring logic in one place.

---

### B. God-Node Surfacing

#### New function: `brain.graph.top_hubs()`

Add `top_hubs()` to `nous/brain/brain.py` alongside the existing `neighbors()` and `_neighbors()` functions:

```python
async def top_hubs(
    self,
    limit: int = 10,
    node_type: str | None = None,   # 'decision' | 'fact' | 'episode' | 'procedure' | None
) -> list[dict]:
    """
    Return the highest-degree nodes in brain.graph_edges for this agent.
    Uses undirected degree (source + target appearances combined).
    Inspired by Graphify's god_nodes() — degree centrality, top-N approach.
    """
```

The underlying SQL aggregates both edge directions:

```sql
SELECT node_id, node_type, COUNT(*) AS degree
FROM (
    SELECT source_id AS node_id, source_type AS node_type
    FROM brain.graph_edges WHERE agent_id = :agent_id
    UNION ALL
    SELECT target_id AS node_id, target_type AS node_type
    FROM brain.graph_edges WHERE agent_id = :agent_id
) combined
WHERE (:node_type IS NULL OR node_type = :node_type)
GROUP BY node_id, node_type
ORDER BY degree DESC
LIMIT :limit;
```

Each row is resolved to its human label (decision description, fact subject, episode summary) via a follow-up lookup in the respective Heart/Brain table. Return shape:

```python
[
    {
        "node_id": "uuid",
        "node_type": "decision",   # | fact | episode | procedure
        "label": "Use pgvector for embedding storage",
        "degree": 31,
        "extraction_method_breakdown": {   # F065 addition
            "deterministic": 12,
            "heuristic": 16,
            "inferred": 3,
        },
    },
    ...
]
```

#### New tool: `recall_hubs`

Expose `top_hubs()` as a tool callable by the agent:

```python
# nous/api/tools.py
{
    "name": "recall_hubs",
    "description": (
        "Return the most-connected (highest-degree) nodes in Nous's knowledge graph. "
        "Use to discover which concepts, decisions, facts, or episodes act as hubs "
        "that many other memories reference. Optionally filter by node_type."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "limit":     {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
            "node_type": {"type": "string",  "enum": ["decision", "fact", "episode", "procedure"]},
        },
    },
}
```

#### Auto-surfacing at session start

During session-start working-memory construction, check whether any hub's degree has shifted by more than 20% since the degree was last recorded in `heart.facts` (subject `"graph_hub_degree:<node_id>"`). If any hub crosses this threshold, prepend a one-line notice to the system prompt context block:

```
[graph] Hub shift detected: "Use pgvector for embedding storage" now degree 38 (was 31, +22.6%).
```

Degree snapshots are written back to `heart.facts` after each session-start check so the baseline tracks naturally. The 20% threshold is configurable via `NOUS_GRAPH_HUB_SHIFT_THRESHOLD` (default `0.20`).

---

## Migration

**File:** `sql/migrations/047_f065_edge_provenance.sql`

```sql
-- Migration 043: F065 — Edge Provenance & God-Node Surfacing
-- Adds extraction_method to brain.graph_edges.
-- Run: psql $DATABASE_URL < sql/migrations/047_f065_edge_provenance.sql

BEGIN;

-- Step 1: Add nullable column (no table lock on large tables)
ALTER TABLE brain.graph_edges
    ADD COLUMN IF NOT EXISTS extraction_method VARCHAR(20)
        CHECK (extraction_method IN ('deterministic', 'heuristic', 'inferred'));

-- Step 2: Backfill — deterministic relations
UPDATE brain.graph_edges
SET extraction_method = 'deterministic'
WHERE relation IN ('extracted_from', 'discussed_in', 'supersedes')
  AND extraction_method IS NULL;

-- Step 3: Backfill — LLM-inferred contradictions (F027 Supersession Detection contradiction path)
UPDATE brain.graph_edges
SET extraction_method = 'inferred'
WHERE relation = 'contradicts'
  AND extraction_method IS NULL;

-- Step 4: Backfill — everything else is heuristic
UPDATE brain.graph_edges
SET extraction_method = 'heuristic'
WHERE extraction_method IS NULL;

-- Step 5: Tighten to NOT NULL with default
ALTER TABLE brain.graph_edges
    ALTER COLUMN extraction_method SET NOT NULL,
    ALTER COLUMN extraction_method SET DEFAULT 'heuristic';

-- Step 6: Index for filtered recall queries
CREATE INDEX IF NOT EXISTS idx_graph_edges_extraction_method
    ON brain.graph_edges(agent_id, extraction_method);

COMMIT;
```

---

## API Changes

| Component | Change | Type |
|---|---|---|
| `brain.graph_edges` (SQL) | Add `extraction_method VARCHAR(20) NOT NULL DEFAULT 'heuristic'` | Schema — migration 047 |
| `nous/brain/brain.py` | Add `top_hubs(limit, node_type)` public method | New function |
| `nous/brain/brain.py` | Add `_top_hubs_query()` private helper (SQL aggregation) | New function |
| `nous/api/tools.py` | Register `recall_hubs` tool | New tool |
| `nous/api/retrieval_pipeline.py` | Apply `graph_inferred_edge_penalty` multiplier at Stage 4 | Modified |
| `nous/config.py` | Add `graph_inferred_edge_penalty: float = 0.7` | New config field |
| `nous/config.py` | Add `graph_hub_shift_threshold: float = 0.20` | New config field |
| `nous/api/session.py` (or equivalent session-start hook) | Hub-shift check + working-memory injection | Modified |
| `GraphEdge` SQLAlchemy model | Add `extraction_method` column mapping | Modified |

---

## Test Plan

1. **Backfill correctness.** After running migration 047 on a staging DB, assert: all `relation='extracted_from'` rows have `extraction_method='deterministic'`; all `relation='contradicts'` rows have `extraction_method='inferred'`; zero rows have `extraction_method IS NULL`.

2. **Default on new edges.** Insert a new edge via `Brain.auto_link()` without specifying `extraction_method`. Assert the row has `extraction_method='heuristic'` (DEFAULT).

3. **recall_deep penalty applied.** Seed two edges from node A: one `extraction_method='deterministic'`, one `extraction_method='inferred'`, both `weight=1.0`. Call `recall_deep` on a query that activates node A. Assert the inferred neighbor's final score is ≤ `deterministic_score × 0.7 × 1.01` (within floating-point tolerance).

4. **Penalty disabled at 1.0.** Set `NOUS_GRAPH_INFERRED_EDGE_PENALTY=1.0`. Repeat case 3. Assert both neighbors score identically (no penalty).

5. **`recall_hubs` tool returns correct top-N.** Insert 20 nodes with known degree distributions. Call `recall_hubs(limit=5)`. Assert the five returned nodes are exactly the five highest-degree nodes, sorted descending. Assert `node_type` filter works: `recall_hubs(limit=5, node_type='fact')` returns only fact nodes.

6. **Hub-shift auto-surface.** Record a baseline degree of 25 for a hub node in `heart.facts`. Increase the node's degree to 31 (24% gain — above 20% threshold). Simulate a session-start. Assert the working-memory context block contains the hub-shift notice line. Then set degree to 26 (4% gain — below threshold) and assert no notice is injected.

---

## Out of Scope

- **Surprising connections** — Graphify's `surprising_connections()` function (composite score using cross-community, cross-file-type, and peripheral-to-hub bonuses) requires Leiden community detection. This is deferred to **F066**, which will evaluate Leiden as an optional dependency and decide whether to implement a Postgres-native approximation or accept the `graspologic` / Python < 3.13 constraint.

- **Leiden community detection** — Not included. F065 does not change the clustering strategy. Spreading activation (`spreading_activation.py`) and 1-hop expansion (F022) remain the only graph traversal modes.

- **NetworkX dependency** — F022 established the principle: Postgres + `brain.graph_edges` is sufficient at Nous's scale. F065 maintains this. `top_hubs()` uses a SQL aggregation query, not `nx.degree()`. Adding NetworkX (and its ~15 MB transitive closure) is explicitly deferred pending a stronger use case.

- **Visualization** — Graphify exports `graph.html` (vis.js) and Obsidian vaults. Nous has no equivalent export today; this remains out of scope for F065.

---

## Open Questions

1. **`extraction_method` on newly-written edges — who sets it?** The backfill is straightforward, but callers that create new edges (e.g. `graph_densifier.py`, `FactGraphLinker`, `DecisionGraphLinker` from F040) currently don't pass an `extraction_method`. Do we (a) infer it at write time from the `relation` type using the same backfill rules, or (b) require callers to pass it explicitly and fail loudly if omitted? Option (a) is less error-prone but hides the intent; option (b) is more disciplined but needs all call sites updated simultaneously.

2. **Inferred-edge penalty interaction with spreading activation.** The `0.7` penalty is defined for 1-hop `recall_deep` expansion (Stage 4 in `retrieval_pipeline.py`). Spreading activation in `spreading_activation.py` uses `weight` directly and is unaware of `extraction_method`. Should spreading activation also apply the penalty, and if so, where — inside `spreading_activation.py` or by pre-filtering edges before the activation loop?

3. **Hub degree baseline storage.** The auto-surface feature proposes storing degree snapshots in `heart.facts` (subject `"graph_hub_degree:<node_id>"`). This creates O(hub count) facts per agent — currently ~10, manageable. But if hub count grows (e.g. after F040 orphan backfill densifies the graph), this could generate noise in `recall_deep` results. Is `heart.facts` the right store, or should hub baselines live in a dedicated lightweight table or in `nous/config.py` as a runtime-only cache?

4. **20% hub-shift threshold — is it the right signal?** The threshold is borrowed from the god-node intuition (hub = high-degree node that "everything flows through"), but degree growth can be mechanical — a single densification sweep from F040 could add 50 edges to every fact node uniformly. Would a relative *rank* shift (e.g. a node entering or leaving the top-10 list) be a more meaningful signal than a raw degree percentage?

---

## Fact-check log

> Verified 2026-05-21 by automated spec review. Source files: `nous/brain/brain.py`, `nous/brain/graph_linker.py`, `nous/storage/models.py`, `sql/init.sql`, `sql/migrations/`, `docs/features/F022*`, `docs/features/F027*`, `docs/features/F040*`. External sources: `https://github.com/safishamsi/graphify` (README + `analyze.py`).

### Verified — no correction needed

| Claim | Verdict |
|---|---|
| Graphify repo `github.com/safishamsi/graphify` exists | ✅ Confirmed — active repo, 50.6 k stars |
| Graphify uses NetworkX + Leiden + tree-sitter + vis.js | ✅ Confirmed — all four named in README |
| `analyze.py` contains `god_nodes(G, top_n=10)` with degree-centrality sort | ✅ Confirmed — function at line 88; `degree = dict(G.degree())`, then `sorted(degree.items(), key=lambda x: x[1], reverse=True)` |
| `brain.graph_edges` has `weight FLOAT` column | ✅ Confirmed — `sql/init.sql` line 202; `models.py` line 263 |
| `brain.graph_edges` has `auto_linked BOOLEAN` column | ✅ Confirmed — `sql/init.sql` line 203; `models.py` line 264 |
| F022 (Graph-Augmented Recall) is shipped | ✅ Confirmed — `docs/features/F022-graph-augmented-recall.md` exists; INDEX marks it shipped |
| F040 (Graph Densification) is shipped | ✅ Confirmed — `docs/features/F040-graph-densification.md` exists; INDEX marks it shipped |
| F027 covers `contradicts` edge creation | ✅ Confirmed — `docs/features/F027-supersession-detection.md` documents `contradicts` edge type and creation path |

### Corrections applied

**1. Graphify README quote was not verbatim.**
Original spec quoted: *"Confidence tags — every inferred relationship is marked EXTRACTED, INFERRED, or AMBIGUOUS. You always know what was found vs guessed."*
Actual README text: *"Every edge is tagged `EXTRACTED`, `INFERRED`, or `AMBIGUOUS` - you always know what was found vs guessed."*
Fix: replaced the blockquote with the verbatim README text.

**2. `auto_link_decisions()` function does not exist.**
The spec referred to `auto_link_decisions()` in three places (Problem section, backfill table, test plan). The actual function is `Brain.auto_link()` (public) / `Brain._auto_link()` (private) in `nous/brain/brain.py:1208–1238`. Edge creation for cross-type links is done via `GraphLinker` (`nous/brain/graph_linker.py`). All three occurrences corrected to `Brain.auto_link()`.

**3. Migration number 043 is already taken.**
`sql/migrations/043_dag_node_columns.sql` exists. The next available slot is `047` (last used: `046_work_queue_items.sql`). All references to "migration 043" and `043_f065_edge_provenance.sql` updated to `047`.

**4. F027 label "contradiction detector" was inaccurate.**
F027's file is `F027-supersession-detection.md` (full title: "Supersession Detection & Principled Forgetting"). While F027 does include the `contradicts` edge creation path, calling it the "contradiction detector" alone was misleading. Labels updated to "F027 (Supersession Detection) contradiction path" throughout.
