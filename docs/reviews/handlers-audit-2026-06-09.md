# Event Handlers Subsystem — Deep Code Audit (2026-06-09)

**Scope:** `nous/handlers/*` (all 19 modules, every line read) + `nous/events.py`, with call-path tracing into `nous/heart/*`, `nous/brain/*`, `nous/cognitive/layer.py`, `nous/main.py` (findings there reported only where the handler is the consumer/victim).
**Method:** code-only; reachability checked against `nous/config.py` defaults AND `.env.prod-snapshot`. Prior audits cross-referenced: `docs/reviews/memory-storage-retrieval-deep-analysis-2026-06-09.md` ("DA"), `docs/reviews/rubric-admission-audit-2026-06-09.md` ("RA"), `docs/reviews/procedure-subsystem-audit-2026-06-06.md` ("PA"). Findings already shipped in PR #495 (fact_extractor / knowledge_extractor rank-score dedup, F067 chunk collision) were verified FIXED at HEAD and are not re-reported.

Reachability tags: **LIVE** (fires on prod config), **LATENT** (needs an uncommon-but-real condition), **INERT** (flag-gated off), **DEAD** (no caller/emitter).

---

## 1. How it actually works

`EventBus` (`nous/events.py`) is a single in-process asyncio queue (max 1000, overflow **drops** with a WARN — `events.py:180-184`). One background task dequeues and dispatches each event: first to the DB persister (`main.py:83-91` → `brain.emit_event`, which stamps `agent_id` from the Brain instance, not the event — `brain.py:1723-1724`), then to all subscribed handlers concurrently via `gather` with per-handler error isolation (`events.py:246-274`). `stop()` cancels the loop then drains the residual queue (`events.py:194-214`).

Handlers are constructed in `main.py:244-459`; subscription happens in each handler's `__init__`. Long-running loops (session monitor, task scheduler, subtask workers, decision-review sweep) are independent asyncio tasks, not bus handlers.

### Event → handler contract table

