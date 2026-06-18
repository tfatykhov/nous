# F082 — PPR Recall Leg (query-seeded Personalized PageRank over the Brain graph)

**Status:** 📋 Proposed (Draft spec v1)
**Author:** Nous (with Tim)
**Origin:** HippoRAG-v2 (ICML 2025, arXiv 2502.14802) source review, Jun 17 2026
**Stakes:** medium · **Confidence:** 0.65
**Depends on:** F022 (spreading activation — coexists), F040 (graph densification / `graph_constants` exclusion model), F050 (QueryExpander/MRR@K eval harness — must be re-synced first), F069 (episode chunks — seed question parked)
**Relates to:** `recall_hubs` (degree centrality — additive, not replaced), F043 (sleep backfill rerank — warm-baseline opportunity), F044 tinyHippo-Lite (telemetry-first rollout template)

---

## 1. Motivation

The HippoRAG-v2 source review found one genuine capability gap, not redundancy:

> Nous has **no global, query-personalized PageRank** over its knowledge graph.

What we have today is strictly *local*:

- **F022 spreading activation** — a decayed recursive CTE seeded from vector hits. With `decay≈0.5` and `max_depth=2`, activation is effectively dead by hop 2; it's a 1–2 hop neighborhood expander, not a global stationary-distribution ranker.
- **`recall_hubs`** — static, query-*independent* degree centrality. Same hubs every query.

HippoRAG's core retrieval is exactly the thing we lack: `igraph.personalized_pagerank(damping=0.5, undirected, implementation="prpack")` with a **reset vector seeded by the query**. That lets evidence flow globally across the graph and surface multi-hop-relevant facts that a vector top-k never touches (the "associative recall" win).

**Thesis:** Add a single PPR *recall leg* over `brain.graph_edges`, seeded by the scores Nous already computes (top-k fact/decision retrieval), and fuse it into RRF alongside the existing legs. This captures ~80% of HippoRAG's value natively, with **zero** neuromorphic dependency and no new extraction pipeline.

---

## 2. Scope

### In scope
- A new retrieval leg: query-personalized PageRank over the existing `brain.graph_edges` table.
- Generalize RRF fusion from 2-leg (vector+keyword) to **N-leg** so the PPR leg slots in.
- Reuse the existing edge-exclusion model (`autobehavior_exclusion_sql` + `RETRIEVAL_EXCLUDED_RELATIONS`).
- Feature-flagged, **default off**; telemetry-first rollout.

### Out of scope
- New edge types or a new OpenIE/triple-extraction pipeline (HippoRAG builds its own KG; we ride the existing one).
- Passage-node / DPR dual-coding (HippoRAG mixes phrase + passage nodes; Nous nodes are already heterogeneous decisions/facts/episodes/procedures — no new node class).
- Replacing F022 or `recall_hubs`. PPR is *additive*.
- igraph as a runtime dependency in the hot path (see §4.2 — we run PPR in SQL/Python, not igraph, to avoid loading the whole graph per query).

---

## 3. Design

### 3.1 The reset (personalization) vector

HippoRAG's reset vector = `phrase_weights` (top-200 query→fact cosine, IDF-penalized) **+** `passage_weights` (DPR × 0.05). Nous already produces the equivalent of `phrase_weights` for free: the **top-k retrieval scores** from the existing vector/RRF stage.

```
reset[node_i] = normalized( seed_score_i )   for node_i in existing top-k hits
reset[node_j] = 0                            otherwise
```

- Seed set = existing Stage-1 retrieval hits (facts + decisions + episodes), already carrying a `seed_score` (the field is *already on* `NeighborResult` from the Path-A fix).
- Normalize seeds to sum to 1.0 → valid personalization distribution.
- No new embedding work. The "IDF penalty" is implicitly handled by hybrid search's keyword leg already; optional refinement in §7.

### 3.2 The graph

- Nodes: decisions, facts, episodes, procedures (existing `node_type`s).
- Edges: `brain.graph_edges` filtered by `agent_id`, treated **undirected** (matches HippoRAG `directed=False`).
- Edge weights: `COALESCE(weight, 1.0)`.
- **Exclusions (reuse, do not reinvent):**
  - `autobehavior_exclusion_sql()` — drops `supersedes`, `contradicts`, `happened_before`, `co_occurred`, and `co_mention`-method edges. These are lineage/temporal/builder edges, not associative connectivity — identical rationale to F022 and the density gate.
  - This keeps PPR consistent with every other graph consumer (single source of truth = `graph_constants`).

### 3.3 The algorithm

Power-iteration PPR (no igraph dependency):

```
PR = (1 - d) * reset + d * Mᵀ · PR
```

