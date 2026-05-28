# F075 — Temporal Fact Extraction + Date-Aware Retrieval

**Status:** 📝 Draft **v2.17** (2026-05-28) — incorporates arch / python-pro / devil's-advocate spec review + 17 rounds of codex PR review fixes
**Proposed by:** Tim + investigation thread
**Date:** 2026-05-27
**Depends on:** F002 (Heart), F022 (Cross-type linking), F040 (Graph Densifier), F047 (Actionability backfill pattern), F051 (Eval harness for measurement)
**Blocks:** F074.x re-measurement of temporal_reasoning on BEAM
**Related:** `nous/heart/content_date_extractor.py` (untracked, retrieval-time approach — see §Alternatives)
**Forge decision:** TBD (will record when impl plan greenlit)
**Reviews:** `docs/reviews/F075-spec-arch-review.md`, `F075-spec-python-pro-review.md`, `F075-spec-devil-review.md`

---

## v2.17 changelog (codex re-review round 17)

Codex flagged 1 P2 on v2.16, validated by tracing the unit math:

- **P2 — BudgetTracker unit mismatch.** v2.15-v2.16 initialized with `BudgetTracker(token_budget // _TOKENS_PER_LLM_CALL)` (CALL count, e.g. 200 calls for a 50K-token budget) but `_process_batch` called `budget.consume(_TOKENS_PER_LLM_CALL)` (TOKEN count, e.g. 250). After the first classification, the counter went `200 - 250 = -50`, `ok()` returned False, the loop exited with ~199 calls unused. At `--token-budget 50000` only ~1 row got processed. Fixed: `consume()` is now no-arg and decrements `remaining_calls` by 1; added explicit class definition to the spec so units are unambiguous.

## v2.16 changelog (codex re-review round 16)

Codex flagged 3 P2s on v2.15. Validated each finding against the actual code (`nous/heart/schemas.py`, `nous/brain/graph_linker.py:179`, the v2.15 `_process_batch` loop body) before patching:

- **P2 — `FactInput` schema row missed `event_date_classified_at`.** Wire-path row 3 declared only `event_date` on FactInput, but row 4 says `_learn` copies BOTH `event_date` and `event_date_classified_at` and row 5 declares both on the ORM. Producers (Layer 1a/1b/Layer 4) trying to pass `event_date_classified_at=...` would either fail Pydantic strict mode or have the kwarg silently dropped. Fixed: row 3 now mandates both fields on FactInput.
- **P2 — `_fetch_chunk_context` defined but never called.** The whole point of fetching chunk context was to give the classifier better signal than `fact.content` paraphrase. v2.15's `_process_batch` body invoked `_classify_event_date(row)` directly — never hitting the helper. Dead code. Fixed: `_process_batch` now calls `_fetch_chunk_context(session, agent_id, row["source_episode_id"], row["embedding"])` BEFORE the classifier call and passes the chunk text as `chunk_context=` kwarg.
- **P2 — Embedding parameter not serialized for pgvector cast.** v2.15 passed `row["embedding"]` raw into `CAST(:embedding AS vector)`. The repo convention at `nous/brain/graph_linker.py:179, 266` is to serialize to the pgvector text literal `"[" + ",".join(str(float(v)) for v in embedding) + "]"` before binding. Without this, the bind/cast can fail before any chunk is retrieved. Fixed: `_fetch_chunk_context` now builds `embedding_str` first, then binds.

## v2.15 changelog (codex re-review round 15)

Codex flagged 1 P2 on v2.14:

- **P2 — Budget-exhausted early return silently rolls back already-classified rows.** When the token budget runs out mid-batch after some rows have already been classified + UPDATEd, the v2.14 `_process_batch` returned `(updated, True)` before reaching the per-batch `session.commit()` at the bottom of the function. The surrounding `async with session_factory()` then closes the uncommitted work session, rolling back the in-flight UPDATEs. Those rows stay at `event_date_classified_at IS NULL` — the LLM calls already spent on them are wasted because the next run re-classifies the same rows. Only happens when remaining budget < fetched batch size, but at small `--token-budget` values it would burn API spend silently. Fixed: added `await session.commit()` immediately before the early `return (updated, True)` to persist work-so-far.

## v2.14 changelog (codex re-review round 14)

Codex flagged 1 P1 (lock-leak STILL not fully resolved) + 2 P2s on v2.13:

- **P1 — Per-batch commit pattern still leaks the lock at batch boundaries.** Round-13 switched per-row → per-batch commits to address the round-12 lock-leak finding. Codex now points out that with MULTIPLE batches, the same problem recurs: the first batch's commit ends its transaction, the underlying connection can return to the pool, and the second batch's `execute()` may bind a *different* connection. The session-scoped advisory lock was acquired on the original connection — it stays held there while subsequent `execute()` (and the eventual `pg_advisory_unlock`) run on different connections. Lock leaks for the rest of the original connection's pool lifetime. Fixed: switched to a **two-connection pattern**. One checked-out raw connection (`engine.connect()`) holds the advisory lock for the script's full lifetime, never commits, never returns to the pool. Per-batch work uses fresh `session_factory()` sessions that DO commit per batch but never touch the lock. This eliminates the failure mode across any number of batches.
- **P2 — Chunk-lookup cosine query missing `WHERE embedding IS NOT NULL`.** v2.13 added Python-side null-guards on `episode_id` and `embedding` (fact-side) but didn't filter chunk-side NULL embeddings. For legacy episodes whose chunks have NULL embeddings, `embedding <=> CAST(:embedding AS vector)` produces NULL distances; `LIMIT 1` can return an arbitrary chunk to the classifier → bad date stamped during backfill. Fixed: added `AND embedding IS NOT NULL` to the chunk lookup SQL. Now 3 guards: fact `episode_id is None`, fact `embedding is None`, chunk `embedding IS NOT NULL`.
- **P2 — `happened_before` INSERT still used asyncpg `$1` placeholder.** Round-12 fixed `_process_batch` to `:name` binds and round-13 fixed the chunk lookup, but the Layer 2 `happened_before` INSERT SQL still had `WHERE a.agent_id = $1`. GraphDensifier uses `session.execute(text(...), params)` throughout — `$1` is not bound, the edge build would fail before writing any edges. Fixed: `$1` → `:agent_id` with explanatory comment.

## v2.13 changelog (codex re-review round 13)

Codex flagged 2 P2s on v2.12 — both SQLAlchemy idiom carryovers from the round-12 AsyncSession rewrite:

- **P2 — Per-row `session.commit()` can release the lock-holding connection.** In AsyncSession with a connection pool, `commit()` can return the underlying physical connection to the pool. The session-scoped advisory lock (held on the original connection) would stay held until that pooled connection eventually closes, while the next `execute()` — including the final `pg_advisory_unlock` — could bind a *different* connection where the unlock is a no-op for the held lock. Result: lock leak; future runs skip indefinitely. Fixed: per-row commit replaced with per-batch commit. The session stays bound to a single physical connection through the lock+batch+unlock lifetime. Trade-off documented: a crash mid-batch loses ≤ batch_size in-flight UPDATEs, but re-runs pick them up via `event_date_classified_at IS NULL` (no data loss, just retry cost).
- **P2 — Chunk-lookup SQL still used asyncpg `$1/$2/$3` placeholders.** Round-12 fixed `_process_batch` SELECT/UPDATE to SQLAlchemy `:name` binds, but the chunk-lookup snippet in §Layer 4 §Context source was missed. With `session.execute(text("..."), {...})`, `$1` placeholders are not bound and the query either fails or runs without parameters. Fixed: rewrote the snippet as an async helper using `:agent_id`, `:episode_id`, `:embedding` binds with explicit null-guards on `episode_id` and `embedding` for legacy rows.

## v2.12 changelog (codex re-review round 12)

Codex flagged 1 P1 (continued cap refinement) + 2 P2s on v2.11:

- **P1 — Downstream cap at `_store_candidate_facts` still truncates to 5.** v2.11 fixed `_merge_summaries` to emit `dated[:30] + stable[:5]` = up to 35 candidates. But `fact_extractor.py:249` then iterates `candidates[:5]` — so only the first 5 (dated, by partition order) reach `Heart.learn`. The 6th-onward dated facts that row 13's fix preserved are dropped at the storage gate. Two-site fix required, not one. Fixed: wire-path row 14 added — `_store_candidate_facts` applies the same partition (`dated[:event_limit] + stable[:5]`).
- **P2 — Pre-learn dedup paths bypass the round-11 `_learn` event_date guard.** Both extractor paths (`fact_extractor.py:176-179`, `263-269`) call `search_facts(content, limit=1)` and skip on `fact_dedup_threshold` BEFORE `Heart.learn` is invoked. The round-11 `_learn` guard never fires for hits caught at this pre-check — distinct-date events still collapse. Fixed: wire-path row 15 added — same `event_date != event_date → bypass` rule applied at both pre-learn dedup sites. (Requires `search_facts` to surface `event_date` on results, already mandated by row 7's FactSummary update.)
- **P2 — Backfill pseudo-code mixed AsyncSession lock with asyncpg-style SQL.** v2.11's `_run_batches` acquired the lock via `db.session()` (SQLAlchemy AsyncSession), but `_process_batch` used `conn.fetch(...)` with asyncpg `$1` placeholders. AsyncSession has `execute()` not `fetch()` — script would `AttributeError` before classifying any row. F047 uses consistent `session.execute(text(...), {...})` throughout (`actionability_backfill.py:78-205`). Fixed: pseudo-code rewritten to use SQLAlchemy `session.execute(text("..."), {...})` + `result.mappings().all()` + per-row `session.commit()`.

## v2.11 changelog (codex re-review round 11)

Codex flagged 1 P1 (refinement of round-10) + 1 P2 on v2.10:

- **P1 — Round-10's partition-then-`[:5]` fix is STILL insufficient.** v2.10 sorted dated facts first then truncated at `[:5]`. But if a transcript contains MORE than 5 dated events, the 6th-onward dated facts are still dropped. F075 explicitly targets long multi-day haystacks where 5 dated events is well below typical density. Fixed: split candidates into separate pools — `dated[:candidate_facts_event_limit] + stable[:5]`. Default event-limit = **30** (configurable via new `NOUS_CANDIDATE_FACTS_EVENT_LIMIT`). Stable cap stays at 5.
- **P2 — Dedup/supersession ignores `event_date`, collapsing distinct-date events.** `Heart._learn` dedup uses embedding cosine ≥ 0.80 and supersession uses subject match — neither considers `event_date`. Two facts like *"Christina obtained API key on March 10"* vs *"Christina rotated API key on March 12"* have high embedding similarity AND collide on subject, so the second silently drops or supersedes the first. **The exact date-pair temporal_reasoning needs is destroyed.** Fixed: added rule — when both candidate and existing have `event_date IS NOT NULL` and the dates differ, dedup/supersession is bypassed. Different dates = different events. Regression test added to `test_temporal_extractor.py`.

## v2.10 changelog (codex re-review round 10)

Codex flagged 1 P1 (material) + 2 P2s on v2.9:

- **P1 — Multi-chunk merge truncates dated facts before FactExtractor sees them.** For transcripts exceeding `transcript_max_chars` (16K default), `EpisodeSummarizer._merge_summaries` at `episode_summarizer.py:445-468` summarizes chunks separately and returns `merged_candidate_facts[:5]` in chunk order. Dated events from later chunks are dropped — exactly the BEAM-100K case F075 targets. Fixed: wire path row 13 added — `_merge_summaries` stable-partitions the merged list so `event_date IS not None` candidates sort first BEFORE the [:5] truncation. Python's stable sort with `key=lambda c: c.get("event_date") is None` (False < True) puts dated facts at the front in their original chunk order; truncation then drops stable-fact tail, never dated-fact tail.
- **P2 — Token budget exhaustion checked only between batches.** v2.9's `_process_batch` had no per-row budget gate, so a small `--token-budget` would be exceeded by up to a full batch (or unbounded if the outer loop kept invoking). F047 mirrors this exact pattern via `classifier._budget_check` at `actionability_backfill.py:57-65`. Fixed: `_process_batch` now takes a `BudgetTracker`, calls `budget.ok()` before every `_classify_event_date(row)`, and returns `(updated, stop_requested)` so the outer loop can halt cleanly.
- **P2 — Subject-ILIKE chunk lookup too brittle for LLM-generated subjects.** v2.9's `WHERE content ILIKE '%' || subject || '%'` fails when the subject is a descriptor not a verbatim transcript substring — `OpenWeather API key acquisition` doesn't appear in "I got my OpenWeather API key on March 10" because `acquisition` is absent. Fixed: switched to pgvector cosine distance against `fact.embedding` (already SELECTed in the batch query). Matches the rest of the codebase's fact↔chunk semantic-similarity pattern. Falls back gracefully when fact has no embedding (legacy rows).

## v2.9 changelog (codex re-review round 9)

Codex flagged 2 P2s on v2.8:

- **P2 — Validator snippet was non-runnable.** Round-8's regex addition placed `import re` and `_DATE_PATTERN = re.compile(...)` between the `@classmethod` decorator and the `def _parse_event_date` method — invalid Python (you can't `import` mid-class-body) AND the bare `_DATE_PATTERN` reference inside the method would look up a missing module global. Fixed: snippet now explicitly shows module-scope layout (`import re`, `_DATE_PATTERN` ABOVE `class FactInput`), with a callout that the method's bare reference works because it's a module global.
- **P2 — `event_date_classified_at` marker site was structurally wrong.** v2.8 had `FactManager._learn` stamp the marker whenever the flag is on. But `Heart.learn(FactInput(...))` is called from many non-F075 paths (`tools.py:516`, `rest.py:1722`, `knowledge_extractor.py:127`). Those facts never ran through the F075 classifier but would have been falsely marked, then skipped by backfill forever. Fixed: marker is now a `FactInput` field populated by the F075-aware producer paths only (Layer 1a `_store_candidate_facts`, Layer 1b direct `FactInput(...)`, Layer 4 backfill UPDATE). `FactManager._learn` becomes a pure sink — persists whatever's in `FactInput`, no policy. Other learn callers leave the field at its `None` default → those rows stay backfill-eligible.

## v2.8 changelog (codex re-review round 8)

Codex flagged 2 more P2s on v2.7. Both stdlib/precedent-collision gotchas:

- **P2 — `date.fromisoformat()` accepts alternate ISO forms in Python 3.12.** v2.7's validator claimed strictness based on `date.fromisoformat` alone, but Python 3.12's stdlib accepts `'20240310'` (no hyphens) and `'2024-W10-7'` (ISO week date) — both contradict the prompt/schema contract of `YYYY-MM-DD`. Fixed: added explicit `re.compile(r"^\d{4}-\d{2}-\d{2}$").fullmatch` gate before calling `fromisoformat`; the regex enforces surface shape, `fromisoformat` then enforces day-validity (rejects 2024-02-30).
- **P2 — Advisory lock key collides with F047's actionability lock.** v2.7 copied F047's key derivation exactly (`SHA-256(agent_id)`). Result: F075 backfill and F047 actionability backfill compete for the SAME advisory lock per agent — if F047 runs at startup while an operator invokes F075 backfill, F075 falsely reports "another temporal backfill holds the lock" and skips, even though the competing job is unrelated. Fixed: added a `_LOCK_NAMESPACE = "f075-temporal"` prefix that's concatenated before hashing, so only same-feature backfills exclude each other on the same agent.

## v2.7 changelog (codex re-review round 7)

Codex flagged 2 stale-cross-reference P2s on v2.6. Both were leftover from rounds 5/6 fixes that didn't propagate to all mentions of the same rule. Incorporated:

- **P2 — Layer 4 §Per-row UPDATE post-script still claimed unconditional `event_date_classified_at = NOW()` write by live path.** v2.6 §Layer 1a §Flag-gating added the rule that the marker is only written when the flag is on, but the matching sentence in §Layer 4 wasn't updated and still says "writes `event_date_classified_at = NOW()` whenever it stores a fact." Two sources of truth contradicting. Fixed: §Layer 4 sentence now defers to §Layer 1a §Flag-gating, restating the conditional rule.
- **P2 — Acceptance criterion #1 still said "4 unit files".** v2.6 deferred `test_date_aware_boost.py` with Layer 3 but the acceptance count wasn't updated. Fixed: criterion #1 now says "3 unit files + 1 integration file" and lists the file names explicitly so reviewers can verify scope at a glance.

## v2.6 changelog (codex re-review round 6)

Codex flagged 3 more P2s on v2.5. All incorporated:

- **P2 — Layer 3 tests in required set but Layer 3 is deferred.** v2.5 §Tests listed `tests/test_date_aware_boost.py` as required even though §Layer 3 is explicitly "If implemented later" deferred. CI would either fail against missing date-boost code or force the implementer to ship the deferred feature. Fixed: `test_date_aware_boost.py` now explicitly tagged as DEFERRED with Layer 3 — it ships when Layer 3 does.
- **P2 — `event_date_classified_at` write also needs flag-gating.** v2.5's §Layer 1 thread-through unconditionally stamps the marker on every fact write, but when the flag is OFF the row was NOT actually classified (legacy prompt was used). A later flag-flip + backfill would skip those rows forever via `event_date_classified_at IS NULL`. Fixed: live-path write is gated on `settings.temporal_extraction_enabled` — flag-off keeps the column NULL so rows remain eligible for catch-up classification.
- **P2 — Advisory lock variant wrong for multi-batch backfill.** v2.5 specified `pg_try_advisory_xact_lock` but that releases at the end of each per-row UPDATE transaction, letting concurrent CLI invocations interleave mid-loop. F047's `actionability_backfill.py:79, 97` uses session-scoped `pg_try_advisory_lock` + explicit `pg_advisory_unlock` for exactly this multi-batch shape. F049 uses xact_lock correctly because all work is in one short sweep — different pattern. Fixed: §Layer 4 advisory lock section now shows the F047 session-scoped pattern verbatim with explicit unlock in a try/finally.

## v2.5 changelog (codex re-review round 5)

Codex flagged 5 more P2s on v2.4. All incorporated:

- **P2 — happened_before edges may target inactive facts.** Original SQL filtered neither side for `active=TRUE`. With superseded facts in the corpus, an active Jan-1 fact could point at an inactive Jan-2 successor while the active Jan-2 siblings get no edge — dead-weight reinforcement because Heart recall only returns active rows. Fixed: both `a` and `b` filter `active=TRUE`.
- **P2 — Layer 1b direct FactInput path missing.** Wire path covered `_store_candidate_facts` (step 2) but the FactExtractor's direct LLM fallback path at `fact_extractor.py:189-196` builds `FactInput(...)` directly without going through `_store_candidate_facts`. For eval / direct-ingest traffic that bypasses the summarizer, the extracted `event_date` would still be silently discarded. Fixed: wire path adds row 11 covering the direct construction.
- **P2 — ORM source_type/target_type constraints also stale.** v2.3 brought `ck_edges_relation` current but left `ck_edges_source_type` and `ck_edges_target_type` at their init.sql values — both omit `'chunk'` (added by F070 migration 051). ORM-driven fresh schemas reject every F070 chunk edge today. Fixed: §Schema migration now updates all three ORM constraints in one pass.
- **P2 — PipelineResult conversion drops metadata.** Layer 3 boost reads `r.metadata.get("event_date")` from `PipelineResult`, but `_heart_results_to_pipeline` at `retrieval_pipeline.py:659-669` doesn't copy `RecallResult.metadata` into the new `PipelineResult.metadata` dict. Even with Layer 1 correctly populating the source-side metadata, the boost reads `None` after conversion and is a silent no-op. Fixed: wire path adds row 12 for the conversion step.
- **P2 — Layer 1 prompt change unconditional, violating dark-launch.** v2.4 said all flags default OFF but the wire path changes were unconditional — the summarizer prompt and dict-unpacks would emit/consume `event_date` regardless of the flag. Rollback claim was unreliable. Fixed: §Layer 1a now explicitly notes the prompt addition, schema slot, dict-unpacks, and `event_date_classified_at` live-path write are ALL gated on `settings.temporal_extraction_enabled`. Backfill script unaffected (governed by explicit invocation, not the flag).

## v2.4 changelog (codex re-review round 4)

Codex flagged 2 more P2s on v2.3, both wire-path completeness issues. All incorporated:

- **P2 — `_generate_summary` boundary missing from started_at wire path.** v2.2 added `episode_summarizer.py:377, 383, 394` to thread `started_at`, but skipped `summarize_episode → _generate_summary` (lines 135 and 361). `episode.started_at` is in scope at the outer boundary but never reaches `_summarize_single` without threading through the intermediate `_generate_summary` signature. Fixed: wire path now has 5 hops covering every boundary in the chain (135, 361, 377, 383, 394 + prompt template at 50-77).
- **P2 — FactSummary / FactDetail constructors silently default `event_date=None`.** Adding `event_date` to the schemas isn't enough; each manual `FactSummary(...)` call at 4 sites in `nous/heart/facts.py` (lines 1046, 1160, 1256, 1355) plus `_to_detail` (line 1459) must pass `event_date=fact.event_date`. Without these, the recall surface returns None even when the DB column is populated, making Layer 3 boost and the end-to-end integration test silent failures. Fixed: wire-path table extended with rows 8-10 covering all construction sites + `RecallResult.metadata["event_date"]` serialization.

## v2.3 changelog (codex re-review round 3)

After v2.2 push, codex flagged 2 more P2s. All incorporated:

- **P2 — Wrong `GraphDensifier.run_backfill_cycle` call shape.** v2.2's edge-build trigger wrote `densifier.run_backfill_cycle(agent_id=...)` but `run_backfill_cycle()` at `graph_densifier.py:1045` takes no args — `agent_id` is captured in the constructor (`graph_densifier.py:108-119`). Calling with `agent_id=` would `TypeError`. Fixed: §Edge-build trigger now shows the correct per-agent instantiation pattern, mirroring `sleep_handler.py:1354`.
- **P2 — ORM `CheckConstraint` not updated alongside SQL migration.** The SQLAlchemy `GraphEdge` class at `models.py:239-244` declares its own `ck_edges_relation` that fresh-schema paths (`Base.metadata.create_all` in tests, Alembic autogenerate) honor. v2.2's spec only updated the migration SQL. Pre-existing drift: the ORM constraint was never updated for F070's `part_of`/`summarized_by` either. Fixed: §Schema migration now requires updating the ORM `CheckConstraint` to include all three missing relations (`part_of`, `summarized_by`, `happened_before`).

## v2.2 changelog (codex re-review round 2)

After v2.1 push, codex re-reviewed and flagged 5 more findings. All incorporated:

- **P1 — stale eligibility predicate in §Idempotence.** The v2.1 fix updated the per-batch SELECT to `event_date_classified_at IS NULL` but left the §Idempotence one-liner saying `WHERE event_date IS NULL`. Same starvation bug, separate doc location. Fixed: §Idempotence now matches the SELECT predicate.
- **P2 — quadratic happened_before edges.** The `NOT EXISTS (intermediate-date)` formulation links every fact on date D to every fact on date D+1 because it excludes intermediate *dates*, not intermediate *facts*. 20 facts on Jan 1 + 20 on Jan 2 = 400 edges, not 20. Fixed: §Layer 2 INSERT now uses `LATERAL ... LIMIT 1` to pick exactly one representative successor per source fact.
- **P2 — `EPISODE_START_TIMESTAMP` not wired through summarizer.** The Layer 1 prompt directs the LLM to resolve relative dates against `EPISODE_START_TIMESTAMP`, but `_summarize_single(transcript, decision_context)` at `episode_summarizer.py:394` doesn't receive it. Fixed: §Layer 1a wire path adds 3 hops to thread `Episode.started_at` from `summarize_episode` → `_summarize_single` → prompt template.
- **P2 — backfill SELECT missing `subject` column.** The chunk-context lookup in §Layer 4 uses `$3 = subject keywords` but the batch SELECT only returns `(id, content, source_episode_id)`. Fixed: SELECT now includes `subject`.

## v2.1 changelog (codex PR review)

Codex flagged 2 P1s on PR #460 against the v2 spec; both incorporated here:

- **Codex P1 — `brain.graph_edges` schema constraints.** Layer 2's INSERT was missing `agent_id` (`NOT NULL` per `sql/init.sql:197`) and the existing relation CHECK constraint (last extended by migration `051_f070_chunk_graph_edges.sql:34-45`) does not allow `'happened_before'`. v2.1 §Schema migration now extends the CHECK to add the new relation (mirroring F070's pattern verbatim), and the INSERT includes `agent_id` + `auto_linked=TRUE`.
- **Codex P1 — backfill starvation.** v2's eligibility predicate `WHERE event_date IS NULL` meant non-event rows (stable facts the classifier correctly returned `None` for) stayed eligible forever — the script would loop on the newest-N stable facts and never advance to older dated rows. F047 doesn't have this problem because every actionability classification produces a non-NULL TRUE/FALSE. v2.1 adds an `event_date_classified_at TIMESTAMPTZ` column written on every classification attempt regardless of outcome; eligibility becomes `event_date_classified_at IS NULL`. The live-path extractor also writes the marker so freshly-ingested facts skip backfill.

## v2 changelog

v1 → v2 incorporates 3 parallel spec reviews. Key changes:

**Critical (architectural target was wrong):**
- **Arch P1-1, Python P1-1 fixed.** v1 proposed augmenting `FactExtractor._EXTRACT_PROMPT`. That is the **fallback** path. Prod uses the 008.4 fast-path at `fact_extractor.py:143-148` where `candidate_facts` come from the **episode summarizer**. v2 targets the summarizer's `candidate_facts` schema (`episode_summarizer.py:50-56`) as **primary**, FactExtractor's prompt as **defense-in-depth**. Also: `ExtractedFact` class doesn't exist — v2 targets `FactInput` (`nous/heart/schemas.py:85-98`) and the `_store_candidate_facts` dict-unpacking loop (`fact_extractor.py:251-256`) which silently discards unknown keys today.
- **Arch P1-2 fixed.** v1 assumed prompt change would let the LLM see dates. But the summarizer's extraction call operates on 100-150 word prose (the just-generated summary), not the raw transcript. Date-in-rate-limit-code (conv 2 Q0 case) gets compressed out. v2 directs the summarizer to extract dates from the **transcript text it already holds in scope** (`episode_summarizer.py:258`).
- **Python P1-4 fixed.** v1 added the SQL column but missed: (a) `Fact` ORM at `storage/models.py:469-511` needs `Mapped[date | None]`, AND (b) `Heart.recall` must populate `RecallResult.metadata["event_date"]` or Layer 3 is a silent no-op forever.

**Layer scope reframing:**
- **Arch P1-3 / Devil's #3 fixed.** v1 framed Layer 2 (`happened_before` edges) as a primary retrieval lever. v2 reframes as **modest reranking reinforcer when both endpoints already retrieved**. The consumer (`graph_adjacency_boost`) is real and the F065 trap is avoided — but the consumer is default-off (`config.py:1055-1056`) AND cannot surface candidates below the retrieval cut. Impl plan must include flipping `NOUS_GRAPH_ADJACENCY_BOOST_ENABLED=true`.

**Diagnostic update from Devil's #2:**
- **Failure-mode reclassification** (verified empirically 2026-05-27 via `diag_temporal_failure_classes.py`):
  - 2 of 5 = PATTERN_MATCH_CONV2_Q0 (conv 2 Q0, conv 4 Q1): date in chunks, missing from facts, chunk not in top-20 retrieval
  - 2 of 5 = PARTIAL_MATCH (conv 3 Q1, conv 5 Q1): date in chunks, missing from facts, chunk IS retrieved (rank 3), but LLM doesn't compose a correct date-arithmetic answer from the prose
  - 1 of 5 = source ambiguity (conv 2 Q1): unrelated to extractor work
- v1's "3 of 5 retrieval-miss" was an under-classification. Extractor still helps the PARTIAL_MATCH class by creating discrete dated facts that are easier for the LLM to combine, but it's a different mechanism than for the PATTERN_MATCH class.

**Python-level corrections:**
- **Backfill SQL was malformed.** v1's `WHERE id = (SELECT id FROM batch)` crashes when LIMIT > 1. v2 copies F047's per-row pattern at `actionability_backfill.py:198-216`.
- **Advisory lock keying.** v1 invented a keying scheme. v2 copies F047's `hashlib.sha256().digest()[:8]` → signed bigint pattern at `actionability_backfill.py:108-115`.
- **Migration shape.** Added `BEGIN;...COMMIT;` wrapper per `052_f069_document_source_kind.sql:22,34` style.
- **LLM client.** v2 specifies `call_background_llm_structured` from `nous/handlers/__init__.py:86` with tool_use schema for guaranteed JSON — no parse-repair logic needed.

**Default flags:**
- **Python/Arch P3 fixed.** All new flags default to `False` for consistency with prior dark-launch convention (F042, F047, F067, F071).

---

## Problem

BEAM-100K prod-v3 measures temporal_reasoning at **0.417** (n=5 conv, decision `631cbc75`). LongMemEval N=100 paper-faithful measured temporal_reasoning at **0.583** in the same family of weakest categories. Both benchmarks expose the same gap: Nous can recall topical conversation content but **fails on date arithmetic** ("how many days between X and Y").

### Diagnostic chain (decisions `0b52b37d`, `2c6c2c37`; memory `project_f074_temporal_diagnosis_2026_05_27`)

Three "easy lever" hypotheses were investigated and all empirically or analytically falsified:

| Lever | How falsified | Cost |
|---|---|---|
| `NOUS_STALENESS_HALF_LIFE_DAYS` flip 20→30 | Code-read: staleness lives in `nous/cognitive/context.py` only. BEAM harness bypasses pre_turn per F074 §5 | $0 |
| `recall_top_k` 10→20 | n=3 empirical smoke: per-Q delta = 0.000 on all 6 temporal Qs. Aggregate −0.016, abstention −0.167 | $7.30 |
| Inline date markers (`content_date_extractor.py`) | Annotates dates **inside retrieved snippets**; for PATTERN_MATCH_CONV2_Q0 class, the right snippet isn't retrieved | $0 (eliminated by diagnostic) |

### Failure-mode classification (verified `diag_temporal_failure_classes.py`)

5 prod-v3 temporal_reasoning failures (out of 10 Qs across conv 1-5) classified empirically:

| Q | Class | Source-chat | Heart facts | Heart chunks | Retrieved top-20 |
|---|---|---|---|---|---|
| Conv 2 Q0 ("API key March 10") | **PATTERN_MATCH_CONV2_Q0** | 1× "march 10" | 0 | 1 | NO |
| Conv 4 Q1 ("8/10 triangle score") | **PATTERN_MATCH_CONV2_Q0** | 1× "8/10" | 0 | 1 | NO |
| Conv 3 Q1 ("planning peer review April 2") | **PARTIAL_MATCH** | 2× "April 2" | 0 | 2 | YES (rank 3) |
| Conv 5 Q1 ("15 problems quiz") | **PARTIAL_MATCH** | 1× "15 problems" | 0 | 3 | YES (rank 3) |
| Conv 2 Q1 ("testing period April 5") | Source ambiguity | competing dates | varied | varied | both retrieved |

**Common across PATTERN_MATCH + PARTIAL_MATCH (4 of 5 failures):** the date-anchored event exists in `heart.episode_chunks` but has **0 corresponding facts in `heart.facts`**. The fact extractor produced facts about endpoints, error codes, components — but never the date-anchored event tuple.

### Synthetic validation (2026-05-27, ~$0.05)

Hand-injecting one fact — `"Christina obtained the OpenWeather API key on March 10, 2024."` — into `heart.facts` for `beam-100K-conv-002` with text-embedding-3-large embedding caused:
- Synthetic fact ranked **#3 of 39** in `run_recall_pipeline` at K=20 (score 0.827)
- LLM answer became: *"you obtained the OpenWeather API key on March 10, 2024, and completed the UI wireframe on March 12, 2024. That's **2 days** between the two events."*
- Conv 2 Q0 score moves from 0.000 → 1.000

### Root cause