| Event | Emit site(s) | Payload keys emitted | Subscribers (keys read) | Contract verdict |
|---|---|---|---|---|
| `turn_completed` | `cognitive/layer.py:1179-1198` | session_id, frame, surprise_level, decision_id, has_errors, **is_background** | SessionTimeoutMonitor.on_activity (session_id, is_background) | OK (#462 fix verified) |
| `message_received` | **NONE — never emitted anywhere** (runner uses synchronous `monitor.touch()` instead, `api/runner.py:358-368,931-938`) | — | SleepHandler._on_wake; SessionTimeoutMonitor.on_activity | **SEVERED — see HD-3** |
| `session_ended` | `cognitive/layer.py:1805-1822` | session_id, episode_id, transcript, reflection, had_reflection, facts_extracted | EpisodeSummarizer (episode_id, transcript) OK · DecisionReviewer (session_id) OK · OutcomeDetector (**summary** — absent) · SessionTimeoutMonitor (session_id) OK · tool-cache cleanup (session_id) OK | **PARTIAL — HD-9 (known RA-R1)** |
| `episode_summarized` | `episode_summarizer.py:336-348` | episode_id, summary, candidate_facts, transcript | FactExtractor (all four) | OK |
| `conversation_compacting` | `cognitive/layer.py:1696-1701` | message_snapshot | KnowledgeExtractor (message_snapshot) | OK |
| `fact_learned` | `heart/heart.py:314-324` | fact_id, content, category, subject, modifies | FactGraphLinker (fact_id, content) | OK |
| `decision_recorded` | `brain/brain.py:408-412` (emitted **before** the recording transaction commits) | decision_id, category | DecisionGraphLinker (decision_id) | OK keys / **race — HD-19** |
| `procedure_stored` | `heart/heart.py:438-470` (store + update_body) | procedure_id, name, domain, description, tags | ProcedureGraphLinker (procedure_id, description, domain) | OK |
| `sleep_started` | session_monitor.py:345-351 (agent_id="system") · rest.py:1017-1021 (manual) | idle_seconds / manual | SleepHandler.handle (agent_id only) | OK |
| `sleep_completed` | `sleep_handler.py:514-525` | phases_completed, interrupted, facts_created, … | no bus subscriber; consumed from DB by `dashboard_queries.py:594` | OK (persisted under brain agent_id) |
| `outcome_signals_detected` | `outcome_detector.py:112-122` | episode_id, signals | CorrectionExtractor (both) | OK |
| `subtask_outcome` | `subtask_executor.py:118-123`, `subtask_worker.py:413-418` | subtask_id, final_outcome, ok, attempts, tokens, … | none on bus (DB telemetry only) | OK |
| `subtask_completed` / `subtask_failed` | `subtask_worker.py:446-453` | subtask_id (hex, no dashes), task, result/error | none on bus (DB telemetry) | OK (ID format inconsistent with `subtask_outcome` — INFO-5) |

### Sleep cycle (the previously unaudited core)

`SleepHandler.handle` spawns `_run_sleep` as a detached task (`sleep_handler.py:371-381`). Phase order (`:420-513`): review(stub) → prune(stub) → compress(stub) → reflect → resolve_contradictions → stale_scan → cluster_consolidation → recover_abandoned (F060) → graph_densification (F040) → relink_open_episodes (F057) → prune_dead_edges (F053) → prune_hub_snapshots (F065) → generalize (F012 K-lines) → evolve_rubric (F024-3b). Every phase is individually try/except-wrapped (one failing phase never kills the cycle — good), returns bool for `phases_completed`, and checks `self._interrupted` between phases — which is the problem: `_interrupted` can never become True (HD-3). `sleep_completed` is emitted even when phases failed; failure is only visible as a missing entry in `phases_completed` plus `*_error` keys some (not all) phases write into `sleep_stats`.

### Worker/scheduler plumbing

- **Queue claiming is race-free:** `subtasks.dequeue` uses `SELECT … FOR UPDATE SKIP LOCKED` and flips status→running in the same transaction (`heart/subtasks.py:81-105`). Priority is an int map {urgent:50, normal:100, low:200} (`:16`) — ordering correct.
- **Timeout/cancel:** `_process_subtask` wraps execution in `asyncio.wait_for`; on timeout it persists `timed_out` using the `HardenedRunState` side channel and emits the outcome event itself; `execute_hardened` deliberately skips persist+emit on CancelledError so DB and event stream can't disagree (`subtask_executor.py:263-282,410-482`). F049 cleanup (`end_conversation` under `shield(wait_for(...))`) runs in `_execute_subtask`'s `finally` on every exit path including cancellation (`subtask_worker.py:288-313`). This chain is correct.
- **Scheduler:** `get_due` → per-schedule debounce on `metadata->>'schedule_id'` (fail-open) → create subtask → continuation state writes (each soft-failed) → advance/deactivate (`task_scheduler.py:113-265`). Missed fires don't storm: `advance` recomputes from `fired_at=now` via croniter (`heart/schedules.py:151-153`).
- **Session monitor:** idle math is monotonic-clock based; `touch()` at run_turn start closes the close-mid-turn race; #462 background-exclusion verified end-to-end (emit site `layer.py:1187`, gate `session_monitor.py:329-343`); closures fan out via gather with CancelledError re-raise (shutdown can't hang) and an `abort_if` activity-resumed check.

---

## 2. Findings register

### P1

#### HD-1 — ProcedureLearner dedup gate compares an RRF *rank* score to 0.85 → all three auto-procedure creation pathways permanently suppressed — **LIVE**
`nous/handlers/procedure_learner.py:550-564`; consumed at `:310,437` and by monitor pathway 3 at `nous/cognitive/monitor.py:279`.
`_is_duplicate` runs `search_procedures(query_text, limit=1)` and treats `score > 0.85` as duplicate. But `procedures.search` returns **normalized RRF rank scores, then multiplies by the F037 utility boost** — the code's own sibling comment states it: *"search() returns normalized RRF scores that encode RANK, not closeness — the nearest procedure scores ~0.95 for ANY query"* (`nous/heart/procedures.py:502-506`, boost at `:452-467`). With 62 active procedures in prod, the top hit of any query scores ≈0.95 → `_is_duplicate` ≈ always True → decision-cluster (F012 pathway 1), episode-lesson (pathway 2), and monitor-recovery (pathway 3) procedures are silently skipped right before `store_procedure`. `procedure_learning_enabled` defaults True (`config.py:682`) and the sleep generalize phase runs it every cycle. This is the same bug class as DA-S1/S2 (fixed for facts in PR #495 via `find_similar_facts`) — unfixed here, and it mechanically explains the PA prod observation "K-line = 0" (PA attributed duplication to the skill path but did not identify this gate as always-true; PA's "phrasings slip past 0.85" model is inverted).
**Fix:** probe with `heart.find_similar_procedures` (raw cosine, exists since §14 — `heart.py:505-513`) instead of `search_procedures`, keep 0.85 as a real cosine threshold.

