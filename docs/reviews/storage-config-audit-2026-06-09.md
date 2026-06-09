# Storage Layer + Configuration Audit — 2026-06-09

**Scope:** `nous/storage/` (database.py, models.py, migrator.py, __init__.py), `nous/config.py`, `nous/runtime_config.py`, `nous/utils.py`, `sql/init.sql`, all 53 files in `sql/migrations/` (006–060), `docker-compose.yml` + `Dockerfile` (storage/env wiring only).

**Method:** code-only. Every ORM table diffed against init.sql + cumulative migrations. Migrator splitter/ordering/atomicity traced. Every suspicious config field grepped for consumers. One empirical check (pydantic-settings empty-string behavior) run against the repo's installed pydantic-settings 2.13.1.

**Headline:** the ORM ↔ SQL column inventory is in good shape — no ORM column is missing from the cumulative DB schema and no live DB column is silently invisible to the ORM (the GENERATED `search_tsv` columns are deliberately unmapped). The real problems live in (a) the docker-compose env wiring, which can crash a fresh install outright and silently reverts/blank several documented defaults, and (b) the migrator's atomicity contract, which eight existing migrations violate with embedded `BEGIN;`/`COMMIT;`.

---

## Findings Register

Severity: P1 = breaks fresh install / data loss / wrong behavior on live path; P2 = significant risk or drift; P3 = minor; INFO = observation.
Reachability: LIVE (hit on a normal path), LATENT (needs a specific-but-plausible trigger), INERT (flag-gated off), DEAD (no consumer).

### P1

**ST-1 — Fresh `docker compose up` without a host `.env` crashes Settings (empty-string JSON dict env).**
- Severity: P1 · Reachability: **LIVE** (fresh-install path)
- Where: `docker-compose.yml:70` + `nous/config.py:195`
- `docker-compose.yml:70` sets `NOUS_CONTEXT_BUDGET_OVERRIDES=${NOUS_CONTEXT_BUDGET_OVERRIDES:-}` — when the var is unset on the host (fresh clone, no `.env`), the container gets the var set to **empty string**. `context_budget_overrides: dict[str, int]` (config.py:195) is a complex field with no `mode="before"` validator; pydantic-settings JSON-decodes the raw value.
- Evidence (verified against installed pydantic-settings 2.13.1):
  ```
  NOUS_CONTEXT_BUDGET_OVERRIDES= python -c "... S() ..."
  → SettingsError: error parsing value for field "context_budget_overrides" from source "EnvSettingsSource"
  ```
  `Settings()` is instantiated at startup in `nous/main.py`, so the container crash-loops before `Database()` is ever constructed.
- Fix: either drop the line from compose (the var is optional), use `${NOUS_CONTEXT_BUDGET_OVERRIDES:-{}}`, or add a `@field_validator(..., mode="before")` on the three dict fields (`context_budget_overrides`, `relevance_min_results`/`relevance_max_results`, `frame_default_models`, `dag_global_max_concurrent_by_frame`) that maps `""` → `{}`. Setting `env_ignore_empty=True` in `model_config` is the structural fix for the whole class of empty-string env values.

### P2

**ST-2 — Eight migrations embed `BEGIN;`/`COMMIT;` inside the migrator's single outer transaction, breaking run atomicity and partial-failure recovery.**
- Severity: P2 · Reachability: **LATENT** (bites on any mid-run failure/crash after migration 032)
- Where: `nous/storage/migrator.py:100` (one `engine.begin()` wraps ALL pending files + their version-row INSERTs); offenders: `032_dag_orchestration.sql`, `047_f065_edge_provenance.sql`, `048_f066_1_dag_fix_stage.sql`, `049_f065_auto_linker_inferred.sql`, `052_f069_document_source_kind.sql`, `053_temporal_fact_extraction.sql`, `054_comention_extraction_method.sql`, `055_cooccurrence_edges.sql`.
- The splitter (`migrator.py:30-85`) does not strip transaction-control statements, so `COMMIT` is executed in-band. The first embedded `COMMIT` (032 on a fresh DB) commits everything so far and drops the connection into autocommit; every subsequent statement commits individually. Consequences: (1) a failure mid-file after 032 leaves a *partially applied, committed* migration with no version row; (2) re-run then re-executes non-idempotent statements — concretely `033_dag_completion_check.sql:595-600` uses plain `ADD COLUMN` (no `IF NOT EXISTS`) ×6, so the retry fails with `duplicate column` and **the migrator is wedged: app cannot boot**; (3) a crash between a file's last statement and its version INSERT produces the same wedge.
- Evidence the contract is known: `057_procedure_supersession.sql:1441-1443` — "no BEGIN/COMMIT (run_migrations wraps every file in one transaction)" — and the same note in 058/059. The eight earlier files predate the rule and were never cleaned.
- Fix: strip `BEGIN`/`COMMIT` statements in `_split_sql_statements` (one-line filter), or remove them from the eight files (checksum column is never re-verified, see ST-17, so editing applied files is safe in this scheme). Separately consider transaction-per-migration instead of one-per-run.