**The episode summarizer's `candidate_facts` schema does not capture date-anchored event tuples.** The summarizer's output prompt (`episode_summarizer.py:50-56`) instructs the LLM to emit `{"subject", "content", "category"}` per candidate fact — no slot for dates. As a result, output is biased toward stable facts (`<entity> <attribute>` form) rather than episodic events (`<entity> <action> on <date>` form). Dates survive in chunks but chunks rank low on date-arithmetic queries when chunk content is dominated by surrounding context (code, prose).

This is also a **real product gap**, not benchmark-specific. Any user asking Nous "when did I do X" or "how long ago was Y" hits the same wall.

---

## Goals

1. **Extract date-anchored events as discrete facts** during episode summarization, structured as `<entity> <action> on <ISO-date>`.
2. **Persist `event_date: date | null`** on `heart.facts` — indexable column for date-range queries and graph edges. Surfaced into `RecallResult.metadata` for downstream consumers.
3. **Build `happened_before` edges** during sleep cycle: chronologically-adjacent same-episode facts only. Same-episode constraint inherits F070's ceiling (see §Risks).
4. **Date-aware retrieval boost** in `run_recall_pipeline` — detect date-language queries, gentle multiplicative boost on `event_date != NULL` facts within inferred window. *Deferred pending Layer 1+2+4 measurement.*
5. **Retrofit existing data** via `scripts/backfill_temporal_facts.py` — re-process NULL rows under PG advisory lock with token budget (F047 pattern), using chunk context not summary prose.
6. **Measurable**: re-run BEAM Phase 1 n=5 with this feature ON; expect temporal_reasoning ≥0.55. Validated on LongMemEval N=20 temporal-category retrieval first to avoid wasting BEAM budget.

## Non-goals

- **No new memory type.** Date-anchored events are still `heart.facts` rows.
- **No multi-date facts.** One fact = one event = one `event_date`. Range events → two facts + optional `happened_before` edge. F075.2 deferred.
- **No timezone handling.** ISO date at day granularity (`YYYY-MM-DD`).
- **No timeline UI.** Dashboard surface is F075.1.
- **No content_date_extractor.py wiring.** That module annotates dates at retrieval-time inside snippets; this feature operates at ingest-time on facts. The `_extract_regex` helper is reused (with commit + tests) by Layer 3's date-language detector.
- **No removal of existing FactExtractor prompts.** Augments behavior; existing extraction continues unchanged.
- **No reading per-message timestamps from chat metadata.** Some BEAM chats carry timestamps; prod conversations don't reliably. Dates must come from chat content.

---

## Design

### Layer 1 — Date-anchored extraction at summarization time

**Approach:** primary target is `EpisodeSummarizer.candidate_facts` schema (`nous/handlers/episode_summarizer.py:50-56`). Defense-in-depth augmentation of `FactExtractor._EXTRACT_PROMPT` for eval / direct-ingest paths that bypass the summarizer.

#### Layer 1a (primary): EpisodeSummarizer

`nous/handlers/episode_summarizer.py:50-56` defines the structured JSON the summarizer's LLM emits. Today's schema for each candidate fact: `{"subject", "content", "category"}`. v2 augments to optional 4th field:

```python
# Before (episode_summarizer.py:50-56, paraphrased):
"candidate_facts": [
  {"subject": "...", "content": "...", "category": "..."}
]

# After (F075):
"candidate_facts": [
  {"subject": "...", "content": "...", "category": "...",
   "event_date": "YYYY-MM-DD"  # OPTIONAL — only when fact describes a dated event
  }
]
```

**Prompt addition at `episode_summarizer.py:76-77`** (the candidate-facts instruction block):

```
DATE-ANCHORED EVENTS (F075):
When the transcript describes an event happening on a specific date — particularly
something the user did or that was completed — capture it as a SEPARATE candidate
fact with the date attached:

  subject:    <short descriptor of the event>
  content:    "<entity> <action verb> <object> on <full date>."
  category:   "event" (or relevant existing category)
  event_date: "<ISO YYYY-MM-DD>"

Examples:
  - "I got my OpenWeather API key on March 10" →
    {"subject": "OpenWeather API key acquisition",
     "content": "Christina obtained the OpenWeather API key on March 10, 2024.",
     "category": "event",
     "event_date": "2024-03-10"}
  - "We deployed v2.1 to staging last Tuesday" (episode_start_timestamp = 2024-04-11) →
    {"subject": "v2.1 staging deployment",
     "content": "Team deployed v2.1 to staging on 2024-04-09.",
     "category": "event",
     "event_date": "2024-04-09"}

CRITICAL: extract from the TRANSCRIPT text below — not from any summary you have
generated. Dates mentioned in passing (e.g. inside code blocks or user asides) are
just as important as headline dates. Resolve relative phrases ("yesterday", "last
week", "3 days ago") against EPISODE_START_TIMESTAMP. If the date is ambiguous
or unresolvable, OMIT event_date (set null) but still extract the fact without
the date.
```

**Critical wiring detail (Arch P1-2):** the summarizer already passes the transcript content into the LLM call when constructing the summary. The prompt addition above directs the same LLM to also extract dates from that transcript — no new data is needed; the LLM just needs the instruction. This is why we target the summarizer rather than the downstream extractor.

**Flag-gating (Codex round 5 fix):** the prompt addition, the `event_date` schema field on the candidate_facts dict, and the `_store_candidate_facts` / direct-path `FactInput` dict-unpacks are ALL gated on `settings.temporal_extraction_enabled`. When the flag is `False` (default), the summarizer emits the legacy prompt without the date instruction, the candidate_facts dict carries no `event_date` key, the dict-unpacks ignore the field if present, and `event_date_classified_at` is NOT written by the live path. This makes the dark-launch rollback in §Rollback honest end-to-end: setting the flag false truly stops new ingests from producing event_date facts. The Layer 4 backfill script is unaffected by the flag (it runs explicitly when invoked; the flag governs live ingest only).

#### Layer 1b (defense-in-depth): FactExtractor

`nous/handlers/fact_extractor.py:_EXTRACT_PROMPT` is the fallback path used when `candidate_facts` are absent (eval shortcuts, direct ingest tools). Same instruction block as 1a, adapted for the fact-extractor's output schema. Lower priority because production traffic always has summarizer-emitted candidates.

#### Wire path (required additions across 7 hops)

Per Arch P1-1's enumeration:

| # | File:Line | Change |
|---|---|---|
| 1 | `episode_summarizer.py:50-77` | Add `event_date` to candidate_facts schema + prompt |
| 2 | `fact_extractor.py:251-256` | `_store_candidate_facts` reads `event_date = item.get("event_date")` from dict |
| 3 | `nous/heart/schemas.py:85-98` | `FactInput` adds BOTH `event_date: date \| None = None` AND `event_date_classified_at: datetime \| None = None`. Both fields must be declared so F075 producer paths (Layer 1a/1b/Layer 4) can pass them as kwargs without Pydantic dropping the value silently. Row 4's `_learn` copy and row 5's ORM column rely on this declaration. |
| 4 | `nous/heart/facts.py:428-449` | `FactManager._learn` passes `event_date` AND `event_date_classified_at` from `FactInput` to `Fact()` ORM constructor verbatim. NO policy here — `_learn` is a pure sink. Marker policy lives in the F075 producer paths only (Layer 1a, 1b, Layer 4). |
| 5 | `nous/storage/models.py:469-511` | `Fact` ORM adds `event_date: Mapped[date \| None] = mapped_column(Date, nullable=True)` AND `event_date_classified_at: Mapped[datetime \| None] = mapped_column(DateTime(timezone=True), nullable=True)` |
| 6 | `sql/migrations/053_temporal_fact_extraction.sql` | `ALTER TABLE heart.facts ADD COLUMN event_date DATE + event_date_classified_at TIMESTAMPTZ` + partial indexes + extend `brain.graph_edges` relation CHECK to add `'happened_before'` |
| 7 | `nous/heart/schemas.py:114-165` | `FactDetail` and `FactSummary` add `event_date: date \| None = None` |
| 8 | `nous/heart/facts.py:1046, 1160, 1256, 1355` | Each `FactSummary(...)` constructor passes `event_date=fact.event_date` (4 sites). Optional field defaults to None silently otherwise → recall surface returns None even when DB column populated |
| 9 | `nous/heart/facts.py:1459` (`_to_detail`) | Pass `event_date=fact.event_date` to `FactDetail` construction |
| 10 | `nous/heart/facts.py` `_to_recall_result` path | `RecallResult.metadata["event_date"] = fact.event_date.isoformat() if fact.event_date else None` so Layer 3 boost has the field to read |
| 11 | `nous/handlers/fact_extractor.py:189-196` (Layer 1b direct path) | The fallback `FactInput(...)` construction must also pass `event_date=fact.get("event_date")` AND `event_date_classified_at=datetime.now(UTC)` (gated on `settings.temporal_extraction_enabled`). This path does NOT go through `_store_candidate_facts`, so step 2 alone doesn't cover it. Without this, eval / direct-ingest traffic that bypasses the summarizer silently discards extracted dates. |
| 12 | `nous/api/retrieval_pipeline.py:659-669` (`_heart_results_to_pipeline`) | Copy `event_date` from `RecallResult.metadata` into the new `PipelineResult.metadata` dict. Today the conversion drops metadata entirely. Layer 3's `r.metadata.get("event_date")` reads from `PipelineResult`, so the field must survive the conversion. |
| 13 | `nous/handlers/episode_summarizer.py:445-468` (`_merge_summaries`) | **P1 — multi-chunk merge truncation.** For transcripts exceeding `transcript_max_chars` (16K default), the summarizer summarizes chunks separately and `_merge_summaries` returns `merged_candidate_facts[:5]` in chunk order — dated events from later chunks get dropped before FactExtractor sees them. This is the exact case F075 targets (BEAM-100K conversations routinely exceed 16K with many date-anchored events per episode). Fix: split dated and stable candidates into separate pools with independent caps — `dated[:settings.candidate_facts_event_limit] + stable[:5]`. Default event-limit = **30** so multi-day dev projects with daily date-anchored check-ins are preserved. Stable cap stays at 5 (general patterns where 5 covers most episodes). Both pools keep original chunk order so chronological information survives. |
| 14 | `nous/handlers/fact_extractor.py:249` (`_store_candidate_facts`) | **P1 — downstream cap re-truncates to 5.** `_store_candidate_facts` iterates `candidates[:5]` — even after `_merge_summaries` (row 13) emits 35 candidates, only the first 5 dated facts get stored, defeating row 13's fix. Apply the same partition here: `dated, stable = partition(candidates); dated[:settings.candidate_facts_event_limit] + stable[:5]`. Both caps come from the same settings to keep the merge+store pipeline self-consistent. |
| 15 | `nous/handlers/fact_extractor.py:176-179, 263-269` (pre-learn dedup paths) | **P2 — event_date dedup-bypass needs to apply BEFORE Heart.learn.** Both extractor paths run `search_facts(content, limit=1)` and skip on `fact_dedup_threshold` BEFORE invoking `Heart.learn` — the round-11 `_learn` event_date guard never fires for these pre-learn dedup hits. Apply the same rule at both pre-learn sites: when both candidate AND `existing[0]` have `event_date IS NOT NULL` and the dates differ, do NOT skip — proceed with the learn. Requires `search_facts` results to surface `event_date` (already covered by wire row 8 if FactSummary carries it, which row 7 mandates). |

