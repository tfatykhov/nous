# The Nous Memory-Retrieval System: A Code-Grounded White Paper

**Date:** 2026-06-08
**Method:** 5-agent parallel code-only discovery + single-author verification
**Scope:** Everything that turns a query into ranked memory — both the `recall_deep` tool pipeline and the every-turn cognitive context-assembly path.
**Constraint:** Derived **from source code only** (Python + SQL). No feature specs, design docs, or memory notes were used as authority. Every claim carries a `path:line` anchor. Where deployment behavior is discussed, it is sourced explicitly to `.env.prod-snapshot` and labeled as a deployment overlay — the algorithm description itself rests on `config.py` defaults, which are code.

---

## Abstract

Nous has **two independent retrieval surfaces** that share primitives but diverge in structure, scoring, and feature coverage:

1. **`recall_deep`** — an agent-invoked tool. Runs `run_recall_pipeline` (`nous/api/retrieval_pipeline.py`), which orchestrates a hybrid Heart search, episode-chunk vector search, multi-mechanism graph expansion, contradiction detection, a recency resolver, and an optional score-merge. This is the "rich" path.

2. **Cognitive context assembly** — runs automatically on **every** chat turn inside `CognitiveLayer.pre_turn` → `ContextEngine.build` (`nous/cognitive/`). It is the retrieval that actually feeds the model by default. It has **no** graph expansion, **no** spreading activation, and **no** contradiction surfacing, and it re-implements staleness/recency/dedup with its own (drifted) logic.

The central finding is structural: **at the point where memory types are merged and ranked, Nous compares scores drawn from incompatible numeric scales as if they were one space.** Facts and episodes carry RRF scores normalized to `[0,1]`; procedures carry a boosted score that can exceed `1.0`; censors carry a raw cosine similarity hard-floored at `≥0.7`; and any leg whose embedding call failed carries a raw full-text rank (~`0.05–0.08`). In the production configuration (cross-encoder and MMR both **off**), the final ranking is a direct descending sort over this mixed pool. A hard floor and an unbounded boost break the monotonic relationship between score and relevance *across types*, so structurally-high types (censors, effective procedures) displace genuinely more relevant facts at the `[:limit]` truncation.

A second theme: a large fraction of the retrieval machinery is **gated off or unreachable** — date-aware boost is dead code, spreading activation is behind an effectively-unsatisfiable density gate, and several scoring features fire but are then discarded by a non-monotonic re-sort. The system's *observability* of its own behavior is also broken (`PipelineStats.ce_reranked`/`mmr_applied` are hardcoded `False`).

---

## 1. Architecture Overview

### 1.1 The two paths

```
                    ┌─────────────────────────────────────────────┐
  agent calls       │  recall_deep  (nous/api/tools.py:612)        │
  recall_deep ─────▶│    └─ run_recall_pipeline                    │
                    │         (nous/api/retrieval_pipeline.py:174) │
                    │       Heart.recall ─┐                        │
                    │       chunk vector  ├─ assemble ─ (rerank?) ─│──▶ tool result text
                    │       graph expand ─┘   contradiction        │
                    │                          recency resolver    │
                    └─────────────────────────────────────────────┘

                    ┌─────────────────────────────────────────────┐
  every chat turn   │  CognitiveLayer.pre_turn                     │
  (automatic) ─────▶│    (nous/cognitive/layer.py:349)             │
                    │    └─ ContextEngine.build                    │
                    │         (nous/cognitive/context.py:125)      │
                    │       Brain.query / Heart.search_* directly  │──▶ system prompt
                    │       per-type budget + staleness + dedup    │
                    │       NO graph / NO spreading / NO contradict │
                    └─────────────────────────────────────────────┘
```

Both ultimately call the same low-level engines (`Heart` managers, `Brain.query`, the hybrid searcher in `nous/heart/search.py`), but they wire them differently and post-process differently. **The memory the model sees automatically each turn is a strictly weaker retrieval than what it gets when it chooses to call `recall_deep`** — and the two can disagree on supersession and ranking for identical inputs (§6.4).

### 1.2 Deployment configuration overlay (`.env.prod-snapshot`)

The algorithm description in §2–§6 is written against **`config.py` defaults** (code). Production flips a number of flags; because several findings change reachability under prod, the relevant deltas are tabulated once here and referenced as the **"prod overlay"** throughout. This file is deployment config, not code — it is used only to assign reachability verdicts, never as the description's authority.

| Flag | `config.py` default | `.env.prod-snapshot` | Effect when ON |
|------|--------------------|----------------------|----------------|
| `NOUS_EPISODE_CHUNKS_ENABLED` | `false` | **`true`** (`:46`) | chunk vector leg runs; `rerank_by_score=True` |
| `NOUS_HEART_GRAPH_ALL_TYPES_ENABLED` | `false` | **`true`** (`:67`) | Path A all-types graph expansion runs |
| `NOUS_GRAPH_ADJACENCY_BOOST_ENABLED` | `false` | **`true`** (`:52`) | adjacency boost runs |
| `NOUS_RECENCY_RESOLVER_ENABLED` | `false` | **`true`** (`:92`) | superseded-fact down-rank runs |
| `NOUS_QUERY_EXPANSION_ENABLED` | `false` | **`true`** (`:90`) | Haiku multi-query RRF union runs |
| `NOUS_RECALL_INCLUDE_PARENT_EPISODES` | `false` | **`true`** (`:91`) | parent-episode summaries appended |
| `NOUS_TEMPORAL_EXTRACTION_ENABLED` | `false` | **`true`** (`:119`) | `event_date` populated (feeds recency resolver) |
| `NOUS_CROSS_ENCODER_ENABLED` | `false` | `false` (`:26`) | — (CE rerank stays OFF in prod) |
| `NOUS_MMR_ENABLED` | `false` | `false` (`:88`) | — (MMR stays OFF in prod) |

