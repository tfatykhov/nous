# Brain Organ Deep Audit — 2026-06-09

**Scope:** `nous/brain/*` at HEAD (post PR #495 / F080 §14). Code-only — every claim verified against function bodies. Reachability verdicts checked against both `nous/config.py` defaults and the prod overlay `.env.prod-snapshot`.

**Verdict tags:**
- **LIVE** — fires under prod config as deployed
- **LATENT** — only fires if a flag/condition flips
- **INERT** — computed but has no consumer (or consumer never invokes it)
- **DEAD** — unreachable code

Prod overlay facts used throughout: `NOUS_CE_BACKFILL_ENABLED=true` (min_content_chars=34), `NOUS_COOCCURRENCE_LINKING_ENABLED=TRUE`, `NOUS_HEART_GRAPH_ALL_TYPES_ENABLED=true`, `NOUS_GRAPH_ADJACENCY_BOOST_ENABLED=true`, `NOUS_GRAPH_NEIGHBOR_SEED_SCORE_ENABLED=true`, `NOUS_GRAPH_HUB_AUTOSURFACE_ENABLED=true`, `NOUS_CROSS_ENCODER_ENABLED=false`, `NOUS_SLEEP_ENABLED=true`, spreading activation left at default `"auto"` (density gate 3.0), `NOUS_CONFIDENCE_CALIBRATION_FACTOR=0.7627`, `graph_inferred_edge_penalty` unset (default 1.0 = no-op).

---

## 1. How the subsystem actually works (from code)

### 1.1 Decision lifecycle (`brain.py`)
- `record()` → `_record()` (brain.py:331): noise pre-filter (`_is_noise_decision`, hard `ValueError`), quality score (`quality.py` — pure metadata completeness, max 1.0), embedding of `description+context+pattern` via `EmbeddingProvider`, F058 confidence scaling (`confidence = raw * 0.7627`, raw preserved in `confidence_raw`), ORM cascade insert (tags/reasons/bridge), DB audit event row, **in-process bus emit** (`decision_recorded`, brain.py:406-412), then `_auto_link` inside a SAVEPOINT (cosine ≥ `auto_link_threshold` 0.85, max 3 `related_to` decision↔decision edges, lower-UUID-normalized, ON CONFLICT by columns — the historical wrong-constraint-name no-op is fixed at HEAD).
- `update()` (brain.py:457): rewrites fields, recalibrates confidence, recomputes quality, **re-embeds**, re-extracts bridge. Does **not** re-run auto-link or touch existing edges.
- `delete()` (brain.py:565): NULLs heart FK references, explicitly deletes `brain.graph_edges` rows for the decision (audit D1 fix, migration 060 companion), then deletes the row. Not agent-scoped.
- `review()` sets outcome/reviewed_at; consumed by `CalibrationEngine.compute()` (SQL-only Brier/accuracy/per-category/per-reason) which is served live via `/calibration` and `/status`. `generate_calibration_snapshot()` would persist a `CalibrationSnapshot` — **no caller exists anywhere** (see BR-20).
- `query()` (brain.py:677): hybrid search — pgvector cosine leg + `ts_rank_cd` keyword leg fused by `_rrf_merge` (normalized to [0,1] by the 1/k theoretical max), abandoned decisions (`failure` + conf 0.0) excluded, optional `bridge_side` ILIKE join.
- `check()` delegates to `GuardrailEngine` (CEL via celpy in a 2-worker thread pool, 0.1s timeout, fail-closed for block/absolute). **Zero runtime callers** (see BR-7).

### 1.2 Graph writers
Five writer families, all into `brain.graph_edges` (UNIQUE `(source_id, target_id, relation)`, polymorphic types, `extraction_method` provenance via `edge_provenance.classify`):
1. **`Brain._auto_link`** at record time (decision↔decision `related_to`, inferred tier).
2. **Event-bus linkers** (`graph_linker.py`): `FactGraphLinker` handler → `link_fact_to_decisions` (common-template re-embed, `evidence_for`) + `link_fact_to_facts` (`related_to`); `DecisionGraphLinker` handler → reverse fact→decision / episode→decision on `decision_recorded`; `link_episode_deterministic` (structural `discussed_in` / `extracted_from`) from episode summarizer / sleep.
3. **Sleep densifier** (`graph_densifier.py`): `run_backfill_cycle()` from `sleep_handler` — orphan backfill per entity type (facts/decisions/episodes/procedures: hybrid-search candidates → optional F043 CE rerank (`backfill_rerank.py`) → cosine threshold gate (`_get_threshold` routes to relaxed CE-mode thresholds whenever `ce_backfill_enabled` is set) → `GraphLinker.create_edge` with relation-weight multipliers); F070 chunk backfill (structural `part_of`, same-episode `summarized_by`, intra-episode chunk adjacency); F075 `happened_before` SQL chain; F076 co-mention (deterministic entity extraction in `entity_extraction.py`, no cosine gate, own provenance tier); Gap-1 co-occurrence (shared `source_episode_id`, `co_occurred`, weight 0.9). `discover_clusters()` bridges disconnected components weekly (in-memory rate limit).
4. **F070.1 cross-episode chunk methods** — defined but only invoked by `scripts/backfill_f070_chunks.py`, never from the sleep cycle (see BR-8).
5. **Heart-side writers** (facts.py contradiction path — out of scope).

### 1.3 Graph readers
- `Brain.neighbors()` (brain.py:1179): union of both edge directions, SQL window-function dedup (max-weight edge per neighbor, non-inferred tie-break), optional `neighbor_type` pushdown, per-type content resolution (decision/fact/episode/chunk/procedure). Procedures filtered to `active=true` (F080); **facts/episodes/chunks are not active-filtered** (BR-1). Consumed by `retrieval_pipeline` Stage 2 (decision expansion), Stage 2b Path A (prod-ON), and `cognitive/context.py`.
- `spreading_activation.py`: density (`edges/unique-nodes`, excludes `co_mention` only) gates a recursive CTE (depth ≤ 2, decay 0.5, excludes `contradicts` + `co_mention`). Consumer (`retrieval_pipeline.py:602-634`) wraps results in `NeighborResult` with **placeholder descriptions** (BR-6).
- `top_hubs()` (brain.py:1441): undirected degree ranking + provenance breakdown; consumed by the `recall_hubs` tool and the F065 hub autosurface in `cognitive/layer.py` (prod-ON) with `hub_snapshots.py` for rank-shift detection.

### 1.4 Embeddings
`EmbeddingProvider` (embeddings.py): httpx → OpenAI, 3 retries on 5xx/some network errors, PR #495 LRU cache (model+dims+sha256 key, packed float32, defensive copy on read, batch-aware with misalignment guard). Prod model `text-embedding-3-large` truncated to 1536 dims via the `dimensions` param — consistent with `vector(1536)` columns.

---

## 2. Findings register

### P1

#### BR-1 — `Brain._neighbors` surfaces soft-deleted / superseded facts and episodes as graph neighbors
- **Severity:** P1 **Reachability:** LIVE
- **Where:** `nous/brain/brain.py:1316-1342` (fact/episode/chunk resolution blocks)
- **Description:** F080 added an `active=true` filter for procedure neighbors only (brain.py:1232-1235, 1349-1359). Fact and episode neighbors are resolved with no `active` filter: `select(Fact.id, Fact.content, ...).where(Fact.id.in_(...))` and the episode equivalent. Facts are soft-deleted/superseded by setting `active=false` (+ `superseded_by`); their graph edges remain. Any superseded or soft-deleted fact reachable by an edge re-enters retrieval through: (a) Path A Stage 2b (`heart_graph_all_types_enabled=true` in prod), (b) decision 1-hop expansion (`graph_recall_enabled` default true — `neighbors()` without `neighbor_type` returns fact/episode neighbors of decisions), and (c) `cognitive/context.py:1379`. The supersession machinery (F027, recency resolver) is thereby bypassed: the graph resurrects exactly the stale memory the write path retired. The SQL pushdown rationale that justified the procedure filter (return N *valid* rows before LIMIT) applies identically to facts/episodes.
- **Evidence:** `Fact.active` and `Episode.active` exist (`storage/models.py:496, 390`); `_ENTITY_CONFIG` itself uses `t.active = true` for orphan selection — read side is the only place the filter is missing.
- **Fix:** mirror the F080 procedure pattern: pushdown `active=true` subselect filters for fact and episode neighbor types (both pre-LIMIT in the edge query and in content resolution); decide explicitly whether superseded facts should be reachable with a `[superseded]` marker instead of silently.

### P2

#### BR-2 — CE backfill min-score gate compares non-CE scores against the sigmoid floor in every fail-open path; lone cross-type candidates are always dropped
- **Severity:** P2 **Reachability:** LIVE (`NOUS_CE_BACKFILL_ENABLED=true` in prod, sleep enabled)
- **Where:** `nous/brain/backfill_rerank.py:187-201`; `nous/heart/reranker.py:97-98` (short-circuits); `nous/brain/graph_densifier.py:379-399` (synthetic 0.0 scores)
- **Description:** `_backfill_cross_type` feeds candidates in as `(cid, 0.0)` synthetic rows. `cross_encoder_rerank` returns candidates **unchanged** when `len(candidates) <= 1` or on ANY internal exception (model load/predict failure — documented "never raises"). `ce_rerank_backfill_candidates` then walks the head and breaks on the first `score < ce_backfill_min_score` (0.30):
  - **Lone cross-type candidate:** score stays 0.0 → `0.0 < 0.30` → `kept=[]` → the candidate is unconditionally dropped, zero cross-type edges for that orphan. The docstring claims "the downstream cosine gate is the correctness floor for that case" — it never gets there. Common case: 5-vector + 5-keyword candidates frequently collapse to 1 after the content-map and min-chars (prod 34) filters.
  - **CE runtime failure (cross-type):** all synthetic-0.0 candidates dropped → cross-type backfill silently produces zero edges while logs show nothing above DEBUG. The upstream fail-open becomes a downstream fail-closed.
  - **CE runtime failure (same-type):** surviving scores are normalized **RRF rank scores** compared against a **sigmoid-space** floor — the recurring numeric-space-mismatch class; tail candidates (RRF < 0.30) silently pruned with no CE judgment.
  Once same-type edges exist, the node is no longer an orphan, so the missing cross-type edges are never retried — permanent loss of fact→decision / decision→fact connectivity that prod's Path A consumes.
- **Fix:** in `ce_rerank_backfill_candidates`, detect the pass-through cases (`len(wrapped) == 1`, or reranker returned with scores untouched) and skip the `min_score` gate (let the cosine gate decide), or give `cross_encoder_rerank` an explicit failure signal instead of "unchanged list".

#### BR-3 — `find_orphans` counts `co_occurred` edges as connectivity, permanently exempting such facts from F040 backfill
- **Severity:** P2 **Reachability:** LIVE (`NOUS_COOCCURRENCE_LINKING_ENABLED=TRUE` in prod)
- **Where:** `nous/brain/graph_densifier.py:157-171`
- **Description:** The orphan probe excludes only `extraction_method IS DISTINCT FROM 'co_mention'`, with an explicit comment explaining why a builder-only edge must not make a node look non-orphan ("counting it here would permanently skip such facts from later backfill cycles"). Gap-1 `co_occurred` edges (extraction_method `'co_occurrence'`) have exactly the same property — they link fact↔fact within one episode and provide none of the cross-type connectivity F040 exists to build — but they are **not** excluded. With co-occurrence ON in prod, every fact that gains a `co_occurred` sibling edge before its first backfill cycle is permanently skipped: no fact↔fact cosine edges, no fact→decision edges, ever.
- **Fix:** `AND e.extraction_method NOT IN ('co_mention', 'co_occurrence')` (keeping the `IS DISTINCT FROM`-style NULL semantics).

#### BR-4 — `decision_recorded` bus event emitted before commit → reverse graph linker races an uncommitted row and silently no-ops
- **Severity:** P2 **Reachability:** LIVE
- **Where:** `nous/brain/brain.py:404-412` (emit), `nous/handlers/decision_graph_linker.py:72-75` (read)
- **Description:** `_record` emits the bus event right after `flush()`, **before** the surrounding transaction commits. `EventBus.emit` enqueues immediately and the processing loop dispatches concurrently. `DecisionGraphLinker.handle` opens a **new session** and does `await self._brain.get(decision_id)`; if it wins the race against the committing transaction (the window includes `_auto_link`'s vector search + the eager re-fetch + commit — tens of ms of DB roundtrips), `get()` returns None and the handler returns silently. Reverse fact→decision / episode→decision linking is skipped for that decision. This is **not** self-healing: if `_auto_link` created any decision↔decision edge, the decision is not an orphan and the F040 backfill never revisits it.
- **Fix:** emit the bus event after commit (e.g., from `record()`'s session-None branch post-commit, or a short retry/delay in the handler before giving up).

#### BR-5 — Deliberation decisions are graph-linked on the "Plan: …" stub; `update()` never re-links, leaving edges anchored to discarded text
- **Severity:** P2 **Reachability:** LIVE (deliberation fires for decision/debug frames)
- **Where:** `nous/brain/brain.py:414-429` (auto_link at record), `nous/brain/brain.py:457-542` (`_update` — re-embeds, no edge maintenance); producer: `nous/cognitive/deliberation.py:108-125, 171-179`
- **Description:** `DeliberationEngine.start` records a stub decision (`description="Plan: {task}"`, confidence 0.5). At that moment `_record` runs `_auto_link` (cosine 0.85 against the stub embedding) and emits `decision_recorded`, so `DecisionGraphLinker` also links facts/episodes against the **stub** text. `finalize` later rewrites description/context/pattern and regenerates the embedding via `update()` — which performs **no** edge maintenance: stale-similarity edges persist and no links are computed for the final text. All shared-prefix "Plan: …" stubs systematically inflate pairwise cosine, so auto-link can join unrelated deliberation decisions; the eventual decision content is never what the graph encodes. Decisions recorded via the `record_decision` tool are unaffected.
- **Fix:** either defer linking until finalize (skip `_auto_link` + bus emit for pre-registered stubs, trigger both from `update()`), or have `_update` drop `auto_linked=true` edges and re-run `_auto_link` when description changes.

#### BR-6 — Spreading-activation results reach recall output as content-free placeholders, with seed echo inflation and no cycle control
- **Severity:** P2 **Reachability:** LATENT→LIVE (auto-gated: fires whenever non-co_mention graph density ≥ 3.0; prod runs default `"auto"`)
- **Where:** `nous/brain/spreading_activation.py:97-131`; consumer `nous/api/retrieval_pipeline.py:618-633`
- **Description:** Three compounding defects on the SA path:
  1. **Placeholder output:** SA returns `(id, type, activation)` only; the consumer constructs `NeighborResult(description=f"[{ntype}] {str(nid)[:8]}")` and ships it through `_graph_expanded_to_pipeline` into the recall text. When the density gate flips, decision-graph expansion switches from 1-hop neighbors with real descriptions to **id stubs the LLM cannot use** — strictly worse than the fallback it replaces.
  2. **Echo/cycle re-traversal:** the recursive CTE joins `(e.source_id = a.id OR e.target_id = a.id)` with no visited set — at depth 2 every edge is re-walked back to its origin, so each seed re-activates itself (`SUM` includes the echo) and every neighbor is counted once per path. With dense co_occurred/co_mention-era graphs this both distorts ranking and multiplies row counts (bounded only by `edges^depth`).
  3. **No active filter:** inactive facts/episodes activate like any node (compare BR-1).
  Additionally `compute_graph_density` (full `graph_edges` aggregation) runs **per recall_deep call** — the `cached_density` parameter name documents an intent no caller implements.
- **Fix:** resolve descriptions for activated nodes (reuse `_neighbors`' per-type resolution); add `depth`-indexed visited tracking or at minimum exclude the immediate back-edge; cache density with a TTL.

#### BR-7 — The decision guardrail layer (`Brain.check` / `GuardrailEngine`) has zero runtime callers
- **Severity:** P2 **Reachability:** INERT (severed wire)
- **Where:** `nous/brain/brain.py:831-879`, `nous/brain/guardrails.py` (entire module)
- **Description:** Exhaustive grep over `nous/` finds no call to `brain.check(`, `guardrails.check(`, `GuardrailEngine`, or `validate_expression` outside `nous/brain/`. The deliberation engine mentions guardrails only in a docstring (`deliberation.py:95`); `mcp.py` advertises "thorough analysis with guardrails" in the `nous_decide` description but routes to the normal chat runner. `brain.guardrails` rows (seeded by seed.sql, REST-listed via `/censors`? no — censors are Heart) are never evaluated; `activation_count`/`last_activated` can never change. The architecture docs present guardrails as a live decision gate; at HEAD they gate nothing. Consequence: every guardrail-engine bug below it (BR-15, BR-16) is also inert, and F058's claim that calibrated confidence feeds "all downstream gates (guardrails…)" is vacuous for this gate.
- **Fix:** either wire `Brain.check` into the deliberation/record path (the original 004.1 design) or delete the engine and its table to stop the architecture from lying.

#### BR-8 — F070.1 cross-episode chunk backfill is not wired into any runtime cycle, while prod env tunes its thresholds
- **Severity:** P2 **Reachability:** INERT (script-only)
- **Where:** `nous/brain/graph_densifier.py:972-1037` (`backfill_orphan_chunks_cross_episode`), absent from `run_backfill_cycle` (graph_densifier.py:1069-1167) and from `sleep_handler`
- **Description:** `run_backfill_cycle` invokes facts/decisions/episodes/procedures/chunks/happened_before/co_mention/co_occurrence — but never the F070.1 cross-episode pass. Its only caller is `scripts/backfill_f070_chunks.py` (one-shot manual). Prod `.env` sets `NOUS_GRAPH_THRESHOLD_CHUNK_CHUNK_CROSS=0.75` and `NOUS_GRAPH_THRESHOLD_CHUNK_FACT_CROSS=0.65`, signalling the operator expects this leg to run continuously; new chunks since the last manual script run accumulate with zero cross-episode edges, starving the F070-series retrieval consumers the prod flags enable.
- **Fix:** add the cross-episode pass to `run_backfill_cycle` (with the `attempted`/`exclude_ids` loop the method already supports), or document that it is script-only and remove the prod env expectations.

#### BR-9 — `co_occurred` edges are counted by graph density and traversed by spreading activation, unlike sibling `co_mention` edges
- **Severity:** P2 **Reachability:** LIVE (prod co-occurrence ON; effect conditional on SA flipping)
- **Where:** `nous/brain/spreading_activation.py:30-47` (density excludes co_mention only), `:114-121` (traversal excludes co_mention only)
- **Description:** F076 carefully excludes `co_mention` from density and SA traversal so a default-on builder "must not silently push an agent over the threshold and flip decision retrieval before that rollout is intentional." Gap-1 `co_occurred` edges have the identical profile (default-OFF in code but **ON in prod**, weight 0.9, builder-only until consumer flags flip) and are excluded from neither: every co_occurred pair raises density toward the 3.0 auto-trip AND, once SA is on, weight-0.9 co_occurred edges are traversed for decision retrieval. `build_cooccurrence_edges`' own docstring acknowledges "the adjacency/spreading consumers would let [pairs] reinforce each other." The two sibling builders have contradictory exposure discipline with no recorded rationale.
- **Fix:** extend both `IS DISTINCT FROM 'co_mention'` filters to also exclude `'co_occurrence'` (matching BR-3's fix), or record a decision that co_occurred is an intentional SA participant.

### P3

#### BR-10 — `_get_relation` emits semantically inverted relations for reversed type pairs
- **P3, LIVE.** `nous/brain/graph_densifier.py:94-97`. The fallback `_RELATION_MAP.get((target, source))` reuses the forward relation for a reversed edge: episode-orphan→fact gets `extracted_from` ("episode extracted_from fact"), decision-orphan→fact gets `evidence_for` ("decision is evidence for fact"), procedure-orphan→decision gets `informed_by` (inverted). Consumers treat edges undirected so ranking is unaffected, but relation text shown in neighbors/dashboards/LLM context asserts backwards semantics. Fix: swap source/target when the lookup only matches reversed.

#### BR-11 — CE-mode relaxed thresholds keyed on the flag, not on CE actually running
- **P3, LATENT.** `nous/brain/graph_densifier.py:83-91`. `_get_threshold` routes to relaxed thresholds (e.g. fact-fact 0.55 vs 0.82) whenever `ce_backfill_enabled` is set; `ce_rerank_backfill_candidates` passes candidates through unfiltered when `CROSS_ENCODER_AVAILABLE=False` or `query_text` is empty. A deployment with the flag on but sentence-transformers missing gets relaxed cosine gates with no CE precision pre-filter — the exact combination F045 calibrated against. (Prod currently has the dep installed.) Fix: route on `ce_backfill_enabled and CROSS_ENCODER_AVAILABLE`.

#### BR-12 — Backfill batches run inside one long transaction containing embedding API calls
- **P3, LIVE.** `nous/brain/graph_densifier.py:453-464` (and siblings). Each `backfill_orphan_*` opens one session for the whole batch (prod caps: 200 facts, 200 episodes) and performs OpenAI embed calls per candidate inside it (`_backfill_cross_type` re-embeds every candidate); commit only at the end. One connection is held for minutes; an uncaught exception mid-batch (e.g. `hybrid_search` failure — not wrapped) loses every edge built that cycle; inserted-row locks are held the whole time. The PR #495 LRU mitigates repeat embeds but not first-pass cost. Fix: commit per-orphan (the methods are already idempotent via ON CONFLICT).

#### BR-13 — `EmbeddingProvider._post_with_retry` retry tuple misses common transient httpx errors
- **P3, LIVE.** `nous/brain/embeddings.py:135`. Retries catch `(ConnectError, ReadTimeout, WriteTimeout)` — but not `httpx.ConnectTimeout` (a `TimeoutException`, not `ConnectError`), `httpx.ReadError`, `httpx.RemoteProtocolError`, or `httpx.PoolTimeout`. A connect-timeout (the most common transient failure to a saturated endpoint) bypasses retry entirely and propagates after attempt 1, downgrading records to embedding-less and aborting linker runs. Fix: catch `httpx.TransportError`.

#### BR-14 — `Brain._delete` / `_think` are not agent-scoped
- **P3, LIVE (internal callers only).** `nous/brain/brain.py:565-601, 621-638`. `_delete` runs raw `DELETE FROM brain.decisions WHERE id = :did` plus heart-table NULL-outs and edge deletes with no `agent_id` predicate (contrast `_get_decision_orm`, which is scoped); `_think` inserts a thought for any decision id. Current callers (deliberation, REST detail paths) pass ids obtained from scoped queries, so this is defense-in-depth today — but `delete()` is a public Brain API on a multi-agent-ready schema. Fix: add `AND agent_id = :agent_id` to all four statements.

#### BR-15 — Guardrail CEL evaluation blocks the event loop and can permanently wedge its 2-thread pool
- **P3, INERT (per BR-7).** `nous/brain/guardrails.py:31-34, 158-168`. `future.result(timeout=0.1)` is a synchronous wait on the event loop (≤100 ms per guardrail, serial). On timeout the future is **not** cancelled — the worker thread keeps running celpy; after two stuck evaluations the pool is exhausted, all later submissions queue, every `result(timeout=0.1)` times out, and every block/absolute guardrail evaluates fail-closed (everything blocked) for the life of the process. Fix (if BR-7 is ever rewired): `loop.run_in_executor` + `asyncio.wait_for`, larger/recycling pool.

#### BR-16 — `_jsonb_to_cel` interpolates DB values into CEL source
- **P3, INERT.** `nous/brain/guardrails.py:292`. `f"decision.stakes == '{value}'"` — a stakes value containing a quote produces an uncompilable (or attacker-shaped) CEL expression; compile failure then **fail-closes** block-severity guardrails. Input is operator/agent-written JSONB, so impact is breakage rather than injection. Fix: validate against the stakes enum before interpolation.

#### BR-17 — Reversed-direction duplicate edges across writers
- **P3, LIVE.** `Brain._auto_link` normalizes lower-UUID-as-source (brain.py:1634-1637); `GraphLinker.create_edge` and both densifier paths write orphan→candidate unnormalized; co_mention/co_occurrence canonicalize `a<b`. The UNIQUE constraint is directional, so A→B and B→A `related_to` duplicates between the same pair are possible when different writers touch the same nodes. `_neighbors`' window dedup hides them from that consumer, but degree counts (`top_hubs`, density) and the heart-side adjacency boost count them twice. Fix: normalize direction for all symmetric relations at `create_edge`.

#### BR-18 — `link_fact_to_facts` final gate compares template-vs-raw cosine against a template-calibrated threshold
- **P3, LIVE.** `nous/brain/graph_linker.py:269-293`. The fact-to-decision path re-embeds candidates with the common template and compares template-vs-template similarity to the threshold; the fact-to-fact path gates on `row.similarity` — the **stored raw embedding** vs the new fact's **template** embedding — against `cross_type_same_threshold` (prod 0.75). The asymmetric `"fact: "` prefix systematically deflates similarity, making the effective threshold stricter than configured and inconsistent with its sibling. Fix: either skip the template for same-type (compare raw-vs-raw) or re-embed candidates like the decision path.

#### BR-19 — Dangling edges for non-decision node deletions; placeholders leak into retrieval
- **P3, LATENT.** `nous/brain/brain.py:1396-1398`. `_delete` cleanup covers decisions only. Hard deletes elsewhere (F081 procedure dedup hard-deleted 26 rows; PR #495 `--repair-dialogue` re-creates chunks under new UUIDs; episode cascade deletes chunks) strand edges. `_neighbors` then emits `"[chunk] <uuid>"` / `"[fact] <uuid>"` placeholder descriptions (procedures alone are dropped) into Path-A results. Migration 060 cleaned historical strays once; nothing prevents recurrence. Fix: drop the fallback row for fact/episode/chunk too (mirroring the procedure `continue`), and/or a periodic dangling-edge sweep in the sleep cycle.

#### BR-20 — `discover_clusters`: restart-reset rate limit, full-graph load, and unconfigured chunk thresholds
- **P3, LIVE.** `nous/brain/graph_densifier.py:1456-1596`. (a) The 7-day rate limit lives in `self._last_cluster_discovery` — every process restart re-arms it, so restart-heavy weeks run it per first-sleep. (b) It loads **all** edges into Python for union-find each run (prod graph includes ~37K chunk edges per F070 measurements). (c) Chunk-hub bridges fall through `_get_threshold` to the generic default (0.60 CE-mode) and `("chunk","episode")` bridges get relation `part_of` across unrelated components — structural semantics for a cosine bridge. Fix: persist last-run in DB, cap edge fetch, add chunk keys to the threshold maps.

#### BR-21 — `top_hubs` degree includes co_mention/co_occurred webs and has no deterministic tie-break
- **P3, LIVE (hub autosurface ON in prod).** `nous/brain/brain.py:1453-1466`. Hub ranking counts every edge — F076 excluded co_mention from *density* but not from *hub degree*, so fan-out-capped-at-20 co-mention webs can dominate the top-10 the autosurface narrates. `ORDER BY degree DESC LIMIT :limit` has no secondary sort: equal-degree nodes at the boundary flap in/out across calls, generating repeating entered/left hub-shift notices (each "left" writes a rank-NULL snapshot, then the node can "enter" again next turn). Fix: `ORDER BY degree DESC, node_id` + decide whether builder-tier edges belong in degree.

#### BR-22 — `fetch_candidate_content` F054 decision guard filters description, not context; absent on the cross-type path
- **P3, LIVE.** `nous/brain/backfill_rerank.py:96-108` vs docstring (":69-73") claiming `context.strip()` is measured — the content column for decisions is `t.description` (`_entity_config.py:21`), so the guard measures description length. Additionally `_backfill_cross_type` fetches candidate content itself (graph_densifier.py:366-375) without any `min_decision_chars` guard — the F054 fix only protects the same-type path, while the cross-type fact→decision path (F054's stated motivation, the "~5/9 evidence_for NO/WEAK verdicts") relies on the generic `ce_backfill_min_content_chars` (lowered to 34 in prod). Fix: route the cross-type fetch through `fetch_candidate_content(settings=...)`.

#### BR-23 — `Brain.link` validates relation only after the edge is persisted
- **P3, LATENT (no current callers — see dead-code inventory).** `nous/brain/brain.py:1090-1143`. `_link` inserts + flushes the edge, emits the event, **then** constructs `GraphEdgeInfo` whose `relation: RelationType` Literal excludes `happened_before`/`co_occurred` (valid per the DB CHECK). With a caller-provided session, a pydantic `ValidationError` is raised after the edge exists in the transaction — partial-effect on exception. Fix: validate relation first; add the two newer relations to `RelationType`.

### INFO

- **BR-24 (DEAD wire):** `generate_calibration_snapshot()` (brain.py:996-1031) has no caller anywhere; `brain.calibration_snapshots` is never written and never read (no consumer in rest.py / dashboard_queries.py / handlers). The calibration *report* path is alive (`/calibration`, `/status`); the snapshot/history loop was never closed.
- **BR-25 (DEAD):** `Brain.link()` public method — no runtime callers (MCP/REST/cognitive all absent).
- **BR-26 (DEAD settings):** `spreading_activation_alpha/beta/gamma` (config.py:621-623) referenced nowhere outside config.
- **BR-27 (DEAD branch):** `_backfill_same_type`'s keyword-only weight path (graph_densifier.py:278-280) is unreachable: `find_orphans` default `require_embedding=True` guarantees `orphan_embedding` is non-NULL for every same-type caller.
- **BR-28 (degenerate scoring):** `edge_confidence` (graph_linker.py:48-63) is only ever called with `shared_tags=0, shared_subject=False, temporal_proximity_days=0.0` (graph_densifier.py:421-426) — the multi-signal score collapses to `0.6*sim + 0.15`, and cross-type edge weights therefore live in a compressed [0.15, 0.75] band vs same-type raw-cosine weights [threshold, 1.0], systematically deprioritizing cross-type edges in weight-ordered consumers (`_neighbors` ORDER BY weight).
- **BR-29 (INERT by config):** the whole F065 provenance-penalty machinery (`classify`, `extraction_method` threading through `_neighbors`/`NeighborResult`, `_f065_provenance_penalty`) is behaviorally inert in prod: `graph_inferred_edge_penalty` defaults to 1.0 and prod doesn't override. Intended dark launch — flagged so doc-sync doesn't claim it shapes retrieval.
- **BR-30:** `build_comention_edges`/`build_cooccurrence_edges` report `inserted += len(batch)` / `+= 1` regardless of ON CONFLICT skips — logged/returned counts overstate actual inserts (and cooccurrence's pre-existing-edge probe only sees canonical-direction fact-fact rows of any relation, which is correct, but the counter still counts conflicts).
- **BR-31:** `EmbeddingProvider` reads `NOUS_EMBEDDING_CACHE_SIZE` straight from `os.environ` rather than `Settings` — works (env-prefixed) but bypasses the config layer every other knob uses.
- **BR-32:** `_is_noise_decision` rejects any description starting with a quote character (brain.py:311) — legitimate decisions quoting a term ("'Strict mode' adopted for…") are hard-rejected with ValueError.
- **BR-33:** `hub_snapshots.record_snapshot`/`prune_older_than` swallow exceptions logging at WARN **without** `exc_info` — the failure mode is invisible (same anti-pattern the auto_link fix at brain.py:420-429 documents). `get_latest_top_n` carries unused imports (`desc`, `func`, `_select`) in the outer function.
- **BR-34:** `FactGraphLinker.handle` logs total linking failure at **DEBUG** (fact_graph_linker.py:79) — a permanently broken event-bus linker is silent in prod logs (INFO level).
- **BR-35:** `entity_extraction._is_capitalized` treats ALL-CAPS technical tokens as proper nouns — "REST API", "POSTGRES VECTOR" become co-mention entities; acceptable per the precision-first design but worth knowing as the main noise source for BR-21's hub webs.

---

## 3. Dead-code inventory

| Item | Location | Note |
|---|---|---|
| `Brain.generate_calibration_snapshot` + `CalibrationSnapshot` table | brain.py:996; models.py:341 | never called / never read (BR-24) |
| `Brain.link` | brain.py:1072 | no callers; post-insert validation bug (BR-23/25) |
| `Brain.check` + `GuardrailEngine` (+ `validate_expression`) | brain.py:831; guardrails.py | no callers (BR-7) — functional code, severed wire |
| `spreading_activation_alpha/beta/gamma` | config.py:621-623 | consumed nowhere (BR-26) |
| `_backfill_same_type` keyword-only weight branch | graph_densifier.py:278-280 | unreachable (BR-27) |
| `edge_confidence` parameters `shared_tags`/`shared_subject`/`temporal_proximity_days` | graph_linker.py:48 | only ever called with zeros (BR-28) |
| `backfill_orphan_chunks_cross_episode` (runtime) | graph_densifier.py:972 | script-only; not in any cycle (BR-8) |
| `RerankCandidate.content` round-trip via `text_fn` lambda | backfill_rerank.py | fine, but the `i` in `entity_extraction.py:104` enumerate is unused |

## 4. Improvement opportunities

1. **Active-filter as a shared invariant.** Every graph read path (`_neighbors`, spreading activation, top_hubs labels) should consult a single per-type "is alive" predicate — three F080-style point fixes have now each fixed one consumer for one type.
2. **Edge lifecycle owner.** Writers are spread over 5 families with inconsistent direction normalization, weight spaces (raw cosine vs `edge_confidence` band vs fixed 0.9 vs structural 1.0 vs RRF-proxy), and provenance tags. A single `EdgeWriter` choke point (normalize direction for symmetric relations, validate relation against one enum, enforce weight-space documentation) would eliminate BR-10/17/23/28 as a class.
3. **Density caching.** `compute_graph_density` per recall call is the most obviously avoidable hot-path cost in the organ; a 5-minute TTL cache (or compute-at-sleep and persist) is a one-liner consumer-side.
4. **Backfill checkpointing.** Per-orphan commit + per-orphan try/except in `backfill_orphan_*` makes the sleep cycle resilient and shrinks transactions (BR-12) for free, since all writes are already ON CONFLICT-idempotent.
5. **Fail-open vs fail-closed contract for CE.** `cross_encoder_rerank`'s "return unchanged on error" contract is the root of BR-2; returning an explicit sentinel (or raising into a caller-side fallback) would let every consumer decide its own failure semantics.
6. **Calibration loop closure.** Either wire `generate_calibration_snapshot` into the sleep cycle (giving the dashboard a history series) or remove it; with `confidence_raw` now persisted, the snapshot is the natural place to re-derive `NOUS_CONFIDENCE_CALIBRATION_FACTOR` periodically instead of a frozen 2026-04 constant.