#### HD-2 — Sleep UPDATES-prefix supersession guard compares an RRF rank score to 0.80 → wrong-fact supersession (data corruption) — **LIVE**
`nous/handlers/sleep_handler.py:759-805` (`_handle_updates_prefix`).
The F031 orient loop instructs the reflection LLM to mark updated knowledge with `UPDATES: <existing content>` (`:739-744`). The handler then takes `search_facts(referenced_content, limit=3)` and supersedes `results[0]` unless `best_match.score < 0.80` — a guard added specifically "to prevent wrong-fact supersession (review fix from devil's advocate P0-1)" (`:752-753`). `facts.search` returns normalized RRF rank scores (`heart.py:357-367`, `heart/search.py:162-178`): the top hit scores ≈0.98 when it appears in both legs and ≈vector_weight (~0.7) when vector-only — in neither case does the number measure similarity to the referenced content. Consequences: (a) when the rank score ≥0.80, `supersede_fact` deactivates whatever fact happened to rank first — possibly an unrelated fact sharing keywords — and chains `superseded_by` to new content (irreversible without manual SQL); (b) when vector-only (~0.7 < 0.80), a genuinely-matching fact is *not* superseded and a duplicate is learned instead. The guard is decorative. Reachable every sleep cycle: `sleep_enabled=true` in prod, `_phase_reflect` runs with the LLM, and the orient context actively solicits the UPDATES form.
**Fix:** same as HD-1 — probe with `find_similar_facts(referenced_content)` and threshold the raw cosine (mirror of the PR #495 S1 fix, which covered fact_extractor/knowledge_extractor but not this third site).

### P2

#### HD-3 — `message_received` has no emit site → sleep interruption is fully severed — **LIVE**
Subscriptions: `sleep_handler.py:363` (`_on_wake`), `session_monitor.py:88`. Emitters: **none** (grep over `nous/`; `api/runner.py:358-368,931-938` explains the runner deliberately calls `monitor.touch()` *instead of* emitting the event). Therefore `SleepHandler._interrupted` is never set: every `if self._interrupted` check in all 14 phases (`sleep_handler.py:432-512` and per-item checks at `:628,824,1162,1607,1800`) is dead, the module docstring's "Sleep is interruptible" contract (`:14-16`) is false, and a user message arriving mid-sleep contends with reflection/contradiction/cluster/densification LLM+DB work for the full cycle duration (prod backfill caps 200+200). DA line 128 noted the densifier-flag copy is "always-False" but scoped it to F040; the root cause — no emitter at all — kills interruption for *every* phase. The monitor's `message_received` subscription is harmless dead code (touch() covers it).
**Fix:** emit `message_received` (or call `sleep_handler.interrupt()`) from `runner.run_turn` alongside `monitor.touch()`; also propagate into `GraphDensifier.interrupt()`.

#### HD-4 — DecisionReviewer Tier-1 signals systematically mislabel outcomes feeding calibration — **LIVE**
`nous/handlers/decision_reviewer.py:51-135, 195, 255-296`. `decision_review_enabled=True` (config.py:321); handler fires on every `session_ended` AND hourly sweep over all unreviewed decisions <30 days, writing `reviewer="auto"` outcomes into Brain (which drive Brier/calibration).
Three independent label corrupters, evaluated in order, first ≥0.7 confidence wins (`:298-311`):
1. **ErrorSignal** (`:56-74`): any description matching `\b(error|failed|failure|broken|crashed|bug)\b` → outcome=failure at 0.9. A decision *about fixing a bug* ("decided to fix the failing test by …") is auto-labelled a failed decision.
2. **EpisodeSignal** (`:89-110`): maps `episode.outcome` — but `cognitive/layer.py:1747-1749` hardcodes `outcome="success"` for **every** non-trivially-ended session (DA-E9 noted the vocabulary issue; the reviewer consequence is the live damage). So any decision linked to any normally-closed episode auto-reviews "success" at 0.8 regardless of what happened.
3. **FileExistsSignal** (`:117-135`): `Path(path_str).exists()` is resolved against the **process cwd** (the container WORKDIR). Repo-relative paths like `nous/api/tools.py` exist in the image by construction → near-guaranteed "success" at 0.7 for any decision that mentions a repo path.
Net effect: auto-review labels are dominated by lexical accidents, consistent with the F058 finding (Brier 0.252 ≈ random on 401 reviewed decisions).
**Fix:** drop ErrorSignal keyword matching (or require failure language about the *outcome*, not the topic); stop hardcoding episode outcome success at the layer (out of this scope, but the reviewer should not trust it meanwhile); anchor FileExistsSignal to a configured workspace root or delete it.

#### HD-5 — Schedule timezone token parsed then discarded; cron always runs in UTC — **LIVE**
`nous/handlers/time_parser.py:37-39,151-159` captures the tz in `_DAILY_RE` group 3 and comments *"the scheduler layer handles tz conversion"* — but `heart/schedules.py:46-47,151-153` evaluates the cron with `croniter(expr, now_utc)` and no conversion exists anywhere (`rest.py:938-957`, `api/tools.py:2300+` call `parse_every` directly). "daily at 9am EST" fires at 09:00 **UTC** (04:00 EST). Wrong-by-hours behavior on the live `schedule_task` tool; the misleading comment guarantees the next reader assumes it works.
**Fix:** convert h24 to UTC using the captured tz (or reject tz tokens loudly).

#### HD-6 — `parse_when("tomorrow 9am")` schedules **today** 9am (or errors), despite being a documented format — **LIVE**
`nous/handlers/time_parser.py:65-106`. The docstring advertises `"tomorrow 9am"` via "dateutil fallback", but `dateutil_parser.parse(..., fuzzy=True)` *ignores* unknown tokens: "tomorrow" is dropped and "9am" resolves against today's default date. Before 09:00 local: returns today 09:00 → one-shot fires the same day, a day early. After 09:00: `dt <= now` → ValueError "Time is in the past" — confusing but safe. Same fuzzy-token hazard applies to "next monday …" ("next" is dropped; dateutil's bare-weekday resolution is week-relative, not strictly-future).
**Fix:** handle `tomorrow`/`next <weekday>` explicitly before the dateutil fallback.