**Implication:** the common belief that "the graph/chunk apparatus is shipped dark" is true of code defaults but **false in production** — prod turns on chunks, Path A, adjacency boost, the recency resolver, and query expansion. What stays off in prod is exactly the cross-encoder and MMR — the two modifiers that would *re-base* scores into a single comparable space. Their absence is what makes the score-space-mismatch bug (§3.4, §7-B1/B4) the live production defect rather than a latent one.

> **Embedding-model drift note.** The code embeds with `text-embedding-3-small`, `dimensions=1536` (`nous/heart/embeddings.py:26-28`). Production is understood to run `text-embedding-3-large`. Any reproduction of prod ranking must override the model; the dimension assumption (1536) is wired into the SQL `vector(1536)` columns and would need to match.

---

## 2. The `recall_deep` Pipeline (`run_recall_pipeline`)

`recall_deep` (`tools.py:612`) is a thin wrapper. It computes two booleans and forwards everything to `run_recall_pipeline` (`retrieval_pipeline.py:174`):

- `chunks_rerank = episode_chunks_enabled AND (search_all OR "fact" in types)` → passed as `rerank_by_score` (`tools.py:691-704`). **Prod: True.**
- `_f071_exclude_ids = CURRENT_TURN_EXCLUDE_IDS.get()` contextvar → `exclude_ids` (F071 in-context exclusion).
- `residual_activations` (F055) computed only if `residual_activation_enabled` (default **False**) else `None`.

The pipeline fills a mutable accumulator via `_run_stages`, then assembles a flat `list[PipelineResult]` in stage order, applies post-processing, and returns `(results, stats)`. **There is no final global top-K truncation in the pipeline** — `limit` is applied per leg only (§7-B9). The returned list is the sum across all legs; `recall_deep` formats *all* of it to text (subject to downstream tool-result pruning, `NOUS_TOOL_SOFT_TRIM_*`).

### Stage 1 — Heart hybrid search (`retrieval_pipeline.py:336-357`)
`heart_types` = the requested types intersected with `{episode, fact, procedure, censor}`; `search_all` (default `["all"]`) expands to all four (`:340-347`). Calls `heart.recall(query, limit, types=heart_types, residual_activations, apply_mmr)`. **All real scoring happens inside `heart.recall`** (§3). Output: `acc.heart_results: list[RecallResult]`. Always runs unless the caller restricts to `["decision"]` only.

### Stage 1.5 — Episode-chunk vector leg (`retrieval_pipeline.py:368-396`)
Gated by `episode_chunks_enabled` (default False; **prod True**) AND (`search_all OR "fact" in types`). `_search_episode_chunks` (`:871`) embeds the query and runs a raw pgvector cosine search over `heart.episode_chunks`: `sim = 1 - (embedding <=> qvec)`, `ORDER BY embedding <=> qvec LIMIT min(episode_chunk_recall_limit, limit*2)` (`:897-903`). Chunk score is raw cosine `[0,1]`, appended into the Heart section as `type="chunk"`. Embedder-missing → `[]` (silent, by design); other failures RAISE → caught at `:384`, logged WARN, counted in `stage_errors["chunk_recall"]`.

### Stage 2 — Cross-type Heart→decision expansion (`retrieval_pipeline.py:401-438`)
Gated by `graph_recall_enabled` (default True) AND `cross_type_linking_enabled` (default True), fires when heart or chunk results exist. For the **top-3** heart results of type fact/episode, calls `brain.neighbors(hr.id, node_type=hr.type, limit=2, neighbor_type="decision")` — the `neighbor_type` pushes the decision filter into SQL so `LIMIT 2` returns decisions. Score (`_heart_graph_to_pipeline`, `:934`): `edge_weight × graph_recall_decay(0.7) × penalty`, where `penalty = graph_inferred_edge_penalty` (default 1.0) for `inferred` edges else 1.0. `type="decision"`, `source="graph_expanded"`, `stage_origin="heart_graph"`.

### Stage 2b — Path A: Heart/chunk seeds → all non-decision neighbors (`retrieval_pipeline.py:450-546`)
Gated by `heart_graph_all_types_enabled` (default **False**; **prod True**). Seeds = top-3 fact/episode heart results + top-3 chunk results, each carrying its retrieval score as `seed_score`. For each seed × each `nbr_type ∈ {fact, episode, chunk, procedure}`, calls `brain.neighbors(..., limit=heart_graph_neighbors_per_seed(3), neighbor_type=nbr_type)`. Dedups against existing heart/chunk ids and against each other, keeping the higher composed score. Score (`_score_memory_neighbor`, `:958`): if `graph_neighbor_seed_score_enabled` (default **False**, **prod False**) AND `seed_score` present → `seed_score × edge_weight × penalty`; **else** falls back to `edge_weight × decay × penalty` — a ceiling of ~0.70 (see §7-B-graph for why this matters).

### Stage 3 — Brain decision query (`retrieval_pipeline.py:554-556`)
Gated by `search_all OR "decision" in types`. `decision_results = await brain.query(query, limit)`. `_decisions_to_pipeline` (`:1002`): `score = d.score or 0.0`; preserves `raw_score`, `category`, `stakes`, `confidence`, `pattern` in metadata.

### Stage 4 — Decision graph expansion: 1-hop OR spreading activation (`retrieval_pipeline.py:558-654`)
Gated by `decision_results AND graph_recall_enabled`.
- **Spreading-activation branch** if `spreading_activation_enabled != "false"` (default `"auto"`): computes graph density; `should_use_spreading_activation` auto-enables only above density 3.0 (§4.2). If used: seeds = top-`graph_recall_max_expand(5)` decisions with `score or 0.5`; recursive-CTE returns `(id, type, activation)`; keep `activation > 0.1` (**the "0.1 activation floor" lives at `retrieval_pipeline.py:604`**, *not* :342) and not already seen. SA score: `activation × decay(0.7)`.
- **1-hop fallback** otherwise: for top-5 decisions, `brain.neighbors(dec.id, node_type="decision", limit=graph_recall_max_neighbors(3))`. Score: `edge_weight × decay × penalty`.