**Plus — dedup/supersession must respect distinct dates (Codex round-11 P2 fix):** Heart.learn currently performs (a) embedding-similarity dedup (cosine ≥ `fact_native_cosine_threshold = 0.80`) and (b) subject-based supersession (same subject → newer deactivates older). Both operate without considering `event_date`. For temporal events with the same entity but different dates — e.g. *"Christina obtained the API key on March 10"* and *"Christina rotated the API key on March 12"* — embedding similarity is high AND subjects collide, so the second fact would either be silently dropped as duplicate or supersede the first. Either outcome destroys the date pair temporal_reasoning needs.

Rule: **when both candidate and existing fact have `event_date IS NOT NULL` and the dates differ, dedup/supersession is bypassed.** Different dates → different events, treated as semantically distinct regardless of embedding/subject similarity. Add the guard in:
- `Heart._learn` dedup check (before the cosine threshold compare)
- Whatever subject-based supersession path runs in sleep cycle / extraction (search for `superseded_by` writes)

Regression test: ingest two facts with same subject, same entity, same actor — only `event_date` differs — assert both rows remain active and surface separately in retrieval. Add to `tests/test_temporal_extractor.py`.

**Plus — marker write site (Codex round-9 fix):** `event_date_classified_at` must be populated by **F075 producers only**, NOT by a generic write inside `FactManager._learn`. Multiple unrelated paths call `Heart.learn(FactInput(...))` directly — `nous/api/tools.py:516` (`learn_fact` tool), `nous/api/rest.py:1722` (REST endpoint), `nous/handlers/knowledge_extractor.py:127` (pre-prune extraction) — none of which run the F075 classifier. If `_learn` blindly stamped `event_date_classified_at = NOW()` for every write when the flag is on, those rows would be falsely marked "F075-classified" and skipped by the backfill forever.

The correct design: `FactInput` carries the field optionally (default `None`), and **only the F075-aware code paths populate it**:

- **Layer 1a (summarizer path):** `_store_candidate_facts` at `fact_extractor.py:251-256` sets `event_date_classified_at=datetime.now(UTC)` on the `FactInput` it constructs, gated on `settings.temporal_extraction_enabled`.
- **Layer 1b (direct extractor path):** the `FactInput(...)` construction at `fact_extractor.py:189-196` sets the same value, gated on the same flag.
- **Layer 4 (backfill):** the per-row `UPDATE` writes both `event_date` (date or NULL) and `event_date_classified_at = NOW()` simultaneously.
- **All other paths** (`tools.py:516`, `rest.py:1722`, `knowledge_extractor.py:127`, any future `Heart.learn` caller) leave the field at its `None` default — those rows stay eligible for the backfill to catch up.

`FactManager._learn` simply persists whatever's in `FactInput.event_date_classified_at` to the DB column — no flag check, no `now()` injection, no policy. The producer decides.

**Plus (Codex v2.1 review):** the summarizer's LLM call must receive `Episode.started_at` so it can resolve relative dates ("yesterday", "last Tuesday") deterministically. Current chain `summarize_episode → _generate_summary(transcript, decision_context) → _summarize_single(transcript, decision_context)` (lines 135, 361, 394) carries neither the episode object nor `started_at`. Wire path additions for relative-date resolution — every hop must be touched, including the intermediate `_generate_summary` boundary that codex flagged in round 4:

| File:Line | Change |
|---|---|
| `episode_summarizer.py:135` | Pass `started_at=episode.started_at` to `_generate_summary` call from `summarize_episode` |
| `episode_summarizer.py:361` | `_generate_summary(transcript, decision_context, started_at: datetime \| None = None)` signature |
| `episode_summarizer.py:377, 383` | Pass `started_at=started_at` through to `_summarize_single` calls |
| `episode_summarizer.py:394` | `_summarize_single(transcript, decision_context, started_at: datetime \| None = None)` signature |
| `episode_summarizer.py:50-77` | Prompt template includes `EPISODE_START_TIMESTAMP: {started_at.isoformat()}` block above the transcript when `started_at is not None` |

Without thread-through at every hop, the prompt's "resolve relative phrases against EPISODE_START_TIMESTAMP" instruction has nothing to anchor against and dates from transcripts like "deployed yesterday" become guesses. Skipping the `_generate_summary` boundary means the value never enters scope at the inner call.

**Plus** Heart.recall surface (Python P1-4): when serializing recall results, `RecallResult.metadata["event_date"]` must be populated with the iso string (or absent if None). Without this, Layer 3 is silent no-op.

#### Pydantic v2 validator

In `nous/heart/schemas.py`. Module-scope imports/constants, then the field + validator inside `FactInput`:

```python
# nous/heart/schemas.py — module scope
import re
from datetime import date
from pydantic import field_validator

_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class FactInput(BaseModel):
    # ... existing fields ...
    event_date: date | None = None
    event_date_classified_at: datetime | None = None  # set by F075 producers only

    @field_validator("event_date", mode="before")
    @classmethod
    def _parse_event_date(cls, v):
        if v is None or isinstance(v, date):
            return v
        if isinstance(v, str):
            # Codex round-8 catch: Python 3.12 date.fromisoformat() accepts
            # alternate ISO forms like '20240310' (no hyphens) and
            # '2024-W10-7' (ISO week date). The spec/prompt contract is
            # strictly 'YYYY-MM-DD' — anything else must be dropped.
            # Two-step check:
            # (1) module-level _DATE_PATTERN regex enforces surface shape
            # (2) fromisoformat enforces day-validity (rejects 2024-02-30)
            if not _DATE_PATTERN.fullmatch(v):
                return None
            try:
                return date.fromisoformat(v)
            except ValueError:
                return None  # fail-soft: drop bad date, keep fact
        return None
```

Layout matters: `import re` and `_DATE_PATTERN` are at module scope (above `class FactInput`), so the validator method can reference `_DATE_PATTERN` as a module global without `cls.` prefix. The regex gate is the contract enforcement: anything that didn't come out of the prompt's stated `YYYY-MM-DD` slot is rejected before `fromisoformat` gets a chance to parse it as a different ISO variant. `date.fromisoformat` is then used purely for day-validity checking on the regex-passing inputs.

### Layer 2 — `happened_before` edges (reranking reinforcer)

**Scope reframing per Arch P1-3 / Devil's #3:** Layer 2 is a **modest reranking reinforcer**, not a retrieval-surfacing lever. It only affects candidates already in the retrieved set. For the dominant PATTERN_MATCH failure class, the right fact is below the retrieval cut — Layer 2 cannot help that class directly. The value is on the PARTIAL_MATCH class, where both temporally-linked facts often ARE in the candidate set.

**Edge build (in `GraphDensifier.run_backfill_cycle`):**

`brain.graph_edges` requires `agent_id NOT NULL` (`sql/init.sql:197`) and constrains `relation` via a CHECK clause that currently allows only the existing relation set (`sql/init.sql:198-201` + migration `051_f070_chunk_graph_edges.sql:34-45`). The schema migration (§Schema migration) must extend the CHECK to add `'happened_before'`, mirroring the pattern F070 used to add `'part_of'` and `'summarized_by'`.

```sql
-- Each dated fact gets at most ONE outgoing happened_before edge,
-- pointing to a single representative fact on the next-distinct date
-- in the same episode. LATERAL with LIMIT 1 prevents the quadratic
-- explosion that would occur when multiple facts share the same
-- event_date (a NOT EXISTS-on-dates approach would link every fact
-- on date D to every fact on date D+1, producing O(N²) edges per
-- adjacent-date pair).
INSERT INTO brain.graph_edges
    (source_id, source_type, target_id, target_type, agent_id, relation, weight, auto_linked)
SELECT a.id, 'fact', b.id, 'fact',
       a.agent_id, 'happened_before', 1.0, TRUE
FROM heart.facts a
JOIN LATERAL (
    SELECT b.id
    FROM heart.facts b
    WHERE b.agent_id = a.agent_id
      AND b.source_episode_id = a.source_episode_id
      AND b.event_date IS NOT NULL
      AND b.event_date > a.event_date
      AND b.active = TRUE                  -- never link to superseded/inactive
    ORDER BY b.event_date ASC, b.id ASC   -- deterministic tiebreaker
    LIMIT 1
) b ON TRUE
WHERE a.agent_id = :agent_id                -- SQLAlchemy :name bind — GraphDensifier
                                              -- executes via session.execute(text(...), ...)
                                              -- and does NOT support asyncpg $1 placeholders
  AND a.event_date IS NOT NULL
  AND a.active = TRUE                       -- active sources only (Heart recall default)
ON CONFLICT (source_id, target_id, relation) DO NOTHING;
```

Edge count per episode: at most N (where N = active facts with non-NULL `event_date` in the episode). When multiple facts share the same `event_date`, all of them get a single outgoing edge pointing to the *same* representative successor — a 20-on-Jan-1 / 20-on-Jan-2 cluster produces 20 edges (Jan-1 facts → one Jan-2 fact), not 400. `auto_linked=TRUE` mirrors F040's sleep-cycle convention. Same-date facts deliberately get no edges between each other (we don't claim ordering on concurrent events). Both sides filter `active = TRUE` — otherwise an inactive successor could become the only target while the active siblings get no `happened_before` reinforcement (Heart recall returns only active rows, so the edge would be dead weight).

**Consumer (already-shipped):**

`_apply_graph_adjacency_boost` in `nous/api/retrieval_pipeline.py:243-247, 699-738`. Excludes `contradicts` only; sums all other edge relations including `happened_before`. **No new consumer code required.**

**Required impl-plan step:** flip `NOUS_GRAPH_ADJACENCY_BOOST_ENABLED` from `false` to `true` (current default in `config.py:1055-1056`). Without this flip Layer 2 ships dark.

**Same-episode constraint ceiling (Arch P2-3):** following F070's pattern, edges only cross facts within the same `source_episode_id`. This is acceptable for v1 because BEAM convs are single long haystacks where within-episode date arithmetic is the common case. Cross-episode `happened_before` (e.g., "how long between event in March vs event in May") deferred to F075.1.

### Layer 3 — Date-aware retrieval boost (DEFERRED)