#### HD-7 — Crash-orphaned `running` subtasks are only reclaimed at startup, and only if already timeout-expired → stuck rows can block their schedule — **LATENT**
`subtask_worker.py:64-68` calls `reclaim_stale()` exactly once at pool start; `heart/subtasks.py:307-324` reclaims only rows where `started_at + timeout < now`. A crash with an in-flight subtask followed by a quick restart (the normal case — prod timeout is 2000s, `.env.prod-snapshot:109`) leaves the row `running` with no live worker, and nothing ever re-checks until the *next* process restart. Until then: (a) the dashboard shows a phantom running task; (b) if the subtask was schedule-fired, `_has_active_subtask_for_schedule` (`task_scheduler.py:79-111`, status in pending/running) returns True every tick → **that schedule silently never fires again** until another restart happens ≥timeout later.
**Fix:** run `reclaim_stale()` periodically (e.g., in the scheduler/worker poll loop), not only at boot.

#### HD-8 — F060 recovery is half-wired: recovered episodes keep `ended_at/outcome` NULL, their candidate_facts are discarded, and summary-fallback text is chunked as dialogue — **LIVE** *(confirms DA lines 96/107/E9 — still true at HEAD)*
`sleep_handler.py:1516-1708` + `episode_summarizer.py:150-202`. `_phase_recover_abandoned_episodes` calls `summarize_episode` which persists `structured_summary` only (`episodes.update_summary` backfills title/summary/lessons but never `ended_at`/`outcome`/`active` — `heart/episodes.py:134-161`), and deliberately does not emit `episode_summarized` (`episode_summarizer.py:163-168`) while the sleep phase doesn't invoke FactExtractor either — so the LLM spend extracts zero facts and the episode stays an open-looking row excluded from `list_recent` (`ended_at IS NOT NULL` filter). The F060.1 summary-fallback additionally pushes the ≤500-char plain summary through `_chunk_and_store_transcript` as `source_kind='dialogue'` when chunks are enabled (prod: `NOUS_EPISODE_CHUNKS_ENABLED=true`).
**Fix:** after successful recovery, set `ended_at`/`outcome` and either emit `episode_summarized` or call the extractor directly; skip chunking for `source_kind == "summary"` inputs.