### Stage 5 — Contradiction detection (`retrieval_pipeline.py:659-689`)
Gated by `graph_recall_enabled AND contradiction_detection` (both default True). `all_ids` = decision ids ∪ graph_expanded ids **only** — *not* heart facts/chunks (§7-B3). If ≥2 ids, queries `brain.graph_edges WHERE relation='contradicts'` over that set.

### Post-stage assembly (`retrieval_pipeline.py:218-309`)
1. **Flat list in stage order** (`:219-249`): heart → chunks → heart_graph decisions → Path-A memory neighbors → decisions, then (optional) `_attach_fact_source_episodes` (only if `session_group_heart_section`, default False), then **adjacency boost**, then `extend` with `graph_expanded`.
2. **Adjacency boost** (`_apply_graph_adjacency_boost`, `:729`; flag default False, **prod True**): for candidate pairs both present in `brain.graph_edges` (excluding `contradicts`), sum edge weights → `degree`; `score *= 1 + alpha(0.15) × degree/max_degree`; **internally re-sorts**. **Runs at `:246`, before `graph_expanded` is appended at `:249`** (§7-B-graph-6).
3. **Contradiction attach** (`_attach_contradictions`, `:1180`): appends `tgt_id` to the source result's `contradicts` list (one-directional, §7-B8).
4. **Recency resolver** (`_resolve_recency_conflicts`, `:1081`; flag default False, **prod True**): see §5.4.
5. **`rerank_by_score`** (`:277-278`): `if rerank_by_score: results.sort(key=score desc)`. Default False; **prod True** (chunks on). This single switch decides whether the cross-leg pool competes on score or stays in stage order.
6. **F071 in-context exclusion** (`:286-293`): drop results whose `str(id)` is in `exclude_ids[r.type]`. Type-keyed; applied after sort.
7. **Stats** built (`:295`) — with `ce_reranked` and `mmr_applied` **hardcoded False** (§7-B5).

---

## 3. The Hybrid Search Engine (Heart) and the Cross-Type Merge

This is the core scorer. Everything in Stage 1 routes through it.

### 3.1 Query → embedding (per-leg, repeated)
Each manager's `_search` embeds the query independently: `facts._search` (`facts.py:1227`), `episodes._search` (`episodes.py:505`), `procedures._search` (`procedures.py:378`), `censors._search` (`censors.py:364`). **The same query is embedded 3–4 times per `recall()`** (§7-B-hs-8). `EmbeddingProvider.embed` (`embeddings.py:68-75`) POSTs to OpenAI, model `text-embedding-3-small`, `dimensions=1536`; retries 3× on 5xx/network, 4xx raises. On any failure each `_search` logs a warning and proceeds with `embedding=None` → keyword-only fallback.

### 3.2 The two legs (`search.py:120-224`)
**Vector leg** (`:189-197`): `1 - (embedding <=> qvec)` cosine, `ORDER BY ... <=>` over the HNSW `vector_cosine_ops` index, `LIMIT limit*3`.
**Keyword leg** (`:207-218`): `ts_rank_cd(search_tsv, plainto_tsquery) / (1 + ts_rank_cd)` over the GIN index, `LIMIT limit*3`. Raw `ts_rank_cd` is tiny (~0.05–0.08), squashed into `[0, ~0.08)`.
The two legs' scores are **never directly compared** — only their *ranks* feed RRF.

### 3.3 RRF fusion (`search.py:76-117`)
```
keyword_weight = 1.0 - vector_weight          # default vector_weight=0.7 → kw=0.3
score(doc) = vector_weight/(k + v_rank) + keyword_weight/(k + k_rank)   # k = rrf_k = 60
score = score / (1/k)                          # normalize to [0,1] since v_w + k_w = 1.0
```
Absent-in-a-leg → that leg's rank = `limit+1` (penalty). Defaults: `vector_weight=0.7`, `rrf_k=60` (`config.py:101-102`). **Result: facts, episodes, and procedures (single-query path) emit a normalized RRF score in `[0,1]`.** A doc ranked #0 in both legs → 1.0; ranks fall off quickly (≈0.92 at vector-rank-0/keyword-absent, ≈0.69 at rank 1, ≈0.55 at rank 2 for `limit≈20`).

**Degraded path:** when `embedding is None`, `hybrid_search` returns `keyword_results[:limit]` **directly** — raw `ts_rank_cd/(1+…)` (~0.05–0.08), **not** RRF-normalized. A type whose embed leg failed lands ~10–20× lower in score space than its hybrid-succeeding siblings (§7-B-hs-3).

### 3.4 Per-type score post-processing — the spaces actually merged

| Type | Score handed to the merge | Space |
|------|---------------------------|-------|
| Fact | RRF `[0,1]`, then supersession filter may `×0.3` or `×confidence` (`facts.py:337-369`) | `[0,1]` |
| Episode | RRF `[0,1]` (`episodes.py:548`) | `[0,1]` |
| Procedure | `hybrid × (1 + boost)`, `boost = α(eff−0.5) + β(frame_eff−0.5)` (`procedures.py:446-457`) | **can exceed 1.0** |
| Censor | **raw cosine `1−(emb<=>q)`, hard-floored `> 0.7`** (`censors.py:387,400`) | **`[0.7, 1.0]`** |
| Tier-1 facts (`list_by_category`) | **hardcoded `1.0`** (`facts.py:1178`) | `1.0` |

### 3.5 The cross-type merge (`heart._recall`, `heart.py:867-1117`)
1. `fetch_limit = limit*2`; each type searched **sequentially** (AsyncSession not concurrency-safe, `:880`), each in its own try/except with rollback on failure.
2. Each item's per-type score is copied **verbatim** into `RecallResult.score` (`:983-988`) — **no normalization to a common space.** (`_to_recall_result` handles episode/fact/procedure **and censor** — `:1163` — so censors do enter `merged`.)
3. F055 residual boost (default off).
4. **F042 CE rerank** if `cross_encoder_enabled AND CROSS_ENCODER_AVAILABLE` (`:1008-1039`) — re-bases head scores into sigmoid space. **Prod: OFF.**
5. **F030 MMR** if `mmr_active` (`:1067-1108`). **Prod: OFF.**
6. **Prod live path:** `merged.sort(key=r.score, reverse=True); merged = merged[:limit]` (`:1110-1112`).