**Phase decision per Arch §Phase Strategy:** defer until Layer 1+2+4 are measured. Synthetic validation showed the date-anchored fact ranks #3 of 39 on its own embedding strength — Layer 3 may be unnecessary. Gate decision on LME pre-check + BEAM measurement.

**If implemented later**, design:

```python
async def _apply_date_boost(
    results: list[PipelineResult],
    query: str,
    settings: Settings,
) -> list[PipelineResult]:
    """F075: gentle multiplicative boost for facts whose event_date is
    within the date window implied by the query.
    """
    if not settings.date_aware_boost_enabled:
        return results
    window = _infer_query_date_window(query)  # (start, end) | None
    if window is None:
        return results

    factor = settings.date_aware_boost_factor
    boosted = []
    for r in results:
        if r.type != "fact":
            boosted.append(r); continue
        ed_iso = r.metadata.get("event_date")  # surfaced via Layer 1 Heart.recall change
        if ed_iso is None:
            boosted.append(r); continue
        try:
            event_date = date.fromisoformat(ed_iso)
        except ValueError:
            boosted.append(r); continue
        if window[0] <= event_date <= window[1]:
            # Match _apply_adjacency_boost coalesce pattern at retrieval_pipeline.py:736-737
            new_score = (r.score or 0.0) * factor
            boosted.append(replace(r, score=new_score))
        else:
            boosted.append(r)
    boosted.sort(key=lambda r: (r.score or 0.0), reverse=True)
    return boosted
```

`_infer_query_date_window` reuses the `_extract_regex` helper from `nous/heart/content_date_extractor.py`. Per Arch P3-1 / Python P3, that file must be committed and given tests before being imported in `retrieval_pipeline.py`.

### Layer 4 — Retrofit script

`scripts/backfill_temporal_facts.py`. F047 pattern — copies `nous/handlers/actionability_backfill.py` patterns verbatim.

#### Advisory lock (copy F047 verbatim — Python P1-3)

```python
import hashlib

_LOCK_NAMESPACE = "f075-temporal"  # Codex round-8 catch: salt by feature so
                                    # F075 backfill doesn't collide with F047
                                    # actionability backfill on the same agent.

def _advisory_lock_key(agent_id: str) -> int:
    """Stable signed bigint key derived from a feature-salted agent_id SHA-256.

    Mirrors actionability_backfill.py:108-115 shape but adds a feature
    namespace prefix so two unrelated backfills on the same agent do not
    block each other. F047 hashes only the bare agent_id, so without
    this salt a startup actionability sweep would falsely prevent an
    operator-invoked temporal backfill from running on the same agent.
    """
    salted = f"{_LOCK_NAMESPACE}:{agent_id}".encode("utf-8")
    digest = hashlib.sha256(salted).digest()[:8]
    return int.from_bytes(digest, byteorder="big", signed=True)
```

**Use session-scoped `pg_try_advisory_lock` + explicit `pg_advisory_unlock` at end-of-`_run_batches`, NOT the transaction-scoped `xact` variant.** Codex round-6 catch: this script's batch loop spans many per-row UPDATE transactions, each committing independently. A `pg_try_advisory_xact_lock` would release at the end of every per-row UPDATE transaction, letting a second concurrent CLI invocation acquire the lock mid-loop and interleave. F047's `actionability_backfill.py:79, 97` uses the session-scoped variant for exactly this reason. F049's working_memory sweep uses xact_lock correctly because it does all work inside one short transaction — different shape.

**Hold the lock on a CHECKED-OUT raw connection, NOT an `AsyncSession`** (Codex rounds 13 + 14 catch). Session-scoped advisory locks are bound to the specific PHYSICAL connection that acquired them. With `AsyncSession`, every `commit()` ends the transaction and the underlying connection can return to the pool — leaving the lock held on a connection that's about to be reused. Subsequent `execute()` calls (next batch, eventual `pg_advisory_unlock`) may bind a *different* connection where the unlock is a no-op. The lock leaks for the rest of the original connection's pool lifetime; future invocations skip indefinitely. Round-13's per-batch commit fix is also insufficient because the same risk reappears at every batch boundary in a multi-batch run.

Two-session pattern: one connection holds the lock for the script's full lifetime; row work uses separate sessions per batch (which CAN safely commit per batch since they don't hold the lock).

```python
# v2.14 pattern. The lock-holding connection is checked out for the
# entire script lifetime via `engine.connect()`; per-batch work uses
# fresh sessions that don't touch the lock.
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

async def _run_with_lock(
    engine: AsyncEngine,
    session_factory,            # async_sessionmaker(engine)
    agent_id: str,
    batch_size: int,
    token_budget: int,
) -> None:
    key = _advisory_lock_key(agent_id)
    # engine.connect() checks out one specific connection and holds it
    # until the `async with` exits. Per-row/per-batch commits below DO
    # NOT happen on this connection, so the pool cannot release it.
    async with engine.connect() as lock_conn:
        locked = await lock_conn.execute(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": key},
        )
        if not locked.scalar():
            logger.info("F075 backfill: another process holds the lock for %s, exiting", agent_id)
            return
        try:
            budget = BudgetTracker(token_budget // _TOKENS_PER_LLM_CALL)
            while True:
                # Each batch opens a FRESH session against the same engine.
                # Per-batch commit on this session is safe; it doesn't
                # touch lock_conn.
                async with session_factory() as work_session:
                    updated, stop = await _process_batch(
                        work_session, agent_id, batch_size, budget,
                    )
                if stop or updated == 0:
                    break
        finally:
            # Unlock on the SAME connection that acquired the lock.
            await lock_conn.execute(
                text("SELECT pg_advisory_unlock(:k)"), {"k": key},
            )
```

The lock-holding connection (`lock_conn`) never commits any data, never returns to the pool, and is the only handle that ever touches `pg_try_advisory_lock` / `pg_advisory_unlock`. The work sessions handle batch SELECT/UPDATE/commit independently. This eliminates the lock-leak failure mode across any number of batches.

#### Per-row UPDATE (Python P1-2 fix) + classification-state marker (Codex P1 fix)

v1's `WHERE id = (SELECT id FROM batch)` was broken for `LIMIT > 1`. v2 copies F047's `actionability_backfill.py:198-216` shape AND fixes a starvation bug Codex flagged on the spec-v2 review: F047 works because every row gets a non-NULL `actionable` verdict. F075 has a third state ("classified, no date found") that looks identical to "never classified" if we use `event_date IS NULL` as the eligibility predicate — so the script would re-process the same newest-N stable facts forever and never advance to older dated rows.

Fix: the schema adds `event_date_classified_at TIMESTAMPTZ` (see §Schema migration). The backfill writes it on every classification attempt regardless of outcome. Eligibility uses `event_date_classified_at IS NULL` (not `event_date IS NULL`).

```python
# Pseudo-code; uses SQLAlchemy AsyncSession to match the locked session
# context from _run_batches above. F047's actionability_backfill.py:78-205
# uses the same idiom — session.execute(text(...), {...}).
from sqlalchemy import text

async def _process_batch(
    session: AsyncSession,        # the same locked session from _run_batches
    agent_id: str,
    batch_size: int,
    budget: BudgetTracker,        # injected by _run_batches; F047 pattern
) -> tuple[int, bool]:            # (updated_rows, stop_requested)
    result = await session.execute(
        text("""
            SELECT id, subject, content, embedding, source_episode_id
            FROM heart.facts
            WHERE agent_id = :agent_id
              AND event_date_classified_at IS NULL  -- eligibility = "never tried"
              AND active = TRUE
            ORDER BY learned_at DESC
            LIMIT :batch_size
        """),
        {"agent_id": agent_id, "batch_size": batch_size},
    )
    rows = result.mappings().all()

    updated = 0
    for row in rows:
        # Codex round-10 P2 catch: check budget BEFORE every LLM call, not
        # just between batches. Mirrors F047's classifier._budget_check
        # gate at actionability_backfill.py:57-65 — without this, a small
        # --token-budget gets exceeded by up to a full batch worth of
        # rows (or unbounded if the outer loop keeps invoking).
        if not budget.ok():
            logger.info(
                "F075 backfill: token budget exhausted, stopping at %d rows",
                updated,
            )
            # Codex round-15 P2 catch: must commit work-so-far BEFORE the
            # early return. Otherwise the surrounding `async with
            # session_factory()` exit rolls back the in-flight UPDATEs and
            # those rows stay event_date_classified_at IS NULL on disk —
            # the LLM calls already spent against them are wasted because
            # the next run re-processes the same rows.
            await session.commit()
            return (updated, True)  # signal _run_batches to halt

        # Codex round-16 P2 catch: actually use _fetch_chunk_context
        # defined below. Without this, the classifier sees only
        # fact.content (lossy paraphrase) — defeating the whole point
        # of Layer 4 using chunks instead of episode.summary[:500].
        chunk_ctx = await _fetch_chunk_context(
            session,
            agent_id,
            row["source_episode_id"],
            row["embedding"],
        )
        classified = await _classify_event_date(row, chunk_context=chunk_ctx)  # date | None
        budget.consume()  # decrement remaining_calls by 1

        # ALWAYS mark classified — even when no date found. This is what
        # prevents re-processing the same stable facts on subsequent batches.
        await session.execute(
            text("""
                UPDATE heart.facts
                SET event_date = :event_date,
                    event_date_classified_at = NOW(),
                    updated_at = NOW()
                WHERE id = :id
            """),
            {"event_date": classified, "id": row["id"]},  # event_date may be NULL
        )
        updated += 1

    # Per-batch commit on the work session is safe because this session
    # does NOT hold the advisory lock — that lives on lock_conn in
    # _run_with_lock above. Trade-off: a crash mid-batch loses in-flight
    # UPDATEs (≤ batch_size rows). Idempotent re-runs pick them up via
    # event_date_classified_at IS NULL.
    await session.commit()
    return (updated, False)
```

The work session is short-lived (one batch) and never touches `pg_advisory_lock`. The lock-holding `lock_conn` (in `_run_with_lock`) is the only connection that ever issues lock/unlock calls — never commits data.

`BudgetTracker` is a tiny class mirroring F047's bookkeeping (`actionability_backfill.py:48-65, 162-167`): initialized with `remaining_calls = token_budget // _TOKENS_PER_LLM_CALL`; `ok()` returns `remaining_calls > 0`; `consume()` (no-arg) decrements `remaining_calls` by 1. The outer `_run_batches` loop exits cleanly when `_process_batch` returns `stop_requested=True`.

```python
# Codex round-17 P2 catch: units must match. v2.15-v2.16 init'd with
# call-count (token_budget // _TOKENS_PER_LLM_CALL) but consume() took
# a token count. First classification subtracted _TOKENS_PER_LLM_CALL
# from a counter sized in calls (e.g. consume(250) on a 200-call cap)
# → ok() goes false after one call → ~199 calls unused at budget=50000.

class BudgetTracker:
    def __init__(self, max_calls: int) -> None:
        self.remaining_calls = max_calls

    def ok(self) -> bool:
        return self.remaining_calls > 0

    def consume(self) -> None:
        self.remaining_calls -= 1
```