#### HD-9 — `session_ended` carries no `summary` → OutcomeDetector's scores channel permanently NULL — **LIVE** *(known: RA-R1; verified unchanged at HEAD)*
Emit: `cognitive/layer.py:1805-1812`. Read: `outcome_detector.py:79-81,101`. `self_improvement_scores` is NULL on every `heart.outcome_signals` row; downstream RubricEvolver runs on uniform proxy scores (RA-R2) — and prod has `NOUS_RUBRIC_EVOLUTION_ENABLED=true` (`.env.prod-snapshot:97`), so the sleep evolve_rubric phase burns a weekly cycle that can only drift weights toward uniform. Listed for completeness; remediation owned by the rubric-audit follow-up.

#### HD-10 — `_review_weak_procedures` queries `search_procedures("auto:")` which matches nothing → weak-procedure retirement dead — **LIVE / DEAD-path** *(known: PA bug 1; verified unchanged at HEAD)*
`procedure_learner.py:469-544`. `tags` are in neither `search_tsv` nor the embedding text, so the candidate list is empty every cycle; `stats["weak_reviewed"]` is structurally 0. Note interaction with HD-1: even if retirement worked, nothing auto-learned exists to retire.

#### HD-11 — TaskScheduler can double-fire when post-enqueue lifecycle writes fail — **LATENT**
`task_scheduler.py:182-263`. Failure order is: subtask **created** → continuation writes (soft-failed, OK) → `deactivate()` (once) / `advance()` (recurring). If `deactivate`/`advance` raises (transient DB error), the outer `except Exception` logs and the schedule's `next_fire_at` stays in the past → next tick re-fires. The debounce guard (`:132`) suppresses the duplicate only while the first subtask is still pending/running; a fast subtask (or a `once` schedule whose work finished) gets executed twice. Bounded (one extra fire per failure) but a real at-least-once → at-least-twice hazard for `once` schedules.
**Fix:** for `once`, deactivate *before* enqueue (compensate on enqueue failure), or stamp `last_fired_at` first and add it to the debounce predicate.