**ST-3 — docker-compose pins pre-F048 subtask timeouts, re-introducing the bug F048 fixed.**
- Severity: P2 · Reachability: **LIVE** (any compose deployment that doesn't override)
- Where: `docker-compose.yml:40-41` — `NOUS_SUBTASK_DEFAULT_TIMEOUT=${...:-120}`, `NOUS_SUBTASK_MAX_TIMEOUT=${...:-600}` vs `nous/config.py:477-478` (600 / 3600, "F048: bumped … so outer wait_for doesn't cancel inner streaming") and the CLAUDE.md env table (600 / 3600).
- Because compose always *sets* the env var, the code default never applies in containers: the outer `asyncio.wait_for(120)` again cancels before the 600 s per-chunk background-streaming read completes — exactly the F048 failure mode.
- Fix: update compose defaults to 600/3600 (and audit the rest of the list — see ST-5/ST-6).

**ST-4 — compose blanks the built-in identity prompt: `NOUS_IDENTITY_PROMPT=${NOUS_IDENTITY_PROMPT:-}`.**
- Severity: P2 · Reachability: **LIVE** (compose deployments without the var)
- Where: `docker-compose.yml:35` vs `nous/config.py:308-314`.
- Empty string is a valid `str`, so `identity_prompt` becomes `""` instead of the documented built-in default ("First section of every system prompt. How Nous knows who it is…"). The agent boots with no identity block. Same `:-` empty-string pattern, different failure shape than ST-1 (silent instead of crash).
- Fix: remove the line or use a guard (`${NOUS_IDENTITY_PROMPT:?}`-style is wrong here; just drop it — Settings reads the env var directly when present).

**ST-5 — compose `environment:` list is a whitelist; ~80% of documented `NOUS_*` flags cannot be set through it.**
- Severity: P2 · Reachability: **LIVE** (operational trap)
- Where: `docker-compose.yml:7-81` (nous service; no `env_file:` directive), `Dockerfile` (no `.env` copied — correct for secrets), `nous/config.py:19` (`env_file=".env"` resolves relative to CWD `/app`, where no `.env` exists).
- Every flag added after ~F042 (`NOUS_EPISODE_CHUNKS_ENABLED`, `NOUS_TEMPORAL_EXTRACTION_ENABLED`, `NOUS_EPISTEMIC_GATE_ENABLED`, `NOUS_QUERY_EXPANSION_*`, `NOUS_PROC_CATALOG_ENABLED`, `NOUS_EMBEDDING_MODEL`, `NOUS_FACT_DEDUP_TIEBREAKER_ENABLED`, …) is absent from the list. CLAUDE.md and multiple feature rollouts instruct "operator flips NOUS_X post-deploy" — with this compose file the flip is **silently inert** inside the container.
- Fix: add `env_file: .env` (or `${VAR}` passthrough block) to the nous service so host `.env` reaches the container, then prune the per-var list to genuine overrides.

**ST-6 — compose default-value drift cluster vs config.py/CLAUDE.md.**
- Severity: P2 (aggregate) · Reachability: **LIVE** in compose deployments
- Where → code → compose:
  - `NOUS_MODEL` / `NOUS_BACKGROUND_MODEL`: code `claude-sonnet-4-6` (config.py:349,329) vs compose `claude-sonnet-4-5-20250514` (compose:18,31) — a stale (and oddly-dated) model id.
  - `NOUS_STALENESS_HALF_LIFE_DAYS`: code 30 (config.py:199), CLAUDE.md 30, compose **14** (compose:72).
  - `NOUS_RELEVANCE_DROP_RATIO`: code **0.5** (config.py:183), CLAUDE.md **0.6**, compose 0.6 (compose:68) — three-way disagreement; the code default is the one nobody documents.
  - `NOUS_CROSS_ENCODER_MODEL`: code `BAAI/bge-reranker-v2-m3` (config.py:768) vs compose + CLAUDE.md `cross-encoder/ms-marco-MiniLM-L-6-v2` (compose:77). Known-intentional for prod (BGE too slow on prod VM) but the intent lives nowhere in-tree.
- Fix: reconcile each to one source of truth; document intentional prod-only pins in compose comments.

**ST-7 — Migrator has no cross-process guard: concurrent boots race `run_migrations`.**
- Severity: P2 · Reachability: **LATENT** (single-instance today; compose `restart: unless-stopped` + a second replica or a fast restart during a long migration run can race)
- Where: `nous/storage/migrator.py:88-132`.
- Two processes can both read `schema_migrations`, both see the same pending set, and both execute the files. Most statements are `IF NOT EXISTS`-guarded, but data migrations (049 reclassification, 058 consolidation, 060 edge DELETE) execute twice concurrently, and the loser of the version-row INSERT race aborts its entire outer transaction → boot failure with half the work committed via ST-2's autocommit hole.
- Fix: take `pg_advisory_lock(<const>)` on the migration connection before reading `schema_migrations` (the repo already uses advisory-lock patterns in F047/F049).

### P3

**ST-8 — Splitter fragility: dollar-quoting unsupported, inline `--` comments unstripped.**
- P3 · LATENT · `nous/storage/migrator.py:30-85`.
- `023_procedure_fullbody_search.sql:345-347` contains `AS $$ SELECT array_to_string(arr, sep) $$;` — it parses correctly **only because** the `$$` body happens to contain no `;` or `'`. The docstring admits `$$` is unsupported. Also, only lines *starting* with `--` are stripped; a trailing comment containing an apostrophe (`ADD COLUMN x INT -- it's a count`) would flip the in-string tracker and mis-split. No current file trips either, but every new migration is one careless comment away.
- Fix: add `$$`-tracking to the splitter (small), or lint migrations in CI for `$$`-with-semicolon and inline comments.

**ST-9 — `Settings.workspace_dir = "/tmp/nous-workspace"` hardcodes POSIX.**
- P3 · LATENT (Windows dev) · `nous/config.py:381`. Contrast `dag_workspace_root` (config.py:881-884) which correctly uses `tempfile.gettempdir()`. Same file, two conventions.

**ST-10 — No bounds/cross-field validation on several numeric knobs.**
- P3 · LATENT · `nous/config.py`:
  - `vector_weight` (line 106) unbounded — the DB-load path validates `0.0 <= w <= 1.0` (`runtime_config.py:133`) but the env path accepts 1.5.
  - `subtask_default_timeout` vs `subtask_max_timeout` (477-478): no `default <= max` validator (DAG timeouts have one at 1471-1478; subtasks don't).
  - `heartbeat_quiet_start`/`quiet_end`/`digest_hour_utc` (805-806, 821): no 0–23 bounds.
  - `embedding_dimensions` (41): no `ge`, and — more importantly — no guard that it equals the `vector(1536)` DDL dimension (see ST-11).

**ST-11 — `embedding_dimensions` is configurable but every DDL column is `vector(1536)`; no startup cross-check.**
- P3 · LATENT · `nous/config.py:41` vs `sql/init.sql:140,267,309,350,400` and `050_episode_chunks.sql:1165`.
- Setting `NOUS_EMBEDDING_DIMENSIONS=3072` (the natural value for prod's text-embedding-3-large) makes every embed write/search fail at runtime with a pgvector dimension error — loud, but only after boot, and the error points at the query, not the config. A one-time startup probe (compare `atttypmod` on one embedding column vs settings) would fail fast with a clear message. Note CLAUDE.md still documents text-embedding-3-small while prod runs -large at 1536 reduced dims.

**ST-12 — `nous_system.dynamic_checks.agent_id` defaults to `'nous'`, not `'nous-default'`.**
- P3 · LATENT · `027_dynamic_checks.sql:457` (`DEFAULT 'nous'`) and ORM `models.py:912` (`default="nous"`).
- Any insert path that omits `agent_id` creates rows invisible to the default agent (`nous-default`, config.py:39). Current writers pass it explicitly, but the default is a trap and disagrees with every other table's convention (no default, NOT NULL).

**ST-13 — F061's promised `final_outcome` CHECK constraint never shipped.**
- P3 · INERT · `041_subtask_hardening.sql:748-749`: "CHECK constraint on final_outcome is deferred to a follow-up migration (042) after pre-flag rows are backfilled" — migration 042 became F062 payload_schema instead. `final_outcome` (models.py:815) accepts any string forever.

**ST-14 — `RuntimeConfig.load_from_db` swallows all exceptions at DEBUG.**
- P3 · LATENT · `nous/runtime_config.py:153-154`. A real failure (permissions, bad jsonb, connection blip) is logged as "table not available yet (normal on first run)" at debug level — persisted overrides silently stop applying. Catch the specific missing-table error; log others at WARNING. Also `persist_to_db` (156-166) commits the caller's session — a surprising side effect for a helper.

**ST-15 — DB-only constraints/indexes absent from the ORM ⇒ SQLite test mode enforces a weaker schema.**
- P3 · LATENT · `tests/sqlite_compat.py:203-207` runs `Base.metadata.create_all`, so anything not declared on the ORM doesn't exist in tests:
  - `uq_episode_chunks_episode_index` UNIQUE(episode_id, chunk_index) (050:1187-1188) — the F067 idempotent re-ingest invariant — not on `EpisodeChunk` (models.py:415-452).
  - `idx_rubric_active_agent` one-active-rubric-per-agent partial unique (022:312-313) — not on `RubricVersion`.
  - `uq_procedures_active_lower_name` (059:1532-1534) — not on `Procedure`.
  - Value CHECKs that exist only in SQL: `decisions.confidence BETWEEN 0 AND 1` (init.sql:133), `episodes.surprise_level BETWEEN 0 AND 1` (init.sql:265), `chk_subtask_priority` (init.sql:465), `chk_schedule_has_timing` (init.sql:492-495), `chk_dag_budget` (032:541), `episode_chunks.source_kind` CHECK (052:1270).
- Consequence: tests can pass writes that prod rejects (and vice versa for uniqueness). Declare the load-bearing ones (the three unique indexes) on the ORM via `Index(..., unique=True, postgresql_where=...)`.

**ST-16 — `nous_system.config` runtime overrides are global, not agent-scoped.**
- P3 · LATENT · `021_config_table.sql:285-289` (key PK, no agent_id) + `runtime_config.py`. `vector_weight` / `rrf_k` / `cross_encoder_enabled` overrides apply to every agent on the instance. Documented as deliberate in 021's header, but it contradicts the "all tables agent-scoped" project rule; fine single-agent, wrong multi-agent.

**ST-17 — Migration checksums are stored but never verified.**
- P3 · INFO-adjacent · `migrator.py:117,126-130`. `checksum` is written on apply and never read again — an edited already-applied migration silently diverges between environments. Either verify on boot (warn on mismatch) or drop the column.

**ST-18 — No version-collision detection in the migrator.**
- P3 · LATENT · `migrator.py:112-114`. Two files `035_a.sql` and `035_b.sql` would apply the first (lexicographic) and *silently skip* the second forever (version key already in `existing`). The 035/036 numbering gap (documented in `038_query_expansions.sql:664-666`) shows renumbering accidents already happened once. A one-line duplicate-prefix assertion would close it.

### INFO

**ST-19 — Doc-drift inventory (counts/comments, not behavior).**
- `sql/init.sql:3` says "23 tables … heart (10)" and the heart banner (init.sql:246) says "(8 tables)" — heart actually has 11 in init.sql; cumulative DB now has ~34 tables.
- `models.py:1` says "all 20 Nous tables" — file defines 31 models.
- `nous/storage/__init__.py:26-47` exports only the original 19 names; 12 newer models (AgentIdentity, EpisodeChunk, ProcedureTaskAffinity, RubricVersion, OutcomeSignal, ConversationState, Subtask, Schedule, ToolCache, DynamicCheckModel, DAG*, WorkQueueItem, GraphHubSnapshot) must be imported from `nous.storage.models` directly. Harmless, just inconsistent.
- `Dockerfile:1` is `python:3.12-slim`; CLAUDE.md claims "3.14 in container".
- CLAUDE.md REST table says 42/52 endpoints in two places.

**ST-20 — Vestigial initdb mount.** `docker-compose.yml:115` mounts `010_subtasks.sql` as a third initdb script, but init.sql already creates `heart.subtasks`/`heart.schedules` (init.sql:445-496) with the *newer* defaults; 010 is `IF NOT EXISTS` so the mount is a guaranteed no-op. Remove to avoid implying initdb-time migrations are a pattern.

**ST-21 — `Database` pool/session posture.** `database.py:18-25`: `pool_size=10, max_overflow=5`, `pool_pre_ping=True`, no `pool_timeout`/`pool_recycle` overrides (SQLAlchemy defaults: 30 s wait then `TimeoutError`). With 2 subtask workers + heartbeat + scheduler + sleep + event handlers + API traffic on one pool, 15 connections is plausible to exhaust under burst; the failure mode (30 s stall then exception) is acceptable but worth a metric. `session()` (46-50) yields without commit and the sessionmaker context closes/rolls back properly — no leak path found. `echo=settings.log_level == "debug"` (line 23) is exact-match lowercase only.

**ST-22 — `connect()` schema probe is the only preflight.** `database.py:27-40` verifies the three schemas exist but not init.sql completeness; an empty-but-schema'd DB would pass and then fail in the migrator/bootstrap. Acceptable: init.sql is only ever applied by initdb, which creates schemas and tables atomically.

**ST-23 — `host: str = "0.0.0.0"`** (config.py:299) binds all interfaces by default; compose publishes the port anyway, but bare-metal runs expose the unauthenticated REST API LAN-wide.

**ST-24 — Migration 058 hard-codes prod UUIDs + `agent_id='nous-default'`** as a permanent migration every fresh install executes (as guarded no-ops). Works, but one-time prod data surgery as a migration is a hygiene smell; the splitter-safety constraints it had to obey (no DO blocks) are a direct consequence of ST-8.

---

## ORM ↔ SQL Drift Table

Legend: ✓ = match (cumulative: init.sql + migrations). Only deltas listed; all unlisted columns matched on name/type/null/default.

| Table | ORM model | Columns | Constraints/indexes | Notes |
|---|---|---|---|---|
| nous_system.agents | Agent | ✓ | ✓ | |
| nous_system.agent_identity | AgentIdentity | ✓ | partial unique `idx_identity_agent_section_current` DB-only (init.sql:74) | not exported in `__init__` |
| nous_system.frames | Frame | ✓ | ✓ | |
| nous_system.events | Event | ✓ | ORM `trace_id index=True` would emit `ix_…` name; DB has `idx_events_trace_id` (026:395) — names differ, SQLite-tests-only impact | |
| nous_system.schema_migrations | — (raw SQL) | n/a | n/a | managed by migrator bootstrap |
| nous_system.config | — (raw SQL) | n/a | trigger added in 021 | no agent_id (ST-16) |
| nous_system.context_log | — (raw SQL) | n/a | n/a | consumers in observability/ |
| nous_system.behavior_snapshots | — (raw SQL) | n/a | n/a | SERIAL pk (only non-UUID pk in DB) |
| nous_system.dynamic_checks | DynamicCheckModel | ✓ (DB TEXT vs ORM String — fine) | UNIQUE(agent_id,name) DB-only (027:475) — **not in ORM** | agent_id default `'nous'` (ST-12) |
| nous_system.execution_dags | ExecutionDAG | ✓ | `chk_dag_budget` DB-only | |
| nous_system.dag_nodes | DAGNode | ✓ (incl. 033/043/048 adds) | status/node_type CHECKs ✓ post-048 | DB default timeout 120 vs Settings 600 — clamped at read sites (F046) |
| nous_system.dag_edges | DAGEdge | ✓ | `chk_dag_edge_type` incl. `on_failure` (048:1056) **not in ORM** (no CHECK declared) | |
| nous_system.work_queue_items | WorkQueueItem | ✓ | ✓ | |
| nous_system.eval_runs | — (nous_eval raw SQL) | n/a | n/a | eval-only; created on main DB by 037 |
| brain.decisions | Decision | ✓ (session_id/reviewer via 009; confidence_raw via 039) | DB `confidence BETWEEN 0 AND 1` not in ORM; DB constraint names auto-generated vs ORM `ck_*` | search_tsv unmapped (intentional) |
| brain.decision_tags | DecisionTag | ✓ | ✓ | no agent_id (child) |
| brain.decision_reasons | DecisionReason | ✓ | ✓ | no agent_id (child) |
| brain.decision_bridge | DecisionBridge | ✓ | ✓ | no agent_id (child) |
| brain.thoughts | Thought | ✓ | ✓ | |
| brain.graph_edges | GraphEdge | ✓ (extraction_method via 047) | relation/type/method CHECK lists ✓ vs 055 final state; `idx_graph_edges_extraction_method` (047:984) not in ORM | FKs dropped by 016 — dangling-edge hazard handled by 060 + code fix |
| brain.graph_hub_snapshots | GraphHubSnapshot | ✓ | 2 DB indexes not in ORM | |
| brain.guardrails | Guardrail | ✓ | ✓ | |
| brain.calibration_snapshots | CalibrationSnapshot | ✓ | ✓ | |
| heart.episodes | Episode | ✓ (compaction_count 020, session_id 040, transcript 025/init) | `surprise_level BETWEEN 0 AND 1` DB-only; `idx_episodes_session_id` partial (040:733) not in ORM | search_tsv unmapped |
| heart.episode_chunks | EpisodeChunk | ✓ (source_kind/source_ref 052) | **UNIQUE(episode_id, chunk_index)** (050:1187) and source_kind CHECK (052:1270) **not in ORM** (ST-15) | |
| heart.episode_decisions | EpisodeDecision | ✓ | ✓ | no agent_id (join) |
| heart.facts | Fact | ✓ (017/019/034/053 adds; `actionable_confidence` REAL↔Float fine) | `confidence BETWEEN 0 AND 1` DB-only; partial indexes (031, 034, 053, 060) not in ORM | search_tsv unmapped |
| heart.procedures | Procedure | ✓ (runtime_metadata 044, superseded_by/archived_at 057) | `uq_procedures_active_lower_name` (059) **not in ORM** (ST-15) | search_tsv rebuilt by 023 = init.sql definition (fresh-DB no-op-equivalent) |
| heart.episode_procedures | EpisodeProcedure | ✓ | ✓ | no agent_id (join) |
| heart.procedure_task_affinity | ProcedureTaskAffinity | ✓ (DB TEXT vs ORM String(100) — fine) | ✓ | |
| heart.rubric_versions | RubricVersion | ✓ | `idx_rubric_active_agent` partial unique **not in ORM** (ST-15) | |
| heart.outcome_signals | OutcomeSignal | ✓ | ✓ | |
| heart.censors | Censor | ✓ (024 + 056 adds; action default 'steer' ✓) | post-056 CHECK names ✓ | no search_tsv by design (embedding-only retrieval) |
| heart.working_memory | WorkingMemory | ✓ | ✓ | `items`/`open_threads` typed `Mapped[dict]` but hold JSON arrays (`'[]'` default) — type annotation lie, cosmetic |
| heart.conversation_state | ConversationState | ✓ | ✓ | |
| heart.subtasks | Subtask | ✓ (011/012/041/042 adds) | `chk_subtask_priority` DB-only; promised final_outcome CHECK missing (ST-13) | parent_session_id DB VARCHAR (unbounded) vs ORM String(200) — fine |
| heart.schedules | Schedule | ✓ (015/045 adds) | `chk_schedule_has_timing` DB-only | |
| heart.tool_cache | ToolCache | ✓ | ✓ | |
| heart.query_expansions | — (raw SQL) | n/a | n/a | deliberately global, no agent_id (documented in 038 header) |

**Vector dimensions:** every embedding column in DDL and ORM is 1536 (decisions, episodes, facts, procedures, censors, episode_chunks). No 3072 anywhere; prod's text-embedding-3-large must run at `dimensions=1536` (it does, via `embedding_dimensions` default) — see ST-11 for the missing guard.
**HNSW coverage:** all six embedded tables have `hnsw(embedding vector_cosine_ops)` (init.sql:531,564,574,582,591; 050:1183). Complete.
**tsvector/GIN:** decisions, episodes, facts, procedures, episode_chunks all have GENERATED search_tsv + GIN. Censors intentionally have none.
**Fresh-DB path:** linear replay of init.sql → 006…060 was traced statement-by-statement: no duplicate-object or missing-object failure on a clean run (023's DROP COLUMN/recreate of the already-final init.sql tsv works because DROP COLUMN cascades the GIN index; 028/058 are guarded no-ops; 033/029's bare ADD COLUMNs are fine linearly — they are only dangerous on the ST-2 retry path).

---

## Dead Config / Dead Table Inventory

Config fields defined in `nous/config.py` with **zero consumers** in `nous/`, `nous_eval/`, `scripts/` (grep-verified; tests asserting the default don't count):

| Field | Line | Status |
|---|---|---|
| `quality_block_threshold` | 53 | DEAD — quality gating reads the guardrail CEL (`decision.quality_score < 0.55`, seed.sql + migration 028), not this field |
| `agent_description` | 307 | DEAD — seed.sql hardcodes the same string; nothing reads the setting |
| `tim_chat_id` | 797 | DEAD |
| `emerson_hook_url` / `emerson_hook_token` | 798-799 | DEAD |
| `subtask_bootstrap_timeout` / `subtask_work_timeout` | 494-495 | DEAD (documented "observability-only labels until PR-2"; PR-2 never wired them) |
| `schedule_continuation_default_prompt` | 904 | DEAD (documented reserved for F064.5-v2) |
| `date_aware_boost_enabled` / `_factor` / `_window_pad_days` | 1229-1244 | DEAD (documented F075 Layer 3 deferred; no consumer) |

Verified **alive** despite suspicion: `web_search_daily_limit` (web_tools.py:97), `cross_type_same_threshold` (graph_linker.py:286,293), `auto_link_threshold`/`auto_link_max` (brain.py:1597-1599), `heartbeat_drive_*` (main.py:645, checks.py:772), `critic_max_latency_ms`/`critic_passthrough_max_words` (critic.py), `procedure_staleness_days`/`procedure_weakness_threshold` (procedure_learner.py:479,487), `smart_compress_*` (smart_compress.py), `temporal_backfill_default_token_budget` (scripts/backfill_temporal_facts.py:344), `context_log_ring_size`/`max_total` (main.py:574-575), `graph_threshold_chunk_chunk_cross` (graph_densifier.py:1001 — the "reserved" CLAUDE.md note is stale; F070.1 consumes it).

Dead tables: **none** — every table created by init.sql/migrations has either an ORM model with consumers or a raw-SQL consumer (`context_log`/`behavior_snapshots` → observability + main.py; `config` → runtime_config.py; `query_expansions` → heart/query_expansion.py; `eval_runs` → nous_eval/run_history; `work_queue_items` → heart/work_queue.py; `graph_hub_snapshots` → brain/hub_snapshots.py).

Tables without `agent_id` (recurring-miss check): `nous_system.config` (ST-16, documented), `heart.query_expansions` (documented global), `nous_system.schema_migrations` (correct), plus pure child/join tables keyed by an agent-scoped parent (`decision_tags`, `decision_reasons`, `decision_bridge`, `episode_decisions`, `episode_procedures`, `dag_nodes`, `dag_edges`). No undocumented misses.

---

## Improvement Opportunities

1. **Kill the empty-string env class structurally** — `env_ignore_empty=True` on `Settings.model_config` resolves ST-1 and ST-4's mechanism in one line (verify no field intentionally distinguishes `""` from unset; `email_allowlist`'s "empty = reject all" semantics survive because unset also yields `""` default).
2. **Migrator hardening trio** (cheap, high leverage): filter `BEGIN`/`COMMIT` in the splitter (ST-2), advisory lock around the run (ST-7), duplicate-version-prefix assertion (ST-18). ~20 LOC total in migrator.py.
3. **CI lint for migrations**: reject `$$` bodies containing `;`, inline `--` comments, bare `ADD COLUMN`/`CREATE TABLE`/`CREATE INDEX` without `IF NOT EXISTS`, and `BEGIN`/`COMMIT`. The conventions already exist as comments in 056-059; make them mechanical.
4. **Startup dimension probe** (ST-11): one SELECT of `atttypmod` on `heart.facts.embedding` compared to `settings.embedding_dimensions` turns a confusing runtime failure into a clear boot error.
5. **Declare load-bearing unique indexes on the ORM** (ST-15) so SQLite tests enforce the same invariants the prod code relies on (chunk re-ingest idempotency, one-active-rubric, one-active-procedure-name).
6. **Single source of truth for env defaults**: the compose `environment:` block duplicating ~60 defaults is the root cause of ST-3/ST-4/ST-6. Replace with `env_file: .env` + a minimal override list; defaults live in config.py only.
7. **config.py is 1528 lines and growing ~linearly with features** — consider splitting into per-domain Settings mixins composed into one `Settings`; it would also make dead-field sweeps (this audit found 9) tractable per-domain.
