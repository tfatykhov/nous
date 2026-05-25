# F070 — Chunk-aware sleep consolidation

**Status:** 📝 Draft (2026-05-25)
**Proposed by:** Tim
**Depends on:** F040 (Graph Densification — ✅ shipped), F067 (Episode Chunks — ✅ shipped)
**Related:** F022 (Graph-Augmented Recall), F034.6 (Hub-Shift Autosurface), proposed adjacency-boost work (built 2026-05-25, currently inert)

---

## Problem

F067 (episode chunks + parent-episode recall) shipped ~2 weeks ago and is now the dominant retrieval path (default-on in prod, ~10 chunks per `recall_deep` call). **But the sleep cycle and graph densification infrastructure were never updated to handle chunks.**

Concrete evidence from 2026-05-25 audit of the LME eval corpus:

```
corpus: facts=7898  chunks=35804  episodes=1751
graph_edges: 1775
  fact <-> fact:       1676
  procedure <-> procedure:  99
  chunk-*:                  0   ← zero
  episode-*:                0   ← zero
```

35,804 chunks. Zero edges involving any chunk. The graph densifier walks facts/decisions/episodes/procedures but ignores `heart.episode_chunks` entirely.

### Consequences observed today

1. **Adjacency boost (built 2026-05-25) is a structural no-op.** It boosts candidates connected to other candidates via graph edges. With chunks having degree 0 and being the bulk of top-K results, almost no boost fires. Validated: BGE-only retrieval = 0.950 hit@5, BGE+adjacency boost = 0.950 hit@5 (exact tie).

2. **F022 spreading activation can't reach chunks.** It traverses edges, chunks have none.

3. **F034.6 hub-shift autosurface ignores chunks** — promotes by graph degree, chunks have degree 0.

4. **No chunk dedup.** Repeated boilerplate (e.g., signature lines, common preambles) creates many near-duplicate chunks across sessions, all retrieved independently.

5. **No chunk pruning.** Stale or archived-session chunks accumulate indefinitely. Storage grows linearly.

### What this is not

- **Not** re-chunking — chunk content stays as-is.
- **Not** changing the chunk ingest path (that's F067).
- **Not** adding new chunk fields (no schema migration in v1 — see scope below).
- **Not** building cross-corpus chunk linking (single-agent only).

### Scope split: v1 vs v2

`heart.episode_chunks` today has NO `superseded_by` or `active` columns (unlike `heart.facts` which has both). Adding them is a real schema migration.

To keep v1 shippable in one PR with no migration, **v1 only builds graph edges**. The two operations that would need new columns are deferred:

- **Cross-episode dedup-marking** (would need `superseded_by` on chunks) → **deferred to F070.1**
- **Stale-chunk soft-delete pruning** (would need `active` on chunks) → **deferred to F070.1**

v1 still adds dedup *edges* (chunk↔chunk cross-episode at cosine ≥ 0.85), it just doesn't mark winners/losers persistently. Retrieval-time dedup (gbrain's 4-layer pattern) can use those edges without needing the column.

---

## Goals

1. **Build a chunk graph during sleep** so chunks become first-class graph citizens.
2. **Enable adjacency boost / spreading activation / hub-shift to operate on chunks.**
3. **Dedup near-identical chunks** to reduce noise in top-K.
4. **Stale-chunk pruning** to bound storage growth.
5. **Feature-flagged + measurable.** Off by default until A/B confirms; re-run adjacency-boost A/B after F070 ships to validate.

## Non-goals

- Re-running embeddings on existing chunks.
- Cross-agent edge graph.
- Semantic chunking (different problem — F069 territory).

---

## Design

### 1. Extend `GraphDensifier` to handle chunks

Current `nous/brain/graph_densifier.py` operates on fact/decision/episode/procedure node types. Add `chunk` as a fifth node type with these edge-creation rules:

| Edge | When | Method |
|---|---|---|
| `chunk → fact` | Same `source_episode_id` | One edge per (chunk, fact) pair within the same episode. Weight = cosine similarity between chunk embedding and fact embedding. Threshold: 0.55. |
| `chunk → chunk` (intra-episode) | Same `episode_id`, sequential | Adjacent chunks (by `chunk_index`) auto-linked at weight=1.0 ("sequential" edge type). Non-adjacent linked at weight = cosine if > 0.7. |
| `chunk → episode` | Always | Chunk's source episode (already FK; emit as explicit edge so graph traversal works). Weight=1.0. |
| `chunk → chunk` (cross-episode) | Cosine > 0.85 | Dedup-style edges marking near-duplicates across sessions. **v1 emits edges only**; persistent `superseded_by` flagging is deferred to F070.1 (requires schema migration). Retrieval-time dedup can still consume the edges. |

