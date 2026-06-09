# Heart Organ (Memory System) — Deep Code Audit

**Date:** 2026-06-09 · **HEAD:** `2f6a193` (post-PR #495 — the storage/retrieval integrity fixes are merged)
**Scope:** `nous/heart/*` (all 25 files read fully) + `nous/api/retrieval_pipeline.py`. Boundaries: `nous/handlers/*`, `nous/api/tools.py`, `nous/brain/*`, `nous/cognitive/*` covered by other agents (touched here only to close wires).
**Method:** code only; every claim carries `path:line`. Reachability verdicts use `config.py` defaults overlaid with `.env.prod-snapshot` (LIVE / LATENT / INERT / DEAD). Numeric-space checks on every score-vs-threshold comparison; closed-loop tracing on admission, censors, working memory, residual activation, subtasks/schedules.
**Job:** (a) verify prior-audit P1/P2 status at HEAD, (b) net-new findings in under-audited corners.

---

## (a) Current-state overview

PR #495 (commit `2f6a193`) genuinely fixed the write-path headline pair: both fact-dedup gates now threshold **raw cosine** (`facts.py:1400-1485` `find_similar_for_dedup`; `facts.py:1019-1108` `_find_duplicate`, HNSW-served), in-band dupes are F027-classified before confirming (`facts.py:896-997`), `event_date` merges onto undated dupes (`facts.py:999-1017`), dedup probes no longer mutate recall stats, `exclude_ids` reaches `_supersede_by_subject` (`facts.py:585-590, 837-851`), the F067/F069 chunk-index collision is closed under the shared advisory lock (`episode_summarizer.py:259-300`), F055 working-memory writes merge instead of clobber (`working_memory.py:430-547`), decision deletes remove their graph edges + migration 060 cleanup, an embedding LRU kills the 4–10× repeat-embed tax, and the §14 cosine leg gained the procedure score floor (`context.py:1445-1449`).

What did **not** change: the transaction-spans-LLM-calls pattern in `_learn` (now with one *more* potential Haiku call — the band classifier — inside the transaction), NULL-embedding facts with no repair path, the pre-commit `fact_learned` emission, every graph-writer defect (G1–G9), the episode/WM lifecycle items E3–E9, and the read-side score-space residue (B3/B4/B5/B6/B7).

The headline **net-new** finding of this audit: the episode semantic-search leg is structurally dead in the production lifecycle — every episode closed since PR #79 (2026-02-28) is `active=false` and therefore filtered out of hybrid search, while ongoing episodes are filtered out by `outcome != 'abandoned'` NULL semantics. No episode state produced by the live lifecycle can be retrieved by `search_episodes`, `Heart.recall`'s episode leg, the cognitive Episodes section, or `search_recent_episodes_by_embedding` (HT-1). Eval corpora insert episodes directly (`active=true` + outcome set), which is why this never showed up in LME/BEAM numbers — the exact "eval fixtures must match prod input thinness" trap.

---

## (b) Known-findings status at HEAD

### Deep analysis 2026-06-09 (`memory-storage-retrieval-deep-analysis-2026-06-09.md`)

| ID | Was | Verdict at `2f6a193` | Evidence |
|---|---|---|---|
| S1 Leg-1 RRF-vs-similarity dedup | P1 | **FIXED** | `facts.py:1400-1485` raw-cosine probe; `heart.py:369-380` `find_similar_facts`; fact_extractor rewired (PR #495) |
| S2 knowledge_extractor 0.85 rank-score | P1 | **PARTIALLY-FIXED** | dedup math fixed (`knowledge_extractor.py:117-130` raw cosine); still passes **no** `source_episode_id`/`source_text` (`:133-139`) → survivors remain ungrounded for admission |
| S3 0.80 threshold swallows contradiction band | P1 | **FIXED** | `_classify_dupe_in_band` (`facts.py:896-954`) + `_apply_band_action` (`:956-997`) + `_confirm_duplicate` date merge (`:999-1017`) — but see net-new **HT-2** (the date-bypass branch re-opens a sibling hole) |
| S4 NULL-embedding facts, no backfill | P2 | **STILL-LIVE** | `facts.py:439-443` swallow → `:549` insert; no `embedding IS NULL` repair consumer for facts |
| S5 transaction spans ≤4 LLM calls + dup race | P2 | **STILL-LIVE (slightly worse)** | `facts.py:480` (band classifier — new), `:499` (admission utility), `:528` (actionability), `:686` (contradiction), `:856-858` (supersession) all inside `_learn`'s session |
| S6 `fact_learned` emitted pre-commit | P2 | **STILL-LIVE** | `heart.py:313-324` |
| S7 ~10 embeds per stored fact | P2 | **FIXED (cache)** | `embeddings.py` bounded LRU (`NOUS_EMBEDDING_CACHE_SIZE=1024`, packed float32) |
| S8 sleep MERGE learn() without exclude_ids | P2 | **STILL-LIVE** | `sleep_handler.py:930-938` (`contradiction_resolution` learn has no `exclude_ids`); same for F031 merge |
| S9 dedup probes inflate recall stats | P2 | **FIXED** | `facts.py:1414-1418` (no `_fire_track_access` in dedup probe) |
| S10 `_find_duplicate` seq scan | P2 | **FIXED** | `facts.py:1078-1090` plain distance ORDER BY LIMIT 20 + `set_local_ef_search` |
| S11 exclude_ids not in `_supersede_by_subject` | P2 | **FIXED** | `facts.py:585-590, 837-851` |
| S12 reflection facts as `category="rule"` | P2 | **STILL-LIVE** | `layer.py:1777-1783` (boundary; spot-verified) |
| S13 batch (P3) | P3 | **MOSTLY STILL-LIVE** | `learn_fact` source discard, `track_access` not agent-scoped (`facts.py:238-247`), `_create_graph_edge` DEBUG swallow (`:216-217`), stale `FactDetail` when contradiction handling deactivates the new fact (`:602` vs `:734-737`), unlocked confidence decrements (`:755, :987, :1316`), event-bus overflow drop. **FIXED:** Settings re-instantiation (`search.py:71-99` env-fingerprint cache). **PARTIALLY-FIXED:** F075 top-1 date bypass (date preference now scans top-20, `facts.py:1096-1101`) |
| E1 F067↔F069 chunk-index collision | P1 | **FIXED** | `episode_summarizer.py:259-300` — MAX+1 under the same `ingest_document:{episode_id}` advisory lock + per-kind idempotency check |
| E2 F055 WM wholesale clobber | P1 | **FIXED** | `working_memory.py:430-547` merge-by-ref_id, curated-wins, combined-cap; real snippets threaded (`residual_activation.py:254-265`, `tools.py:789-797`) |
| E3 500-char/turn transcript truncation | P2 | **STILL-LIVE** | `layer.py:390`, `:1159` |
| E4 end_session pops state pre-persist | P2 | **STILL-LIVE** | `layer.py:1723-1724`, swallow at `:1754-1755` |
| E5 F060-recovered episodes invisible | P2 | **STILL-LIVE** | recovery loop A never sets `ended_at`/`outcome`; `episodes.py:373, 383, 516, 526` — and now subsumed by net-new **HT-1** (the whole leg is dead, not just recovered rows) |
| E6 episode dedup shares episode across sessions | P2 | **STILL-LIVE** | `episodes.py:71-85` (no session_id scope), `_end` overwrites unconditionally `:229-239` |
| E7 chunk persistence one-shot/unretryable | P2 | **PARTIALLY-FIXED** | manual `scripts/backfill_episode_chunks.py` (+`--repair-dialogue`) shipped in #495; still no automatic retry on the live path (`episode_summarizer.py` guard) |
| E8 WM load_item no ref_id dedup | P2 | **STILL-LIVE** | `working_memory.py:133-145` appends unconditionally (residual path got dedup; curated path didn't) |
| E9 batch (P3) | P3 | **MOSTLY STILL-LIVE** | `cleanup_stale` one-transaction batching (`working_memory.py:364-386` — single commit at `:386` defeats the stated lock-avoidance); outcome always `'success'` (`layer.py:1749`); chunk recall surfaces deactivated episodes (`retrieval_pipeline.py:913-922`); `record_surfaced` bare `create_task` GC-eligible (`tools.py:798`); WM row resurrected after end_session (`working_memory.py:530-544` creates if missing); ORM/DB constraint-name coupling on WM upsert (`working_memory.py:58`). **PARTIALLY-FIXED:** concurrent double-summarize for chunks only (per-kind existence check under lock) |
| G1–G9 graph writer defects | P2/P3 | **STILL-LIVE (carried)** | PR #495's `graph_densifier.py` delta is embed-cache plumbing only; reverse-duplicate edges, inverted relations, four weight spaces, co_occurrence orphan mask, dead interrupt, no failed-orphan memory, CE-mode threshold trap, dead F070.1 cycle hook — all unchanged |
| D1 decision delete dangles edges | P1 | **FIXED** | `brain.py:565-600` explicit edge DELETE; `sql/migrations/060_memory_integrity.sql` cleanup |
| D2 query embedded 4–7× | P2 | **FIXED (cache)** | embedding LRU; calls still issued, served from cache |
| D3 sessions across LLM/embeds | P2 | **STILL-LIVE** | same sites as S5 + `heart._recall` embeds in-session |
| D4 missing lower(subject) index | P2 | **FIXED** | migration 060 `idx_facts_agent_subject_lower` |
| D5 chunk recall ignores episode soft-delete | P2 | **STILL-LIVE** | `retrieval_pipeline.py:913-922` (no JOIN on `episodes.active`) |
| D6 embedding-space mixing unguarded | P2 | **STILL-LIVE** | no boot assert/model marker; prod runs `text-embedding-3-large` against code default small |
| D7 migrator concurrency/checksum | P2 | **STILL-LIVE** | `migrator.py` unchanged |
| D8 SA CTE no visited-set | P2 | **STILL-LIVE / UNREACHABLE-today** | `spreading_activation.py` unchanged |
| D9 censor vector scan shape | P2 | **PARTIALLY-FIXED (by design)** | read-only `_semantic_search` index-served (`censors.py:403-420`); enforcement `_semantic_match` deliberately exact (`:272-289`, codex P1 — accepted trade) |
| D10 batch | P3 | Settings re-instantiation **FIXED**; rest **STILL-LIVE** |
| R1 §14 floorless body preload | P2 | **FIXED** | `context.py:1445-1449` applies `procedure_score_floor` to raw-cosine scores (`procedures.py:493-570` provides the cosine probe) |
| R2 recall_deep contract lie | P2 | **FIXED** | docstring + schema updated in `tools.py` (PR #495) |
| R3 conversation-frame procedure hole | P2 | **STILL-LIVE** | `schemas.py` frame budgets unchanged |
| R4 batch | P3 | **STILL-LIVE** (F081 char-based embed cap `procedures.py:60-61`; §14 N+1 etc.) |

### Retrieval whitepaper 2026-06-08 (register at HEAD)

| ID | Verdict | Evidence |
|---|---|---|
| B1 censor floor ≥0.7 in pool | FIXED on recall_deep default path (F080, `retrieval_pipeline.py:363-365`); **STILL-LIVE on Heart.recall direct surfaces** — see net-new **HT-3**; floor unchanged `censors.py:266, 396` + keyword matches hardcoded **1.0** (`censors.py:486`) |
| B2 procedure boost >1.0 | same shape as B1 (`procedures.py:452-467`); LATENT on default path, LIVE on direct surfaces |
| B3 embed-fail leg raw ts_rank | **STILL-LIVE** (`search.py:281-283`; mirror `facts.py:1668-1669`) |
| B4 mixed-scale rerank sort | **STILL-LIVE (residual mix)** (`retrieval_pipeline.py:282-283`: RRF facts vs raw-cosine chunks vs graph-composed scores) |
| B5 stats hardcoded False | **STILL-LIVE** (`retrieval_pipeline.py:301-302`) |
| B6 contradiction ids exclude facts/chunks | **STILL-LIVE** (`:679-686`) |
| B7 no global top-K | **STILL-LIVE** (`:224-315`) |
| B8 one-directional contradiction attach | **STILL-LIVE** (`:1211-1224`) |
| B9 dead recency_date arg | **STILL-LIVE** (harmless; `:1171, :1187`) |
| B-cog-A…G | **STILL-LIVE (carried — cognitive boundary)** |
| B-graph-5 | changed by prod overlay (`NOUS_GRAPH_NEIGHBOR_SEED_SCORE_ENABLED=true`); seed-score scoring live (`:976-993`) |
| B-graph-6 boost before graph_expanded append | **STILL-LIVE** (`:249-254`) |
| B-graph-8 density recompute per recall | **STILL-LIVE** (`:586-600`) |
| B-graph-9 dup decisions Stage 2/4 | **STILL-LIVE** (`:430` vs `:578`) |
| B-hs-8 query embedded 3–4× | **FIXED (cache)** |
| B-hs-4 Tier-1 score=1.0 | **STILL-LIVE** (`facts.py:1387`) |
| B-hs-10 `_search_all` fork | **STILL-LIVE** (`facts.py:1609-1704`) |
| B-rm-1 date-aware boost dead | **STILL-DEAD** (config-only; zero consumers outside `config.py`) |
| B-rm-3 CE head/tail scales | **LATENT** (`reranker.py:103-167`; CE off in prod) |
| B-rm-7 cache-write desync / B-rm-8 monotonic budget | **STILL-LIVE** (`query_expansion.py:498-526`, `:447`) |

### Procedure subsystem audit 2026-06-06 (§2 bugs)

| Bug | Verdict | Evidence |
|---|---|---|
| 1 dead auto-retirement (`search_procedures("auto:")`) | **STILL-LIVE** | `procedure_learner.py:476` unchanged; tags still not in `search_tsv` or embed text (`procedures.py:31-61`) |
| 2 `get_evolution_candidates` no prod consumer | **STILL-LIVE** | zero callers outside `nous/heart/` |
| 3 coarse turn-blanket reinforcement | **STILL-LIVE** (boundary) |
| 4 conversation frame 0 procedure budget | **STILL-LIVE** (= R3) |
| 5 recall_deep procedures unreinforced | **MOOT-SHIFTED** — F080 removed procedures from the default recall pool entirely; reinforcement gap remains for explicit-type calls |
| 6 `neutral_count` dead | **STILL-LIVE** | written only on explicit `record_outcome("neutral")` (`procedures.py:313-314`); ignored by `_compute_effectiveness` (`:958-968`) |
| 7 vestigial columns | **STILL-LIVE** | `related_procedures`/`censor_ids` "reserved" (`schemas.py:263-264`) |
| 8 embedding failure → silent NULL | **PARTIALLY-FIXED** | `_embed_with_retry` (`procedures.py:112-132`): retry + ERROR log; still stores NULL |
| 9 learn_skill count reset | **FIXED** | `update_body` in-place refresh (`procedures.py:181-232`) + F081 |
| 10 no REST retire endpoint | **STILL-LIVE** | no `retire` route in `rest.py` |
| §6-B1 resurrection loop | **FIXED** | `superseded_by`/`archived_at` guards (`procedures.py:884-892, 896-916`); reactivate name-collision precheck (`:802-819`); migrations 057/058 |

---

## (c) NET-NEW findings register

### P1

**HT-1 — Episode semantic search is structurally dead for the production lifecycle. [P1, LIVE]**
`EpisodeManager._end` sets `active = False` on every closed episode (`episodes.py:235`, shipped PR #79, 2026-02-28). Every episode hybrid search runs through `hybrid_search` with the default `active_filter=True` → `AND t.active = true` (`episodes.py:519-527` → `search.py:241-242`), plus `extra_where="AND t.outcome != 'abandoned'"` (`episodes.py:516, 526`). SQL three-valued logic excludes NULL-outcome rows from `!= 'abandoned'`. State space:
- **Closed** episodes (the searchable corpus): `active=false` → excluded.
- **Ongoing / F060-recovered / stuck-open** episodes: `outcome IS NULL` → excluded.
- **Marked-abandoned** (F060.2): `active=false AND outcome='abandoned'` → doubly excluded.
- Only `active=true AND outcome NOT NULL` rows match — produced by no live writer; only bulk eval ingest and pre-PR-#79 rows qualify.

Affected consumers (all return ~nothing in prod shape): `Heart.recall` episode leg (`heart.py:951`), recall_deep Stage 1 episodes, ContextEngine "Related Episodes" section (`context.py:918`), heartbeat checks (`heartbeat/checks.py:282, 888`), censor actions (`censor_actions.py:123`), and `search_recent_by_embedding` (`episodes.py:607-616`: `active = true AND outcome != 'abandoned'`) — which kills the cognitive pre-turn episode-dedup probe (`layer.py:1649`, always returns no match → its >0.85 skip never fires) and the summarizer's similar-episode (ep↔ep `related_to`) linking. Eval corpora (LME/BEAM loaders) insert episodes directly with `active=true` + outcome set, masking this completely — the documented "eval fixtures must match prod input thinness" trap.
*Why prior audits missed it:* E5/E9 analyzed the `outcome != 'abandoned'` NULL semantics for recovered rows and the always-`'success'` outcome but treated normally-closed episodes as searchable; the `active=False`-on-close × `active_filter=True` interaction was never composed.
*Fix:* episodes' `active` is overloaded (ongoing-flag AND soft-delete). Either pass `active_filter=False` + `AND (t.outcome IS NULL OR t.outcome != 'abandoned')` from `EpisodeManager._search`/`_search_recent_by_embedding`, or stop setting `active=False` in `_end` and keep `active` strictly as the soft-delete bit (one-time backfill `UPDATE heart.episodes SET active=true WHERE ended_at IS NOT NULL AND outcome != 'abandoned'`).

**HT-2 — F075 date-bypass survivors are immediately re-collided by `_find_contradiction`, which has no event-date guard; an UPDATE verdict deactivates the older dated event. [P1, LIVE]**
When `_find_duplicate` returns a hit whose `event_date` differs from the candidate's, `_learn` bypasses dedup (`facts.py:473-478`, `pass # treat as new event`) — but unlike the S3 band path (`:491` appends `dupe.id` to `exclude_ids`), the bypass branch leaves `exclude_ids` unchanged. The post-insert contradiction scan then runs with `safe_excludes = exclude_ids + [fact.id]` (`:608-613`) and finds the same dated sibling (similarity was above the prod 0.80 dedup threshold, so typically inside the 0.85–0.95 contradiction band). The F027 routing in `_find_contradiction` has **no event-date check**: `UPDATE`/`current=new`/conf ≥0.8 sets `old_fact.superseded_by`, `active=False`, confidence ×0.3 (`facts.py:719-731`). Two same-shape dated events ("API key obtained March 10" vs "rotated March 12") are exactly the pairs the classifier's own prompt examples route to UPDATE (`facts.py:64-68`). Net: the distinct-event preservation F075 exists for is undone one statement later — the older event is silently deactivated. `_supersede_by_subject` got the date-disagreement skip (`:845-851`); `_find_contradiction` never did. Prod: `NOUS_TEMPORAL_EXTRACTION_ENABLED=true`, threshold 0.80, LLM wired → LIVE.
*Fix:* in the date-bypass branch, append `dupe.id` to `exclude_ids` (mirroring `:491`); additionally skip the UPDATE/supersede routing in `_find_contradiction` when both facts carry differing non-null `event_date`.

### P2

**HT-3 — F080 coherent ranking exists only in `run_recall_pipeline`; `Heart.recall` direct surfaces still merge censors and boosted procedures into the knowledge pool. [P2, LIVE]**
The censor/procedure exclusion is applied in the pipeline only (`retrieval_pipeline.py:363-365`). Direct callers of `heart.recall` with default types get all four legs: the MCP server's recall (`mcp.py:263` for `"all"`, `:278` for typed) and censor trigger-actions (`censor_actions.py:90`). There the whitepaper's B1/B2 remain fully live: censors enter at raw cosine ≥0.7 (`censors.py:266, 396`) — or at a hardcoded **1.0** when the regex/substring keyword path matches (`censors.py:486`, also the fallback when query embedding fails) — and procedures at boosted RRF >1.0 (`procedures.py:452-467`), displacing facts at the `[:limit]` cut inside `heart._recall` (`heart.py:1133-1135`). Prod: `NOUS_MCP_ENABLED=true`.
*Fix:* move the knowledge-only default (or score normalization) into `Heart._recall` so every consumer inherits it; keep explicit `types=` honored.

**HT-4 — Subtask `complete()`/`fail()` are unconditional, unguarded UPDATEs: a stale worker can overwrite a reclaimed/re-run/cancelled task's state; no agent scoping. [P2, LIVE]**
`complete` (`subtasks.py:143-149`) and `fail` (`:197-203`) update `WHERE Subtask.id == :id` with no `status` precondition and no `agent_id` filter. `reclaim_stale` (`:307-324`) flips timed-out `running` rows back to `pending`; if the original worker's coroutine is still alive (event-loop stall, generous `wait_for`, prod `NOUS_SUBTASK_DEFAULT_TIMEOUT=2000`s), its terminal write lands on a row another worker has since re-dequeued — marking the in-flight re-run `completed`/`failed` with the stale attempt's result, corrupting `final_outcome`/`report_jsonb` and the F061 dashboard. Same class: `mark_delivered` (`:299-305`), `get` (`:234-237`) are id-only.
*Fix:* add `WHERE status='running' AND worker_id=:worker AND agent_id=:agent` preconditions to the terminal transitions; log when rowcount==0.

### P3

**HT-5 — `_update_summary` / `_bump_compaction_count` not agent-scoped. [P3, LIVE]**
`episodes.py:137` and `:187-188` fetch by `Episode.id` only — the only two Heart write paths without the `agent_id` filter every sibling `_get_*_orm` applies (recurring-miss class). UUID guessing is impractical, but multi-agent readiness is violated; `schedules.advance/deactivate/get` (`schedules.py:140, 174, 183`) share the pattern.

**HT-6 — `schedules.create(schedule_type="once", fire_at=None)` creates a permanently dormant active schedule. [P3, LIVE]**
`schedules.py:43-44` sets `next_fire = None` for `once` without validating `fire_at`; `get_due` filters `next_fire_at <= now` (`:88`) → the row never fires and never deactivates, but shows as active in `list()`. The recurring branch raises on missing timing (`:51`); the once branch doesn't.

**HT-7 — `resolve_thread` removes every containing-match, not the documented first match. [P3, LIVE]**
Docstring says "remove first match" (`working_memory.py:246, 267`); the comprehension at `:268` drops **all** threads whose description contains the (possibly very short) needle — `resolve_thread("fix")` wipes every thread mentioning "fix".

**HT-8 — `upsert_residual_items` cold-start insert race loses the write. [P3, LIVE]**
Two concurrent `record_surfaced` tasks on a fresh session both see `existing is None` (`working_memory.py:470-471`) and both `session.add` (`:530-544`); the loser's commit raises on the unique constraint and is swallowed at WARN by `record_surfaced` (`residual_activation.py:278-283`) — one recall's residual set is silently dropped. Bounded impact (next recall rewrites).

**HT-9 — Residual SA seeds are all typed `"fact"`. [P3, INERT]**
`seed_for_spreading` hardcodes `node_type="fact"` (`residual_activation.py:182`); a residual episode/chunk/decision id seeded into the spreading-activation CTE matches no edges under `(source_id, source_type)` polymorphic keys — those seeds are no-ops. Inert today only because SA is density-gated unreachable.

**HT-10 — Residual boost clamp `min(1.0, …)` can *demote* boosted procedures. [P3, LIVE prod-TODAY]**
`boost_scores` assumes scores ≤1.0 (`residual_activation.py:209`); a residually-activated procedure carrying the F037 boosted score (e.g. 1.26) is reset to 1.0 — receiving a "boost" lowers it below an unboosted 1.2 sibling. Numeric-space mismatch of the recurring class. Narrow: requires procedures in the pool (prod-TODAY default; explicit-type / MCP at HEAD-deploy) + `NOUS_RESIDUAL_ACTIVATION_ENABLED=true` (prod).

**HT-11 — Subtask pending-limit check is TOCTOU. [P3, LIVE]**
`subtasks.py:49-58` SELECT-count-then-INSERT with no lock/constraint — concurrent creates exceed `_MAX_PENDING=5`. Advisory only; harmless overshoot.

**HT-12 — Censor enforcement side-effects ride the caller's injected transaction. [P3, LIVE]**
`check()` increments `activation_count`/`last_activated` and emits `censor_triggered` in the caller's session (`censors.py:188, 220-250`); the pre-turn caller (`layer.py:777`) commits much later — a failed turn rolls censor telemetry back, undercounting activations the F078 UI relies on.

**HT-13 — `heart._recall` with an injected session cascades sub-search failures. [P3, LIVE-edge]**
Documented choice (`heart.py:849-855, 977-989`): no rollback on caller-owned sessions, so one failed leg aborts the transaction and every later leg fails with `InFailedSQLTransaction`. The only prod injected-session caller is censor trigger-actions (`censor_actions.py:90`) — a single bad leg silently empties a censor's prescribed recall *and* poisons the surrounding pre-turn transaction.

### INFO

- **`heart.py:822-824, 991-994`** — `recall()` doc/comment still claims per-type scores "are directly comparable"; the 2026-06-08 whitepaper establishes the opposite. Doc drift that keeps inviting B1-class regressions.
- **`heart.py:905-913, 948`** — `search_map` values `("episodes", {"limit": …})` are dead; the loop dispatches via if/elif.
- **`procedures.py:392`** — `extra_where = " AND t.active = true"` duplicates `hybrid_search`'s own active clause (harmless double filter).
- **`censors.py` `escalation_threshold`** — F078 removed auto-escalation; the column/DTO field (`censors.py:788`, `schemas.py:334`) is consulted by nothing.
- **`content_date_extractor.py:60`** — comment says "or 01/05/24" but the regex requires a 4-digit year.
- **`retrieval_pipeline.py:912`** — chunk query vector serialized at `%.6f` (other sites use full `str(float(v))`); negligible precision asymmetry.
- **`.env.prod-snapshot`** — `NOUS_WORK_QUEUE_FILE_JSONL_PATH=/tmp/nous_workspace/...` vs `NOUS_WORKSPACE_DIR=/tmp/nous-workspace` (underscore vs hyphen). If the queue file is written under the workspace dir, the poller watches a path nothing writes. Deployment config, not code — flagging for operator verification.
- **`admission.py:153`** — utility-scoring payload sets `cache_control` on all three tiny blocks; harmless but pointless cache writes per fact.

---

## (d) Dead-code inventory

| Item | Location | Status |
|---|---|---|
| `content_date_extractor.py` — entire module (324 lines, regex+dateparser extraction, `format_dates_inline`) | `nous/heart/content_date_extractor.py` | **DEAD** — zero imports anywhere in `nous/` (F075 went the LLM-extraction route instead) |
| `has_cjk()` | `cjk.py:41-43` | no callers (`count_cjk_aware_words` is the only consumed symbol) |
| `get_evolution_candidates` | `procedures.py:576-647` | computed, no prod consumer (procedure-audit bug 2, unchanged) |
| `neutral_count` | `procedures.py:313-314`, ignored at `:958-968` | written by no auto path, read by nothing |
| `related_procedures` / `censor_ids` on procedures | `schemas.py:263-264` | reserved columns, never written |
| `escalation_threshold` on censors | `schemas.py:334`, `censors.py:788` | orphaned by F078 |
| Date-aware boost settings (F075 L3) | `config.py` only | flags with no consumer (whitepaper B-rm-1, unchanged) |
| `search_map` tuple values | `heart.py:905-913` | dead data |
| `_dedup_decisions` | `context.py` (boundary) | still defined, never called (B-cog-C) |

---

## (e) Improvement opportunities

1. **Disambiguate episode `active`** (HT-1 root cause): split "ongoing" from "soft-deleted" — either a dedicated state column or stop flipping `active` in `_end`. Add a prod-shape retrieval probe to the eval harness that exercises the real lifecycle writers (start→end→summarize) instead of bulk-ingested rows, so filter-composition kills can't hide again.
2. **Make `Heart._recall` own the pool policy** (HT-3): F080's exclusion (and any future normalization) belongs at the merge site, not in one caller. The MCP surface should not be a time capsule of pre-F080 ranking.
3. **One event-date contract across all three write-time passes** (HT-2): `_find_duplicate` (has it), `_supersede_by_subject` (has it), `_find_contradiction` (missing). A small shared `dates_disagree(a, b)` helper applied at all routing sites prevents the next drift.
4. **Status-guarded subtask state machine** (HT-4): terminal transitions conditioned on `(status, worker_id, agent_id)` with rowcount checks; this also gives observability on stale-worker writes.
5. **NULL-embedding repair sweep for facts** (S4): the F081 skill backfill and `reembed_all` for procedures exist; facts — the highest-volume table — still have no `embedding IS NULL` consumer. A bounded sleep-phase pass closes the last silent-invisibility hole.
6. **Move LLM verdicts out of `_learn`'s transaction** (S5/D3): compute admission-utility, actionability, band/supersession/contradiction classifications against snapshots, then apply in a short write transaction. The band classifier added by #495 made the in-transaction LLM ceiling 5 calls.
7. **Retire or wire the dead surface area** (§d): especially `content_date_extractor.py` and the auto-retirement FTS bug (procedure bug 1) — the latter is a two-line fix (`WHERE tags && ARRAY['auto:...']`) that has been known since 2026-06-06.
8. **Censor keyword-match score**: if censors ever re-enter a ranked pool, the hardcoded 1.0 (`censors.py:486`) must carry a calibrated score or a non-score marker; today it is the single largest score-space outlier left in the codebase.

---

*Files of record:* all of `nous/heart/` (heart, facts, episodes, procedures, censors, censor_actions, working_memory, residual_activation, search, query_expansion, admission, actionability, content_date_extractor, subtasks, subtask_validator, subtask_report, schedules, schemas, chunking, document_chunker, reranker, cjk, hashing, work_queue, `__init__`), `nous/api/retrieval_pipeline.py`; spot-verified wires into `nous/api/{tools,mcp}.py`, `nous/cognitive/layer.py`, `nous/handlers/{episode_summarizer,knowledge_extractor,sleep_handler,procedure_learner}.py`, `nous/brain/brain.py`, `nous/main.py`, `sql/init.sql`, `sql/migrations/060_memory_integrity.sql`, `.env.prod-snapshot` (deployment overlay only).
