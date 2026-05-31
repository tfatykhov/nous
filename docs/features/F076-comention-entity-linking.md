# F076 — Co-mention / Shared-Entity Linking (associative graph layer)

**Status:** spec → review → implement
**Default:** ON (core capability — the associative graph's whole purpose)

## Problem

Nous builds graph edges **only by embedding cosine similarity** (`graph_densifier.py`
fact↔fact ≥0.82, cross-type ≥0.55–0.72). Two memories that **name the same entity** but
aren't embedding-similar enough stay **orphans** — e.g. "Green is Steve Hillage's 4th album"
and "Miquette Giraudy … with Steve Hillage" share *Steve Hillage* yet have cosine 0.43 ≪ 0.82,
so no edge forms. This is the root cause of unbridgeable multi-hop retrieval (formation sweep
finding #1). Cosine links things vector search already finds; the **associative** edge — link
by *shared mention*, independent of embedding distance — is the HippoRAG mechanism Nous lacks.

Proven on real data (2026-05-30): an injected co-mention edge + Path A (line-403 fix) + fix-B
seed-score recovered a **vector-missed disjoint bridge** into top-3 (`stage_origin=heart_graph_memory`).
This feature builds that edge as a real, default-on **sleep** step.

## Design

### 1. Entity extraction — `nous/brain/entity_extraction.py` (new, deterministic, no deps)
- `extract_entities(text) -> set[str]`: proper-noun phrases (≥2 capitalized tokens), normalized:
  strip possessive `'s`/`’s`, trailing punctuation, leading stopwords (The/A/In/…). Lowercased.
  Fixes the `Marie de' Medici` apostrophe break (connector allows `de'`/`de`/`of`/`the`).
- Pure, unit-tested, reusable. (LLM/NER precision upgrade deferred — note in code.)

### 2. Densifier co-mention pass — `nous/brain/graph_densifier.py`
- New `build_comention_edges(agent_id)` invoked from `run_backfill_cycle` (runs in **sleep**).
- For active **facts** (fact↔fact only): extract entities from content, index
  entity→{fact_ids}, drop hub entities mentioned in > `max_degree` facts, link fact
  pairs sharing an entity with per-node fan-out cap `max_edges_per_node`.
  (chunk↔chunk co-mention was dropped — a noisy redundant web over overlapping raw
  slices; documents get a distilled connector FACT via the document-consolidation
  feature **F077** instead, which joins this same fact graph.)
- Insert `graph_edges(relation='related_to', extraction_method='co_mention', weight=settings
  value, auto_linked=true)`, `ON CONFLICT (source_id,target_id,relation) DO NOTHING`.
  `related_to` ⇒ traversable by Path A / spreading / adjacency; not `contradicts` so spreading
  won't filter it. Idempotent: a co-mention edge is skipped if any edge already connects the pair.
- Batched; respects existing densifier per-cycle caps; logs counts to `_ce_stats`-style telemetry.

### 3. Migration — `sql/migrations/0NN_comention_extraction_method.sql`
- Extend the `brain.graph_edges` `extraction_method` CHECK to allow `'co_mention'`
  (currently `deterministic|heuristic|inferred`). Clean provenance + idempotent rebuild +
  telemetry. F065 inferred-edge penalty does NOT apply to `co_mention` (it's a direct match,
  not a low-confidence inference).

### 4. Config — `nous/config.py`
- `comention_linking_enabled: bool = True`  (NOUS_COMENTION_LINKING_ENABLED) — **default ON**
- `comention_max_degree: int = 10`          — skip hub entities (> N nodes) to bound noise
- `comention_max_edges_per_node: int = 20`  — fan-out cap
- `comention_weight: float = 0.90`          — edge weight (was 0.80; the private-fact value harness showed 0.80 lands a fully-vector-missed disjoint bridge at rank 11, 0.90 clears the cutline at rank 7 with zero displacement — Path-A score = seed_score × comention_weight)
- `comention_min_entity_chars: int = 6`     — entity length floor

### 5. Retrieval consumption (already built — no new work, but note for the A/B)
Co-mention edges are consumed by Path A (Stage 2b, line-403 guard fixed to fire on chunk
seeds) and scored by fix-B seed-score. Whether the **consumer** flags
(`graph_neighbor_seed_score_enabled`, `graph_adjacency_boost_enabled`, `rerank_by_score`)
should also default differently is **decided by the whole-system A/B**, not this spec.

### 6. Tests — `tests/test_comention_linking.py`
- entity extraction: possessive, punctuation, stopword, multi-token, apostrophe (`de'`).
- densifier pass: links entity-sharing nodes; respects degree + fan-out caps; skips hubs;
  idempotent; honors `enabled=False`; co-mention edge survives the extraction_method CHECK.

## Validation (the verdict — separate from shipping the build)
Whole-system A/B on a genuinely-disjoint multi-hop set (2Wiki / MuSiQue-3hop, N≈30):
real `/chat`-or-document ingest → summarizer + facts + chunks → **sleep (now builds
co-mention edges)** → **cognitive answer loop** → grade **answers**, co-mention **off vs on**.
Report answer accuracy delta AND a displacement check on normal single-hop queries (the
Philippe-style cost). Default stays ON per decision; A/B informs caps/weight tuning + whether
consumer flags should flip.

## Risk / honesty
- **Displacement cost** (observed: a marginal bridge dropped 9→11 under noisy edges). Mitigated
  by hub degree-cap + conservative ≥2-token entities + moderate weight; A/B measures net.
- **Entity-extraction precision** (regex is crude). Conservative defaults; LLM/NER upgrade is a
  noted follow-on, not in v1.
- **On-by-default** is the user's decision (core feature); the A/B is the safety check, and
  every knob (incl. the master flag) is reversible by config.
