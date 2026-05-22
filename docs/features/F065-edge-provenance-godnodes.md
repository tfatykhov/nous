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

Existing edges are categorised by their `relation` value alone. **Critical correction (review 2026-05-23):** the earlier `auto_linked`-gated rule was empirically unsound — every production writer hard-codes `auto_linked=True` (verified: `Brain.link()` at `nous/brain/brain.py:1045` has zero callers; `_link` at line 1063 is reached only with `auto_linked=True`; `heart/facts.py:166`, `brain/graph_linker.py:99/188/266/304/326`, `brain/brain.py:1280` all set `auto_linked=True` directly). Gating `'deterministic'` on `auto_linked=FALSE` would have classified **zero** rows. We discriminate on relation semantics instead.

| Condition | `extraction_method` assigned | Reasoning |
|---|---|---|
| `relation = 'supersedes'` | `'deterministic'` | Supersession is a structural claim: one decision/fact replaces another. The supersession path always carries a concrete `superseded_by` link (not a cosine-similarity match). |
| `relation = 'contradicts'` | `'inferred'` | Written by F027 (Supersession Detection) contradiction path via LLM reasoning over high-similarity pairs. Model inference, not structural extraction. |
| All remaining relations (`extracted_from`, `discussed_in`, `related_to`, `supports`, `caused_by`, `informed_by`, `evidence_for`) | `'heuristic'` | Cosine-threshold matching via `graph_linker` / `graph_densifier`. Threshold is meaningful but not infallible. |

**Why three tiers and not two?** The `'deterministic'` tier is small but non-empty today (only `supersedes`) and provides a natural extension point for future writers that DO carry structural provenance (e.g. a future "fact extracted from this specific episode token range" relation). Keeping the tier in the enum costs nothing and lets downstream code treat it as the highest-trust signal.

**Write-time rule for new edges** (resolves Open Question 1): the new-edge classifier applies the same relation-based mapping as the backfill. The mapping lives in a new helper `nous/brain/edge_provenance.py::classify(relation: str) -> str` (single source of truth — no duplication between backfill and write-time). Each `GraphEdge(...)` construction site sets `extraction_method=classify(relation)` BEFORE handing the row to `session.add` / `pg_insert`. There are eight construction sites total (audited 2026-05-23):

| # | File:line | Writer context |
|---|---|---|
| 1 | `nous/brain/brain.py:1074` | `Brain._link` (used by `Brain.link()` and `_auto_link`) |
| 2 | `nous/brain/brain.py:1280` | `Brain._auto_link` direct construction |
| 3 | `nous/heart/facts.py:166-186` | Fact↔fact edges (incl. supersession). NOTE: existing `try/except Exception: logger.debug(...)` wrapper swallows DB errors — pre-existing, not introduced by F065. |
| 4 | `nous/brain/graph_linker.py:99` | `GraphLinker.create_edge` (used by densifier) |
| 5 | `nous/brain/graph_linker.py:188` | `link_episode_deterministic` direct `pg_insert` |
| 6 | `nous/brain/graph_linker.py:266` | `link_fact_to_decisions` direct `pg_insert` |
| 7 | `nous/brain/graph_linker.py:304` | `link_fact_to_facts` direct `pg_insert` |
| 8 | `nous/brain/graph_linker.py:326` | `link_fact_to_facts` second `pg_insert` branch |

A CI grep-guard test (`tests/test_f065_writer_coverage.py`) counts these sites and asserts the actual count matches a registered constant. Adding a 9th site without registering it fails CI.

SQL backfill (included in migration 047):

