# Nous Memory Storage & Retrieval — Deep Code Analysis

**Date:** 2026-06-09 · **HEAD:** `31209e5` (post-F080/F081, PRs #491–#494 merged, **not yet deployed to prod**)
**Method:** 5-agent parallel code-only discovery (fact write path · episode/chunk/WM lifecycle · graph writes + sleep · schema/embeddings substrate · retrieval re-verification) + single-author verification of every P1 against source.
**Scope:** The full memory **write** side (never previously audited) + the storage substrate + a re-verification of the 2026-06-08 retrieval whitepaper's register at HEAD.
**Constraint:** Code only. Every claim carries a `path:line` anchor. Reachability verdicts use `config.py` defaults overlaid with `.env.prod-snapshot`. Two deployment states matter throughout: **prod-TODAY** (pre-F080 deployed code) and **prod-at-HEAD-deploy** (what flips on when #491–#494 ship — the new F080/§14/F079 flags are absent from the snapshot, so config defaults govern them).
**Marks:** ✓ = personally re-read against source this session; ○ = agent-reported with auditable `path:line`.

---

## Executive Summary

The 2026-06-08 whitepaper found that the retrieval **read** side merges incompatible score spaces. This audit's headline is the mirror image on the **write** side: **both fact-dedup gates are broken, in opposite directions, and their composition makes write-time contradiction detection effectively dead in production.**

1. **Leg-1 dedup compares a rank-based score against a similarity threshold.** RRF scores encode *rank*, not closeness: the nearest fact scores ≈0.98 at `limit=1` whether it's a near-duplicate or wholly unrelated, so `score > 0.92` is ~always true (S1 ✓). The Haiku tiebreaker is therefore the *only* real dedup gate in prod — and on tiebreaker failure the path fails open **to dedup**, silently dropping every candidate fact during an LLM outage.
2. **Leg-2's prod threshold (0.80) swallows the contradiction/supersession band (0.85–0.95).** `_find_duplicate` → `_confirm` returns before `_find_contradiction` ever runs, so a *conflicting* fact at cosine 0.86 doesn't trigger F027 classification — it **increments `confirmation_count` on the stale fact and is discarded** (S3 ✓). New information reinforces old falsehoods. Dated facts colliding with undated paraphrases are silently de-dated.
3. **The lifecycle layer loses data at four seams:** transcript chunks destroyed by an index collision between F067 and F069 writers (E1 ✓); transcripts capped at 500 chars/turn *at capture*, hollowing out F067's verbatim-preservation purpose (E3 ○); F060-recovered episodes invisible to every read path with their candidate facts discarded (E5 ○); F055 wholesale-clobbers curated working memory with placeholder stubs (E2 ✓).
4. **The substrate is paying 4–10× for embeddings and defeating its own indexes.** The same fact content is embedded up to ~10× across dedup probes, learn, and graph linking; the same query 4–7× per recall; there is no embedding cache anywhere. The per-learn dedup query and per-turn censor checks are written in shapes pgvector's HNSW cannot serve — sequential scans on the hottest paths (D2/D9 ✓○). DB sessions are held open across Haiku/embedding network calls, a pool-exhaustion pattern (D3 ○).
5. **Retrieval at HEAD: 11 of 17 whitepaper issues are still live verbatim.** F080 genuinely fixes the censor-floor (B1) and procedure-boost (B2) on the default path. But the new §14 cosine fallback **drops every relevance guard the old path had** — no score floor, no staleness, no dedup — preloading up to 5 full 2500-char bodies per turn for arbitrarily-poor matches (R1 ✓). Graph-edge writes feed the now-enabled consumers with duplicated, direction-inverted, and four-different-weight-space edges (G1–G4 ○).

The unifying theme: **per-component logic is mostly sound, but the *contracts between* components — score spaces, thresholds vs. bands, index shapes vs. query shapes, writer conventions vs. consumer assumptions — were never reconciled, and prod's flag overlay activates exactly the combinations where the mismatches bite.**

---

## 1. The Fact Write Path

The dominant producer is episode summarizer → fact extractor → `Heart.learn`. The full flow (verified step-map):

1. `EpisodeSummarizer` emits `episode_summarized` with `candidate_facts` (`episode_summarizer.py:94-117`); dated/stable split-capped 30/5 (`fact_extractor.py:408-411`).
2. **Leg-1 dedup** `_resolve_dedup` (`fact_extractor.py:139-214`): `search_facts(content, limit=1)` → embed #1 → RRF → score vs `fact_dedup_threshold` 0.92 → Haiku tiebreaker (prod ON) → DISTINCT widens to `limit=5` (embed #2), survivors become `exclude_ids`.
3. `FactManager._learn` (`facts.py:417-589`): 30-char gate → embed #3 → **Leg-2** `_find_duplicate` (prod threshold 0.80) → F075 date bypass or `_confirm` → admission (incl. utility LLM) → actionability Haiku → INSERT → subject supersession (per-candidate Haiku) → contradiction detection (Haiku) — all inside one session/transaction.
4. Post-commit: `fact_learned` bus event → `GraphLinker` (embeds #4–#5 + up to 5 decision re-embeds).

### Findings

**S1 — Leg-1 dedup compares a rank-based RRF score to a similarity threshold; the pre-check is ~always true. [P1, LIVE] ✓**
`search.py:91,105,113-115`: RRF score = weighted reciprocal *ranks*, normalized by `1/k`. At `limit=1` with prod `NOUS_RRF_K=30` (`.env.prod-snapshot:96`), a vector-only rank-0 hit scores `(0.7/30 + 0.3/32)·30 ≈ 0.981` — and the vector SQL has **no similarity floor** (`search.py:189-195`), so rank-0 always exists once any embedded fact does. `fact_extractor.py:181-188` then tests `top.score > 0.92` (`config.py:275`): ~always true, regardless of actual similarity.
Consequences: (a) prod (`NOUS_FACT_DEDUP_TIEBREAKER_ENABLED=true`, snapshot:49) — every candidate pays ≥1 Haiku call and the verdict is 100% LLM; tiebreaker failure returns "duplicate" → **silent loss of all candidate facts during any LLM outage**, logged at DEBUG (`fact_extractor.py:314`). (b) flag-off default — top hit deduped unconditionally → near-total write suppression. The F051.5 comment blaming "near-empty corpus" (`fact_extractor.py:101-105`) misdiagnoses: rank-0 is rank-0 at any corpus size.
*Fix:* compare the **raw cosine** of the top hit (it's already computed in the vector leg) against the threshold; keep RRF for ranking only.

**S2 — knowledge_extractor's 0.85 pre-check has the same rank-score bug, with no tiebreaker: compaction-time facts are ~never stored. [P1, LIVE] ✓**
`knowledge_extractor.py:118-124`: `existing[0].score > 0.85` → skip. Same always-true math; this site never adopted `_resolve_dedup`, has no F075 date bypass, and passes no `source_episode_id`/`source_text` (`:127-133`) → the rare survivor is ungrounded for admission. *Fix:* route through the shared `_resolve_dedup` (the #354 "cannot drift" comment covers only fact_extractor's two internal paths).

**S3 — Prod Leg-2 threshold 0.80 makes write-time contradiction detection and subject-supersession unreachable; conflicts confirm the stale fact. [P1, LIVE] ✓**
`.env.prod-snapshot:51` sets 0.80 (default 0.95, `config.py:282`). `_find_duplicate` (`facts.py:878-890`) catches any hit >0.80 → `_confirm(dupe.id)` returns at `facts.py:467` — **before** `_find_contradiction` (`:571`) and `_supersede_by_subject` (`:549`) run. The contradiction band is 0.85–0.95 (`facts.py:142-143`): entirely inside the swallowed range. Worst case verified in flow: "API returns 500" vs stored "API returns 200" at 0.86 → `_confirm` increments `confirmation_count`/`last_confirmed` on the stale fact (`facts.py:988-989`) and discards the new claim. Bonus loss: a dated candidate vs an *undated* dupe doesn't trigger the F075 bypass (`facts.py:459-464` requires both non-null) and `_confirm` never merges `event_date` — with `NOUS_TEMPORAL_EXTRACTION_ENABLED=true` (snapshot:119), F075's dated facts are silently de-dated on collision.
*Fix:* assert `dedup_threshold ≥ CONTRADICTION_SIMILARITY_MAX` at Settings init, or run the F027 classifier on the dedup hit before confirming; merge `event_date` in `_confirm`.

**S4 — Embedding failure → fact persisted with NULL embedding, permanently invisible and dedup-immune; no backfill. [P2, LIVE] ○**
`facts.py:438-443` swallows embed failure (WARNING) and inserts with `embedding=None` (`:525`). The fact then skips both dedup legs forever (also invisible *as a target*: `_find_duplicate` filters `embedding IS NOT NULL`, `:883`), never appears in vector recall, never links. No `embedding IS NULL` repair consumer exists for facts (the F081 backfill covered skills only). *Fix:* fail the learn, or queue re-embeds keyed on NULL.

**S5 — One transaction spans up to 4 LLM calls per learn; SELECT-then-INSERT dup race. [P2, LIVE] ○**
`_learn` holds its session across admission-utility LLM (`facts.py:475`), actionability Haiku (`:504`), supersession classifier (`:794`), contradiction classifier (`:648`). No content unique constraint exists (`models.py:475-517`), so two concurrent learns of the same content (tool + extractor) both pass `_find_duplicate` and both insert; the multi-second LLM window makes the race practical. With prod `NOUS_SUBTASK_WORKERS=6` + heartbeat + sleep all learning, this also pins pooled connections (pool 10+5, `database.py:18-24`). *Fix:* compute LLM verdicts before opening the session; consider a per-agent content-hash guard.

**S6 — `fact_learned` bus event emitted before the caller's transaction commits → dangling graph edges on rollback. [P2, LIVE] ○**
`heart.py:313-324` emits immediately; injected-session callers (`cognitive/monitor.py:375`, `cognitive/layer.py:1783`) commit later. `FactGraphLinker` runs in its own session and commits edges to a fact that may never commit — `graph_edges` is polymorphic (no FK), so rollback strands them permanently. *Fix:* emit post-commit, or linker verifies row existence.

**S7 — Up to ~10 embedding calls per stored fact, ≥4 redundant. [P2, LIVE] ○**
Same content embedded at `fact_extractor.py:181`, `:204`, `facts.py:441`; the identical link template embedded twice (`graph_linker.py:174`, `:261`); up to 5 recent decisions re-embedded per fact (`graph_linker.py:206-209`) with no cache. *Fix:* thread the embedding through; cache template embeddings.

**S8 — Sleep-cycle MERGE paths call learn() without exclude_ids → at prod 0.80 the merged fact dedup-confirms one of its own sources; merged content lost. [P2, LIVE] ○**
F031 merge (`sleep_handler.py:930-938`) and F027 consolidation (`:1260-1266`). The follow-up then supersedes the *other* source — net: LLM merge wasted, B superseded by A, merged text never stored. The `exclude_ids` mechanism already exists (F377). *Fix:* pass source fact IDs as `exclude_ids`.

**S9 — Dedup probes mutate recall statistics on facts that were merely scanned. [P2, LIVE] ○**
`facts.py:1305-1306` `_fire_track_access` fires from `_search`; every Leg-1 probe inflates `recall_count`/`last_recalled_at` (and `updated_at` via trigger, `init.sql:621`) on nearest-neighbor facts no consumer saw — corrupting staleness clocks and usage signals. *Fix:* `track_access: bool` param, False from dedup callers.

**S10 — F075 compound ORDER BY in `_find_duplicate` defeats HNSW → per-learn sequential scan. [P2, LIVE] ✓**
`facts.py:886-889`: `ORDER BY (event_date IS NOT DISTINCT FROM :d) DESC, embedding <=> …` — HNSW serves only a leading distance sort. Every learn full-scans all active embedded facts, computing 1536-dim distance ~3×/row. *Fix:* HNSW top-K by distance, date preference in Python.

**S11 — F377 DISTINCT verdict not propagated to `_supersede_by_subject`. [P2, LIVE] ○**
`exclude_ids` reaches `_find_duplicate`/`_find_contradiction` (`facts.py:449,571`) but not `_supersede_by_subject` (`:549-554`); at sim >0.95 supersession skips LLM disambiguation (`:793`) and deactivates the fact the tiebreaker just ruled distinct. Reachable precisely via the exclude_ids door. *Fix:* thread exclude_ids through.

**S12 — Reflection self-talk stored as `category="rule"` → enters always-loaded Tier-1 with the top admission prior. [P2, LIVE] ○**
`cognitive/layer.py:1777-1783`; "rule" is defined as user-directives-only (`fact_extractor.py:65`), prior 0.95 (`admission.py:25`). Also counts `FactRejected` as stored (`layer.py:1784`). *Fix:* category `concept`/`technical`; isinstance check.

**S13 (P3 batch, all LIVE ○):** `learn_fact` tool discards caller `source` → everything gets the user_direct admission bonus (`tools.py:526,556`; `admission.py:410`) · `track_access` UPDATE not agent-scoped (`facts.py:241-243`) · `_create_graph_edge` swallows ImportError at DEBUG — F027's bridge edges could be 100% broken invisibly (`facts.py:195-217`) · stale `FactDetail` returned when contradiction handling deactivates the just-inserted fact; callers record it as stored and graph-link it (`facts.py:566` vs `:679-682`) · dedup search + Haiku run before the 30-char reject (`fact_extractor.py:311` vs `facts.py:428`) · `Settings()` re-instantiated 2–3× per hybrid_search (`search.py:34-73`) · F075 date bypass examines only the top-1 hit (`facts.py:449-467`) · unlocked confidence decrements lose concurrent updates (`facts.py:698-700,1105-1107`) · misleading RuntimeError on short supersede/contradict content (`facts.py:1034-1036`) · event-bus overflow silently drops `fact_learned` → unlinked facts (`events.py:180-184`).

---

## 2. Episode / Chunk / Working-Memory Lifecycle

**E1 — F069↔F067 chunk_index collision silently destroys transcript chunks. [P1, LIVE] ✓**
The F067 transcript writer numbers chunks from 0 — `for idx, … in enumerate(…)` → `ON CONFLICT (episode_id, chunk_index) DO NOTHING` (`episode_summarizer.py:260-274`) — with **no advisory lock and no MAX+1 allocation**. F069 `ingest_document` appends after `MAX(chunk_index)+1` under `pg_advisory_xact_lock` (`tools.py:1227-1280`) **to the active episode of the current session** (`:1150-1165`). So: ingest a document mid-session (chunks 0..M), end the session — every transcript chunk with index ≤ M hits `uq_episode_chunks_episode_index` (migration 050:44-45) and is silently dropped. No log, no retry (the `structured_summary` guard blocks reprocessing, `episode_summarizer.py:172-174`). Both features ON in prod.
*Fix:* F067 writer allocates from MAX+1 under the same advisory lock, or uniqueness keys on (episode_id, source_kind, chunk_index).

**E2 — F055 `record_surfaced` wholesale-replaces WorkingMemory.items, destroying curated WM on every recall_deep. [P1, LIVE] ✓**
`residual_activation.py:250-265` builds entries only from the current recall's surfaced results — with placeholder text `"summary": f"residual {node_type}"` — and `upsert_residual_items` does `existing.items = items` (`working_memory.py:458` ✓). Items loaded by `_load_recalled_to_working_memory` with real summaries (`layer.py:1496-1504`) are clobbered; the stubs are what `_format_working_memory` renders into the next turn's prompt. Also defeats F055's own decay model (replace ≠ merge → 1-recall window) and races `load_item`'s FOR UPDATE read-modify-write (fired as bare `create_task` from `tools.py:785-792`). Prod: `NOUS_RESIDUAL_ACTIVATION_ENABLED=true`.
*Fix:* merge surfaced entries into existing items inside one FOR UPDATE transaction; carry real summaries.

**E3 — Transcripts truncated to 500 chars/turn at capture; chunks, summaries, and fact extraction all inherit the loss. [P2, LIVE] ○**
`layer.py:390` (user), `:1159` (assistant); tool calls/results never captured. This lossy join is what is persisted, summarized, F067-chunked, and F060-recovered. F067's stated purpose ("preserves verbatim tokens the fact extractor discards", `models.py:416-420`) is structurally hollow — nothing past char 500 of any message exists anywhere. The 16K `transcript_max_chars` budget is barely reachable at ≤510 chars/turn. *Fix:* raise/remove the per-turn cap (the 16K chunked-summarization path already absorbs long transcripts).

**E4 — `end_session` pops in-memory state before persistence; failure in `end_episode` loses the transcript permanently. [P2, LIVE] ○**
`layer.py:1723-1724` pops `_active_episodes`/`_session_metadata` first; the except at `:1754-1755` swallows. Same class on restart (process-local dicts). Corroborated by the F060 prod audit shape: 0/103 stuck-open episodes had transcripts (`sleep_handler.py:1523-1524`). *Fix:* persist transcript incrementally, or pop after success.

**E5 — F060-recovered episodes stay invisible to every read path; their candidate_facts are discarded. [P2, LIVE] ○**
Recovery never sets `ended_at`/`outcome` (`sleep_handler.py:1629-1633`; `_update_summary` backfills text only, `episodes.py:134-161`); `list_recent` requires `ended_at IS NOT NULL` (`episodes.py:373`), search excludes via `outcome != 'abandoned'` NULL-semantics (`:516,526,612`). The summary's `candidate_facts` are dropped (`sleep_handler.py:1629-1638`; direct invocation deliberately doesn't emit `episode_summarized`, `episode_summarizer.py:166-168`) — the recovery LLM spend extracts zero facts. *Fix:* close the episode and emit the event (or invoke the extractor directly).

**E6 — Episode dedup can share one episode across two live sessions → transcript clobber. [P2, LIVE] ○**
`EpisodeManager._start` reuses any ongoing episode in 30 min at `text_overlap > 0.80` (`episodes.py:72-85`), ignoring session_id; `text_overlap` is asymmetric containment (`utils.py:6-21`), so short summaries contained in a new first message score 1.0. pre_turn stores the reused id (`layer.py:715-716`) — violating the R-P0-2 rule documented 12 lines above it; `_end` overwrites `ended_at`/`outcome`/`transcript` unconditionally (`episodes.py:229-239`). Plausible trigger: recurring scheduled tasks with near-identical first messages. *Fix:* scope manager dedup to session_id; `_end` refuses already-ended rows.

**E7 — Chunk persistence is one-shot after the summary write; failures permanently unretryable on the live path. [P2, LIVE] ○**
Summary persisted at `episode_summarizer.py:184`, chunks at `:187-198` with swallowed failures; the `structured_summary` guard (`:172-174`) blocks all retries. The code's own comment defers to "a backfill handler" that doesn't exist (only a manual script). *Fix:* chunk-completion recheck in the guard or a sleep-phase backfill.

**E8 — WM `load_item` has no ref_id dedup → duplicates evict distinct items. [P2, LIVE] ○**
`working_memory.py:133-143` appends unconditionally; up to 10/turn (`layer.py:1480-1504`) against `max_items=20` with `_evict_lowest`. *Fix:* upsert by ref_id.

**E9 (P3 batch ○):** `cleanup_stale` batches all DELETEs in one transaction, defeating its stated lock-avoidance (`working_memory.py:351-386`) · F060 summary-fallback feeds 200-char stubs through the chunk writer as `source_kind='dialogue'` (`sleep_handler.py:1617-1633`) · `episodes.outcome` always `'success'`; summarizer's resolved/partial/unresolved vocabulary never lands and doesn't even match the CHECK constraint (`layer.py:1749`; `models.py:367`) · chunk recall surfaces chunks of deactivated/abandoned episodes; no sweep, unbounded growth (`retrieval_pipeline.py:916-921`) · concurrent `session_ended` double-runs the summary LLM (get-then-set, `episode_summarizer.py:172,184`) · `record_surfaced` resurrects the WM row after `end_session` cleared it; bare `create_task` is GC-eligible (`tools.py:785`) · ORM/DB unique-constraint name mismatch on WM upsert breaks metadata-created schemas (`models.py:738` vs `init.sql:423`, `working_memory.py:58`) · embeds awaited inside open sessions in episode `_start`/`_end`/`_update_summary` · `agents.last_active` write+commit every turn (`layer.py:393-398`).

---

## 3. Graph Writes & Sleep Consolidation

Sixteen distinct writers feed `brain.graph_edges` (full writer/consumer matrix in §7). Prod has the consumers ON (`NOUS_HEART_GRAPH_ALL_TYPES_ENABLED`, `NOUS_GRAPH_ADJACENCY_BOOST_ENABLED`, `NOUS_GRAPH_NEIGHBOR_SEED_SCORE_ENABLED` all true in the snapshot) — writer defects now shape live ranking.

**G1 — Reverse-duplicate edges: same-type backfill and intra-episode chunk linking write both A→B and B→A. [P2, LIVE] ○**
UNIQUE is directional (`init.sql:205`). `find_orphans` snapshots before writes (`graph_densifier.py:454,559`); `_backfill_same_type` has no reverse-edge check (`:282-292`); `_link_chunk_to_intra_episode_chunks` none either (`:691-704`) — adjacent chunk pairs get **two** weight-1.0 edges. The adjacency-boost consumer sums every edge row onto both endpoints (`retrieval_pipeline.py:792-796`) → doubled pairs contribute 4× the intended weight, and density (the SA auto-trip metric) inflates. F070.1 fixed exactly this with target-side detection (`:817-838`); v1 and F040 never got the fix. *Fix:* canonicalize undirected relations to (least, greatest) or add a reverse NOT EXISTS in `create_edge`.

**G2 — Relation taxonomy split-brain: reverse cross-type backfill writes direction-inverted relations; live vs sleep writers disagree on the same pair. [P2, LIVE] ○**
`_get_relation` falls back to the reversed key (`graph_densifier.py:94-97`) → decision→fact `evidence_for` ("decision is evidence for fact"), episode→fact `extracted_from` — inverted vs the canonical live writers (`graph_linker.py:215-230,357-374`). ProcedureGraphLinker and the densifier write *different relations* for the same node pair (`procedure_graph_linker.py:104,134` vs `_RELATION_MAP` `graph_densifier.py:36,39`). The directional UNIQUE can't dedup the semantic duplicate in the opposite direction; rendered relation labels assert wrong-direction facts. *Fix:* direction-aware `_RELATION_MAP` (swap source/target on reversed match); align linker taxonomies.

**G3 — Four incompatible weight spaces on graph_edges, summed as one by consumers. [P2, LIVE] ○**
Live linker writes raw cosine (no multiplier, `graph_linker.py:224,303`); densifier same-type writes cosine×multiplier; cross-type writes `0.6·sim+0.15`×multiplier (`graph_densifier.py:421-435`); co-mention/co-occurrence write fixed 0.90; structural 1.0. Adjacency boost and seed-score scoring consume them as one scale — ranking influence is determined by *which writer fired*, not link strength. *Fix:* one weight contract through `create_edge`; per-relation discounts at read time.

**G4 — `find_orphans` excludes `co_mention` but not `co_occurrence` → Gap-1 edges permanently mask facts from backfill. [P2, LIVE] ○**
`graph_densifier.py:166` excludes only `co_mention`; `build_cooccurrence_edges` writes `extraction_method='co_occurrence'` (`:1280-1291`, prod ON, snapshot:22). A fact whose only edge is co-occurrence is classified non-orphan and skipped forever — the exact bug the F076 comment documents, reintroduced by the newer writer. *Fix:* `NOT IN ('co_mention','co_occurrence')`.

**G5 — Densifier interrupt wiring is dead; densification + cluster discovery are uninterruptible. [P2, LIVE] ○**
`_on_wake` never calls `GraphDensifier.interrupt()` (zero callers anywhere); the pre-call flag copy (`sleep_handler.py:1353`) is always-False and wiped by `run_backfill_cycle`'s first line (`graph_densifier.py:1061`). At prod caps (200+200 orphans/cycle, snapshot:53-54) with per-orphan embeds + CE inference, a user message cannot stop a multi-minute phase that contends for the embedding API and DB. *Fix:* wire `_on_wake` → `interrupt()`; don't reset an externally-set flag.

**G6 — No memory of failed orphans: hard negatives re-embedded and re-reranked every cycle; older orphans starved. [P2, LIVE] ○**
`find_orphans` orders `created_at DESC LIMIT n` (`graph_densifier.py:172-174`) with no attempted-set (F070.1's `exclude_ids` fix never reached the main loops); a stable >200 population of below-threshold recent orphans means thousands of redundant embedding calls per sleep, forever, while older orphans are never reached. *Fix:* persist `backfill_attempted_at` + cooldown.

**G7 — Relaxed CE-mode thresholds are gated on the flag, not on the reranker actually running. [P2, LATENT] ○**
`_get_threshold` keys on `ce_backfill_enabled` alone (`graph_densifier.py:89-91`); `ce_rerank_backfill_candidates` silently no-ops when sentence-transformers is absent (`backfill_rerank.py:150-156`) → relaxed cosine gates with no precision pre-filter. Live trap today: prod's `NOUS_GRAPH_THRESHOLD_EPISODE_EPISODE=0.70` is silently ignored in CE mode. *Fix:* consult `CROSS_ENCODER_AVAILABLE`; warn on ignored overrides.

**G8 — F070.1 cross-episode chunk backfill is never invoked by the sleep cycle. [P2, DEAD] ○**
`backfill_orphan_chunks_cross_episode` (`graph_densifier.py:955-1020`) has one caller: the one-time script. `run_backfill_cycle` calls only same-episode v1 (`:1104`). The cross-session chunk graph stops accruing after the script run; prod's cross-chunk threshold overrides are dead in steady state. *Fix:* add a bounded cross-episode pass to the cycle.

**G9 (P3 batch ○):** sleep stats inflated — `_co_occurrence` not popped before `sum()` (`sleep_handler.py:1356-1366`) · insert counters count ON-CONFLICT no-ops as created (`graph_densifier.py:1280-1291,1419-1427`; `graph_linker.py:230-239,309-318`) · `discover_clusters`: restart-volatile 7-day limit, O(E) full-graph load, per-hub embeds, no interrupt checks (`:1449-1541`) · summarizer's anchor/similarity queries miss `agent_id`/`active` filters (`episode_summarizer.py:323-326,366-375`) · `happened_before` chains decay into shortcut DAGs on out-of-order inserts (`graph_densifier.py:1165-1203`) · stub phases (`prune`, `compress`) report success in `phases_completed` (`sleep_handler.py:560-584`) · double-sleep spawn race — `_sleeping` set inside the task, not before create_task (`:377-422`) · `_phase_stale_scan` unbounded vs F053 prune capped at 1000/cycle — backlog asymmetry (`:1074-1106`) · `edge_provenance.VALID_METHODS` drifted from migration 055's CHECK · per-candidate cosine verification one roundtrip each (`graph_densifier.py:264-277`) · phase ordering puts F057 deterministic relink *after* densification, whose semantic links destroy the orphan predicate F057 needs (`sleep_handler.py:477-489,1773-1778`) · co-mention bulk can silently auto-trip spreading activation via density (`config.py:1288-1290` admits it) · `edge_confidence`'s temporal term always grants max bonus (caller hardcodes 0 days, `graph_densifier.py:421-426`).

---

## 4. Storage Substrate (Schema · Indexes · Sessions · Embeddings)

**D1 — Decision hard-delete leaves dangling graph_edges; the CASCADE comment is stale since migration 016. [P1, LIVE] ✓**
`brain.py:565-586` NULLs two heart FKs then `DELETE FROM brain.decisions`, with a comment claiming CASCADE covers `graph_edges` — but migration 016:5-6 dropped both FKs (✓ verified) and `init.sql:191-208` has no REFERENCES. Every decision delete strands edges; blind walkers (SA CTE, `_neighbors`, density, adjacency boost) return ghost IDs and density inflates toward the SA auto-trip. Secondary: `brain.decisions` has no `active` column at all — the only delete is hard, violating the project's own soft-delete convention. *Fix:* explicit edge DELETE in `_delete` + one-time cleanup migration; consider `active` on decisions.

**D2 — Per-recall query embedded 4–7×; per-fact content up to ~10×; zero caching. [P2, LIVE] ✓○**
Each manager re-embeds the same query (`episodes.py:505`, `facts.py:1227`, `procedures.py:388`, `censors.py:364`), plus MMR (`heart.py:1088`), chunk leg (`retrieval_pipeline.py:909`), decision search (`brain.py:682`). Serial latency stacks on the hottest read path. The F050 `variant_pairs` plumbing proves the thread-the-vector parameter path already exists. *Fix:* embed once in `Heart.recall`/pipeline; pass down.

**D3 — Sessions/transactions held across LLM + embedding network calls — pool-exhaustion pattern. [P2, LIVE] ○**
`facts._learn` (4 LLM calls mid-transaction), `heart.recall` (4 embeds), `decision_graph_linker.py:82-160` (≤10 embeds in one session), `graph_densifier.py:300-420` (embeds + CE inference — ~67 s/call on prod CPU per the BGE finding). Pool is 10+5 shared by chat, 6 subtask workers, heartbeat, handlers, scheduler. *Fix:* compute network verdicts outside sessions; commit per item.

**D4 — Missing `lower(subject)` expression index → per-learn sequential scan. [P2, LIVE] ○**
`facts.py:776-780` and `:1645-1650` filter on `lower(subject)`; the only index is raw `(subject)` (`init.sql:571`). *Fix:* `CREATE INDEX idx_facts_agent_subject_lower ON heart.facts (agent_id, lower(subject)) WHERE active;`

**D5 — Chunk recall ignores parent-episode soft-delete. [P2, LIVE] ○**
`retrieval_pipeline.py:915-922` filters agent_id + embedding only; chunks have no `active` and only die on hard episode delete (never happens). Deactivated/abandoned episode content resurfaces verbatim. *Fix:* `JOIN heart.episodes e ON e.id=c.episode_id AND e.active`.

**D6 — Embedding-space mixing has no guard. [P2, LIVE] ○**
Every column is `vector(1536)`; `embedding_model`/`embedding_dimensions` float freely (`config.py:40-41`); no per-row model provenance; nothing asserts dims==1536 at boot. Prod's small→large swap works only because `dimensions:1536` truncates — a swap without full re-embed silently mixes incompatible spaces with zero error. *Fix:* boot assert + a stored model marker + re-embed runbook.

**D7 — Migration runner: no concurrency guard; one transaction for all pending; checksums never compared. [P2, LIVE] ○**
`migrator.py:88-132`: two booting processes race (data migrations can double-apply; loser dies on tracking PK); multi-migration catch-up holds DDL locks across all files and forbids CONCURRENTLY; stored checksums never validated; comment-stripping eats `--` lines inside string literals. *Fix:* `pg_advisory_xact_lock` first statement; per-migration transactions.

**D8 — Spreading-activation recursive CTE has no visited-set → exponential row growth, worst exactly when density auto-enables it. [P2, UNREACHABLE-today] ○**
`spreading_activation.py:97-127`: `UNION ALL`, A↔B re-expands per depth; rows ≈ degree^depth per seed. F040/F070/F076 deliberately push density toward the 3.0 trip point. *Fix:* path-array cycle guard or row LIMIT in the recursive term.

**D9 — Censor vector matching can't use HNSW (hot per-turn path). [P2, LIVE] ○**
`censors.py:271-280` and `:394-404`: `ORDER BY similarity DESC` (alias, not the distance operator) with no LIMIT → full scan per censor check, distance computed twice per row. *Fix:* `ORDER BY embedding <=> :v LIMIT k`, threshold post-filter.

**D10 (P3 batch ○):** `Settings()` re-instantiated 2–3× per hybrid_search (`search.py:34-73`) · `idx_facts_active` indexes a constant (`init.sql:572`) · EmbeddingProvider: no 429/`Retry-After` handling; retry tuple omits ConnectTimeout/PoolTimeout; `dimensions` sent unconditionally (`embeddings.py:42-74`) · ORM constraint name `uq_edges_src_tgt_rel` vs DB auto-name — the class of bug that already silently no-op'd decision auto-linking for months (`models.py:240`; post-mortem at `brain.py:1639-1652`) · `heart.tool_cache` uniqueness lacks agent_id (`models.py:884`) · HNSW never tuned, `limit*3` over-fetch sits at the ef_search=40 cliff · `ts_rank_cd` computed twice per row in keyword legs · `search_all_including_inactive` forks the hybrid pipeline wholesale (`facts.py:1344-1370`) · join/child tables without agent_id are FK-scoped (acceptable); `query_expansions`/`nous_system.config` documented-global.

---

## 5. Retrieval at HEAD — Whitepaper Register Re-verified + New-Code Findings

### 5.1 Register verdicts (17 items)

| Issue | Verdict at HEAD | Anchor |
|---|---|---|
| B1 censor floor ≥0.7 in pool | **FIXED-BY-F080** on default path (`retrieval_pipeline.py:363-365` excludes censors when `search_all`); LATENT for explicit `memory_types=["censor",…]`; **still fully live in prod-TODAY** | floor unchanged `censors.py:387,400` |
| B2 procedure boost >1.0 | **FIXED-BY-F080** default path; boost itself unchanged (`procedures.py:451-467`), LATENT on explicit mixed-type calls | |
| B3 embed-fail leg raw ts_rank | **STILL-LIVE** (`search.py:220-222`) | |
| B4 mixed-scale rerank_by_score sort | **CHANGED P1→~P2**: sort unchanged (`retrieval_pipeline.py:282-283`) but F080 removes the floor/boost breakers; residual mix = RRF [0,1] vs raw-cosine chunks vs graph scores. Prod-TODAY: full B4 live | |
| B5 stats hardcoded False | **STILL-LIVE** (`:301-302`; F080 added `coherent_ranking_applied` but didn't thread CE/MMR out) | |
| B6 contradiction ids exclude facts/chunks | **STILL-LIVE** (`:679-684`) | |
| B7 no global top-K | **STILL-LIVE** (`:224-315`; docstring still wrong `tools.py:626`) | |
| B-cog-A pre-truncation ID collection | **STILL-LIVE** for facts/decisions/episodes; **FIXED** inside the new §14 procedure section only (`context.py:672-704` cites B-cog-A) | |
| B-cog-B gap-filter on boost-sorted list | **STILL-LIVE** (`search.py:433-435`; `context.py:1073-1087`) | |
| B-cog-C decisions no dedup; dead `_dedup_decisions` | **STILL-LIVE** (`context.py:539-543`; `:1660` zero callers) | |
| B-cog-D nondeterministic query_text | **STILL-LIVE** (`intent.py:138,142,192-194`) | |
| B-cog-E drifted recency resolvers | **STILL-LIVE** (`context.py:1248-1293` vs `retrieval_pipeline.py:1099-1195`) | |
| B-graph-5 Path A ceiling | **CHANGED**: snapshot now sets `NOUS_GRAPH_NEIGHBOR_SEED_SCORE_ENABLED=true` → seed-score scoring live, ceiling lifted; co_occurrence/co_mention still escape the provenance penalty (`:948`) | |
| B-graph-6 boost before graph_expanded append | **STILL-LIVE** (`:249-254`; Path-A neighbors now inside the boost set, Stage-4 items still excluded) | |
| B-graph-8 density recompute per recall | **STILL-LIVE** (`:582-600`) | |
| B-graph-9 duplicate decisions Stage 2/4 | **STILL-LIVE** (`:430` vs `:578`) | |

**Net:** 11/17 still live verbatim; B1/B2 fixed on the default path (the headline P1 pair); B4 materially de-fanged; one verdict (B-graph-5) flipped by a prod snapshot change, not code.

### 5.2 New findings in F080 / §14 / F081 code

**R1 — §14 cosine fallback drops every relevance guard the passive path had: floorless full-body injection every turn. [P2, LIVE at HEAD-deploy] ✓**
`context.py:1427-1448` (verified: only an `active` check — no score floor, no staleness, no frame boost, no dedup, no relevance filter; the replaced passive path had all of them, `context.py:782-822`). Takes top `slots*2` cosine hits, keeps first `slots` active, preloads full bodies (≤2500 chars each). The graph leg fires on ~43% of queries per the code's own docstring, so the floorless cosine leg is the steady state: up to 5 procedure bodies budget-fitted per turn even for near-zero cosine matches. `proc_selection_graph_primary=True` default, snapshot silent → prod behavior at deploy. *Fix:* apply `procedure_score_floor` (0.40) to `summ.score` before body fetch; run `_apply_relevance_filter` over cosine candidates.

**R2 — recall_deep's contract now lies about coverage. [P2, LIVE at HEAD-deploy] ○**
Tool schema (`tools.py:1452`), docstring (`:627-628`), and the prod identity prompt all advertise "searches everything", but coherent ranking silently drops procedures+censors from the default pool. The model will read a null result as "no such censor exists." Procedures have compensating surfaces (catalog, §14, get_procedure); **censors have no semantic search surface left on the default path** — only the unranked every-turn Active Censors dump. *Fix:* update schema + identity prompt; append a one-line exclusion hint to results.

**R3 — Conversation-frame procedure coverage hole. [P2, LIVE at HEAD-deploy] ○**
`conversation` budgets zero procedures (`schemas.py:124-129`, `intent.py:220-226`) so §14 never runs there, *and* F080 removed procedures from recall_deep's default pool — catalog-only coverage in the agent's most common frame. Defensible, but it's an accident of two features composing, not a decision. *Fix:* decide explicitly; possibly small procedure budget in conversation frame.

**R4 (P3 batch ○):** §14 cosine leg N+1 `get_procedure` per candidate (active filter already pushed into search — redundant check) (`context.py:1430-1447`) · `_format_procedure_bodies` drops a body that *starts with* literal `source:`; truncation can cut inside a code fence, swallowing the get_procedure pointer (`context.py:1471-1496`) · F081 16000-char embed cap is char-based vs the 8191-token limit — URL/code-dense bodies can still 400→NULL-embed, with a wasted deterministic retry (`procedures.py:60-61,112-132`) · `coherent_ranking_applied` stat set before the search can fail (`retrieval_pipeline.py:363-365`) · §14 window-function neighbor dedup verified CORRECT (PR #492/#493 codex fix held; one cosmetic missing agent_id in `_active_proc`, `brain.py:1218-1220`).

**F080 regression sweep — explicitly checked, no defect:** censor *enforcement* (middleware, Active Censors section, subtask gate) is independent of the recall pool; explicit `memory_types` still honored; Path A can surface procedure descriptions as graph neighbors without reintroducing B2's score space.

### 5.3 Deploy-state flag table (the ones that change behavior at #491–#494 deploy)

| Flag | config default (HEAD) | prod snapshot | At HEAD-deploy |
|---|---|---|---|
| `coherent_ranking_enabled` (F080) | True | absent | **flips ON** |
| `proc_selection_graph_primary` (§14) | True | absent | **flips ON** (floorless cosine leg = R1) |
| `proc_catalog_enabled` (F079) | True | absent | ON — config comment says "set false in prod until skill dedup lands"; dedup (migrations 058/059) IS in HEAD, so consistent **only if migrations run at deploy** |
| `critic_skill_injection` | "disabled" | **enabled** | §14 critic leg active in prod |
| `graph_neighbor_seed_score_enabled` | False | **true** | Path A real scoring (flips whitepaper's B-graph-5 verdict) |
| `cross_encoder_enabled` / `mmr_enabled` | False/False | false/false | the re-basing modifiers stay OFF — residual B4 mix remains the live ranking |
| `NOUS_RRF_K` | 60 | **30** | steeper rank falloff in all RRF scores (incl. the S1 dedup math) |
| `NOUS_FACT_NATIVE_COSINE_THRESHOLD` | 0.95 | **0.80** | S3 — contradiction band swallowed |
| `NOUS_ADMISSION_SHADOW_MODE` / threshold | True/0.55 | **false/0.60** | admission enforcing in prod |
| `residual_activation_enabled` (F055) | False | **true** | E2 WM clobber live |

---

## 6. Prioritized Remediation

**Tier 1 — data-integrity (write path):**
1. **Fix both dedup gates together (S1, S2, S3).** Leg-1: threshold the raw cosine of the top hit, not its RRF rank score. Leg-2: enforce `dedup_threshold ≥ contradiction_band_max` at Settings init (or classify-before-confirm). This single pair of changes restores write-time contradiction detection, ends the always-pay-Haiku regime, un-suppresses knowledge_extractor, and removes the LLM-outage data-loss mode. Merge `event_date` in `_confirm`.
2. **Stop the silent losses (E1, E2, S4).** Chunk-index allocation under the F069 advisory lock; merge-don't-replace in `upsert_residual_items`; fail or queue-retry NULL-embedding learns.
3. **Dangling-edge hygiene (D1, S6).** Edge DELETE in decision `_delete` + cleanup migration; emit `fact_learned` post-commit.

**Tier 2 — cost and capacity:**
4. **Embed once, thread everywhere (S7, D2).** One query embed per recall, one content embed per learn, template-embedding cache in linkers/densifier. Largest single cost lever in the system.
5. **Index-shape fixes (S10, D4, D9).** Rewrite `_find_duplicate` ORDER BY, add the `lower(subject)` expression index, fix censor ORDER BY+LIMIT. Three sequential scans on per-learn/per-turn paths become index scans.
6. **Move network calls out of transactions (S5, D3).**

**Tier 3 — graph coherence (before further densification investment):**
7. One weight contract + direction-aware relation map + reverse-edge dedup (G1, G2, G3); fix the co_occurrence orphan mask (G4); wire the interrupt (G5); attempted-orphan memory (G6).

**Tier 4 — retrieval (carry-over + new):**
8. Add `procedure_score_floor` to the §14 cosine leg before #494 deploys (R1) — one-line guard, prevents a regression the moment HEAD ships.
9. Fix the recall_deep contract text (R2).
10. The whitepaper's still-open remediation stack (normalize-before-merge for the residual B4 mix; re-sort after score mutations; thread real CE/MMR stats; unify the two recency resolvers) remains valid and open.

**Cheap observability wins:** stop counting ON-CONFLICT no-ops as created edges (G9); pop `_co_occurrence` from sleep stats; isinstance-check `FactRejected` in reflection counting (S12); raise `_create_graph_edge` swallow from DEBUG to WARNING (S13).

---

## 7. Appendix — Graph Writer/Consumer Matrix (abridged)

| Writer | Relations | Weight space | Prod state | Consumers |
|---|---|---|---|---|
| GraphLinker live (fact_learned) | fact→decision `evidence_for`, fact→fact `related_to` | raw cosine | LIVE | Stage 2/2b, SA, boost |
| DecisionGraphLinker (decision_recorded) | fact→decision, episode→decision | cosine×mult | LIVE | same |
| ProcedureGraphLinker | proc→fact `informed_by`, proc→decision `caused_by` | cosine×mult | LIVE | §14 graph leg, 2b |
| Summarizer deterministic + similar-episodes | fact→episode `extracted_from` (1.0), ep→ep `related_to` | structural / cosine×0.8 | LIVE | 2b, boost |
| Densifier same-type (F040) | `related_to` (4 types) | cosine×mult | LIVE (caps 200/200/30/20) | 2b, SA, boost |
| Densifier cross-type | inverted-direction relations (G2) | (0.6s+0.15)×mult | LIVE | same |
| Chunk consolidation v1 (F070) | `part_of`/`summarized_by`/chunk↔chunk | mixed; doubled adjacents (G1) | LIVE | 2b chunk expansion |
| Cross-episode chunks (F070.1) | chunk↔chunk/fact | cosine | **DEAD** (script-only, G8) | — |
| `happened_before` (F075 L2) | fact→fact directed | 1.0 | LIVE | adjacency boost only |
| Co-mention (F076) / co-occurrence (Gap-1) | fact↔fact | fixed 0.90 | LIVE | 2b+seed-score, boost; co_occurrence wrongly counts vs orphan status (G4) |
| Contradicts (F027/write-time) | fact→fact | 1.0 | LIVE (band-starved by S3) | `_attach_contradictions` only |
| Cluster discovery | hub↔hub | raw sim×mult | LIVE, 7d volatile limit | 2b, boost |

Sleep phase order (`sleep_handler.py:420-540`): review → prune(stub) → compress(stub) → reflect → resolve_contradictions → stale_scan → cluster_consolidation → recover_abandoned → **graph_densification** → relink_open_episodes (ordering bug G9) → prune_dead_edges → prune_hub_snapshots → generalize(K-lines) → evolve_rubric.

---

*Files of record:* `nous/heart/{facts,episodes,procedures,censors,working_memory,heart,search,chunking,residual_activation}.py`, `nous/handlers/{fact_extractor,knowledge_extractor,episode_summarizer,sleep_handler,decision_graph_linker,procedure_graph_linker,fact_graph_linker,session_monitor}.py`, `nous/brain/{brain,graph_linker,graph_densifier,spreading_activation,embeddings,backfill_rerank,edge_provenance}.py`, `nous/cognitive/{layer,context,intent,monitor}.py`, `nous/api/{tools,retrieval_pipeline}.py`, `nous/storage/{models,database,migrator}.py`, `nous/config.py`, `sql/init.sql`, `sql/migrations/{016,038,047,050,051,054,055,058,059}*.sql`, `.env.prod-snapshot` (deployment overlay only).