**This is the crux.** With CE and MMR off, the final cross-type ranking — and the `[:limit]` cut that *drops* items — is a raw descending sort over the four incompatible spaces of §3.4. A censor at 0.78 outranks every fact below RRF-rank-1; an effective procedure at 1.26 outranks everything; an embed-failed fact type collapses to ~0.06 and is cut entirely. The `[:limit]` truncation happens here, inside `heart.recall`, **before** the pipeline ever sees the results — so the displacement is not recoverable downstream.

---

## 4. The Graph-Expansion Layer

### 4.1 Edge substrate (what read traverses)
Edges live in `brain.graph_edges` (`sql/init.sql:191`): polymorphic `(source_id, source_type)`/`(target_id, target_type)`, `relation` (CHECK set grown across migrations 016/051/055), `weight FLOAT DEFAULT 1.0`, `extraction_method` (`migrations/047`: `deterministic|heuristic|inferred|co_mention|co_occurrence`), `UNIQUE(source_id, target_id, relation)`. Persisted `weight = raw_cosine × RELATION_WEIGHT_MULTIPLIERS[relation]` (`graph_linker.py:118-123`, multipliers 0.7–1.0) → edge weights bounded ≲1.0. The neighbor pull `Brain.neighbors → _neighbors` (`brain.py:1134,1164`) UNIONs both edge directions (`:1177-1199`) — **both directions are traversed**, good.

### 4.2 Reachability of each mechanism

| Mechanism | Code | Reachability verdict |
|-----------|------|----------------------|
| A: decision 1-hop | `retrieval_pipeline.py:629-637` | **LIVE** (computed); visible to top-K only when `rerank_by_score=True` (**prod True**) |
| B: spreading activation | `spreading_activation.py:66` | **UNREACHABLE** in `auto` mode — density gate (§below) effectively never met |
| C: cross-type heart→decision | `retrieval_pipeline.py:423-428` | **LIVE** |
| D: Path A all-types | `retrieval_pipeline.py:450` | default OFF → **prod LIVE** (`HEART_GRAPH_ALL_TYPES_ENABLED=true`) but see §7-B-graph-5 |
| Adjacency boost | `retrieval_pipeline.py:729` | default OFF → **prod LIVE**; mis-ordered (§7-B-graph-6) |
| Seed-score scoring | `_score_memory_neighbor:958` | **INERT** (`graph_neighbor_seed_score_enabled=False` in prod) |
| Date-aware boost (F075 L3) | `config.py:1199-1213` | **DEAD CODE** — no consumer in any config |