This makes the script terminate cleanly (every batch advances the eligibility cursor) and remains idempotent (re-runs only pick up rows still at `event_date_classified_at IS NULL`, which by definition haven't been tried).

The live-path Layer 1 extractor writes `event_date_classified_at = NOW()` only when `settings.temporal_extraction_enabled = True` (i.e., when the new prompt actually ran). When the flag is `False` (dark-launch default), the marker stays NULL so those rows remain eligible for the backfill to pick up once the flag is flipped on. See §Layer 1a "Flag-gating" — same rule, restated here for the backfill reader.

#### Classification LLM call (Python P2 — guaranteed JSON)

Use `call_background_llm_structured` from `nous/handlers/__init__.py:86` (or wherever the helper lives) with a tool_use input_schema for guaranteed JSON output. No parse-repair logic in the script.

#### Context source (Arch P2-1 / Python P2 — chunk context, not summary)

v1 fed `episode.summary[:500]` to the classifier. That suffers the same lossy-prose problem as Layer 1. v2 fetches the most-relevant `heart.episode_chunks` row for the fact. Codex round-10 catch: an ILIKE-on-`fact.subject` lookup is too brittle because LLM-generated subjects are descriptors, not verbatim transcript substrings — the spec's own example subject `OpenWeather API key acquisition` would not match the chunk text "I got my OpenWeather API key on March 10" because `acquisition` is absent. v2 uses **cosine similarity against the fact's existing embedding**, which is the matching strategy the rest of the codebase already uses for fact↔chunk relation:

```python
# Codex round-13 P2 catch: SQLAlchemy text() requires :name binds, not
# asyncpg $1/$2/$3. The chunk lookup runs on the same locked AsyncSession
# from _run_batches, so it must use the same binding style as the
# _process_batch SELECT/UPDATE above.
async def _fetch_chunk_context(
    session: AsyncSession,
    agent_id: str,
    episode_id: UUID | None,
    embedding,            # fact.embedding from the batch row; may be None or pgvector type
) -> str | None:
    """Nearest chunk in the source episode by cosine to fact.embedding.

    Returns None when episode_id is NULL (legacy facts without source
    episode), embedding is NULL (pre-large-embedding-rollout rows), or
    no chunk matches. Callers fall back to fact.content as primary signal.
    """
    if episode_id is None or embedding is None:
        return None
    # Codex round-16 P2 catch: the embedding from SELECT comes back as a
    # pgvector type (numpy array / list of floats), not a string. Passing
    # it raw into CAST(:embedding AS vector) can fail binding. The repo
    # convention at nous/brain/graph_linker.py:179, 266 is to serialize
    # to the pgvector text literal "[v1,v2,...]" first, then bind as str.
    embedding_str = "[" + ",".join(str(float(v)) for v in embedding) + "]"
    result = await session.execute(
        text("""
            SELECT content
            FROM heart.episode_chunks
            WHERE agent_id = :agent_id
              AND episode_id = :episode_id
              AND embedding IS NOT NULL    -- Codex round-14: legacy chunks
                                            -- with NULL embedding produce NULL
                                            -- cosine distances and LIMIT 1 then
                                            -- returns an arbitrary chunk →
                                            -- classifier sees wrong context →
                                            -- bad date stamped at backfill time
            ORDER BY embedding <=> CAST(:embedding AS vector)
            LIMIT 1
        """),
        {
            "agent_id": agent_id,
            "episode_id": episode_id,
            "embedding": embedding_str,
        },
    )
    row = result.first()
    return row[0] if row else None
```

Feeds raw chunk text (up to ~600 chars) to the classifier instead of the lossy 500-char summary slice. Three guards protect against legacy/malformed data: (1) `episode_id is None` → return None upfront; (2) `embedding is None` → return None upfront; (3) `WHERE embedding IS NOT NULL` in SQL → exclude chunks where chunk-side embedding is missing. Falls back to `fact.content` as primary classifier signal when any guard fires.

#### CLI shape

```bash
uv run python scripts/backfill_temporal_facts.py \
    --agent-id nous-default \
    --batch-size 100 \
    --token-budget 50000 \
    --dry-run     # estimate cost without LLM calls
```

#### Idempotence

`WHERE event_date_classified_at IS NULL` predicate makes re-runs safe — only rows that have NEVER been classified are picked up. Stable facts that were classified and intentionally left with `event_date = NULL` (because the classifier correctly found no date) are NOT eligible for re-processing. This is the same column the per-batch SELECT above uses; the predicate stays consistent everywhere.

#### Edge-build trigger

At end-of-script (post Layer 1 column populated), build `happened_before` edges synchronously. `GraphDensifier` captures `agent_id` in its constructor (`graph_densifier.py:108-119`) and `run_backfill_cycle()` itself takes no args (`graph_densifier.py:1045`) — the call shape mirrors `sleep_handler.py:1354`:

```python
# In the backfill script, after all rows have been classified for the agent:
from nous.brain.graph_densifier import GraphDensifier
from nous.brain.graph_linker import GraphLinker

graph_linker = GraphLinker(db, embedder, settings, agent_id)
densifier = GraphDensifier(db, graph_linker, embedder, settings, agent_id)
result = await densifier.run_backfill_cycle()
```

The densifier instance is agent-scoped; running it post-classification ensures any `happened_before` edges between newly-dated facts are written before the script exits.

---

## Schema migration

`sql/migrations/053_temporal_fact_extraction.sql`:

```sql
BEGIN;

-- F075: add event_date + classification-state columns to heart.facts
ALTER TABLE heart.facts
    ADD COLUMN IF NOT EXISTS event_date DATE DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS event_date_classified_at TIMESTAMPTZ DEFAULT NULL;

-- Index for date-range queries (Layer 3 + Layer 2 edge build)
CREATE INDEX IF NOT EXISTS idx_facts_event_date_agent
    ON heart.facts(agent_id, event_date)
    WHERE event_date IS NOT NULL;

-- Index for backfill eligibility scan (event_date_classified_at IS NULL)
CREATE INDEX IF NOT EXISTS idx_facts_event_date_unclassified_agent
    ON heart.facts(agent_id, learned_at)
    WHERE event_date_classified_at IS NULL;

COMMENT ON COLUMN heart.facts.event_date IS
    'F075: ISO date of the event this fact describes. NULL = stable fact (not event-anchored) OR pre-F075 row pending backfill.';
COMMENT ON COLUMN heart.facts.event_date_classified_at IS
    'F075: timestamp the backfill (or live extractor) classified this row for event_date. NULL = never classified, eligible for backfill. NOT NULL with event_date IS NULL = classified but no date found (terminal state, do NOT re-classify).';

-- F075 Layer 2: extend brain.graph_edges relation CHECK to allow 'happened_before'.
-- Mirrors migration 051_f070_chunk_graph_edges.sql:34-45 pattern.
ALTER TABLE brain.graph_edges DROP CONSTRAINT IF EXISTS ck_edges_relation;
ALTER TABLE brain.graph_edges DROP CONSTRAINT IF EXISTS graph_edges_relation_check;
ALTER TABLE brain.graph_edges
    ADD CONSTRAINT ck_edges_relation CHECK (
        relation IN (
            'supports', 'contradicts', 'supersedes', 'related_to', 'caused_by',
            'informed_by', 'evidence_for', 'discussed_in', 'extracted_from',
            'part_of', 'summarized_by',
            -- F075 additions:
            'happened_before'
        )
    );

COMMIT;
```

Partial indexes:
- `idx_facts_event_date_agent` excludes NULL rows so date-arithmetic queries scan only the event-fact subset.
- `idx_facts_event_date_unclassified_agent` accelerates backfill's eligibility query (`WHERE event_date_classified_at IS NULL ORDER BY learned_at DESC LIMIT N`).

The relation CHECK extension is required by Layer 2's INSERT — without it the edge build raises a constraint violation. The pattern matches F070's migration 051 verbatim.

**ORM CheckConstraint must also be updated** (`nous/storage/models.py:239-244`). The SQLAlchemy `GraphEdge.__table_args__` declares its own `ck_edges_relation` constraint that is currently **stale** — it still lists only the init.sql-era relations and was not updated when F070's migration 051 added `part_of` and `summarized_by`. Any fresh-schema path (tests using `Base.metadata.create_all`, future Alembic autogenerate) would reject all three relations.

F075 brings the ORM fully current for **all three** stale check constraints — `ck_edges_relation`, `ck_edges_source_type`, and `ck_edges_target_type`. `'chunk'` was added to the SQL source/target_type constraints by migration 051 (F070) but the ORM was never updated; fresh ORM-driven schemas reject every F070 chunk edge today. F075 fixes all three in one pass:

```python
# nous/storage/models.py — GraphEdge.__table_args__
CheckConstraint(
    "relation IN ('supports', 'contradicts', 'supersedes', 'related_to', 'caused_by', "
    "'informed_by', 'evidence_for', 'discussed_in', 'extracted_from', "
    "'part_of', 'summarized_by', "             # F070 catch-up
    "'happened_before')",                       # F075 addition
    name="ck_edges_relation",
),
CheckConstraint(
    "source_type IN ('decision', 'fact', 'episode', 'procedure', 'chunk')",  # F070 catch-up
    name="ck_edges_source_type",
),
CheckConstraint(
    "target_type IN ('decision', 'fact', 'episode', 'procedure', 'chunk')",  # F070 catch-up
    name="ck_edges_target_type",
),
```

Style matches `052_f069_document_source_kind.sql:22,34`.

---

## Settings additions (`nous/config.py`)

```python
# F075 — Temporal extraction & date-aware retrieval
# All flags default OFF for dark-launch consistency (F042/F047/F067/F071 pattern).
temporal_extraction_enabled: bool = Field(
    default=False,
    description="F075: include date-anchored event extraction in summarizer + "
                "fact-extractor prompts. Flip to True after measurement confirms "
                "no regression on existing test suite or LME baseline.",
)
date_aware_boost_enabled: bool = Field(
    default=False,
    description="F075 Layer 3: gentle multiplicative boost on facts with "
                "event_date in query's inferred date window. Deferred from v2 "
                "pending Layer 1+2+4 measurement; ship with flag default off.",
)
date_aware_boost_factor: float = Field(
    default=1.20, ge=1.0, le=2.0,
    description="F075: multiplier applied to in-window facts. 1.0 = no boost.",
)
date_aware_boost_window_pad_days: int = Field(
    default=30,
    description="F075: pad days around inferred query date window.",
)
temporal_backfill_default_token_budget: int = Field(
    default=50000,
    description="F075: default Haiku token cap for backfill script.",
)
candidate_facts_event_limit: int = Field(
    default=30,
    ge=0,
    description="F075: per-episode cap on date-anchored candidate facts "
                "merged across chunks (before FactExtractor). Stable facts "
                "stay capped at 5. Default 30 covers BEAM-100K-shaped "
                "multi-day projects with daily check-ins.",
)
```

`CLAUDE.md` env-var table gets 5 new rows. Update is part of the impl PR, not separate.

---

## Tests

**`tests/test_temporal_extractor.py`** (new):
- Summarizer prompt produces `event_date` field when transcript explicit (mock LLM)
- Summarizer prompt resolves relative dates against episode_start_timestamp
- Malformed date string fails `date.fromisoformat` → field dropped, fact kept
- Ambiguous date → field omitted, no false positive
- Same event, same date → dedup catches duplicate (existing dedup path unchanged)
- **Distinct event_date for same subject/entity → both facts persist** (Codex round-11 regression test): ingest two facts with same subject ("API key event") + same entity ("Christina") but `event_date` 2024-03-10 vs 2024-03-12, assert both active rows and both surface in retrieval. Dedup/supersession bypassed by the event-date guard.
- `_store_candidate_facts` reads `event_date` from dict and threads into `FactInput`
- FactExtractor fallback prompt (Layer 1b) also emits event_date

**`tests/test_temporal_edges.py`** (new):
- Two facts same episode, different dates → 1 `happened_before` edge
- Three facts in chronological order → 2 edges (chain, not all pairs)
- Cross-episode facts → no edge
- NULL event_date on either side → no edge
- ON CONFLICT DO NOTHING prevents duplicate edges on re-run

**`tests/test_temporal_backfill.py`** (new):
- Advisory lock (mocked `pg_try_advisory_lock` + `pg_advisory_unlock`) prevents concurrent backfills
- Token budget exhausted → clean halt, no partial-row corruption
- Dry-run produces estimate without LLM calls
- NULL-only filter means re-running picks up only unprocessed rows
- Chunk context source (not summary) — verified via mock chunk lookup

**`tests/test_date_aware_boost.py`** — DEFERRED with Layer 3 (codex round-6 fix). The test set above and acceptance criterion #1 cover Layers 1+2+4 only. When Layer 3 ships (after measurement gate), its impl PR adds:
- Query "how many days between" detects as date-arithmetic
- Query "OpenWeather endpoints" produces no window
- Fact with event_date in window: `(score or 0.0) * factor`
- Fact outside window untouched
- Re-sort stable when no boosts applied; missing scores coalesce to 0.0

This keeps F075 v1 internally consistent — the required test set matches the implemented scope, so CI doesn't fail against missing date-boost code or force the deferred feature to ship.

**Integration test in `tests/test_f075_end_to_end.py`:**
- Ingest a fixture conversation with explicit date references
- Verify extracted facts have correct `event_date`
- Verify `RecallResult.metadata["event_date"]` is populated for retrieved facts
- Verify edges built via sleep cycle
- Confirm baseline retrieval (without Layer 3) still surfaces dated fact at high rank

pytest-asyncio config in `pyproject.toml:59` is already `asyncio_mode = "auto"` (verified by python reviewer); no config change needed. The repo currently has `tests/test_fact_extractor_episode_id.py` (F022-narrow); no general `test_fact_extractor.py` exists yet, so F075 establishes one.

---

## Acceptance criteria

1. **All new tests pass.** 3 unit files + 1 integration file, ~25 tests (`test_temporal_extractor.py`, `test_temporal_edges.py`, `test_temporal_backfill.py`, `test_f075_end_to_end.py`). `test_date_aware_boost.py` ships with Layer 3, NOT in this PR.
2. **Existing tests pass.** No regression in `tests/test_fact_extractor_episode_id.py`, `tests/test_heart.py`, `tests/test_graph_densifier.py`.
3. **Migration runs cleanly on fresh DB** (`docker compose up` cold start), verified by F074 harness pattern.
4. **LongMemEval N=20 retrieval pre-check** (cheap, ~$5): hit@10 on temporal-reasoning category questions improves by ≥+5% vs current baseline. If LME pre-check fails, do NOT proceed to BEAM.
5. **BEAM Phase 1 re-run (n=5 conv) shows temporal_reasoning ≥ 0.55**, ideally ≥ 0.60. Other ability scores stay within ±0.05 of prod-v3 (no collateral regression like K-bump's abstention -0.167).
6. **Per-failure-class verification** (cheap, $0.30 of synthetic-fact tests): inject hand-crafted dated facts for one PATTERN_MATCH (conv 4 Q1) and one PARTIAL_MATCH (conv 5 Q1), confirm both move from 0.000 to ≥0.5 via the same chain as the conv 2 Q0 baseline. Devil's #2 risk-mitigation: pre-implementation gate.
7. **Retrofit script dry-run** on `nous-default` prod-snapshot reports cost estimate; full run completes within token budget; advisory lock prevents concurrent execution.
8. **Settings docs updated** in `CLAUDE.md` env-var table.

---

## Cost & risk

**Implementation cost:**
- Layer 1 (summarizer + extractor prompts + schema wire path): ~110 LOC across 7 files + migration + 8 tests
- Layer 2 (happened_before edges + flag flip): ~60 LOC in GraphDensifier + 5 tests
- Layer 3 (date-aware boost): ~50 LOC in retrieval_pipeline + 5 tests *(deferred)*
- Layer 4 (backfill script): ~280 LOC + 6 tests + CLI
- Total: ~500 LOC (450 if Layer 3 deferred) + 30 tests. ~3-4 days for one engineer.

**Measurement cost:**
- Synthetic per-failure-class verification (criterion #6): ~$0.30
- LongMemEval N=20 temporal retrieval pre-check: ~$5
- BEAM Phase 1 n=5 re-run: ~$7
- Total: ~$13

**Backfill cost (prod):**
- `nous-default` ~5K facts × Haiku ~10 tokens each ≈ ~$0.30
- Plus chunk-context lookup: ~1 extra DB query per fact, negligible

**Risks:**

1. **Prompt-engineering collateral on summarizer.** Layer 1 modifies the summarizer prompt, which is a high-traffic shared path. Risk: existing fact-extraction behavior shifts on non-event content. **Mitigation**: integration test with known-event corpus; LME hand-label qrels comparison; explicit acceptance criterion #2.
2. **PARTIAL_MATCH class may not move as expected.** Devil's #2 revealed 2 of 5 failures are PARTIAL_MATCH where chunks already surface but LLM doesn't compose. Discrete dated facts SHOULD help (they're easier to combine than prose), but this is unverified. **Mitigation**: acceptance criterion #6 pre-implementation synthetic verification at ~$0.30.
3. **`happened_before` edges may bleed into wrong adjacency reinforcement.** `_apply_graph_adjacency_boost` sums ALL non-`contradicts` relations indistinguishably (`retrieval_pipeline.py:712`). If temporal_anchor neighbors mislead retrieval for non-temporal queries, we'd see collateral. **Mitigation**: monitor non-target ability scores in BEAM re-run; if regression > 0.05, revisit adjacency-boost allowlist (F075.1 candidate).
4. **Same-episode `happened_before` ceiling.** Inherits F070 cross-episode-edge gap. Cross-session date pairs never get edges (F075.1). Acceptable for v1 because BEAM convs are single-haystack.
5. **Source ambiguity (1 of 5)** unfixable by this work. Ceiling at ~0.7 on temporal_reasoning from extractor + edges alone.

---

## Alternatives considered & rejected

### A1. Wire `nous/heart/content_date_extractor.py` into `_format_pipeline_text`

The existing untracked module annotates retrieved snippets with `[event: YYYY-MM-DD (~N months ago)]` markers. Falsified for the PATTERN_MATCH class: the right snippet isn't retrieved, so markers have nothing to annotate. **Partially repurposed in Layer 3** — its `_extract_regex` function is reused by the date-language detector (with commit + tests added per P3).

### A2. Synthetic `nous_system.date_anchors` node table

A separate table where each unique date gets a UUID, then `temporal_anchor` edges target those rows. Enables graph-walk semantics ("find all facts on this date"). Deferred to F075.1 because:
- `WHERE event_date = '2024-03-10'` already does the work via index
- Adds a new table for a use case we don't yet have

### A3. Separate `nous/handlers/temporal_fact_extractor.py` parallel pipeline

Doubles LLM cost per episode. Augmenting summarizer + extractor prompts is simpler with same expressive power.

### A4. Multi-date facts (start + end ranges in one fact)

Reject for v1: requires schema beyond one column, complicates dedup, no rubric tests range queries. F075.2 if needed.

### A5. Read timestamps from chat metadata

Reject: prod doesn't reliably have message timestamps. Couples memory to ingest-time metadata.

### A6. Defer Layer 3 entirely

Adopted in v2. Synthetic validation showed fact embedding alone ranks #3 without boost. Ship Layers 1+2+4; measure; add Layer 3 only if temporal_reasoning lands <0.55.

---

## Open questions (resolved in v2)

| v1 question | v2 resolution |
|---|---|
| Augment vs new module (Layer 1) | Augment. Primary target = EpisodeSummarizer. Defense-in-depth = FactExtractor. |
| Layer 3 phase decision | Defer until Layer 1+2+4 measurement. |
| `event_date` validator stringency | Fail-soft (drop field, keep fact) via `date.fromisoformat` in Pydantic v2 `field_validator`. |
| Backfill batching | Per-row UPDATE copying F047's `actionability_backfill.py:198-216`. |
| Edge build trigger | Synchronous at end-of-backfill-script. |
| Token budget default | Total cap with resume, 50K Haiku tokens default. Matches F047. |

---

## Rollback

1. Set `NOUS_TEMPORAL_EXTRACTION_ENABLED=false` — new ingests stop producing event_date facts. Existing facts retain their event_date.
2. Set `NOUS_DATE_AWARE_BOOST_ENABLED=false` — Layer 3 disabled. (Default off.)
3. Set `NOUS_GRAPH_ADJACENCY_BOOST_ENABLED=false` (revert prod flip) — Layer 2 reranking disabled.
4. Migration is forward-only. To fully revert column: drop via v2 migration. Partial index is harmless to leave.
5. Backfill outputs are idempotent and additive — no rollback path needed.

Feature is **flag-gated end-to-end**. Worst case: turn off flags, behavior reverts to pre-F075. Cost = one-time backfill spend.

---

## Deferred to F075.x

- **F075.1** — `nous_system.date_anchors` node table + true `temporal_anchor` edges (graph-walk); cross-episode `happened_before` edges (F070 ceiling fix)
- **F075.2** — Multi-date facts (range events)
- **F075.3** — Timeline dashboard tab
- **F075.4** — Post-extract `dateparser` fallback (defense-in-depth on LLM extraction misses)
- **F075.5** — Adjacency-boost allowlist (per-relation weighting); only if BEAM measurement shows collateral regression