#### HD-12 — `actionability_backfill` has no forward-progress guard: persistent per-row failure = infinite loop on one connection held idle-in-transaction — **LATENT**
`actionability_backfill.py:117-152` loops `while True` re-fetching `actionable IS NULL` rows; rows that fail `classify`/`_update_actionable` stay NULL and are re-fetched forever (same LIMIT batch, no ordering/offset/attempt cap) at 0.5 s cadence. Meanwhile the advisory-lock session opened at `:76-90` executed a SELECT (autobegin) and never commits — that pooled connection sits "idle in transaction" for the entire backfill (normally fine for minutes; pathological under the infinite loop: blocks vacuum xmin + burns a pool slot indefinitely). Triggered at every startup (`actionability_backfill_on_startup=True`, `main.py:224-239`).
**Fix:** break after N consecutive all-error batches (or exclude failed IDs in-session); commit/close the lock session's implicit transaction (`pg_advisory_lock` survives commit).

### P3

#### HD-13 — Sleep phases mutate facts in two transactions; crash between leaves inconsistent state — **LATENT**
F031 MERGE (`sleep_handler.py:929-1008`) and F027 cluster merge (`:1259-1314`) first `heart.learn()` the merged fact (own transaction), then deactivate+supersede originals in a second session. A crash/exception between the two leaves the merged fact live **alongside** all originals (duplicate content, no chain). Inner excepts log at WARNING and continue. Low frequency, self-correcting only via future contradiction scans.

#### HD-14 — F031 resolution-event task not GC-protected — **LIVE (cosmetic loss)**
`sleep_handler.py:889-906` uses bare `asyncio.create_task(self._brain.emit_event(...))` for `f031_contradiction_resolution`, unlike `_emit_action_event` (`:387-409`) which retains strong refs in `_pending_emits` precisely to avoid mid-flight GC. Occasional silent loss of audit events; the eval that consumes them under-counts.

#### HD-15 — `_phase_relink_open_episodes`: one DB error inside the shared session aborts the rest of the phase — **LATENT**
`sleep_handler.py:1759-1832`. The per-episode `try` wraps only `link_episode_deterministic`; if that fails with a *database* error the transaction is poisoned, and the next iteration's anchor SELECT (`:1804`, outside the try) raises `PendingRollbackError` → outer except → phase aborts, edges built so far in this session are rolled back. Errors counter under-reports.
**Fix:** per-episode savepoint (`session.begin_nested()`), or rollback+continue on failure.

#### HD-16 — Sleep stale-scan loads and deactivates an unbounded row set in one transaction — **LIVE** *(DA-G9 noted the asymmetry; restated for the fix list)*
`sleep_handler.py:1068-1115`: no LIMIT; a first run on an old corpus can deactivate thousands of facts in one commit while F053 edge pruning is capped at 1000/cycle — orphaned dead edges then take many cycles to drain. Add a per-cycle cap mirroring `dead_edge_pruning_max_per_cycle`.

#### HD-17 — Double-sleep spawn race + `_sleep_task` clobber — **LATENT** *(DA-G9)*
`sleep_handler.py:371-381`: `_sleeping` is set inside the spawned task, not before `create_task`; two `sleep_started` events dispatched in the same tick window can both pass the guard. Second task also overwrites `self._sleep_task`, and the first task's `finally` (`:537`) later nulls the reference to the *second* task. Practically rare (REST trigger + monitor coinciding).