**The density gate.** `should_use_spreading_activation` in `auto` mode returns `density ≥ spreading_activation_density_threshold(3.0)` (`spreading_activation.py:53-63`). `compute_graph_density = edge_count / unique_nodes`, **excluding `co_mention` edges** (`:30-50`). Density ≥ 3.0 means average undirected degree ≥ 6 — far above what a sparse cosine-thresholded graph (fact→decision ≥0.80, fact→fact ≥0.90, `LIMIT 5` per write) produces, where most nodes have degree 0–2. So the recursive-CTE multi-hop engine is dead at recall time unless an operator hard-sets the flag to `"true"`. (Corroborated by the project's own observation that `graph_expansion_used=0` even with hundreds of inferred edges.)

### 4.3 Scoring of expanded nodes
All graph-expanded scores are `edge_weight × graph_recall_decay(0.7) × penalty` (decision/Path-A) or `activation × 0.7` (SA), via `_f065_provenance_penalty` (`:907-931`). The ceiling is ~0.70 — **below the `[0,1]` band that top RRF facts occupy and incomparable with the boosted/floored spaces of §3.4.** When `rerank_by_score=True` (prod), these ~0.35–0.63 graph scores are sorted directly against RRF facts, boosted procedures, and floored censors.

---

## 5. The Rank-Modifier Layer

Two application sites: **inside `heart.recall`** (query expansion, CE, MMR — act on the Heart-only merged set) and **inside `run_recall_pipeline`** (adjacency boost, recency resolver, `rerank_by_score`, F071 — act on the assembled cross-leg list).

### 5.1 Query expansion (F050) — PRE-retrieval — default OFF, **prod ON**
`heart.py:899-921` → `QueryExpander.expand` (`query_expansion.py:157-265`): cheap gate (min-words 3) → cache → single-flight → budget → Haiku (`claude-haiku-4-5`, forced tool use, 256 tok, 2.0s timeout) → fuse (case-insensitive dedup, original at index 0, cap `max_variants(3)`). **Merge is a genuine RRF union, not concatenation:** variants are batch-embedded once and threaded into each manager's `hybrid_search_multi`, fused via `_rrf_merge_n` (`search.py:359-377`). Single-variant collapses to byte-identical `hybrid_search`. **Fail-open everywhere** → `[query]`.

### 5.2 Cross-encoder rerank (F042) — default OFF, **prod OFF**
`heart.py:1008-1039` → `cross_encoder_rerank` (`reranker.py:66-167`): global sort by hybrid score first, head = `candidates[:max_candidates(30)]`, tail untouched; raw logit → sigmoid → **overwrites `.score`** for the head only. Tail keeps hybrid scores → head/tail now on different scales (§7-B-rm-3). **Off in prod**, so latent.

### 5.3 MMR diversity (F030) — default OFF, **prod OFF**
`heart.py:1041-1112` → `mmr_rerank` (`search.py:453-537`): greedy `MMR(d) = λ·cos(d,q) − (1−λ)·max_{s∈sel} cos(d,s)`, λ=0.7. Relevance uses a freshly-embedded query vs **stored** candidate embeddings — not CE sigmoid scores (hence F030.1 `mmr_skip_after_ce`, default True: when CE reordered the head, MMR is skipped entirely so the two never chain). **Off in prod.**

### 5.4 Recency resolver (§1) — default OFF, **prod ON**
`retrieval_pipeline.py:1081-1177`. Facts only, grouped by normalized `subject`. Conflict signal = a `contradicts` edge OR `difflib` ratio ≥ `recency_resolver_similarity_floor(0.55)`; both facts must carry differing parseable `event_date`. Newer → `recency_status="current"`; older → `"superseded"` + `score *= 0.3`. Inert unless `event_date` populated (needs `TEMPORAL_EXTRACTION_ENABLED`, **prod ON**).

> **Code-default vs prod reconciliation (important).** The resolver's `×0.3` runs at `:258`, *before* the `rerank_by_score` sort at `:278`. In the **default** config (chunks off → `rerank_by_score=False`) nothing re-sorts, so the down-rank is **cosmetic** — only the `[superseded YYYY-MM]` text tag survives. In **prod** (chunks on → `rerank_by_score=True`) the sort *does* fire, so the down-rank **is** effective on ordering. The widely-quoted "recency down-rank is cosmetic" is a default-config statement, not a prod statement.

### 5.5 Date-aware boost (F075 Layer 3) — DEAD
`date_aware_boost_enabled/factor/window_pad_days` exist only in `config.py:1199-1213`. **No consumer anywhere in `nous/`.** The documented 1.20× in-window temporal boost does not exist; flipping the flag does nothing. In-window temporal preference is handled solely by the recency resolver's down-rank of the *superseded* side, never a boost of the *in-window* side.

### 5.6 Not in the retrieval path
`apply_frame_boost` (`search.py:399`) is **not** called from `recall_deep`/`run_recall_pipeline`/`heart.recall` — it is a cognitive-path primitive (§6.3). Confidence calibration (`NOUS_CONFIDENCE_CALIBRATION_FACTOR`) is a decision **write-time** transform (`brain.py:365`), not a retrieval ranker.

---

## 6. The Cognitive Context-Assembly Path (every turn)

This is the retrieval that builds the system prompt automatically, distinct from `recall_deep`.

### 6.1 `pre_turn` → frame → intent → plan (`layer.py:349`)
1. **Frame** (`frames.py:40`): pure pattern matching over `activation_patterns` (multi-word substring = 2 pts, single-word set-membership = 1 pt); highest count wins, ties by `FRAME_PRIORITY`; no match → `conversation`. No LLM.
2. **Intent** (`intent.py:108`): regex extraction of greeting/question/temporal-recency/memory-type-hints/topic-keywords/entities → `plan_retrieval` (`:146`) builds a `RetrievalPlan`. Greetings and short keyword-less turns → skip-all plan (zero budgets). Otherwise one `RetrievalQuery` per type with frame-specific budget overrides (REPLACE semantics).
3. **`query_text`** = space-joined `topic_keywords` if any, else raw input (`intent.py:192-193`). Topic keywords are `list(set(...))[:10]` — **set-order nondeterministic** (§7-B-cog-D).

### 6.2 `ContextEngine.build` (`context.py:125`)
Budget resolved via `ContextBudget.for_frame(...).apply_overrides(plan.budget_overrides)` (REPLACE). Sections appended in priority order, each independently token-budgeted. Always-on (no search): date/time, identity, anti-hallucination, F079 procedure catalog (flag-gated), epistemic routing (flag-gated), user-profile Tier-1 facts (`list_facts_by_category` filtered by `text_overlap ≥ 0.6` against the identity prompt), active censors, frame description, working memory.

**Per-type semantic recall** (each gated by `budget.<type> > 0 and type not in skip_types`):
- **Decisions** (`:528-568`): `Brain.query` → staleness → `_enforce_diversity("category", max 3)` → relevance filter → format. **No conversation dedup, no near-dup dedup** (§7-B-cog-C).
- **Facts** (`:570-628`): `Heart.search_facts(exclude TIER1)` → recency resolve (Gap-2, flag-gated) → staleness → frame boost → diversity → dedup → usage boost → relevance filter.
- **Procedures** (`:630-796`): dual-track (Critic-recommended by name + embedding search), capped at `critic_slots + embedding_slots`.
- **Temporal "Recent Conversations"** (`:798-829`): `list_episodes(limit=5, hours=48)`, gated **only** by `temporal_context_enabled` — not by budget or `skip_types` (§7-B-cog-G).
- **Episodes** (`:831-877`): `Heart.search_episodes` → recency → frame boost → diversity → dedup → usage boost → relevance filter.

`budget.total` is used **only** for a fill-ratio log line (`:886-892`) — it is **never enforced**; per-section budgets can sum above it (§7-B-cog-F).

### 6.3 Scoring primitives (and their ordering hazard)
- `_apply_staleness_penalty` (`:1009-1035`): `score × max(0.5^(age/half_life(30d)), 0.3)`; exempts `{rule, preference, technical, concept, person}`; **no re-sort**.
- `apply_frame_boost` (`search.py:399-435`): same-frame ×1.3, censor Jaccard ×(1+0.2j), clamped at 1.0 — then **sorts by boost factor, not score**.
- `_apply_usage_boost` (`context.py:945-962`): multiplies by usage boost — then **sorts by boost factor, not score**.
- `_apply_relevance_filter` (`:964-1007`): keep top `min_k`, cap `max_k`, cut at first `i≥min_k` where `score < prev_score × drop_ratio` — **assumes descending-by-score order.** `drop_ratio = relevance_drop_ratio = 0.5` in code (`config.py:163`).

The hazard (§7-B-cog-B / §7-B-hs-6): the boost steps re-order by *boost factor*, then the relevance filter's gap detector runs on that non-monotonic list — so it cuts at the wrong index.

### 6.4 Downstream-exclusion bookkeeping & path divergence
`recalled_ids` has exactly four keys (decision/fact/procedure/episode) — **no `chunk`** (`:170-175`). These feed F012 procedure reinforcement, the UsageTracker's "was this referenced" char-overlap scoring, and F071 exclusion. The IDs are collected **after** the relevance filter but **before** `_truncate_to_budget` — so items truncated out of the prompt still carry IDs and get scored as "retrieved but unused" (§7-B-cog-A).

The cognitive `_resolve_recency` (`context.py:1166`) **claims to mirror** the recall_deep resolver but has drifted: it keys conflict on the `superseded_by` FK link (vs `contradicts` edge), uses a weaker `if older.recency_status != "current"` stickiness (vs sticky `_mark`), and mutates the live ORM object (vs rebuilding frozen dataclasses). The two surfaces can resolve the same conflicting-fact pair differently.

---

## 7. Bug & Issue Register

Severity: **P1** = corrupts production ranking / drops relevant results; **P2** = degrades correctness or a whole feature; **P3** = minor / dead code / observability.
Reachability: **LIVE** (active in prod), **LATENT** (real but behind a prod-off flag like CE/MMR), **INERT** (flag off + no consumer), **UNREACHABLE** (runtime gate never met), **DEAD** (no consumer in any config).
✓ = personally verified against source this session; ○ = agent-reported with `path:line` (auditable).

### 7.1 Headline — cross-type score-space incoherence

**B1 — Censors enter the ranked pool at a hard floor ≥0.7 and displace mid-ranked facts. [P1, LIVE] ✓**
`censors.py:387,400` (floor 0.7) → `heart.py:1163` (`_to_recall_result` emits censor) → `heart.py:983-988,1110-1112` (verbatim score, global sort, `[:limit]` cut); reachable because `search_all` default includes `"censor"` (`retrieval_pipeline.py:340-341`). Censor scores are raw cosine `≥0.7`; RRF-normalized facts exceed 0.7 only at rank 0–1. So every censor structurally outranks all but the top one or two facts, and the `[:limit]` truncation inside `heart.recall` drops the displaced facts before the pipeline sees them. *(Calibrated: censors do not "crowd the entire top" — the #1 fact can reach ~0.92 — but they outrank the long tail of facts regardless of true query relevance.)*

**B2 — Procedure scores exceed the `[0,1]` band, biasing cross-type ranking. [P2, LIVE] ✓**
`procedures.py:446-457`: `final = hybrid × (1 + boost)`. With `procedure_utility_boost` on (default), an effective procedure at RRF 0.9 + boost 0.4 emerges at **1.26**, above every other type's ceiling. Effective procedures systematically outrank equally/more relevant facts and episodes. Bounded by α/β so P2.

**B3 — Embedding-failure leg returns un-normalized `ts_rank_cd` (~0.06), collapsing a whole type. [P2, LIVE-on-failure] ○**
`search.py:220-222` (mirror `facts.py:1368-1369`): on `embedding=None`, returns `keyword_results[:limit]` raw (~0.05–0.08), not RRF-normalized. A single transient OpenAI hiccup on one of the 3–4 sequential `embed()` calls deterministically demotes that entire type ~10–20× in score space for the whole recall. Logged only as a per-leg warning.

**B4 — The pipeline's `rerank_by_score` sort merges incompatible score scales. [P1, LIVE] ✓**
`retrieval_pipeline.py:277-278`. In prod (chunks on) the whole cross-leg pool — RRF facts/episodes `[0,1]`, boosted procedures `>1.0`, floored censors `[0.7,1.0]`, graph items `~0.35–0.63`, raw-cosine chunks `[0,1]` — is sorted on one axis. The sort is not measuring relevance across types; a hard floor and an unbounded boost break monotonicity-with-relevance between types. *(This, not "graph invisible at position 11+", is the prod-live form of the bug — the position-11+ framing is the `config.py`-default behavior, documented in the code comment at `:261-276`, which prod opts out of by enabling chunks.)*

### 7.2 Cognitive (every-turn) path

**B-cog-A — Recalled IDs collected pre-truncation → spurious "not referenced" usage penalty. [P2, LIVE every-turn] ✓**
`context.py:608-613` (collect) vs `:617` (`_truncate_to_budget`). IDs/content are recorded from the post-filter list; truncation then drops the lowest-priority rendered lines. The dropped items keep their IDs and score `was_referenced=False` in the usage tracker (`layer.py:1077-1094`) — corrupting the feedback loop that drives `_apply_usage_boost`. Same defect class the code already fixed at the *dedup* boundary (comment at `:605-607`), reintroduced at the *truncation* boundary.

**B-cog-B — Relevance gap-filter runs on a boost-sorted (non-monotonic) list. [P2, LIVE every-turn] ✓**
`search.py:434` + `context.py:961` sort by **boost factor**; `_apply_relevance_filter` (`context.py:992-1005`) then assumes descending-by-score. A boosted-low-relevance item floats above an unboosted-high-relevance one, so the gap cut fires early (drops good items) or never (floods context). Compounded by `min(score×boost, 1.0)` clamping (`search.py:430`).

**B-cog-C — Decisions get no conversation dedup; `_dedup_decisions` is dead code. [P2/P3, LIVE] ✓**
The decisions leg runs only `_enforce_diversity("category", max 3)` (`context.py:541`) — no `_apply_dedup`. `_dedup_decisions` (`context.py:1415`) is **defined but never called** (single grep hit, the definition). A just-restated decision is re-injected, wasting budget.

**B-cog-D — `query_text` is a set-ordered keyword bag; nondeterministic + lossy. [P2, LIVE every-turn] ✓**
`intent.py:138` `list(set(...))[:10]` (set iteration order is nondeterministic) → `:192-193` joins them → overrides the natural-language `_default_query` (`context.py:530,573,695,835`). The embedding/keyword query loses phrase structure and word frequency, and **retrieval is irreproducible run-to-run** for the same input.

**B-cog-E — Two recency resolvers have drifted. [P2, LIVE] ○**
`context.py:1166` (cognitive) vs `retrieval_pipeline.py:1081` (recall_deep) differ in conflict signal (`superseded_by` link vs `contradicts` edge), stickiness, and mutation style. The same fact pair can be tagged `current` in one path and `superseded` in the other.

**B-cog-F — `budget.total` is advisory; per-section budgets sum above it. [P3, LIVE] ○**
`context.py:886-892`. For the `task` frame the section defaults sum well above `total`. The nominal frame budget is not a real ceiling.

**B-cog-G — Temporal "Recent Conversations" tier bypasses budget + skip gating + tracking. [P3, LIVE] ○**
`context.py:798-829`, gated only by `temporal_context_enabled`. Fires even on greeting/short turns that zeroed episodes; its IDs never enter `recalled_ids` (invisible to usage tracker + F071).

### 7.3 Graph layer

**B-graph-5 — Path A enabled in prod, but co-occurrence edges it would traverse are write-amplified with no semantic discount. [P2, prod-LIVE] ○**
`HEART_GRAPH_ALL_TYPES_ENABLED=true` activates Stage 2b, but with `graph_neighbor_seed_score_enabled=False` (prod) neighbor scores fall to `edge_weight × 0.7` ≤ ~0.70 — below the top RRF facts, so they rarely surface even when computed. Meanwhile `co_occurred`/`co_mention` edges (default-on writers) escape the inferred-edge penalty (`_f065_provenance_penalty` only penalizes `method=="inferred"`, `:929-930`) and are excluded from the density count and SA traversal — write amplification feeding a read path that mostly can't rank them.

**B-graph-6 — Adjacency boost runs before graph-expanded items are appended; can't boost the items it targets. [P2, prod-LIVE] ✓**
`retrieval_pipeline.py:246` (boost) precedes `:249` (`extend(graph_expanded)`). The boost candidate set excludes the brain-side graph-expanded neighbors entirely, and its internal re-sort (`:791`) is overwritten by the later `rerank_by_score` sort. The feature meant to reward graph-connected clusters omits half the graph-connected nodes, and on skewed-degree graphs `boost = 1 + α·(d/max_deg)` dilutes cluster members toward ~1.00 while only the hub gets the full 1.15× (degree-normalization inversion).

**B-graph-7 — Spreading-activation results carry placeholder descriptions. [P3, UNREACHABLE] ○**
`retrieval_pipeline.py:610`: SA `NeighborResult.description = f"[{ntype}] {uuid[:8]}"` — never resolved to node content (unlike `brain._neighbors`). Latent only because the density gate keeps SA from firing.

**B-graph-8 — Density check recomputes a full-table aggregate on every recall. [P2, LIVE] ○**
`retrieval_pipeline.py:571-577` + `spreading_activation.py:30-47`: `compute_graph_density` runs a full aggregate over `brain.graph_edges` per `recall_deep` (named `cached_density` but nothing caches it), then SA opens a second session — purely to almost always return "density < 3.0, don't spread."

**B-graph-9 — Duplicate decisions across Stage 2 and Stage 4 (independent `seen` sets). [P2, LIVE] ○**
`retrieval_pipeline.py:412` vs `:560`: a decision reached as a cross-type neighbor of a fact (Stage 2) is not in Stage 4's `seen_ids`, so an independent 1-hop reach appends it again — once `stage_origin="heart_graph"`, once `"brain_graph"` — double-counting it in contradiction/adjacency sets.

### 7.4 Pipeline plumbing & observability

**B5 — `PipelineStats.ce_reranked`/`mmr_applied` hardcoded `False`. [P2, LIVE observability] ✓**
`retrieval_pipeline.py:296-297`. The pipeline cannot read back whether CE/MMR fired inside `heart.recall` (that state is local to `_recall` and discarded). Every eval/report sees `False` regardless — a known root cause of "CE invisible / graph_expansion_used=0" eval confusion.

**B6 — Contradiction `all_ids` excludes facts/chunks → fact-vs-fact contradiction unreachable. [P2, LIVE] ○**
`retrieval_pipeline.py:662-666`. `all_ids` = decisions + graph_expanded only. No `contradicts` edge between two facts is ever discovered, so `_attach_contradictions` never populates `fact.contradicts`, and the recency resolver's strong (contradicts-edge) signal is dead for facts — only the difflib-0.55 fallback fires.

**B7 — No global top-K; `limit` is per-leg; result count over-returns. [P3, LIVE] ✓**
`retrieval_pipeline.py:218-309` has no final `[:limit]`. The returned list is the sum across legs; `recall_deep`'s "limit" docstring (`tools.py:626`) is wrong. (The model does not necessarily see all of it — tool-result pruning `NOUS_TOOL_SOFT_TRIM_*` applies downstream.)

**B8 — `_attach_contradictions` is one-directional. [P3, LIVE] ○**
`retrieval_pipeline.py:1193-1206`: only the source result gets the link; the target is never marked. Masked for the resolver (which ORs both directions) but any other consumer sees asymmetric links.

**B9 — `_resolve_recency_conflicts` reads `recency_date` before it is set. [P3, harmless] ○**
`retrieval_pipeline.py:1153` passes `metadata.get("recency_date","")` (always `""`); the label is recomputed at `:1169`. Dead argument, correct output.

### 7.5 Hybrid-search internals

**B-hs-8 — Query embedded 3–4× per recall. [P3, LIVE perf] ✓**
`facts.py:1227`, `episodes.py:505`, `procedures.py:378`, `censors.py:364` each independently `embed(query)`. 4× latency/cost and 4× the surface for the B3 transient-failure demotion. A single shared embed would fix both.

**B-hs-4 — Tier-1 `list_by_category` facts hardcode `score=1.0`. [P2, context-path] ○**
`facts.py:1178`. If these ever rank against hybrid-scored items they tie/beat every real hit. Primary consumer is a separate Tier-1 section, but the DTO is shared.

**B-hs-10 — `_search_all` (inactive-fact path) forks the `hybrid_search` SQL. [P3, drift risk] ○**
`facts.py:1309-1404` reimplements vector+keyword+RRF inline, ignores `hybrid_search_keyword_enabled`, and won't receive fixes (e.g. B3 normalization) made to the canonical searcher.

### 7.6 Rank modifiers (mostly latent — CE/MMR off in prod)

**B-rm-1 — Date-aware boost is fully dead code. [P3, DEAD]** — §5.5.
**B-rm-3 — CE sigmoid head vs hybrid tail are incomparable after `return head+tail`. [P2, LATENT]** `reranker.py:152,167`. Only bites if CE is re-enabled with a subsequent re-sort.
**B-rm-8 — Query-expansion "per hour" budget is `monotonic()//3600` (uptime-aligned, resets on restart). [P3, LIVE]** `query_expansion.py:447`.
**B-rm-7 — Query-expansion cache-write failure desyncs concurrent identical queries (leader expands, followers get `[query]`). [P3, LIVE]** `query_expansion.py:193-207,525-526`.

---

## 8. Synthesis & Conclusions

### 8.1 The core finding
Nous's retrieval is, at the engine level, a competent hybrid search: per-type, the RRF fusion of a pgvector cosine leg and a `ts_rank_cd` keyword leg (`search.py:76-117`) is normalized to a clean `[0,1]` and is reproducible. **The defect is not in any single type's scorer — it is in the merge.** `heart._recall` (`heart.py:983-988,1110-1112`) and the pipeline's `rerank_by_score` sort (`retrieval_pipeline.py:277-278`) both treat four different numeric spaces as one:

- normalized RRF `[0,1]` (facts, episodes),
- boosted RRF `>1.0` (procedures),
- raw cosine hard-floored `[0.7,1.0]` (censors),
- raw `ts_rank_cd` `~0.06` (any embed-failure leg).

A **hard floor** and an **unbounded boost** are the two operations that break the monotonic relationship between score and relevance *across types*. The cross-encoder and MMR — the only two stages that would re-base everything into one comparable space — are precisely the two stages disabled in production. So the production ranking is a raw sort over incoherent scales, and the `[:limit]` cut inside `heart.recall` *drops* the displaced items before any downstream stage can recover them.

### 8.2 The second theme — gated, drifted, and unobservable machinery
A large surface area of the retrieval system either does not run or does not affect ranking:
- **Dead:** date-aware boost (no consumer, §5.5).
- **Unreachable:** spreading activation (density gate ≥ 3.0 vs a degree-0–2 graph, §4.2).
- **Self-defeating:** adjacency boost runs before its targets are in the candidate set (B-graph-6); boost-sort precedes a gap filter that assumes score-sort (B-cog-B); recalled IDs are taken before truncation (B-cog-A).
- **Unobservable:** the pipeline reports CE/MMR as always-False (B5), so evals have been measuring a pipeline whose actual modifier state they cannot see.
- **Drifted:** two recency resolvers and two relevance pipelines (cognitive vs recall_deep) re-implement the same intent differently and can disagree (B-cog-E, §6.4).

The practical consequence is that the every-turn cognitive path — the retrieval that actually feeds the model by default — is *weaker* than the `recall_deep` tool (no graph, no contradiction surfacing) **and** carries its own ranking hazards (B-cog-A/B/C/D).

### 8.3 Prioritized remediation (highest leverage first)
1. **Normalize before merge (fixes B1, B2, B3, B4).** Before the cross-type sort in `heart._recall`, map every type's score into one comparable space — e.g. per-type min-max or rank-normalization, or route everything through the cross-encoder (turn CE on) so a single relevance model produces the final order. This is the single change that addresses the headline P1s. Censors arguably should not compete in the ranked pool at all (they have a separate "Active Guidance" surface) — excluding `censor` from `heart_types` in `recall_deep` is a one-line mitigation.
2. **Re-sort after every score mutation (fixes B-cog-B, B-cog-A ordering).** The boost/staleness/usage steps must either re-sort by *score* (not boost factor) or the relevance gap-filter must sort its own input. Collect recalled IDs *after* truncation.
3. **Make observability honest (fixes B5).** Thread the real `ce_reordered`/`mmr_active` flags out of `heart._recall` into `PipelineStats`. Without this, no eval of items 1–2 can be trusted.
4. **Delete or wire the dead paths (B-rm-1, B-cog-C, B-graph-7).** Remove date-aware boost and `_dedup_decisions`, or wire them; resolve SA placeholder descriptions if SA is ever ungated.
5. **Unify the two recency resolvers and the two relevance pipelines (B-cog-E).** One implementation, two callers — eliminate the drift surface.

### 8.4 Confidence & method note
The five load-bearing P1/P2 claims (censor floor + emission into `merged`, the cross-type sort, the chunks→`rerank_by_score` wiring, the assembly/exclusion order, the `PipelineStats` hardcode, the dead `_dedup_decisions`, the set-order `query_text`, the pre-truncation ID collection) were **personally re-read against source this session** (marked ✓). Remaining P2/P3 items are agent-reported with `path:line` anchors (marked ○) and are individually auditable. Reachability verdicts are assigned against `config.py` defaults and the `.env.prod-snapshot` overlay (§1.2); where a finding's severity depends on a flag, that dependency is stated inline.

---

*Appendix — files of record:* `nous/api/retrieval_pipeline.py`, `nous/api/tools.py`, `nous/heart/heart.py`, `nous/heart/search.py`, `nous/heart/{facts,episodes,procedures,censors,embeddings,query_expansion,reranker}.py`, `nous/brain/{brain,spreading_activation,graph_linker}.py`, `nous/cognitive/{layer,context,intent,frames,dedup}.py`, `sql/init.sql`, `sql/migrations/{016,047,051,055}*`, `nous/config.py`, `.env.prod-snapshot` (deployment overlay only).