- `d = 0.5` (HippoRAG damping — config `ppr_damping`, default 0.5).
- `M` = row-normalized adjacency of the (excluded, undirected, agent-scoped) edge set.
- Iterate until L1 delta < `ppr_tolerance` (default 1e-4) or `ppr_max_iter` (default 30). PR over the Brain graph converges in well under 30 iters at our densities.
- Output: top-`ppr_leg_limit` (default 20) nodes by stationary probability, as `(node_id, node_type, ppr_score)`.

### 3.4 Fusion

Generalize `_rrf_merge` (currently hardcoded to vector+keyword two-list) into **N-leg RRF**:

```
rrf_score(doc) = Σ_legs  w_leg / (k + rank_leg(doc))
```

- Legs: `vector`, `keyword`, `ppr` (extensible).
- Weights resolve via the existing weight-resolution chain (param → runtime `/admin/search-weights` → env). Add `ppr_weight` (default **0.0** = off).
- Missing-from-leg penalty rank = `limit + 1` (unchanged).
- Keep the existing 0–1 normalization (max RRF = Σ w_leg / k).
- **Back-compat:** the 2-arg call site stays valid — N-leg is a superset; when only two legs are passed with the old weights, output is bit-identical.

---

## 4. Integration points

### 4.1 Where it plugs in
- `nous/heart/search.py::_rrf_merge` → refactor to `_rrf_merge_n(ranked_lists: list[(list, weight)], k, limit)`.
- New `nous/brain/ppr_recall.py` (mirrors `spreading_activation.py` structure: density-aware gate + async SQL fetch + Python power-iteration).
- Called from `run_recall_pipeline` Stage 2 (graph stage), parallel to spreading activation — both are graph legs seeded by Stage-1 vector hits.

### 4.2 Why SQL+Python, not igraph
Loading the full agent subgraph into igraph per query is wasteful and adds a C-extension dependency. Instead:
1. One SQL pull of the agent's excluded edge set (already indexed by `agent_id`).
2. Build a sparse CSR adjacency in NumPy (scipy.sparse optional).
3. Power-iterate. At our graph sizes (10²–10⁴ nodes/agent) this is sub-10ms after the edge fetch.

Optional: a **density gate** identical to F022 — skip PPR when the graph is too sparse to matter (`ppr_min_density`, reuse `compute_graph_density`).

---

## 5. Config (all default-off / inert)

```
ppr_recall_enabled            = "auto"   # true|false|auto (density-gated)
ppr_weight                    = 0.0      # RRF leg weight; >0 activates fusion
ppr_damping                   = 0.5      # HippoRAG parity
ppr_max_iter                  = 30
ppr_tolerance                 = 1e-4
ppr_leg_limit                 = 20
ppr_min_density               = <reuse spreading_activation threshold>
ppr_seed_top_k                = 50       # how many Stage-1 hits seed the reset vector
```

`ppr_weight = 0.0` ⇒ leg computed only if telemetry-enabled, never affects ranking. Promotion is a single weight bump.

---

## 6. Rollout (telemetry-first, mirrors tinyHippo-Lite F044)

1. **Phase A — shadow:** compute PPR leg, log its top-20 and the would-be fused ranking, but fuse with `ppr_weight=0`. Emit telemetry: overlap with vector leg, net-new nodes surfaced, latency.
2. **Phase B — eval:** run the offline harness (QueryExpander/MRR@K, score-rerank suite). Gate on MRR@K delta ≥ 0 and no latency regression > budget. **Re-sync the eval harness to prod retrieval first** (known drift — see F050).
3. **Phase C — canary:** `ppr_weight` small (e.g. 0.1) on Tim's agent only; watch recall-quality feel + telemetry.
4. **Phase D — default:** raise to tuned weight, keep `auto` density gate.

---

## 7. Open questions / future
- IDF penalty on seeds: HippoRAG down-weights high-frequency phrase nodes. Worth replicating against hub facts? (Could reuse `recall_hubs` degree as an inverse weight.)
- Should PPR seeds include the *episode-chunk* hits (F069 document chunks), or stay node-level? Probably node-level v1.
- Cache the per-agent CSR adjacency between queries (invalidate on edge write / during sleep) to drop the SQL pull from the hot path.
- Interaction with F043 backfill rerank during sleep — could precompute a "warm" PR baseline.

---

## 8. Verification / acceptance
- **AC1** — N-leg RRF passes existing `test_rrf_search.py` unchanged (2-leg parity, bit-identical output).
- **AC2** — New `test_ppr_recall.py`: deterministic small-graph fixture, hand-checked stationary distribution, exclusion-relation coverage, empty-seed/empty-graph guards.
- **AC3** — Shadow telemetry shows PPR surfacing ≥1 net-new relevant node on multi-hop queries where vector top-k misses (the HippoRAG associative-recall case).
- **AC4** — No latency regression beyond budget with `ppr_recall_enabled=auto`.
- **AC5** — Eval harness (re-synced) shows MRR@K delta ≥ 0 at the canary `ppr_weight` before any default-on promotion.