### 2. New sleep phase: `chunk_consolidation`

Inserted between `graph_densification` and `relink_open_episodes`. Calls extended `GraphDensifier` for `chunk` node type.

**v1 settings (all in this PR):**

| Param | Default | Purpose |
|---|---|---|
| `NOUS_CHUNK_CONSOLIDATION_ENABLED` | `false` | Master switch. |
| `NOUS_GRAPH_BACKFILL_MAX_CHUNKS` | `100` | Max orphan chunks processed per cycle. Caps LLM/embedding cost. |
| `NOUS_GRAPH_THRESHOLD_CHUNK_FACT` | `0.55` | Cosine floor for chunk→fact edges. |
| `NOUS_GRAPH_THRESHOLD_CHUNK_CHUNK_INTRA` | `0.70` | Cosine floor for non-adjacent intra-episode chunk pairs. |
| `NOUS_GRAPH_THRESHOLD_CHUNK_CHUNK_CROSS` | `0.85` | Cosine floor for cross-episode chunk dedup. |

**Deferred to F070.1 (when staleness pruning lands):**

| Param | Default | Purpose |
|---|---|---|
| `NOUS_CHUNK_STALE_MAX_AGE_DAYS` | `730` | Age (days) above which orphan chunks become eligible for soft-delete. Not in v1. |

### 3. Chunk staleness pruning — **deferred to F070.1**

Originally planned for v1: chunks whose source episode is older than a configurable threshold AND has no incoming graph edges → soft-deleted via an `active` flag. **Deferred because `heart.episode_chunks.active` does not exist today** — would need a schema migration that's out of v1 scope.

F070.1 will add the column, the `NOUS_CHUNK_STALE_MAX_AGE_DAYS` setting, and the stale_scan extension. v1 ships without storage-bound pruning; growth is bounded only by ingest rate. At ~24K chunks/year (per F069 capacity estimate), prod can absorb that for at least a year before pruning becomes urgent.

### 4. Migration

No schema change — `brain.graph_edges` already supports any source_type/target_type pair. Existing index on `agent_id` covers query patterns.

### 5. Code touch points

- `nous/brain/graph_densifier.py` — add `chunk` node type handling
- `nous/handlers/sleep_handler.py` — add `chunk_consolidation` phase entry
- `nous/config.py` — add 5 settings fields per the table above
- `nous/brain/schemas.py` — update node-type validation if any
- Tests: extend `test_graph_densifier.py` with chunk-edge cases

---

## Rollout

Single-phase ship behind feature flag.

1. **PR 1**: extend GraphDensifier + sleep phase + tests. Flag off by default.
2. **Validation**: enable flag in eval, run sleep cycle on `nous-lme-corpus`, verify edge counts:
   - Expect ~3-5× more edges (chunk↔fact + intra-episode chunk↔chunk dominate)
   - Spot-check 10 random chunks for plausible edges
3. **A/B**: re-run today's adjacency boost screen. Goal: confirm adjacency boost now lifts hit@5 (specifically temporal-reasoning).
4. **Ship to prod** if A/B confirms. Flip flag.

---

## Open questions

1. **Backfill cost.** With 35K chunks per agent, full backfill = 35K × ~5 same-episode comparisons × ~1ms embedding I/O = ~3min CPU + DB. Each subsequent sleep cycle processes orphans (cap 100). Acceptable.
2. **Cross-episode dedup aggressiveness.** Threshold 0.85 is conservative. Real-world boilerplate (signatures, "Looking forward to chatting" templates) often hits 0.95+; dedup floor at 0.85 may flag legitimate paraphrases. Start conservative, tune via eval.
3. **Sequential edge weight semantics.** Sequential chunk-chunk edges weight 1.0 might dominate graph traversal. Consider edge_type tags so consumers (adjacency boost, spreading activation) can filter or weight differently.
4. **Sleep-phase ordering.** Should chunk_consolidation run BEFORE or AFTER fact-fact densification? Likely AFTER so chunk→fact links use the freshest fact embeddings. Validate empirically.

---

## Provenance

- 2026-05-25 LME corpus audit: 1,775 edges, all fact↔fact / procedure↔procedure. Zero chunk edges. (Logged after adjacency boost screen returned identical numbers to BGE-only.)
- F040 graph densification design — chunk node type was explicitly not included (F067 didn't exist yet)
- F067 episode chunks — added 2 weeks before this audit. Sleep handler was not updated.
- gbrain V0 docs: their chunking strategy treats chunks as first-class graph citizens from the start. We need to catch up.