```sql
-- Deterministic: supersession relations only (structural by definition).
UPDATE brain.graph_edges
SET extraction_method = 'deterministic'
WHERE relation = 'supersedes'
  AND extraction_method IS NULL;

-- Inferred: F027 contradiction detector via LLM reasoning.
UPDATE brain.graph_edges
SET extraction_method = 'inferred'
WHERE relation = 'contradicts'
  AND extraction_method IS NULL;

-- Heuristic catch-all (covers all cosine-derived relations).
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

where `inferred_penalty` defaults to `1.0` (penalty inactive) on initial rollout, and is tuned downward (e.g. to `0.7`) after the F051 harness quantifies the MRR impact.

New config knob:

```python
# nous/config.py
graph_inferred_edge_penalty: float = 1.0   # F065: dark-launch default; flip to 0.7 after harness gate
```

Exposed as env var `NOUS_GRAPH_INFERRED_EDGE_PENALTY`. Setting to `1.0` (default) disables the penalty.

The penalty must be applied at **two** sites that both implement the F022 graph-expansion scoring:

1. `nous/api/retrieval_pipeline.py::_graph_expanded_to_pipeline` (Brain-side 1-hop expansion).
2. `nous/api/retrieval_pipeline.py::_heart_graph_to_pipeline` (Heart→decision expansion path). Reviewer P1 (2026-05-22) flagged this site as missed in the original spec — the same `edge_weight × decay` formula applies and the same penalty must layer on top.

Both sites consume `NeighborResult` from `Brain._neighbors`, which must be extended to carry `extraction_method` (see "Integration touchpoints" below).

**NULL handling (resolves silent-failure audit P0-3):** when `NeighborResult.extraction_method is None` (theoretically impossible post-migration but defensible against future bugs / SA-synthesized rows), the penalty multiplier treats it as `'heuristic'` — fail-open by design, with a single WARN-level log per occurrence (rate-limited to avoid log spam). The `_neighbors` SELECT and `NeighborResult` field are typed as `str` (non-Optional) once the migration is in; the field-default at construction is `'heuristic'`.

**Spreading activation (`nous/brain/spreading_activation.py:103`)** already hard-filters `relation != 'contradicts'`, which is the only `inferred`-tier relation in the backfill. The F065 penalty is therefore a no-op on the SA path today — resolving Open Question 2. **Defense in depth:** the penalty multiplier is wrapped in an explicit `source != 'spreading_activation'` check at `_graph_expanded_to_pipeline`, so a future inferred-tier relation that bypasses the SA pre-filter cannot accidentally double-penalize SA results.

#### Integration touchpoints (full list)

Reviewer P1 (2026-05-22): the API Changes table understates the breadth of the integration. Implementation must touch all five sites:

1. `nous/storage/models.py::GraphEdge` — add `extraction_method: Mapped[str | None] = mapped_column(String(20))`.
2. `nous/brain/schemas.py::NeighborResult` — add `extraction_method: str | None` field.
3. `nous/brain/brain.py::_neighbors` — select `extraction_method` in `source_q` / `target_q` and populate `NeighborResult`.
4. `nous/api/retrieval_pipeline.py::_graph_expanded_to_pipeline` — apply the penalty multiplier.
5. `nous/api/retrieval_pipeline.py::_heart_graph_to_pipeline` — apply the same penalty multiplier.

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

The hook lives inside `nous/cognitive/layer.py::pre_turn` (the canonical session-start path — there is no `nous/api/session.py`, an error in the original spec).

**Shift signal (resolves Open Question 4, reviewer P1-3):** the original spec used "raw degree % shift > 20%". This is brittle — F040's nightly densification sweep can uniformly add edges to every hub and trigger false positives on dozens of nodes per session. F065 instead uses **rank-based shift**: a notice fires when a node enters or leaves the top-10 hub list since the previous snapshot. Rank is meaningful (the agent learns which concept just became dominant); raw degree growth on already-dominant hubs is not.

```
[graph] Hub shift detected: "Use pgvector for embedding storage" entered the top-10 (rank #4, degree 38).
[graph] Hub shift detected: "Old assumption about gateways" left the top-10 (was rank #7, now #14).
```

**New-hub emergence (resolves silent-failure audit P1-2):** on first sight (no prior snapshot row for the node), the pre_turn hook silently inserts a baseline row and skips the notice. The hub-shift signal is about *change*; there's no change history for a node we've never seen before. The next session will compare against this baseline.

**Insert failure handling (resolves silent-failure audit P1-4):** the snapshot insert and rank computation run fire-and-forget via `asyncio.create_task` so pre_turn never blocks on hub-shift detection. Mirrors the F026 / F059 persistence pattern. Any DB error is logged at WARN level inside the task and dropped — hub-shift is a diagnostic, not a correctness requirement.

**Storage (resolves Open Question 3, reviewer P1):** baseline snapshots do NOT live in `heart.facts`. Rows there enter `recall_deep` candidacy, fact-extractor dedup, and the F051 eval candidate set — polluting the corpus with thousands of operator-metadata facts. Instead, F065 adds a new lightweight table:

```sql
CREATE TABLE IF NOT EXISTS brain.graph_hub_snapshots (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id     TEXT NOT NULL,
    node_id      UUID NOT NULL,
    node_type    VARCHAR(20) NOT NULL,
    degree       INTEGER NOT NULL,
    captured_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_graph_hub_snapshots_agent_node
    ON brain.graph_hub_snapshots (agent_id, node_id, captured_at DESC);
```

The pre_turn hook reads the most-recent snapshot per `(agent_id, node_id)` using the canonical Postgres idiom:

```sql
SELECT DISTINCT ON (node_id) node_id, degree
FROM brain.graph_hub_snapshots
WHERE agent_id = :agent_id AND node_id = ANY(:hub_ids)
ORDER BY node_id, captured_at DESC
```

compares against the live `top_hubs()` result by **rank**, and inserts a new snapshot row only when the rank crosses the top-10 boundary. The table is read-only to `recall_deep` (no FTS/vector index by design).

Retention: the sleep handler prunes rows older than `NOUS_GRAPH_HUB_SNAPSHOT_RETENTION_DAYS` (default 90) via a single DELETE on `(agent_id, captured_at)` — added as an index on the snapshot table to support this query efficiently.

---

## Migration

**File:** `sql/migrations/047_f065_edge_provenance.sql`

```sql
-- Migration 047: F065 — Edge Provenance & God-Node Surfacing
-- Adds extraction_method to brain.graph_edges.
-- Run: psql $DATABASE_URL < sql/migrations/047_f065_edge_provenance.sql

BEGIN;

-- Step 1: Add nullable column (no table lock on large tables)
ALTER TABLE brain.graph_edges
    ADD COLUMN IF NOT EXISTS extraction_method VARCHAR(20)
        CHECK (extraction_method IN ('deterministic', 'heuristic', 'inferred'));

-- Step 2: Backfill — deterministic = explicit (not auto_linked) structural relations.
-- The auto_linked = FALSE clause is load-bearing: graph_densifier and
-- FactGraphLinker / DecisionGraphLinker also write extracted_from /
-- discussed_in edges via cosine-threshold matching with auto_linked=TRUE.
-- Without this gate, the deterministic tier is silently polluted by
-- thousands of heuristic edges (review 2026-05-22 P0).
UPDATE brain.graph_edges
SET extraction_method = 'deterministic'
WHERE relation IN ('extracted_from', 'discussed_in', 'supersedes')
  AND auto_linked = FALSE
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

-- Step 7: Hub-snapshot table for session-start shift detection.
-- Lives in brain schema so it's NOT indexed by FTS / vector embedding
-- pipelines that operate on heart.facts; baseline rows must NEVER
-- appear as candidates in recall_deep (review 2026-05-22 P1).
CREATE TABLE IF NOT EXISTS brain.graph_hub_snapshots (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id     TEXT NOT NULL,
    node_id      UUID NOT NULL,
    node_type    VARCHAR(20) NOT NULL,
    degree       INTEGER NOT NULL,
    rank         INTEGER,  -- F065 (review 2026-05-23): rank in top-N at snapshot time; NULL for nodes outside the top-N at write time
    captured_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Most-recent-per-node lookup (used by pre_turn hub-shift hook).
CREATE INDEX IF NOT EXISTS idx_graph_hub_snapshots_agent_node
    ON brain.graph_hub_snapshots (agent_id, node_id, captured_at DESC);
-- Retention prune query (sleep handler).
CREATE INDEX IF NOT EXISTS idx_graph_hub_snapshots_agent_captured
    ON brain.graph_hub_snapshots (agent_id, captured_at);

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
| `nous/cognitive/layer.py::pre_turn` | Hub-shift check + working-memory injection | Modified |
| `GraphEdge` SQLAlchemy model | Add `extraction_method` column mapping | Modified |

---

## Test Plan

1. **Backfill correctness.** After running migration 047 on a staging DB, assert: all `relation='extracted_from'` rows have `extraction_method='deterministic'`; all `relation='contradicts'` rows have `extraction_method='inferred'`; zero rows have `extraction_method IS NULL`.

2. **Default on new edges.** Insert a new edge via `Brain.auto_link()` without specifying `extraction_method`. Assert the row has `extraction_method='heuristic'` (DEFAULT).

3. **recall_deep penalty applied with active setting.** Override `NOUS_GRAPH_INFERRED_EDGE_PENALTY=0.7`. Seed two edges from node A: one `extraction_method='deterministic'`, one `extraction_method='inferred'`, both `weight=1.0`. Call `recall_deep` on a query that activates node A. Assert the inferred neighbor's final score is `deterministic_score × 0.7` (within floating-point tolerance).

4. **Penalty default (1.0) is byte-identical to F022 baseline.** Default config (`NOUS_GRAPH_INFERRED_EDGE_PENALTY=1.0`). Repeat case 3. Assert both neighbors score identically — F065 with the dark-launch default does not alter retrieval.

4b. **NULL `extraction_method` is treated as `heuristic`.** Force a `NeighborResult` with `extraction_method=None` (simulating a SA-synthesized row or a pre-migration row that slipped through). Override penalty to `0.7`. Assert score is computed as if `extraction_method='heuristic'` (no penalty applied) AND a WARN log was emitted exactly once for the agent_id over the test window.

5. **`recall_hubs` tool returns correct top-N.** Insert 20 nodes with known degree distributions. Call `recall_hubs(limit=5)`. Assert the five returned nodes are exactly the five highest-degree nodes, sorted descending. Assert `node_type` filter works: `recall_hubs(limit=5, node_type='fact')` returns only fact nodes.

6. **Hub-shift auto-surface (rank-based).** Seed `brain.graph_hub_snapshots` with a node at rank #4. Simulate a session where the live `top_hubs(limit=10)` now places it at rank #11 (left the top-10). Trigger `pre_turn`. Assert a hub-shift notice was injected and a new snapshot row was written. Then simulate another session where the node moves from rank #5 to rank #6 (still in top-10, no boundary crossing) and assert no notice and no new snapshot row.

7. **New-hub emergence.** Insert a node that does NOT appear in `brain.graph_hub_snapshots` at all. Run `pre_turn`. Assert a baseline snapshot row was inserted AND no hub-shift notice was emitted (silent first-sight).

8. **pre_turn snapshot insert failure is non-blocking.** Monkey-patch the snapshot manager to raise. Run `pre_turn`. Assert the working-memory context block is constructed successfully (no exception escapes) and a WARN-level log entry was emitted.

9. **Writer-coverage CI guard.** `tests/test_f065_writer_coverage.py` greps for `GraphEdge(` constructions and `pg_insert(GraphEdge)` calls in `nous/brain/` and `nous/heart/`. Asserts the count equals `EXPECTED_WRITER_COUNT = 8`. Adding a 9th writer site (e.g. in a new feature) without updating this constant fails CI loudly. Forces every writer to be classified.

---

## Out of Scope

- **Surprising connections** — Graphify's `surprising_connections()` function (composite score using cross-community, cross-file-type, and peripheral-to-hub bonuses) requires Leiden community detection. This is deferred to **F066**, which will evaluate Leiden as an optional dependency and decide whether to implement a Postgres-native approximation or accept the `graspologic` / Python < 3.13 constraint.

- **Leiden community detection** — Not included. F065 does not change the clustering strategy. Spreading activation (`spreading_activation.py`) and 1-hop expansion (F022) remain the only graph traversal modes.

- **NetworkX dependency** — F022 established the principle: Postgres + `brain.graph_edges` is sufficient at Nous's scale. F065 maintains this. `top_hubs()` uses a SQL aggregation query, not `nx.degree()`. Adding NetworkX (and its ~15 MB transitive closure) is explicitly deferred pending a stronger use case.

- **Visualization** — Graphify exports `graph.html` (vis.js) and Obsidian vaults. Nous has no equivalent export today; this remains out of scope for F065.

---

## Open Questions

1. ~~**`extraction_method` on newly-written edges — who sets it?**~~ **Resolved** (2026-05-23 review): write-time classification via `nous/brain/edge_provenance.py::classify(relation)` (single source of truth). Wired into all eight GraphEdge construction sites enumerated above, with a CI grep-guard test that fails if a new site is added without registration. The original "`Brain._create_edge` (and the four call sites that bypass it)" was wrong on two counts: there is no `_create_edge` (the function is `_link`), and the actual count is eight, not four.

2. ~~**Inferred-edge penalty interaction with spreading activation.**~~ **Resolved** (2026-05-22 review): currently moot — `spreading_activation.py:103` already hard-filters `relation != 'contradicts'`, which is the only `inferred`-tier relation in the backfill. If a future inferred-tier relation is added (e.g. an LLM-inferred semantic link), revisit this question then.

3. ~~**Hub degree baseline storage.**~~ **Resolved** (2026-05-22 review): baselines live in a new `brain.graph_hub_snapshots` table (see God-Node Surfacing → Auto-surfacing at session start). `heart.facts` would pollute the retrieval corpus and the F051 eval candidate set.

---

## Rollout & Success Criteria

**LOE estimate:** ~6h focused work — migration + ORM (1h), backfill correctness tests (1h), `top_hubs` + `recall_hubs` tool (1.5h), pipeline penalty wiring at both sites (1.5h), pre_turn hub-shift hook + snapshot table tests (1h).

**Phases:**

1. **Schema + dormant penalty.** Land migration 047 + ORM + `NeighborResult.extraction_method` + the penalty multiplier code path, with `NOUS_GRAPH_INFERRED_EDGE_PENALTY=1.0` (no behavioral change). Verify backfill correctness on a snapshot of prod `brain.graph_edges`.

2. **Surface hubs.** Land `top_hubs()` + `recall_hubs` tool + the `graph_hub_snapshots` table + `pre_turn` hook. Tool is opt-in only — agent has to call it explicitly. No autosurface activation yet.

3. **Activate autosurface.** Flip `NOUS_GRAPH_HUB_AUTOSURFACE_ENABLED=true` after one release of bake time on the hub-snapshot table. Monitor F051 MRR delta — autosurface adds context tokens and could regress retrieval if hub-shift signal is noisy.

4. **Tune penalty.** After F051 harness measures the MRR impact of `inferred` down-weighting, choose the operating value (likely `0.7` if MRR improves, `1.0` if neutral or negative).

**Success criteria:**

- F051 harness MRR does not regress in Phase 1 (penalty inactive — should be byte-equivalent retrieval).
- Backfill correctness audit: spot-check 100 random `brain.graph_edges` rows post-migration and verify the `extraction_method` value matches the table's stated rule.
- `recall_hubs` tool returns sane results (highest-degree nodes are recognizable concepts, not metadata rows or noise).
- Phase 3 autosurface fires < 2× per session on average (avoid notification fatigue).
- Phase 4 inferred-penalty (`0.7`) shows non-negative MRR delta vs `1.0` baseline.

**Rollback:** Phases 1-3 are forward-only on schema but behaviorally gated. Setting `NOUS_GRAPH_INFERRED_EDGE_PENALTY=1.0` and `NOUS_GRAPH_HUB_AUTOSURFACE_ENABLED=false` returns to F022 baseline behavior. The two new tables can be dropped in a follow-up if F065 is rolled back entirely.

~~**4. 20% hub-shift threshold — is it the right signal?**~~ **Resolved** (2026-05-23 review): replaced with rank-based shift (notice fires when a node enters/leaves the top-10 list). The 20% raw-degree heuristic was vulnerable to false-positive storms after F040 densification sweeps — every hub would cross the threshold simultaneously. Rank changes are semantically meaningful and naturally rate-limited (only 10 nodes can be in the top-10 at any time).

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