#### HD-18 — Knowledge extractor residual gaps after PR #495 — **LIVE**
`knowledge_extractor.py:117-139`: (a) hardcoded cosine 0.85 instead of `settings.fact_dedup_threshold` (0.92) — the two write paths now disagree on what "duplicate" means; (b) no F377 tiebreaker and no F075 distinct-date bypass (`_resolve_dedup` was not adopted — DA-S2's fix suggestion remains open); (c) stores facts with no `source_episode_id`/`source_text` → ungrounded for admission, orphaned in the graph.

#### HD-19 — `decision_recorded` is emitted before the recording transaction commits → linker can read-miss and silently skip — **LIVE (small window)**
`brain/brain.py:406-412` emits mid-transaction; `DecisionGraphLinker.handle` fetches via a **new** session (`decision_graph_linker.py:72-74`) and returns silently on None. Window opens whenever the bus task runs while `record()` is still doing auto-link/commit work. No retry; the decision stays orphaned until F040 sleep backfill. Same-pattern check: `fact_learned`/`procedure_stored` are emitted post-commit (heart facade) — only the decision path has this ordering.

#### HD-20 — Reverse/procedure linkers skipped the F022 content-length guard and hide failures at DEBUG — **LIVE**
`decision_graph_linker.py:71-182`, `procedure_graph_linker.py:64-150`: neither applies `cross_type_link_min_content_chars` (the 2026-04-30 F022 audit fix landed only in `GraphLinker.link_fact_to_*`, `brain/graph_linker.py:162-168,250-255`) — short/empty descriptions still generate the exact low-precision edges that fix targeted. Both handlers also log total failure at `logger.debug` (`:182`, `:150`), invisible at prod log level — a broken embedder or SQL regression would zero out live linking with no signal (same class as DA-S13 `_create_graph_edge`).

#### HD-21 — Sleep reflection lessons stored as category="rule" — **LIVE**
`sleep_handler.py:671-685`: fallback lessons go in with `category="rule"`, which the system defines as user-directives-only and loads **every turn**. LLM-authored generalizations escalate themselves into always-on context (same class as DA's `layer.py:1777-1783` finding; different site).

#### HD-22 — Correction extractor logs success on rejected facts — **LIVE**
`correction_extractor.py:126-127`: `heart.learn` result not checked for `FactRejected`; "F039: Stored correction fact" logs regardless, and a (provenance-capped) censor can still be created at `:130-139` for a principle whose fact was rejected.

#### HD-23 — Event-bus shutdown can drop in-flight handler work; queue overflow drops events silently for handlers with persistence side effects — **LATENT** *(overflow half is DA-S13)*
`events.py:194-227`: `stop()` cancels the loop task mid-`_dispatch` (the gather is cancelled; that event's remaining handlers never run) before draining the rest; events emitted *during* drain dispatch are processed, but anything a cancelled handler half-did is lost. Acceptable for telemetry, less so for `episode_summarized`→fact extraction at shutdown.

#### HD-24 — Backfill budget gate mutates a shared classifier — **LATENT**
`actionability_backfill.py:56-61,104`: `classifier._budget_check` is swapped at construction and restored only in `run_once`'s finally — between handler construction (`main.py:231`) and the backfill's completion, *live-path* `classify()` calls are also subject to the backfill's call cap; two concurrent backfills (multi-agent) would clobber each other's gate.

### INFO

- **INFO-1 — Stub phases report success:** `_phase_review_decisions`, `_phase_prune`, `_phase_compress` (`sleep_handler.py:546-584`) do nothing but appear in `phases_completed` ("review", "prune", "compress"), inflating dashboard/telemetry. *(DA-G9.)*
- **INFO-2 — DecisionReviewer `CONFIDENCE_THRESHOLD=0.7` is inert:** every signal's hardcoded confidence is ≥0.7 (`decision_reviewer.py:64,71,107,131,175,183`).
- **INFO-3 — `handle()` runs a full 30-day sweep on every `session_ended`** (`decision_reviewer.py:277-280`) *in addition to* the hourly sweep — O(unreviewed) GitHub/file checks per session end.
- **INFO-4 — `episode_summarizer.py` uses `datetime` in annotations without importing it** (`:450,492`) — safe only because of `from __future__ import annotations`; same for `FactSummary`/`date` strings in `fact_extractor.py:142,201`.
- **INFO-5 — Subtask event ID formats differ:** `subtask_id` is `uuid.hex` in `subtask_completed/failed` (`subtask_worker.py:438`) but `str(uuid)` (dashed) in `subtask_outcome` (`subtask_executor.py:99`) — joins on the events table need to normalize.
- **INFO-6 — `call_background_llm*` only accept dict content blocks** (`handlers/__init__.py:77-79,144-146`); a typed-object client response would silently return None for every background LLM consumer.
- **INFO-7 — `_repair_json` strips trailing commas inside string literals too** (`handlers/__init__.py:196` — regex is not string-aware, unlike `_extract_braces`); can corrupt a candidate that would otherwise fail loudly. Low impact since it's a last-resort path.
- **INFO-8 — `procedure_learner.py:390` `except (ValueError, Exception)`** — redundant tuple; and `_learn_from_episodes` filters on outcomes ("success","partial") that only exist because `layer.py` hardcodes "success" (see HD-4.2) — the filter is effectively "any non-abandoned episode".
- **INFO-9 — KnowledgeExtractor stores `self._bus` it never uses** (`knowledge_extractor.py:82`).
- **INFO-10 — Monitor pathway 3 reaches into `_procedure_learner._call_llm/_is_duplicate/_heart` privates** (`cognitive/monitor.py:262-295`) — wiring coupling that made HD-1 spread to a third pathway invisibly.

---

## 3. Dead-code inventory

| Item | Location | Note |
|---|---|---|
| `SleepHandler._on_wake` + all `_interrupted` checks | `sleep_handler.py:363-369` and 14 phase sites | No `message_received` emitter (HD-3) |
| SessionTimeoutMonitor `message_received` subscription | `session_monitor.py:88` | Superseded by synchronous `touch()` |
| `_phase_review_decisions` / `_phase_prune` / `_phase_compress` bodies | `sleep_handler.py:546-584` | Stubs since 006; still listed as phases |
| `_review_weak_procedures` (entire branch incl. `_WEAK_REVIEW_PROMPT`) | `procedure_learner.py:70-87,469-544` | Candidate query matches nothing (HD-10) |
| `_is_duplicate`-guarded store paths (effectively) | `procedure_learner.py:310-331,437-457`; `monitor.py:279` | Unreachable in practice while HD-1 stands |
| `OutcomeSignal.self_improvement_scores` real-scores branch | `outcome_detector.py:101`; `rubric_evolver.py:190-191` | Severed payload (HD-9 / RA-R1) |
| `CONFIDENCE_THRESHOLD` filtering effect | `decision_reviewer.py:195,303` | All signals ≥0.7 (INFO-2) |
| tz capture group in `_DAILY_RE` | `time_parser.py:39,156-157` | Captured, never used (HD-5) |
| `_build_retry_message` `task` param | `subtask_executor.py:531` | Explicitly reserved/`del`-ed |
| `EventBus._queue.task_done` discipline | `events.py:220` | `get()` without `task_done()`; `join()` unused — harmless |

---

## 4. Improvement opportunities

1. **One shared "similar-by-cosine" probe for every dedup/supersede gate.** PR #495 fixed two of five rank-score sites; HD-1 and HD-2 are the remaining write-path gates, HD-18(a) the remaining threshold drift. A single helper (`find_similar_facts` / `find_similar_procedures` + settings threshold) used by all five removes the class.
2. **Make sleep interruptible for real (HD-3) and add a watchdog:** a `max_sleep_duration` guard would also bound the uninterruptible worst case until the wire is restored.
3. **Auto-review quality gate (HD-4):** before any new signal work, persist the signal_type with each auto review (it's already in `ReviewResult`) and run a one-off accuracy eval against hand labels — the F058 calibration data suggests today's labels are worse than no labels.
4. **Promote linker failures from DEBUG to WARNING with a counter in bus stats** (HD-20); the EventBusStats handler table already exists — failures there are visible, but in-handler swallowed exceptions are not.
5. **Periodic `reclaim_stale` + scheduler-debounce TTL** (HD-7, HD-11): both are one-line policy changes in existing loops.
6. **Emit `episode_summarized` from the F060 recovery path** (HD-8): converts already-spent LLM calls into facts and unifies the two summarize entry points' side effects.
7. **Sleep-phase savepoints** (HD-13, HD-15): wrap per-item mutations in `begin_nested()` so one bad item can't roll back or poison a phase.
8. **Contract tests for event payloads:** a tiny test asserting emitted-key ⊇ read-key per (event, handler) pair would have caught HD-3 and HD-9 at introduction time; the table in §1 is the spec.
