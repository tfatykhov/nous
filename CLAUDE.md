# CLAUDE.md - Nous Development Guide

## What is Nous?

Nous (Greek: mind/intellect) is a cognitive agent framework built on Minsky's Society of Mind principles. It gives AI agents persistent memory, decision intelligence, and the ability to learn from experience.

**Status: v0.1.0 shipped and deployed.** All core architecture is live.

## Architecture

```
Cognitive Layer (hooks into LLM calls)
    ├── Brain (decisions, deliberation, calibration, guardrails)
    ├── Heart (episodes, facts, procedures, censors, working memory)
    ├── Context Engine (token budgets, relevance scoring, intent-driven retrieval)
    └── Event Bus (async handlers for automation)

Runtime: Direct Anthropic API + tool dispatch loop
Storage: PostgreSQL + pgvector (one DB, three schemas: brain/heart/system)
API: REST (42 endpoints) + MCP server + Telegram bot (streaming)
```

## Project Structure

```
nous/
├── docker-compose.yml          # Nous agent + Postgres + pgvector
├── Dockerfile                  # Python container with OAT support
├── sql/
│   ├── init.sql                # Base schema (21 tables, 3 schemas)
│   ├── migrations/             # Schema migrations (006-016)
│   └── seed.sql                # Default agent, frames, guardrails
├── nous/                       # Python package (~30,000 lines)
│   ├── config.py               # Settings via pydantic-settings
│   ├── main.py                 # Entry point, component wiring, lifecycle
│   ├── telegram_bot.py         # Telegram interface (streaming + usage)
│   ├── events.py               # Event bus (async pub/sub)
│   ├── utils.py                # Shared utilities
│   ├── storage/                # Database layer (async SQLAlchemy)
│   │   ├── database.py         # Connection pool, session management
│   │   ├── models.py           # ORM models for all 28 tables
│   │   └── migrator.py         # Schema migration runner
│   ├── brain/                  # Decision intelligence organ
│   │   ├── brain.py            # Core: record, query, review, calibrate
│   │   ├── bridge.py           # Structure + function descriptions
│   │   ├── calibration.py      # Brier scores, confidence tracking
│   │   ├── embeddings.py       # pgvector embedding provider
│   │   ├── graph_linker.py     # Cross-type auto-linking (common-template embedding)
│   │   ├── guardrails.py       # CEL expression guardrails
│   │   ├── quality.py          # Decision quality scoring
│   │   ├── schemas.py          # Pydantic models
│   │   └── spreading_activation.py  # Density-gated multi-hop graph traversal
│   ├── heart/                  # Memory system organ
│   │   ├── heart.py            # Core: learn, recall, episode lifecycle
│   │   ├── episodes.py         # Episodic memory
│   │   ├── facts.py            # Semantic memory
│   │   ├── procedures.py       # Procedural memory
│   │   ├── censors.py          # Guardrail censors
│   │   ├── censor_actions.py   # F031: Censor action executor (read-only tools)
│   │   ├── working_memory.py   # Short-term scratch space
│   │   ├── search.py           # Full-text + vector search
│   │   ├── subtasks.py         # Subtask CRUD operations
│   │   ├── schedules.py        # Schedule CRUD operations
│   │   └── schemas.py          # Pydantic models
│   ├── cognitive/              # Cognitive layer (Nous Loop)
│   │   ├── layer.py            # pre_turn / post_turn / end_session
│   │   ├── frames.py           # Frame selection (task, question, decision, etc.)
│   │   ├── context.py          # Token-budgeted context assembly
│   │   ├── deliberation.py     # Pre-action protocol
│   │   ├── intent.py           # Intent classification for retrieval
│   │   ├── dedup.py            # Conversation deduplication
│   │   ├── monitor.py          # Post-turn self-assessment
│   │   ├── usage_tracker.py    # Context usage feedback loop
│   │   └── schemas.py          # TurnContext, TurnResult, etc.
│   ├── handlers/               # Event bus handlers
│   │   ├── episode_summarizer.py  # Episode summary generation
│   │   ├── fact_extractor.py      # Fact extraction from conversations
│   │   ├── knowledge_extractor.py # Pre-prune fact extraction
│   │   ├── decision_reviewer.py   # Automated decision review
│   │   ├── session_monitor.py     # Session timeout monitoring
│   │   ├── sleep_handler.py       # Sleep/reflection handler
│   │   ├── subtask_worker.py      # Async subtask execution
│   │   ├── task_scheduler.py      # Cron/one-shot scheduling
│   │   └── time_parser.py         # Natural language time parsing
│   ├── skills/                 # Skill discovery system (F011)
│   │   ├── parser.py           # SkillParser + SkillManifest
│   │   └── bootstrap.py        # One-time local skill registration
│   ├── heartbeat/              # Proactive monitoring (F034)
│   │   ├── runner.py           # HeartbeatRunner tick loop + triage
│   │   ├── registry.py         # CheckRegistry + BaseCheck ABC
│   │   ├── checks.py           # HealthCheck, SelfInitiatedCheck, EmailCheck
│   │   ├── dynamic.py          # DynamicCheck + DynamicCheckLoader (F034.5)
│   │   └── schemas.py          # Finding, CheckResult, HeartbeatResult
│   ├── identity/               # Agent identity system (F018)
│   │   ├── manager.py          # Identity section CRUD
│   │   ├── protocol.py         # Initiation protocol
│   │   └── tools.py            # Identity-related tools
│   └── api/                    # External interfaces
│       ├── rest.py             # Starlette REST API (52 endpoints)
│       ├── mcp.py              # MCP server (nous_chat, nous_decide, etc.)
│       ├── runner.py           # Agent runner (tool loop, streaming)
│       ├── tools.py            # Tool dispatcher + registration
│       ├── retrieval_pipeline.py # F051: run_recall_pipeline (shared by recall_deep + eval)
│       ├── builtin_tools.py    # bash, read_file, write_file
│       ├── web_tools.py        # web_search, web_fetch (multi-tier routing)
│       ├── search_providers.py # SearchProvider protocol + Tavily, Exa, Brave
│       ├── search_router.py   # Query classification + cascading fallback
│       ├── compaction.py       # History compaction engine
│       ├── smart_compress.py   # Smart compression for tool results
│       ├── tool_cache.py       # Tool result caching
│       └── models.py           # API request/response models
├── nous_eval/                  # Retrieval evaluation harness (F051) — dev-only sibling package
│   │                           #   NOT shipped in prod Dockerfile; `COPY nous/ nous/` skips this.
│   ├── config.py               # EvalSettings (pydantic-settings, NOUS_EVAL_* prefix)
│   ├── source_registry.py      # sources.yaml loader + per-source toggles
│   ├── corpus_loader.py        # Bulk JSONL → Postgres (ingest + test-DB seed)
│   ├── qrels_loader.py         # Qrel pydantic model + JSONL loader + reviewed_by gate
│   ├── retrieval_runner.py     # run_matrix: RuntimeConfig.reset + per-config Heart/Brain
│   ├── metrics.py              # MRR/P@K/R@K/nDCG (pure Python, no numpy)
│   ├── report.py               # Markdown + JSON + decide_gate_f050
│   ├── retrieval.py            # `python -m nous_eval.retrieval` CLI
│   ├── rebuild.py              # `python -m nous_eval.rebuild` (volume purge)
│   ├── ingest_entry.py         # `python -m nous_eval.ingest_entry` dispatcher
│   ├── tasks.py                # Cross-platform task runner (build-image, push, etc.)
│   ├── ingest.py               # Quarterly prod-DB fixture refresh
│   ├── ingest_longmemeval.py   # 20-Q stratified LongMemEval_S subset ingestion
│   ├── probe_gen.py            # Auto-generate probes from INDEX.md + git log
│   ├── hand_labels_draft.py    # AI-drafted hand-label qrels
│   ├── multi_turn_eval.py      # F051.4: walks LongMemEval haystacks via dispatcher; per-config metrics
│   ├── run_history.py          # F051 Phase 1 finish (#365/#366/#367): persists eval_runs to EVAL DB
│   └── regression.py           # `python -m nous_eval.regression` — compares latest run vs N-day-old baseline, exits non-zero on regression
├── tests/                      # 1750+ tests across 91 files
└── docs/
    ├── research/               # Theory & design notes (001-016)
    ├── features/               # High-level feature specs (F001-F030)
    ├── implementation/         # Build specs (001-014.1, all shipped)
    ├── plans/                  # Implementation plans
    └── reviews/                # Code review documents
```

## What's Shipped (v0.1.0)

| Spec | Component | PR |
|------|-----------|----|
| 001 | Postgres scaffold (24 base tables + 19 migrations, 3 schemas) | #1 |
| 002 | Brain module (decisions, deliberation, calibration, guardrails) | #2 |
| 003 | Heart module (episodes, facts, procedures, censors, working memory) | #3 |
| 003.1 | Heart enhancements (contradiction detection, domain compaction) | #6 |
| 003.2 | Frame-tagged memory encoding | — |
| 004 | Cognitive Layer (frames, recall, deliberation, monitoring) | #10 |
| 004.1 | CEL expression guardrails | #10 |
| 005 | Runtime (REST API, MCP server, agent runner) | — |
| 005.1 | Smart context preparation (intent-driven retrieval) | — |
| 005.2 | Direct Anthropic API rewrite (replaced Claude Agent SDK) | #15 |
| 005.3 | Web tools (web_search, web_fetch via Brave) | #16 |
| 005.4 | Streaming responses (SSE + Telegram progressive editing) | #23 |
| 005.5 | Noise reduction (frame instructions, decision filtering) | #20 |
| 006 | Event Bus (async handlers, DB persistence) | — |
| F010 | Memory improvements (episode summaries, fact extraction, user tagging) | #21 |
| 011.1 | Subtasks & Scheduling (F009) | #85 |
| 011.2 | Subtask Result Delivery (F009) | — |
| 014.1 | Context Quality Engine (F016+F017) | #122 |
| F012 | K-Line Procedure Learning (auto-create procedures from decision clusters, monitor reinforcement) | #134 |
| F011 | Skill Discovery v2 (learn_skill tool, SkillParser, bootstrap, auto-activation via RECALL) | — |
| F022 | Graph-Augmented Recall (polymorphic edges, cross-type linking, contradiction bridge, spreading activation) | — |
| F023 | Memory Admission Control (5-dimension scoring, shadow mode) | — |
| F024 | Critic Agent Phase 0 (smart frame selector, LLM classification, diagnostic critics) | — |
| F024-3b | Self-Modifying Rubrics (outcome signals, dimension proposals, rubric evolution) | #196 |
| F026 | Execution Integrity (execution ledger, action gating, claim verification, ghost planning detection) | #183 |
| F030 | MMR Diversity Reranking (Maximal Marginal Relevance in recall_deep) | #205 |
| F031 | Censor Middleware with Action Payloads (censors execute read-only tools, conditional unblock, update API) | #208 |
| F032 | Execution Ledger Dashboard (per-action visibility, status filtering, side-effect classification) | — |
| F033 | Multi-Tier Search Routing (Tavily primary, Exa research, Brave fallback, query classification) | — |
| F034 | Heartbeat Proactive Monitoring (tick loop, health/email/self-initiated checks, triage, Telegram) | #236 |
| F034.1 | Finding Lifecycle (fingerprint dedup, state machine, escalation, daily digest, outcome signals) | #241 |
| F034.2 | Intelligent Checks (embedding search, LLM email classification, drive significance, tunable params) | #241 |
| F034.3 | Self-Tuning Heartbeat (outcome-driven adjustment, cross-cycle rollback, pinned params) | #241 |
| F034.5 | Dynamic Heartbeat Checks (prompt-driven checks, conversational creation/management, full lifecycle) | #252 |
| F034.6 | on_complete Callback for Dynamic Checks (callback prompt on self-disable, 3-layer failure handling, background execution) | #275 |
| F036 | Prompt Cache Optimization (3-tier system prompt split, cache break detection, single breakpoint strategy, tool schema caching) | #253 |
| 012.3 | Programmatic Tool Calling (run_python with memory functions in scope) | — |
| F025 | Amnesia Prevention Phase 2+3 (staleness exemptions, budget scaling, transcript 16K, dedup 0.92, source text passthrough, chunked summarization, transcript persistence) | — |
| F038 | Memory Quality & Context Loading Fixes (quality gate 0.55, fact 30-char min, procedure floor 0.40, episode recency, user_direct bonus, task synthesis, context dedup, bash hints) | #258 |
| F038 | Unified DAG Orchestration (DAGStore, DAGOrchestrator, DAG tools, dashboard tab) | #289 |
| F040 | Graph Densification (orphan backfill, reverse linking, per-relation thresholds, density dashboard, cluster discovery) | — |
| F042 | Cross-Encoder Reranking (sigmoid-normalized, async, head-truncation, feature-flagged, optional sentence-transformers dep) | #312 |
| F043 | Cross-Encoder Reranking in Sleep-Cycle Graph Backfill (precision pre-filter before cosine gate, reuses F042 reranker, feature-flagged, _ce_stats telemetry) | #314 |
| F044 | tinyHippo-Lite — STC consolidation over `brain.graph_edges` (two-tier tagged→consolidated state machine + `ltp_count` PRP gate; reinforced from re-derivation hooks at all live similarity linkers + agent-scoped recall-touch buffer; Phase 8d homeostatic α-downscale; deterministic/structural edges exempt at every reinforcement touchpoint; ships **telemetry-only** with master flag, boost + downscale are opt-in active mechanisms default OFF and not yet prod-validated; migration `061`; co-designed with deployed Nous, 11 codex review rounds) | #531 |
| F045 | CE-Aware Cosine Thresholds + Content-Length Guard (relaxed per-relation thresholds when CE backfill is upstream, 80-char min to drop URL-only facts, empirically validated at 80% LLM-judged precision) | #315 |
| F046 | [DAG Node Timeout Configuration](docs/features/F046-dag-node-timeout-config.md) (env-var-driven DAG node timeouts — `NOUS_DAG_NODE_DEFAULT_TIMEOUT`=600s, `NOUS_DAG_NODE_MAX_TIMEOUT`=7200s; Settings DI on DAGStore+DAGOrchestrator; schema `timeout_seconds` → `int \| None`; defensive clamp at 3 read sites; unblocks long-running Claude Code / deep-research DAG nodes) | — |
| F047 | [Actionability Classification](docs/features/F047-actionability-classification.md) (learn-time classifier persists `actionable: bool` on `heart.facts`, replacing the `_OBSERVATION_PATTERNS` arms-race at heartbeat read time — 3 tiers: hard filter → positive-wins heuristic → Haiku LLM; backfill handler with PG advisory lock + supervision wrapper; heartbeat now consults persisted verdict with positive-wins fallback for NULL rows, fixing the PR #335 short-circuit bug; supersedes PR #335) | — |
| F048 | [Background Streaming + TCP Keep-Alive](docs/features/F048-background-streaming-keepalive.md) (subtask + heartbeat turns stream under the hood via `call_streaming_aggregated` on both Anthropic clients — keeps TCP socket warm with incremental SSE bytes so long background generations no longer hit idle-connection drops; `AgentRunner.run_turn(is_background=True)` threads through `_tool_loop` to every `_call_api` call; wired at 5 sites: subtask_worker, heartbeat cognitive_triage + on_complete callback, DynamicCheck._run_check, and inline `spawn_task(await_result)`; `httpx.AsyncHTTPTransport` gains `SO_KEEPALIVE` + Linux `TCP_KEEPIDLE` / macOS `TCP_KEEPALIVE` via `_build_socket_options` helper; truncated-stream detection raises rather than silently returning empty content; fixes pre-existing censor-block 2-tuple return bug at runner.py:249; gated by `NOUS_API_BACKGROUND_STREAMING_ENABLED=true` + `NOUS_API_SOCKET_KEEPALIVE_ENABLED=true`) | — |
| F049 | [Session & Memory Lifecycle Hygiene](docs/features/F049-session-lifecycle-hygiene.md) (closes #187 + scoped #166 — `_execute_subtask` wraps body in `try/finally` calling `end_conversation` under `asyncio.shield(asyncio.wait_for(..., 30))` with three distinct except branches (TimeoutError/CancelledError/Exception) at ERROR severity; `WorkingMemoryManager.cleanup_stale()` sweeps stale `heart.working_memory` rows via `ctid IN (SELECT … LIMIT N)` batched DELETE under `pg_try_advisory_xact_lock` keyed on a SHA-256 hash of `agent_id` for cross-process-stable replica serialization; session monitor grows `heart: Heart | None` kwarg and invokes the sweep at most once per `NOUS_WORKING_MEMORY_SWEEP_INTERVAL_SECONDS`; 13 new tests, empirically targets 86/87 stale rows observed in 2026-04-20 audit) | — |
| F051 | [Retrieval Evaluation Harness](docs/features/F051-retrieval-eval-harness.md) (local-first retrieval eval + per-source qrels + paired A/B — new `nous/api/retrieval_pipeline.py::run_recall_pipeline` extracted from `tools.py::recall_deep` to expose structured results alongside unchanged LLM-facing text; new `nous_eval/` module tree (config, source_registry, corpus_loader, qrels_loader, retrieval_runner, metrics, report, CLI entries); persistent `nous-eval-db` Docker image under `docker compose --profile eval` bound to `127.0.0.1:5433`; new `sql/migrations/037_eval_runs.sql` for run history; `_verify_fixture_version` + `_verify_corpus_agent_id` preflight probes; `RuntimeConfig.reset()` between configs; 14-flag disable list for background handlers; `.gitattributes` enforces LF on `.sh`; `F050` gate logic in `decide_gate_f050` requires aggregate MRR +7%, no single-source regression >3%, and majority-positive sources; 69 new tests + byte-identical recall_deep snapshot; 4-agent implementation team with 2-cycle review) | — |
| F067 | Episode Chunks + Parent-Episode Recall (gbrain-inspired: chunked raw transcripts stored in new `heart.episode_chunks` table alongside lossy fact extraction — preserves verbatim tokens that the fact extractor discards; `EpisodeSummarizer.summarize_episode` chunks via `nous/heart/chunking.py::chunk_text` (600ch sliding window, 80ch overlap) and batches embeddings into the new table when `NOUS_EPISODE_CHUNKS_ENABLED=true`; `run_recall_pipeline` adds Stage 1.5 chunk-vector-search leg that surfaces chunks with `PipelineResult.type="chunk"` co-displayed in the Heart Memory section; Phase 2 `_fetch_parent_episodes_for_facts` appends up to 2 parent episode summaries (deduped, 500ch truncation) as a new `=== Parent Episode Context ===` section gated on `NOUS_RECALL_INCLUDE_PARENT_EPISODES=true`; both feature flags default **OFF** — validated on LongMemEval per-question isolation methodology (+13pp QA acc for chunks, +6pp for parent episodes) but **NOT validated on shared-corpus prod-shape retrieval** where the wins may not generalize; ON conflict (episode_id, chunk_index) makes re-ingest idempotent; cascade delete with parent episode; 17 unit tests) | — |
| F024 | Inbound Multimodal Attachments — Telegram + REST accept images/PDFs/text files → Claude content blocks; originals saved under workspace_dir/attachments with a Heart fact memory-reference; text-file bodies chunk-ingested to episode_chunks; base64 stripped from history + never persisted to DB; block-aware token estimation; gated by NOUS_ATTACHMENTS_ENABLED (default ON; requires a vision-capable model); PDF bodies chunk-ingested via pypdf+Claude-transcription fallback (F024.1) | — |
| F084 | [Write-Path Adjudication](docs/features/F084-write-path-adjudication.md) (R1 modal enumerative extraction — density heuristic routes dense-document transcripts through raw-chunked structured extraction INSTEAD of lossy summary leg, producing atomic `(subject_key, attribute_key, source_ordinal)` keyed facts with admission bypass + source-aware 15-char floor; R2 store-time key-conflict supersession — exact-key candidate lookup at `_learn` with F075-precedence + F027 classifier confirm + ordinal/recency policy winner, shared `apply_supersession` primitive, sleep-phase sweep; R2.4 parametric-override trust marker; migration 064 adds 4 nullable columns + partial index; 13 flags all default OFF; backfill scripts R1.4 + R2.5 with rollback SQL; acceptance measured in external MAB harness on backfilled clone) | — |
| F085 | [Keyed Fact Selection](docs/features/F085-keyed-fact-selection.md) (makes F084's enumerative facts selectable by exact entity key — measured −5.0pp selection failure / keyed-sim 0.20–0.23 despite existence being fixed; R3.1 bidirectional entity indexing via new `heart.fact_entity_keys` join table (migration 065) indexing subject AND proper-noun object/value entities, emitted in the SAME extraction LLM call as F084's R1 schema, universal stop-policy (no subject exemption); R3.2 single canonical `normalize_key` v2 in `nous/heart/keys.py` (NFC, article-strip, fixpoint-iterated idempotency) shared by write path, `Heart.learn`, and the three-phase `scripts/backfill_r3_entity_keys.py` backfill (normalize → seed → extract, watermark/rollback/resume); R3.3 land-dark keyed retrieval leg in `run_recall_pipeline` gated on flag + entity-presence (not frame — the MAB eval harness has no frame concept), additive-only score-banded merge with stable sorted-position insertion (not tail-append, so the leg is visible under `rerank_by_score=False`); 5 flags all default OFF; acceptance measured in external MAB harness) | — |
| F086 | [ICL Exemplar Mode](docs/features/F086-icl-exemplar-mode.md) (targets the MAB program's sole decidable ICL loss, live 0.555 vs leader 0.840 — zero-LLM embedding-kNN gathering over exemplar granularity sims at maj@5 0.82; write path: new `is_exemplar_stream` density predicate, distinct from F084's `is_enumerable` which does not fire on `utterance\nlabel: N` streams, routes modally in `FactExtractor.extract_and_store`, parse-only, storing each pair as its own embedded `heart.facts` row (`source='exemplar_extractor'`, content = full pair text for gate-1 sim parity, `subject_key=NULL`, label-aware dedup guard so different-label near-duplicates are never dropped); read path: land-dark Stage 1.7 leg in `run_recall_pipeline` — classification-shaped trigger heuristic (memory-referential interrogatives excluded, not questions generally), source-filtered cosine fetch (migration 066, index-only), similarity floor + score-banded stable insertion; `scripts/backfill_exemplar_facts.py` reads `heart.episode_chunks` not `episodes.transcript` (validated 8000-char capture cap), per-chunk independent parse with cross-chunk ordinal continuation, watermark/rollback; 2 boolean flags default OFF + 7 numeric params; acceptance (4 gates) measured in external MAB harness) | — |
| F087 | [DAG Durability Spine](docs/superpowers/specs/2026-08-08-dag-durability-spine-design.md) (closes the four edge gaps that made long-running DAGs unsafe — the state machine core is untouched. **Durable delivery:** `DAGOrchestrator._bus` was assigned and never read and `grep emit\|publish` over `nous/dag/` returned nothing, so a terminal DAG wrote `result_summary` and stopped; reaching terminal and being delivered are now separate transitions on `execution_dags` (`delivered_at` / `delivery_attempts` / `delivery_error`, migration 069 + partial index) drained by a `tick()` sweep, making delivery at-least-once across crash and restart — the bus cannot carry this because `EventBus.emit` drops on `QueueFull` by design. **Three legs** in new `nous/dag/delivery.py`, independently flagged and independently guarded: bus emit (best-effort), agent-authored summary (opt-in, bounded, falls back to a deterministic template), Telegram push (required iff both token and chat id are configured). **Wall-clock reaper** (default ON, 300 s grace): nothing checked elapsed time on `status='running'` nodes, so a subtask orphaned by a crash wedged its DAG `running` forever and permanently consumed one of `MAX_ACTIVE_DAGS=5`. **Token accounting:** `DAGStore.update_dag_tokens` had no production caller, so `tokens_consumed` was structurally 0 and the budget branch unreachable; now wired from `Subtask.tokens_in+tokens_out` with a `dag_nodes.tokens_counted` idempotency guard — accounting live, enforcement dark. **Fail-loud wiring:** `register_dag_tools` ran unconditionally while the tick was wired only `if heartbeat_runner is not None`, so with the heartbeat off the agent silently created DAGs that never advanced; `dag_create` now refuses via `clock_wired`. 10 flags, 33 new tests, DAG suite 208 passing after making its `Settings` hermetic to developer `.env`) | — |
| F091 | [Retrieval Telemetry](docs/superpowers/specs/2026-08-19-f091-retrieval-telemetry-design.md) (what memory recall retrieved, and **which gate dropped everything it didn't** — the organizing principle is drop attribution, since listing survivors tells an operator nothing the rendered prompt doesn't already show. Before this, all retrieval telemetry was one `logger.info` at `tools.py:1079` reporting 8 scalars from a `PipelineStats` carrying 20+ fields, with the rest computed and discarded; nothing was persisted. Covers **both** retrieval paths — `run_recall_pipeline` AND `ContextEngine.build`, which runs every turn, fills the system prompt, and had only a bullet-count regex over rendered prose. Every candidate ends with one of 10 terminal dispositions plus the assigning stage; `unaccounted` is deliberately distinct so a filter added later that forgets to report becomes a test failure rather than a plausible-looking number. A write-only `RetrievalTrace` collector keeps `PipelineResult` frozen, so the byte-identical `recall_deep` snapshot and the `nous_eval` contract hold by construction; `NullTrace` is a no-op object rather than `is not None` guards at ~30 sites. Graph expansion is **pure capture, zero extra queries** — `NeighborResult` already carries `seed_score`/`edge_weight`/`edge_relation`/`extraction_method` — recorded at Stage 2, Stage 2b, Stage 4 one-hop, and spreading activation (`hop=2`, `seed_type="multi"`, since a multi-hop CTE has no single seed to attribute). Storage is one row per retrieval in `nous_system.retrieval_log` (migration 070), header columns for queryables + JSONB for detail, `trace_id` joining to `context_log`, written through the `ContextLogger` fire-and-forget pattern **including its `_pending_tasks` strong-ref set** (asyncio holds only weak refs to tasks — that bug was already found and fixed once at `context_logger.py:345`). New `retrieval` dashboard route: window rollup → per-retrieval list → candidates grouped by disposition + seed→edge→neighbor expansion tree. `finalize()` is authoritative about what reached the model, so an F083-pinned fact rescued past `diversity` reads `rendered` with the overridden gate preserved on `restored_from` — both the drop and the rescue are true. 37 unit tests + 3 live probes under `scripts/diag/`) | — |
| F090.1/.3/.4 | Callback execution + finished-DAG visibility + Phase-2 gate signals (callback nodes were a no-op stub that copied their instruction text into `result`, completing instantly with none having executed anything; they now execute as real subtasks behind `NOUS_DAG_CALLBACK_EXECUTION_ENABLED` — default OFF because this is a behavior change to an existing node type, so it lands dark and is flipped deliberately rather than on deploy (no measurement of real callback-node usage exists) — gaining every F087 backstop via `subtask_id` (still no `tools` — that stays exclusive to `check` nodes), with the seven `node_type == "subtask"` gates collapsed into one `_SUBTASK_BACKED` constant so the set cannot drift. `dag_manage action=recent` makes finished DAGs discoverable — previously reachable only by a prefix you already knew. `phase2_signals` on `/dashboard/dag` reports `sibling_overlap_rate` and callback/gate execution counts as the go/no-go evidence for Phase 2 — `sibling_overlap_rate` is a verbatim-overlap FLOOR (6-word shingle Jaccard), not a duplication measurement, so a low reading is NOT by itself grounds to cancel Phase 2. **Gate nodes still auto-pass** — F038 Phase 2 Critic integration remains unbuilt) | — |

## How to Work

### Read Before Building

1. Check `docs/implementation/` for build specs
2. Reference `docs/research/` for design rationale
3. Reference `docs/features/` for high-level feature context
4. Check `docs/features/INDEX.md` for current status of everything

### Tech Stack

- **Python 3.12+** (3.14 in container)
- **PostgreSQL 17** with pgvector extension
- **SQLAlchemy 2.0+** (async, declarative ORM)
- **asyncpg** (async Postgres driver)
- **pydantic v2** + pydantic-settings for config
- **Starlette** for REST API
- **httpx** for HTTP clients (Anthropic API, Telegram, etc.)
- **pytest** + pytest-asyncio for tests
- **uv** for dependency management

### Key Principles

- **Brain and Heart are in-process Python modules** — no MCP, no HTTP between them. Direct function calls, shared connection pool.
- **MCP is only the external interface** — for other agents/tools to talk to Nous.
- **Same ideas as Cognition Engines, not same code** — CE proved the concepts, Nous reimplements natively.
- **Direct Anthropic API** — no SDK wrapper. httpx calls with internal tool dispatch loop.
- **Async everywhere** — all database operations use async/await.
- **pgvector for all embeddings** — unified semantic search, no separate vector DB.
- **HNSW indexes over ivfflat** — works on empty tables, better recall.
- **OAT token support** — Max subscription tokens use Bearer auth + beta headers.

### Database

- Three schemas: `brain`, `heart`, `nous_system` (28 tables total)
- All tables are agent-scoped (`agent_id` column) for multi-agent readiness
- Use `vector(1536)` for embeddings (text-embedding-3-small)
- Full-text search via `tsvector` + GIN indexes
- JSONB for flexible fields (config, conditions, items)
- Soft deletes (`active` boolean), never hard delete memory

### Code Style

- Type hints on everything
- Docstrings on public functions
- Use `mapped_column()` for SQLAlchemy models
- Use `pydantic.BaseModel` for API schemas
- Async context managers for database sessions
- Tests use real Postgres (via docker-compose), not mocks

### Running

```bash
# Full stack (Nous + Postgres)
docker compose up -d

# Just Postgres (for local dev)
docker compose up -d postgres

# Install dependencies
uv sync

# Run tests
uv run pytest tests/ -v

# Start Nous locally
uv run python -m nous.main
```

### Environment Variables

DB connection vars are **unprefixed** (shared with docker-compose). All others use `NOUS_` prefix:

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_HOST` | `localhost` | Database host |
| `DB_PORT` | `5432` | Database port |
| `DB_USER` | `nous` | Database user |
| `DB_PASSWORD` | `nous_dev_password` | Database password |
| `DB_NAME` | `nous` | Database name |
| `ANTHROPIC_API_KEY` | — | Anthropic API key |
| `ANTHROPIC_AUTH_TOKEN` | — | OAT token (Max subscription, uses Bearer auth) |
| `NOUS_IDENTITY_PROMPT` | Built-in default | **Agent identity.** First section of every system prompt. How Nous knows who it is, what tools it has, and how to behave. Override to customize. |
| `NOUS_AGENT_ID` | `nous-default` | Agent identifier |
| `NOUS_AGENT_NAME` | `Nous` | Agent display name |
| `NOUS_MODEL` | `claude-sonnet-4-6` | LLM model for chat |
| `NOUS_MAX_TURNS` | `10` | Max tool loop iterations |
| `NOUS_MCP_ENABLED` | `true` | Enable MCP server |
| `NOUS_LOG_LEVEL` | `info` | Log level |
| `BRAVE_SEARCH_API_KEY` | — | Brave Search API key (tertiary fallback) |
| `TAVILY_API_KEY` | — | Tavily Search API key (primary search provider) |
| `EXA_API_KEY` | — | Exa Search API key (deep research queries) |
| `NOUS_SEARCH_PROVIDER` | `auto` | Search routing: auto, tavily, exa, brave |
| `OPENAI_API_KEY` | — | For embeddings (text-embedding-3-small) |
| `NOUS_EVENT_BUS_ENABLED` | `true` | Enable async event bus |
| `NOUS_EPISODE_SUMMARY_ENABLED` | `true` | Enable episode summarization handler |
| `NOUS_FACT_EXTRACTION_ENABLED` | `true` | Enable fact extraction handler |
| `NOUS_SLEEP_ENABLED` | `true` | Enable sleep/reflection handler |
| `NOUS_BACKGROUND_MODEL` | `claude-sonnet-4-6` | Model for background LLM tasks |
| `NOUS_SESSION_TIMEOUT` | `1800` | Session idle timeout in seconds |
| `NOUS_SLEEP_TIMEOUT` | `7200` | Sleep mode timeout in seconds |
| `NOUS_SLEEP_CHECK_INTERVAL` | `60` | Sleep check interval in seconds |
| `NOUS_SUBTASK_ENABLED` | `true` | Enable subtask worker pool |
| `NOUS_SUBTASK_WORKERS` | `2` | Number of async worker tasks |
| `NOUS_SUBTASK_POLL_INTERVAL` | `2.0` | Seconds between queue polls |
| `NOUS_SUBTASK_DEFAULT_TIMEOUT` | `600` | Default subtask timeout (seconds). Bumped from 120 in F048 so the outer `asyncio.wait_for` does not cancel before the new 600 s per-chunk streaming read completes. |
| `NOUS_SUBTASK_MAX_TIMEOUT` | `3600` | Maximum allowed subtask timeout (seconds). Bumped from 900 in F048 to pair with `NOUS_API_BACKGROUND_TIMEOUT_READ`. |
| `NOUS_API_BACKGROUND_STREAMING_ENABLED` | `true` | F048 master switch — route `is_background=True` turns (subtask + heartbeat) through `call_streaming_aggregated` instead of `call()`. |
| `NOUS_API_BACKGROUND_TIMEOUT_READ` | `600` | F048 per-chunk read timeout (seconds) applied only to background streamed requests. |
| `NOUS_API_SOCKET_KEEPALIVE_ENABLED` | `true` | F048 master switch for TCP keep-alive on the httpx transport (both Anthropic client backends). |
| `NOUS_API_SOCKET_KEEPALIVE_IDLE` | `30` | F048 seconds of idle before the first TCP keep-alive probe (Linux `TCP_KEEPIDLE` / macOS `TCP_KEEPALIVE`; ignored on Windows). |
| `NOUS_API_SOCKET_KEEPALIVE_INTERVAL` | `10` | F048 seconds between TCP keep-alive probes (Linux `TCP_KEEPINTVL`; ignored otherwise). |
| `NOUS_API_SOCKET_KEEPALIVE_COUNT` | `3` | F048 number of failed keep-alive probes before dropping the connection (Linux `TCP_KEEPCNT`; ignored otherwise). |
| `NOUS_DAG_NODE_DEFAULT_TIMEOUT` | `600` | Default timeout (s) for DAG nodes when node spec omits `timeout_seconds` (F046) |
| `NOUS_DAG_NODE_MAX_TIMEOUT` | `7200` | Hard ceiling (s) for DAG node `timeout_seconds` — clamped at insert and at read sites (F046) |
| `NOUS_DAG_RESULT_DELIVERY_ENABLED` | `true` | F087 master switch for the delivery sweep. Before F087 a terminal DAG wrote `result_summary` to a row and stopped — `DAGOrchestrator._bus` was assigned and never read, so a multi-hour DAG completed in total silence. Reaching terminal and being delivered are now **separate transitions**: `_check_dag_completion` marks terminal, then a `tick()` sweep drains `status IN (completed,failed,partial,cancelled) AND delivered_at IS NULL`. Because the queue is a table, a process that dies between the two writes re-delivers on the next tick — at-least-once, surviving restart. The event bus **cannot** provide this (`EventBus.emit` drops on `QueueFull` by design), which is why durability lives on `execution_dags`. |
| `NOUS_DAG_DELIVERY_BUS_ENABLED` | `true` | F087 leg: emit `dag.completed` / `dag.failed` with per-node outcomes + token totals. **Best-effort** — a down bus never blocks the Telegram push, and never prevents `delivered_at` being set (retrying a notification the user already received is worse than dropping an event). |
| `NOUS_DAG_DELIVERY_TELEGRAM_ENABLED` | `true` | F087 leg: push the summary to Telegram. **Required iff `NOUS_TELEGRAM_BOT_TOKEN` AND `NOUS_TELEGRAM_CHAT_ID` are both set** — this is the leg the user actually sees, so a transient HTTP failure brings the sweep back. When Telegram is unconfigured the leg is non-required, otherwise every DAG would burn all its attempts against a channel that does not exist. |
| `NOUS_DAG_DELIVERY_AGENT_SUMMARY_ENABLED` | `false` | F087 leg: a background turn reads the finished DAG and writes prose, which then becomes the Telegram body. Costs one LLM turn per finished DAG, hence opt-in. Bounded and best-effort — on timeout or error the deterministic template ships instead. Runs as a normal cognitive turn, so its episode lands in Heart on its own; that is how a finished DAG reaches the next conversation's context without a separate memory-write leg. An authored summary is cached to `execution_dags.delivery_summary` **before** the outcome is recorded, so a retry of a failed required channel reuses it — otherwise one transient Telegram outage would buy up to `NOUS_DAG_DELIVERY_MAX_ATTEMPTS` LLM turns and write a duplicate episode each time. The deterministic template is cheap and is never cached. |
| `NOUS_DAG_DELIVERY_AGENT_SUMMARY_TIMEOUT_SECONDS` | `120` | F087 bound on the summary turn before falling back to the template. |
| `NOUS_DAG_DELIVERY_MAX_ATTEMPTS` | `5` | F087 delivery attempts before giving up. On exhaustion the DAG is marked delivered **with `delivery_error` set** — it stops looping but the failure stays visible on the row instead of vanishing. |
| `NOUS_DAG_DELIVERY_BATCH_SIZE` | `5` | F087 max terminal-but-undelivered DAGs drained per tick (oldest `completed_at` first). |
| `NOUS_DAG_CHECK_RECONCILIATION_BATCH_SIZE` | `20` | Max terminal check-type DAG nodes (`completed`/`failed`/`cancelled`/`skipped`) with a still-`enabled` heartbeat check re-swept per tick for a repeat `manage_check(disable)`. `_cancel_heartbeat_check`, `_cancel_node`, and F066.1's `skip_and_continue` can all leave a check registered — the first two swallow a failed disable call with no retry, and `skip_and_continue` never attempts one at all. Once the node is terminal, `_poll_awaiting_checks` never looks at it again, so without this sweep a leaked `urgent` check (exempt from quiet hours) burns LLM turns indefinitely on a DAG that has already finished. Unlike the delivery sweep above, no `delivered_at`-style attempt/error bookkeeping: `manage_check(disable)` is an idempotent DB write with no externally-visible side effect, so the `enabled == True` join predicate alone makes a node drop out of every future sweep the instant its disable actually lands — this setting bounds per-tick query cost only, mirroring `NOUS_DAG_DELIVERY_BATCH_SIZE`. |
| `NOUS_DAG_NODE_REAPER_ENABLED` | `true` | F087 wall-clock backstop on `status='running'` nodes. **Defaults ON** — a backstop that ships dark is not a backstop, and it only fires in an already-broken state. Before F087, `_effective_timeout` was consumed only at launch (handed to the subtask) and in `_poll_awaiting_checks`; nothing checked elapsed time for a running node. Since `reclaim_stale` runs once at worker start and only touches rows already past timeout, a subtask orphaned by a crash left its node `running` forever → its DAG `running` forever → one of `MAX_ACTIVE_DAGS=5` consumed forever. Five of those and `dag_create` is permanently bricked. Tears down the primitive **before** marking failed (F064.1 ordering) so a live subtask stops burning tokens. Kill switch only. |
| `NOUS_DAG_NODE_TIMEOUT_GRACE_SECONDS` | `300` | F087 grace past a node's effective timeout before the reaper fires. Exists so the reaper never preempts the subtask executor's own richer error — the primitive gets `timeout` seconds to fail the node itself, and only if it is *still* running `grace` seconds later do we conclude nobody is coming. |
| `NOUS_DAG_CALLBACK_EXECUTION_ENABLED` | `false` | F090.1 — execute callback nodes instead of instantly completing them with their own instruction text. A callback routes through `_launch_subtask_node` (`orchestrator.py:2108`) with predecessor results injected by `_build_predecessor_context`, so it accepts the same `frame_type` / `model` / `timeout_seconds` as a subtask and inherits every F087 backstop by carrying a `subtask_id` — but not `tools`: `DAGNodeSpec.tools` is forwarded only inside `_launch_check_node` (`orchestrator.py:2210`), and `SubtaskManager.create` has no `tools` parameter at all. **Default OFF** because this is a behavior change to an existing node type (callbacks previously completed instantly, executing nothing) — it lands dark and is flipped deliberately rather than on deploy; no measurement of real callback-node usage exists to size the deploy-time impact. Read `phase2_signals.callback_executed` on `/dashboard/dag` after flipping. |
| `NOUS_DAG_TOKEN_BUDGET_ENFORCEMENT_ENABLED` | `false` | F087. `DAGStore.update_dag_tokens` had **no production caller**, so `tokens_consumed` was structurally always `0` and the `ratio >= 1.0` branch in `_advance_dag` had never once executed. F087 wires the accounting (`Subtask.tokens_in + tokens_out` rolled up on the running→terminal edge, guarded by `dag_nodes.tokens_counted` because `_sync_subtask_node` is re-entrant). Accounting is **live**; enforcement is **dark**, because flipping both at once would start cancelling DAGs for anyone who had set `token_budget` casually. The over-budget WARN logs either way, so operators can size budgets before enabling the cancel. |
| `NOUS_DAG_STALL_DETECTION_ENABLED` | `false` | F064.1 master switch. When false, the orchestrator never reads `last_activity_at` and stall detection is a no-op. Opt-in. **Operator note (2026-08-08):** the sibling variable is `NOUS_DAG_NODE_DEFAULT_STALL_TIMEOUT` — a `.env` carrying `NOUS_DEFAULT_STALL_TIMEOUT` is silently ignored by pydantic-settings, leaving the default 600 s in force. With `NOUS_DAG_NODE_DEFAULT_TIMEOUT` raised for long-running work, that mismatch kills nodes at 600 s of inactivity rather than the intended value. |
| `NOUS_DAG_NODE_DEFAULT_STALL_TIMEOUT` | `600` | F064.1 default seconds without `last_activity_at` activity before a running node is marked failed with `error="stalled: no activity for {N}s"`. `0` disables per-node. |
| `NOUS_DAG_NODE_MAX_STALL_TIMEOUT` | `3600` | F064.1 ceiling clamp on `DAGNodeSpec.stall_timeout_seconds`. Must be `<= NOUS_DAG_NODE_MAX_TIMEOUT` (cross-validated when stall detection is enabled). |
| `NOUS_DAG_FRAME_CONCURRENCY_ENABLED` | `false` | F064.2 master switch for per-frame-type dispatch caps. When false, all ready nodes in a wave launch in the same tick (today's behavior). |
| `NOUS_DAG_GLOBAL_MAX_CONCURRENT_BY_FRAME` | `{}` | F064.2 operator-level JSON dict (e.g. `{"debug": 1, "research": 3}`). Overrides per-DAG values when set. Values `< 1` fail Settings init. Missing frames are uncapped. |
| `NOUS_DAG_WORKSPACE_SAFETY_ENABLED` | `false` | F064.3 master switch for **insert-time** sanitization. **Read-time containment-assert** runs unconditionally regardless of this flag (security boundary, not a feature). |
| `NOUS_DAG_WORKSPACE_ROOT` | `$TMPDIR/nous-workspace/dag-status` | F064.3 resolved-absolute root every workspace path must be inside. Default computed via `tempfile.gettempdir()` — POSIX `/tmp/...`, Windows `%TEMP%\...`. |
| `NOUS_SKILL_RUNTIME_METADATA_ENABLED` | `false` | F064.4 **consumer-side** flag only. SkillManifest fields (`concurrency_cap`, `timeout_override_seconds`, `hooks`, `requires_human_review`) are always parsed and always persisted on `procedures.runtime_metadata` regardless of this flag. The flag gates only the deferred-to-v2 orchestrator enforcement. |
| `NOUS_SCHEDULE_CONTINUATION_ENABLED` | `false` | F064.5 master switch for scheduled-task Episode reuse. When false, every fire creates a fresh session_id (today's behavior). v1 ships Episode reuse only — no LLM thread continuity. |
| `NOUS_SCHEDULE_MAX_CONTINUATION_TURNS` | `50` | F064.5 hard ceiling on `Schedule.continuation_turns`. Prevents unbounded Episode growth. |
| `NOUS_SCHEDULE_CONTINUATION_DEFAULT_PROMPT` | `"Continue. The previous run completed at {last_fired_at}. Apply the same task to fresh context."` | F064.5 default continuation prompt. **Reserved for F064.5-v2** (LLM thread continuity); not consumed by v1. |
| `NOUS_WORK_QUEUE_ENABLED` | `false` | F064.6 master switch. When true, a `WorkQueueCheck` polls the configured adapter every `interval_seconds` and emits a DAG per new item. |
| `NOUS_WORK_QUEUE_SOURCE` | `file_jsonl` | F064.6 adapter name. v1 ships `file_jsonl`; `github_issues` and `linear` raise `NotImplementedError` until F064.6-v2. |
| `NOUS_WORK_QUEUE_INTERVAL_SECONDS` | `300` | F064.6 polling cadence (`ge=30` to avoid hammering the queue/DB). |
| `NOUS_WORK_QUEUE_FILE_JSONL_PATH` | `""` | F064.6 adapter-specific config — path to JSONL file when `source=file_jsonl`. |
| `NOUS_WORK_QUEUE_MAX_DAGS_PER_TICK` | `5` | F064.6 per-tick admission cap. Bounded at 5 to match `MAX_ACTIVE_DAGS`; excess items wait for the next tick. |
| `NOUS_ACTIONABILITY_ENABLED` | `true` | Enable F047 actionability classification at fact learn time |
| `NOUS_ACTIONABILITY_LLM_ENABLED` | `true` | Use Haiku LLM for ambiguous actionability cases (tier 2) |
| `NOUS_ACTIONABILITY_MODEL` | `claude-haiku-4-5-20251001` | LLM model for F047 actionability tier-2 classification |
| `NOUS_ACTIONABILITY_DEFAULT` | `false` | Fallback verdict when classifier can't decide (false = fail-closed, don't page user) |
| `NOUS_ACTIONABILITY_BACKFILL_ON_STARTUP` | `true` | Run F047 backfill automatically on startup for NULL rows |
| `NOUS_ACTIONABILITY_BACKFILL_TOKEN_BUDGET` | `10000` | Rough Haiku daily token cap for backfill |
| `NOUS_SUBTASK_CLEANUP_TIMEOUT_SECONDS` | `30` | F049 — max seconds to wait for `end_conversation` in the subtask `finally` before logging ERROR. Bounds a hung reflection `_call_api` so it cannot block a worker forever. |
| `NOUS_WORKING_MEMORY_TTL_HOURS` | `24` | F049 — delete `heart.working_memory` rows older than this (0 disables the sweep entirely). Safety net for session paths that bypass `end_conversation`. |
| `NOUS_WORKING_MEMORY_SWEEP_INTERVAL_SECONDS` | `3600` | F049 — minimum seconds between WM TTL safety-net sweeps. |
| `NOUS_WORKING_MEMORY_SWEEP_BATCH_SIZE` | `5000` | F049 — rows per DELETE batch during WM sweep; avoids long exclusive locks at scale. |
| `NOUS_RETRIEVAL_TELEMETRY_ENABLED` | `true` | F091 master switch for retrieval telemetry. Records what recall retrieved AND which gate dropped everything it didn't, across **both** retrieval paths — `run_recall_pipeline` (recall_deep) and `ContextEngine.build` (every turn; previously instrumented only by a bullet-count regex over rendered prose at `context_logger.py:165`). Ships **ON**: header rows, per-leg summaries and graph-expansion edges are cheap, and a telemetry system that lands dark measures nothing. The collector is **write-only** — the pipeline writes in and nothing reads back out — so with the flag off there is no branch consuming trace state and results are byte-identical by construction (`PipelineResult` stays frozen; the `recall_deep` snapshot and `nous_eval` contract are untouched). Verified by `scripts/diag/f091_pipeline_probe.py` and `f091_context_probe.py`, which assert results/system-prompt identity traced vs untraced. |
| `NOUS_RETRIEVAL_TELEMETRY_CANDIDATE_SAMPLE_RATE` | `0.1` | F091 fraction of retrievals that capture the full per-candidate array — the only expensive part. Cost and visibility are **separate knobs**: at any sample rate the header, legs, `excluded_types` and expansions are still recorded, so "which legs fired" and "how did graph expansion work" are always answerable. An unsampled row stores `candidates = NULL` (never `[]`), so the dashboard says "not captured" instead of implying the retrieval found nothing. Raise only after measuring per-turn cost on Path B. |
| `NOUS_RETRIEVAL_TELEMETRY_SNIPPET_CHARS` | `200` | F091 per-candidate content truncation. Full fact bodies are never stored — that is both unbounded growth and a needless copy of user content into a diagnostic table. `0` stores no snippet at all. |
| `NOUS_RETRIEVAL_TELEMETRY_QUERY_CHARS` | `500` | F091 truncation of the stored query, applied at trace construction. On the `context` path this is the RAW USER MESSAGE, so an untruncated copy would put verbatim user text in a diagnostics table for the whole retention window — a class of content `context_log` never stores (it holds counts and ids). The query is a label for finding the retrieval again, not evidence. `0` means **zero chars**, matching `_SNIPPET_CHARS` — it does NOT mean unlimited (an earlier revision treated it that way, so the privacy-tightening setting produced maximum exposure); negatives clamp to 0. |
| `NOUS_RETRIEVAL_TELEMETRY_MAX_CANDIDATES` | `300` | F091 hard per-row candidate cap; hitting it sets `truncated` and logs WARNING (never silent). |
| `NOUS_RETRIEVAL_TELEMETRY_RING_SIZE` | `20` | F091 in-memory ring of recent traces. Live-read fallback only — **both dashboard endpoints read Postgres, so this ring has no consumer today.** Deliberately small: at `candidate_sample_rate=1.0` a 100-deep ring pins 60–100 MB RSS holding candidate arrays nobody reads. Raise only alongside a reader. |
| `NOUS_RETRIEVAL_TELEMETRY_RETENTION_DAYS` | `14` | F091 daily sweep threshold on `nous_system.retrieval_log` (migration 070). `0` disables the sweep. |
| `NOUS_EVAL_DB_HOST` | `localhost` | F051 — eval DB host (separate container on port 5433) |
| `NOUS_EVAL_DB_PORT` | `5433` | F051 — eval DB port (127.0.0.1 bind; separate from main :5432) |
| `NOUS_EVAL_DB_USER` | `nous` | F051 — eval DB user |
| `NOUS_EVAL_DB_PASSWORD` | `nous_eval` | F051 — eval DB password; warns UserWarning if left default |
| `NOUS_EVAL_DB_NAME` | `nous_eval` | F051 — eval DB name |
| `NOUS_EVAL_AGENT_ID` | `nous-eval-corpus` | F051 — agent_id used by ingested corpus; must match Heart.recall filter |
| `NOUS_EVAL_FIXTURES_DIR` | *(unset)* | F051 — path to clone of nous-eval-fixtures repo; unset = smoke mode |
| `NOUS_EVAL_FIXTURE_VERSION` | `v2026-Q2` | F051 — image tag pinned in docker-compose (not "latest" for reproducibility) |
| `NOUS_EVAL_TOP_K` | `10` | F051 — retrieval top-K for all metrics |
| `NOUS_EVAL_REPORT_DIR` | `reports` | F051 — where markdown + JSON reports land |
| `NOUS_EVAL_RUN_HISTORY_ENABLED` | `true` | F051 — persist run to nous_system.eval_runs on main DB |
| `NOUS_EVAL_RUN_HISTORY_INSERT_TIMEOUT_S` | `5.0` | F051 — asyncio.wait_for timeout on eval_runs INSERT (never blocks run) |
| `NOUS_EVAL_F050_GATE_THRESHOLD` | `0.07` | F051 — F050 paired-A/B MRR delta threshold (+7%) |
| `NOUS_EVAL_F050_GATE_MAX_SINGLE_REGRESSION` | `0.03` | F051 — max allowed per-source regression to still pass gate |
| `NOUS_EVAL_F050_GATE_REQUIRE_MAJORITY_POSITIVE` | `true` | F051 — gate additionally requires majority of gate-eligible sources positive |
| `NOUS_SUBTASK_MAX_CONCURRENT` | `3` | Max concurrent subtasks |
| `NOUS_SCHEDULE_ENABLED` | `true` | Enable task scheduler |
| `NOUS_SCHEDULE_CHECK_INTERVAL` | `60` | Seconds between schedule checks |
| `NOUS_TELEGRAM_BOT_TOKEN` | — | Telegram bot token for subtask notifications |
| `NOUS_TELEGRAM_CHAT_ID` | — | Telegram chat ID for subtask notifications |
| `NOUS_SUBTASK_TOOL_CALL_LIMIT` | `20` | Max tool calls per subtask execution |
| `NOUS_INLINE_SUBTASK_TIMEOUT` | `90` | Default timeout for inline (await_result) subtasks |
| `NOUS_FRAME_DEFAULT_MODELS` | `{}` | JSON map of frame type to default model (falls back to `NOUS_BACKGROUND_MODEL`) |
| `NOUS_PROGRAMMATIC_TOOLS_ENABLED` | `true` | Enable run_python tool for client-side code execution |
| `NOUS_PROGRAMMATIC_TOOLS_TIMEOUT` | `10` | Timeout in seconds for run_python code execution. Enforced in-thread by a `sys.settrace` deadline hook, so Python-level runaway code (`while True: pass`) is actually interrupted; a blocking C-level call still cannot be interrupted and holds its slot until it returns. |
| `NOUS_PROGRAMMATIC_TOOLS_MAX_CONCURRENT` | `4` | Max concurrent run_python executions. Excess calls are rejected with an error instead of stacking threads inside the API process. |
| `NOUS_CONTEXT_WINDOW` | auto | Override model context window size in tokens (0 = auto-detect from model name) |
| `NOUS_ANTI_HALLUCINATION_PROMPT` | `true` | Inject "don't guess, re-fetch" safety prompt into system context |
| `NOUS_TOOL_PRUNING_ENABLED` | `true` | Enable 4-tier tool result pruning pipeline |
| `NOUS_TOOL_SOFT_TRIM_CHARS` | `4000` | Threshold above which tool results get soft-trimmed |
| `NOUS_TOOL_SOFT_TRIM_HEAD` | `1500` | Chars to keep from start when soft-trimming |
| `NOUS_TOOL_SOFT_TRIM_TAIL` | `1500` | Chars to keep from end when soft-trimming |
| `NOUS_TOOL_METADATA_DEGRADE_AFTER` | `8` | Tool result age (in results) before metadata degradation |
| `NOUS_TOOL_HARD_CLEAR_AFTER` | `12` | Tool result age before hard-clear replacement |
| `NOUS_TOOL_BULK_RESULT_CHARS` | `50000` | #179: results at/above this original size (survives soft-trim + SmartCompress via markers) from operation-shaped tools (`BULK_ESCALATION_TOOLS` = bash, run_python) escalate to the `bulk` (1, 2, 4) decay profile with anti-replay stubs. `0` disables. |
| `NOUS_KEEP_LAST_TOOL_RESULTS` | `2` | Number of most recent tool results always protected |
| `NOUS_COMPACTION_ENABLED` | `true` | Enable LLM-powered history compaction |
| `NOUS_COMPACTION_THRESHOLD` | auto | Token count triggering compaction (auto-scales per model context window) |
| `NOUS_KEEP_RECENT_TOKENS` | auto | Tokens to preserve during compaction (auto-scales per model) |
| `NOUS_RELEVANCE_FLOOR_ENABLED` | `true` | Enable per-type minimum score filtering on memory retrieval |
| `NOUS_RELEVANCE_DROP_RATIO` | `0.6` | Diminishing returns cutoff — stop at >40% score drops |
| `NOUS_BUDGET_SCALE_ENABLED` | `true` | Scale context budgets based on model context window |
| `NOUS_CONTEXT_BUDGET_OVERRIDES` | `{}` | JSON dict overriding per-frame budget defaults (e.g. `{"total": 12000, "decisions": 3000}`) |
| `NOUS_STALENESS_PENALTY_ENABLED` | `true` | Apply time-decay penalty to memory scores |
| `NOUS_STALENESS_HALF_LIFE_DAYS` | `30` | Half-life in days for staleness decay |
| `NOUS_TRANSCRIPT_MAX_CHARS` | `16000` | Max chars for episode transcript truncation before summarization |
| `NOUS_TRANSCRIPT_MESSAGE_MAX_CHARS` | `8000` | SANITY per-message bound when capturing User:/Assistant: lines into the episode transcript (layer.py capture seam — sole source for stored transcript, summary, facts, F067 chunks). Was hardcoded 500. Tune cost via `NOUS_EPISODE_SUMMARY_MAX_CHUNKS` / `NOUS_EPISODE_CHUNK_MAX_PER_EPISODE`, not this. |
| `NOUS_EPISODE_LESSONS_MAX_CHARS` | `8000` | SANITY bound on the end-of-session reflection stored as episodes.lessons_learned. Was hardcoded 500. |
| `NOUS_EPISODE_SUMMARY_MAX_CHUNKS` | `4` | Max transcript chunks (each ≤ `NOUS_TRANSCRIPT_MAX_CHARS`) summarized per episode — bounds summarizer LLM call count. Selection is head+tail (first N-1 + final chunk); dropped chunks remain raw in episodes.transcript. 0 = unlimited (pre-2026-07-02 behavior). **Cost-control lever: lower this to cut summarizer LLM cost.** |
| `NOUS_EPISODE_CHUNK_MAX_PER_EPISODE` | `100` | F067: max chunks embedded into heart.episode_chunks per episode — bounds embedding volume. Tail beyond the cap stays raw in episodes.transcript. 0 = unlimited (pre-2026-07-02 behavior). **Cost-control lever: lower this to cut F067 embedding cost.** **Operator note:** per-session summarizer/F067 cost is governed by `NOUS_EPISODE_SUMMARY_MAX_CHUNKS` and `NOUS_EPISODE_CHUNK_MAX_PER_EPISODE`; pinning `NOUS_TRANSCRIPT_MESSAGE_MAX_CHARS` down re-introduces permanent information loss and is a last resort. |
| `NOUS_EPISODE_SEED_SUMMARY_CHARS` | `500` | Chars of the first user message used as the episode's seed summary AND its dedup embedding probe. Was hardcoded 200. |
| `NOUS_EPISODE_DEDUP_THRESHOLD` | `0.85` | Cosine threshold above which a new episode is treated as a duplicate and not created. |
| `NOUS_EPISODE_DEDUP_WINDOW_HOURS` | `48` | Lookback window for episode-duplicate detection. |
| `NOUS_EPISODE_MIN_CONTENT_LENGTH` | `200` | Min combined user+assistant chars for a single-turn no-tool session to keep its episode (below = soft-deleted as trivial). |
| `NOUS_CORRECTION_INPUT_MAX_CHARS` | `2000` | F039: chars of the user message and AI response shown to the correction-extraction LLM. Was hardcoded 1000. |
| `NOUS_CORRECTION_MAX_TOKENS` | `1024` | F039: output budget for correction extraction. Raised from hardcoded 512 (F031 bug class: truncated JSON silently drops the correction). |
| `NOUS_CORRECTION_MIN_PRINCIPLE_CHARS` | `20` | F039: min length of an extracted principle before it is stored as a fact (below = silently dropped). Was hardcoded 30, which dropped terse corrections like 'Always use uv, not pip.' (24 chars). |
| `NOUS_EPISODE_SUMMARY_MAX_TOKENS` | `0` (auto) | Override for the episode-summarization LLM max_tokens. 0 = auto (3000 when coverage/open-threads prompts are on, else 1500). |
| `NOUS_KNOWLEDGE_EXTRACTOR_MAX_CHARS` | `24000` | Pre-compaction fact extraction: total chars of the doomed-message snapshot shown to the LLM (head-truncated). Was hardcoded 12000; fires once per compaction, under-capture is permanent loss. |
| `NOUS_SLEEP_REFLECTION_SUMMARY_CHARS` | `500` | Per-episode summary chars fed to the sleep reflection LLM. Was hardcoded 200 (~28% of a typical summary). |
| `NOUS_SLEEP_CONTRADICTION_FACT_CHARS` | `1000` | Per-fact chars shown to the contradiction-resolution LLM (verdicts are destructive: SUPERSEDE/REMOVE/MERGE). Was hardcoded 500; 1000 matches the call's max_tokens. |
| `NOUS_FACT_MIN_CONTENT_CHARS` | `30` | F038-1.2 hard floor: facts shorter than this are rejected before dedup/admission on every write path. |
| `NOUS_FACT_SUPERSESSION_THRESHOLD` | `0.80` | Same-subject supersession cosine gate in _supersede_same_subject (deactivates the old fact). Sibling of `NOUS_FACT_NATIVE_COSINE_THRESHOLD`. |
| `NOUS_GRAPH_LINK_CANDIDATE_WINDOW_DAYS` | `60` | Recency window for graph-link candidates (fact→decision evidence_for at learn time; decision→fact/episode at record time). Was hardcoded 30; 60 doubles coverage with bounded candidate growth (evidence_for precision 0.70, 2026-06-13 audit). 0 = no time cutoff. |
| `NOUS_FACT_DEDUP_THRESHOLD` | `0.92` | **Raw-cosine** threshold for fact extractor dedup (Leg 1 pre-check). Audit S1 (2026-06-09) changed the probe from RRF hybrid search to a raw-cosine nearest-neighbor query (`find_similar_facts`) — RRF scores encode rank, not closeness, so the old pre-check fired for every candidate. The threshold now compares against actual cosine similarity. |
| `NOUS_FACT_NATIVE_COSINE_THRESHOLD` | `0.95` | Native cosine threshold for Heart.learn dedup (Leg 2). F056 #377 made this env-tunable; the F056 dedup eval found 0.80 lifts combined F1 from 0.40 → 0.76 on the smoke fixture. Default kept 0.95 for backwards-compat. Audit S3 (2026-06-09): dupes in the contradiction band (0.85–0.95) are now classified (F027) before confirming, so lowering this below 0.85 no longer silently swallows contradictions/supersessions. |
| `NOUS_EMBEDDING_CACHE_SIZE` | `1024` | Audit D2/S7 (2026-06-09): bounded in-process LRU on EmbeddingProvider, keyed (model, dimensions, sha256(text)). Eliminates the 4–7× repeat query embeds per recall and repeat content/template embeds per learn + sleep cycle. `0` disables. Vectors stored packed float32 (matches pgvector float4): ~6 MB at 1024 entries × 1536 dims. |
| `NOUS_RRF_K` | `60` | RRF smoothing constant for hybrid search rank fusion |
| `NOUS_TOOL_TIMEOUT` | `120` | Max seconds for any single tool execution |
| `NOUS_KEEPALIVE_INTERVAL` | `10` | Seconds between keepalive events during tool execution |
| `NOUS_SSE_PING_INTERVAL` | `15` | Seconds between SSE comment-line pings on `/chat/stream`. Keeps the socket warm during stalls in pre_turn, compaction, or any non-streaming phase. Comment lines are ignored by SSE clients but reset their read timer. |
| `NOUS_GRAPH_RECALL_ENABLED` | `true` | Enable graph expansion in recall_deep |
| `NOUS_GRAPH_RECALL_MAX_EXPAND` | `5` | Max seed results to expand |
| `NOUS_GRAPH_RECALL_DECAY` | `0.7` | Score decay per graph hop |
| `NOUS_GRAPH_RECALL_MAX_NEIGHBORS` | `3` | Max neighbors per seed |
| `NOUS_CROSS_TYPE_LINKING_ENABLED` | `true` | Enable cross-type auto-linking |
| `NOUS_CROSS_TYPE_THRESHOLD` | `0.80` | Cross-type similarity threshold |
| `NOUS_CONTRADICTION_DETECTION` | `true` | Enable LLM contradiction detection |
| `NOUS_CONTRADICTION_MODEL` | `claude-haiku-4-5-20251001` | Model for contradiction classification |
| `NOUS_CONFIDENCE_CALIBRATION_FACTOR` | `0.7627` | F058: multiplicative scale applied to agent-recorded decision confidence at write time. Default derived empirically from `reports/calibration_eval.md` (401 reviewed prod decisions: mean conf 0.834 vs strict accuracy 0.636, Brier 0.252 at random baseline). Set to `1.0` to disable scaling. Pre-calibration value preserved in `brain.decisions.confidence_raw`. |
| `NOUS_DECISION_OUTCOME_SCORE_FACTORS` | `{"superseded": 0.3, "noise": 0.1}` | 2026-07-27: per-outcome multiplicative demotion applied to decision retrieval scores in `Brain._query`, followed by a stable re-sort (the re-sort is load-bearing — `_query` returns rows in merged-search order and NOTHING downstream re-sorts, so a multiplier alone is inert AND desyncs `_apply_relevance_filter`'s monotonic walk). Measured: superseded decisions were ranking #1/#2 above the current one in the "Related Decisions" prompt section. Demotion (not exclusion) so a superseded decision whose successor was never linked still renders, labeled, instead of leaving an empty section. Skipped entirely when a caller passes an explicit `outcome=`. Values must be in `(0, 1]`. `{}` = kill switch (no multiply, no re-sort, byte-identical to pre-2026-07-27). Graph-path re-entry uses a FILTER instead (the resolver carries no score to demote). |
| `NOUS_CROSS_TYPE_LINK_MIN_CONTENT_CHARS` | `40` | F022 audit fix (2026-04-30): minimum content length (after strip) for the live event-bus linker to fire. Mirrors F054's `NOUS_CE_BACKFILL_MIN_DECISION_CHARS` for the backfill path. Empty/near-empty source or target content was the dominant cause of NO/WEAK edge verdicts on `informed_by` and `evidence_for` (precision 0.70). Set to `0` to disable. Filters both source side (in Python before embed) and target side (SQL `length` clause). |
| `NOUS_F026_PERSISTENCE_ENABLED` | `true` | F026 (2026-04-30): persist every action-gate verdict and claim-verification outcome to `nous_system.events` so a retrospective accuracy eval can run against real prod data. Fire-and-forget via `asyncio.create_task` so the gate hot path never blocks on DB I/O. Event types: `f026_action_gate`, `f026_claim_verification`. Set to `false` to disable. |
| `NOUS_SPREADING_ACTIVATION_ENABLED` | `auto` | Spreading activation (auto/true/false). Since 2026-07-11 the CTE is seeded with the top-3 heart FACT results (RRF-scored) alongside decision seeds — spreading fires on decision-less corpora too. |
| `NOUS_SPREADING_ACTIVATION_DENSITY_THRESHOLD` | `3.0` | Density threshold for auto-enable |
| `NOUS_EXECUTION_LEDGER_ENABLED` | `true` | Enable execution ledger (F026) |
| `NOUS_EXECUTION_LEDGER_MAX_TOKENS` | `500` | Token budget for ledger in system prompt |
| `NOUS_CLAIM_VERIFICATION_ENABLED` | `true` | Enable claim verification (F026) |
| `NOUS_CLAIM_VERIFICATION_MODE` | `enforce` | Claim verification mode (shadow/warn/enforce) |
| `NOUS_ACTION_GATING_ENABLED` | `true` | Enable action gating (F026) |
| `NOUS_ACTION_GATING_MODE` | `enforce` | Action gating mode (shadow/warn/enforce) |
| `NOUS_ACTION_GATING_MODEL` | `claude-haiku-4-5-20251001` | Model for Tier 3 LLM gate |
| `NOUS_ACTION_GATING_EXTERNAL_ONLY` | `false` | Skip Tier 2, only gate external/irreversible |
| `NOUS_ACTION_GATING_TURN_WINDOW` | `5` | Only block duplicates within this many turns |
| `NOUS_PROCEDURE_SCORE_FLOOR` | `0.40` | Minimum score for procedures when embeddings enabled (F038) |
| `NOUS_MMR_ENABLED` | `false` | Enable MMR diversity re-ranking in recall_deep |
| `NOUS_MMR_DIVERSITY_WEIGHT` | `0.7` | MMR relevance vs diversity weight (1.0=pure relevance, 0.0=pure diversity) |
| `NOUS_MMR_SKIP_AFTER_CE` | `true` | F030.1: skip MMR when CE rerank just reordered the head. F051 harness measured +30% MRR (0.372 -> 0.484, +190% on jargon-drift) when MMR is gated this way. Set `false` to restore pre-F030.1 chained CE-then-MMR behavior. |
| `NOUS_CROSS_ENCODER_ENABLED` | `false` | Enable F042 cross-encoder reranking in recall_deep (requires sentence-transformers) |
| `NOUS_QUERY_EXPANSION_ENABLED` | `false` | F050 master flag — enable Haiku-driven multi-query expansion at recall time (Phase 1 lands dark; flip after harness gate) |
| `NOUS_QUERY_EXPANSION_MODEL` | `claude-haiku-4-5-20251001` | F050 model used for the expansion call (forced tool use, ~256 tok output) |
| `NOUS_QUERY_EXPANSION_TIMEOUT_SECONDS` | `2.0` | F050 per-call timeout (seconds); on timeout, expand() fails open to [query] |
| `NOUS_QUERY_EXPANSION_MAX_VARIANTS` | `3` | F050 max variants returned including the original query |
| `NOUS_QUERY_EXPANSION_MIN_WORDS` | `3` | F050 gate threshold — queries shorter than this skip expansion |
| `NOUS_QUERY_EXPANSION_MAX_PER_HOUR` | `500` | F050 budget cap on Haiku calls per hour (asyncio.Lock-serialized in-process counter) |
| `NOUS_QUERY_EXPANSION_CACHE_TTL_DAYS` | `30` | F050 cache retention for `heart.query_expansions` rows |
| `NOUS_CROSS_ENCODER_MODEL` | `cross-encoder/ms-marco-MiniLM-L-6-v2` | Cross-encoder model name for F042 reranking |
| `NOUS_CROSS_ENCODER_MAX_CANDIDATES` | `30` | Max candidates to rerank (head-truncation, tail untouched) |
| `NOUS_CROSS_ENCODER_TEXT_LIMIT` | `512` | Max chars per doc fed to cross-encoder |
| `NOUS_CE_BACKFILL_ENABLED` | `false` | Enable F043 cross-encoder reranking in F040 sleep-cycle graph backfill (requires sentence-transformers, reuses F042 model) |
| `NOUS_CE_BACKFILL_TOP_K` | `10` | Max candidates per orphan reranked by cross-encoder before cosine verification |
| `NOUS_CE_BACKFILL_MIN_SCORE` | `0.30` | Sigmoid-normalized CE score floor — candidates below this are dropped before the cosine gate |
| `NOUS_CE_BACKFILL_THRESHOLD_FACT_FACT` | `0.55` | F054 (was F045 0.65): CE-mode fact↔fact cosine threshold. Relaxed 2026-04-26 — F053 density-eval measured +67% same-type edges at unchanged 0.83 LLM-judged precision. |
| `NOUS_CE_BACKFILL_THRESHOLD_FACT_DECISION` | `0.55` | F045: CE-mode fact→decision threshold (KEPT STRICT by F054 — loosening regressed cross-type precision in the 2026-04-26 audit; corpus-quality issue addressed via the new decision content guard below). |
| `NOUS_CE_BACKFILL_THRESHOLD_FACT_EPISODE` | `0.55` | F045: CE-mode fact→episode threshold (KEPT STRICT by F054 — same rationale as fact_decision). |
| `NOUS_CE_BACKFILL_THRESHOLD_DECISION_DECISION` | `0.50` | F054 (was F045 0.60): CE-mode decision↔decision threshold. Relaxed 2026-04-26. |
| `NOUS_CE_BACKFILL_THRESHOLD_EPISODE_EPISODE` | `0.50` | F054 (was F045 0.58): CE-mode episode↔episode threshold. Relaxed 2026-04-26 (extrapolated — eval corpus had 0 episode orphans). |
| `NOUS_CE_BACKFILL_THRESHOLD_PROCEDURE_ANY` | `0.45` | F054 (was F045 0.55): CE-mode procedure→* threshold. Relaxed 2026-04-26. |
| `NOUS_CE_BACKFILL_MIN_CONTENT_CHARS` | `80` | F045: drop candidates whose content (after strip) is shorter than this before CE inference. Filters URL-only / boilerplate facts. |
| `NOUS_CE_BACKFILL_MIN_DECISION_CHARS` | `40` | F054: type-aware content guard for decisions. Mirrors `ce_backfill_min_content_chars=80` for facts. Set to 0 to disable. Addresses empty `brain.decisions.context` polluting cross-type edges (root cause of ~5/9 evidence_for NO/WEAK verdicts in the 2026-04-26 F053 audit). |

**F045 migration note:** When `NOUS_CE_BACKFILL_ENABLED=true`, the `NOUS_GRAPH_THRESHOLD_*` env overrides below are **ignored** — `_get_threshold()` routes to the `NOUS_CE_BACKFILL_THRESHOLD_*` set instead. Operators upgrading from an F043-only deployment that had `NOUS_GRAPH_THRESHOLD_FACT_FACT` overridden must re-set the equivalent `NOUS_CE_BACKFILL_THRESHOLD_FACT_FACT` to keep their override effective.

**F054 rollback note:** F054 relaxed four same-type CE-mode thresholds and added the decision content guard. To revert to pre-F054 (F045) behavior, set: `NOUS_CE_BACKFILL_THRESHOLD_FACT_FACT=0.65`, `NOUS_CE_BACKFILL_THRESHOLD_DECISION_DECISION=0.60`, `NOUS_CE_BACKFILL_THRESHOLD_EPISODE_EPISODE=0.58`, `NOUS_CE_BACKFILL_THRESHOLD_PROCEDURE_ANY=0.55`, `NOUS_CE_BACKFILL_MIN_DECISION_CHARS=0`.
| `NOUS_CRITIC_SKILL_INJECTION` | `disabled` | Critic skill injection mode: enabled, disabled, log_only |
| `NOUS_CRITIC_SKILL_SLOTS` | `2` | Reserved procedure slots for Critic-recommended skills |
| `NOUS_EMBEDDING_SKILL_SLOTS` | `3` | Procedure slots for embedding similarity search |
| `NOUS_RUBRIC_ENABLED` | `true` | Enable self-modifying rubric system (F024-3b) |
| `NOUS_RUBRIC_OUTCOME_DETECTION_ENABLED` | `true` | Enable outcome signal detection on episodes |
| `NOUS_RUBRIC_EVOLUTION_ENABLED` | `false` | Enable weight/split/merge evolution (Phase 1+) |
| `NOUS_RUBRIC_MIN_EPISODES_FOR_CORRELATION` | `50` | Minimum episodes before correlation runs |
| `NOUS_RUBRIC_WEIGHT_CHANGE_CAP` | `0.05` | Max weight shift per adjustment cycle (±5%) |
| `NOUS_RUBRIC_MIN_DIMENSIONS` | `3` | Floor for dimension count |
| `NOUS_RUBRIC_MAX_DIMENSIONS` | `7` | Ceiling for dimension count |
| `NOUS_RUBRIC_MAX_VERSIONS_PER_WEEK` | `1` | Rate limit on rubric evolution |
| `NOUS_RUBRIC_OUTCOME_MODEL` | `claude-haiku-4-5-20251001` | Model for outcome classification |
| `NOUS_HEARTBEAT_ENABLED` | `true` | Enable heartbeat proactive monitoring |
| `NOUS_HEARTBEAT_TICK_INTERVAL` | `30` | Seconds between heartbeat tick loop iterations |
| `NOUS_HEARTBEAT_QUIET_START` | `23` | Quiet hours start (hour, user timezone) |
| `NOUS_HEARTBEAT_QUIET_END` | `8` | Quiet hours end (hour, user timezone) |
| `NOUS_HEARTBEAT_DAILY_TOKEN_BUDGET` | `50000` | Max tokens/day for heartbeat cognitive sessions |
| `NOUS_HEARTBEAT_EMAIL_ENABLED` | `false` | Enable email check (needs IMAP credentials) |
| `NOUS_HEARTBEAT_EMAIL_INTERVAL` | `180` | Seconds between email checks |
| `NOUS_HEARTBEAT_EMAIL_IMAP_HOST` | `imap.gmail.com` | IMAP server host |
| `NOUS_HEARTBEAT_HEALTH_INTERVAL` | `3600` | Seconds between health checks |
| `NOUS_HEARTBEAT_SELF_INITIATED_INTERVAL` | `1800` | Seconds between self-initiated checks |
| `NOUS_HEARTBEAT_ESCALATION_LOW_TO_NORMAL_HOURS` | `72` | Hours before low→normal finding escalation |
| `NOUS_HEARTBEAT_ESCALATION_NORMAL_TO_HIGH_HOURS` | `24` | Hours before normal→high finding escalation |
| `NOUS_HEARTBEAT_ESCALATION_HIGH_REALERT_HOURS` | `12` | Hours between high-urgency re-alerts |
| `NOUS_HEARTBEAT_ESCALATION_ACCUMULATION_THRESHOLD` | `5` | Acknowledged findings count to trigger collection escalation |
| `NOUS_HEARTBEAT_DIGEST_HOUR_UTC` | `9` | UTC hour for daily digest Telegram message |
| `NOUS_HEARTBEAT_SUPPRESSION_TTL_HOURS` | `24` | TTL for suppressed finding state |
| `NOUS_HEARTBEAT_TUNING_ENABLED` | `false` | Enable heartbeat self-tuning (F034.3) |
| `NOUS_HEARTBEAT_TUNING_INTERVAL_HOURS` | `168` | Hours between tuning passes (weekly) |
| `NOUS_HEARTBEAT_TUNING_MIN_SAMPLES` | `10` | Minimum outcome signals before adjusting params |
| `NOUS_HEARTBEAT_TUNING_LEARNING_RATE` | `0.1` | Max parameter change per cycle (fraction of range) |
| `NOUS_HEARTBEAT_TUNING_ROLLBACK_THRESHOLD` | `0.2` | Negative rate increase that triggers auto-rollback |
| `NOUS_HEARTBEAT_DEFAULT_CHECK_TIMEOUT` | `30` | Default max seconds per heartbeat check run |
| `NOUS_HEARTBEAT_MAX_DYNAMIC_CHECKS` | `10` | Maximum number of concurrent dynamic checks |
| `NOUS_HEARTBEAT_DYNAMIC_SYNC_TICKS` | `60` | Ticks between periodic dynamic check sync (re-loads from DB) |
| `NOUS_CACHE_BREAK_DETECTION_ENABLED` | `true` | Enable cache break detection logging (F036) |
| `NOUS_CACHE_SPLIT_SYSTEM_PROMPT` | `true` | Enable 3-tier system prompt splitting (F036) |
| `NOUS_CACHE_SINGLE_BREAKPOINT` | `true` | Use single cache breakpoint strategy (F036) |
| `NOUS_TOOL_SCHEMA_CACHE_ENABLED` | `true` | Cache tool schemas per frame (F036) — process-side Python memoization only; does NOT affect the Anthropic prompt cache |
| `NOUS_STABLE_TOOL_SET_ENABLED` | `true` | Send a STABLE tool superset across all non-`initiation` frames instead of frame-scoped tool arrays. Tools sit at the front of the Anthropic cacheable prefix, so frame-scoped tools busted the whole prefix on every frame change (~10% of prod cache-creation tokens, measured 2026-06-14). Collapses conversational frames to the `task` (`*`) superset; `initiation` keeps its distinct minimal set; `store_identity`/`complete_initiation` are excluded from the superset. `FRAME_TOOLS` still drives the textual frame instructions. Set `false` to restore per-frame tool gating. |
| `NOUS_TOOL_ARG_SALVAGE_ENABLED` | `true` | Dispatch-level repair for model-emitted tool input where a required arg leaked as Claude-internal XML syntax inside another string arg (observed 2026-07-13: `record_decision` description ending `</description>\n<parameter name="confidence">0.55`). When a schema-required key is missing, ToolDispatcher extracts a trailing `<parameter name="KEY">value` run from string args, type-coerces per schema, strips the leaked tail, and logs a warning. A repaired call still tells the model what it got wrong — the result is prefixed `[input repaired]` naming the recovered key, because a silent success teaches the model nothing and it emits the same broken shape next turn. **Two validations run independently of this flag**, both returning an actionable tool error instead of an opaque one: (1) missing required args name the missing + provided keys rather than raising a raw `TypeError` that leaks `create_nous_tools.<locals>` internals; (2) **structural type mismatches** name the field, the declared type and what was actually sent (observed 2026-08-22: `record_decision` with `tags='fannie-mae, cpm, condo, …'` reached the model as a raw pydantic `ValidationError` + docs URL). The type check is deliberately narrow — only container-vs-scalar confusion, since pydantic's lax mode legitimately coerces `"0.9"` → `0.9`, so rejecting scalar-for-scalar would fail calls that succeed today. Union types, absent `type`, and nulls are skipped. Wrong types are reported, never coerced: the model must learn the shape. Set `false` to disable salvage (both validations stay). |
| `NOUS_DAG_ENABLED` | `true` | Enable DAG orchestration (F038) |
| `NOUS_CORRECTION_EXTRACTION_ENABLED` | `true` | Enable correction learning pipeline (F039) |
| `NOUS_CONSOLIDATION_AUDIT_ENABLED` | `false` | F035.6 master kill-switch. When true, each sleep cycle persists a reviewable changelog to `nous_system.consolidation_cycles` + `consolidation_actions` (migration 063). Default off = sleep behaves byte-for-byte as today (no envelope, no action emits, no retention phase). Fact-mutation phases (reflect/stale_scan/F031/F027) record per-action; graph/episode phases record one per-phase summary. |
| `NOUS_CONSOLIDATION_AUDIT_RETENTION_DAYS` | `30` | F035.6 days to retain `consolidation_actions` rows (the per-night `consolidation_cycles` totals are kept indefinitely as the F035.3 drift time-series). `_phase_prune_consolidation_actions` runs last each cycle; `0` disables the sweep. |
| `NOUS_CONSOLIDATION_AUDIT_MAX_INFLIGHT` | `32` | F035.6 soft cap on in-flight batched action-insert tasks; the next batch is awaited inline once exceeded (backpressure). |
| `NOUS_GRAPH_BACKFILL_ENABLED` | `true` | Enable graph densification backfill during sleep (F040) |
| `NOUS_GRAPH_BACKFILL_MAX_FACTS` | `50` | Max orphan facts to process per sleep cycle |
| `NOUS_GRAPH_BACKFILL_MAX_DECISIONS` | `30` | Max orphan decisions to process per sleep cycle |
| `NOUS_GRAPH_BACKFILL_MAX_EPISODES` | `30` | Max orphan episodes to process per sleep cycle |
| `NOUS_GRAPH_BACKFILL_MAX_PROCEDURES` | `20` | Max orphan procedures to process per sleep cycle |
| `NOUS_GRAPH_THRESHOLD_FACT_FACT` | `0.82` | Same-type threshold for fact↔fact linking |
| `NOUS_GRAPH_THRESHOLD_FACT_DECISION` | `0.72` | Cross-type threshold for fact→decision linking |
| `NOUS_GRAPH_THRESHOLD_FACT_EPISODE` | `0.70` | Cross-type threshold for fact→episode linking |
| `NOUS_GRAPH_THRESHOLD_DECISION_DECISION` | `0.78` | Same-type threshold for decision↔decision linking |
| `NOUS_GRAPH_THRESHOLD_EPISODE_EPISODE` | `0.75` | Same-type threshold for episode↔episode linking |
| `NOUS_GRAPH_THRESHOLD_PROCEDURE_ANY` | `0.70` | Threshold for procedure→fact/decision linking |
| `NOUS_GRAPH_HEALTH_ORPHAN_WARN_THRESHOLD` | `0.40` | Orphan rate threshold for health warnings |
| `NOUS_GRAPH_HEALTH_CHECK_ENABLED` | `true` | Enable graph health monitoring |
| `NOUS_EPISODE_CHUNKS_ENABLED` | `false` | F067 master switch. When true, `EpisodeSummarizer` chunks the raw transcript and embeds chunks into `heart.episode_chunks`; `run_recall_pipeline` surfaces chunk hits as a new result type. **Validated on per-question isolation only; opt-in for prod.** |
| `NOUS_EPISODE_CHUNK_SIZE` | `600` | F067 chunk size in chars (sliding window). |
| `NOUS_EPISODE_CHUNK_OVERLAP` | `80` | F067 chunk overlap in chars to avoid splitting key tokens. |
| `NOUS_EPISODE_CHUNK_RECALL_LIMIT` | `10` | F067 **flat per-leg allotment** — chunks retrieved by the chunk-recall leg, independent of the caller's `limit`. Until 2026-08-01 this was clamped by `min(this, limit * 2)`, so any value above 2× the caller's limit was silently inert: prod ran `=30` against `recall_deep`'s default `limit=10` and retrieved 20. The clamp is gone; the value is now authoritative at every caller. Sibling legs (`NOUS_KEYED_FACT_LEG_K`, `NOUS_EXEMPLAR_TOP_K`) behave the same way. Raising this widens the candidate pool AND the rendered `recall_deep` output — chunks are not score-banded and are never seen by MMR/CE, so the allotment is the only control on how many reach the agent. |
| `NOUS_HEART_RRF_PENALTY_LIMIT` | *(unset)* | Sibling of `NOUS_CHUNK_RRF_PENALTY_LIMIT` for the **heart legs** (episodes/facts/procedures). `Heart.recall` derives `fetch_limit = limit * 2` (`heart.py:967`) and passes it straight to `hybrid_search`, where `penalty_rank = limit + 1`. Because `recall_deep`'s `limit` is **LLM-controlled over 1–50**, the heart legs' penalty base swings between **3 and 101** — every single-leg fact/episode/procedure is silently rescored by a parameter that is supposed to control row count only. Set `20` to pin it at what `recall_deep`'s default (`limit=10` → `fetch_limit=20`) produces today. Threaded through **both** merge layers, since query expansion (ON in prod) stacks a per-variant `_rrf_merge` beneath a cross-variant `_rrf_merge_n`; pinning one alone leaves the other coupled. Unset = coupled (previous behavior). |
| `NOUS_CHUNK_RRF_PENALTY_LIMIT` | *(unset)* | Pins the RRF missing-leg penalty base for the F067 chunk leg, decoupling **scoring** from the **row allotment**. `_rrf_merge` scores a document absent from one leg at `penalty_rank = limit + 1` (`search.py:161`) and `hybrid_search` passed its row `limit` straight in — so `NOUS_EPISODE_CHUNK_RECALL_LIMIT` was a scoring knob too. At `NOUS_RRF_K=30`, raising it 20→30 drops every single-leg chunk by ~0.029 and **demotes** chunks: measured −0.83 chunks in top-10 per query over 60 queries, worse on 37/60, with **0/60 of the newly admitted chunks reaching top-10**. Set `20` for **parity with the heart legs**: `Heart.recall` uses `fetch_limit = limit * 2` (`heart.py:967`) and `facts`/`episodes`/`procedures` pass that straight to `hybrid_search`, so at `recall_deep`'s default `limit=10` they are scored against penalty base **20** — which is also what the chunk leg used before #579. Values below 20 give chunks an *advantage* over facts rather than parity (base 10 is **+0.043** at `NOUS_RRF_K=30`). Unset = coupled to the row limit (previous behavior). **Deploy this with `NOUS_EPISODE_CHUNK_RECALL_LIMIT`, not after it.** |
| `NOUS_CHUNK_HYBRID_SEARCH_ENABLED` | `false` | R2 (2026-07-02 MAB audit): RRF-fuse an FTS leg (the migration-050 `search_tsv` GIN index, previously unconsumed) with the vector leg in the F067 chunk-recall stage via the shared `heart.search.hybrid_search` helper. Also moves chunk scores from raw cosine onto the 1/k-normalized RRF [0,1] scale the coherent heart legs use (F080 deviant-leg renorm). Probe on the MAB CR corpus: gold-chunk in top-30 goes 1/5 → 4/5 (top-10: 0/5 → 1/5); compose with `NOUS_EPISODE_CHUNK_RECALL_LIMIT=30`. **Land-dark; flip after the retrieval A/B gate.** |
| `NOUS_EPISODE_CHUNK_MIN_TRANSCRIPT_CHARS` | `50` | F067 minimum transcript length to chunk (shorter transcripts skip). |
| `NOUS_RECALL_INCLUDE_PARENT_EPISODES` | `false` | F067 Phase 2. When true, `recall_deep` appends up to `recall_max_parent_episodes` parent episode summaries to its text output. **Validated on per-question isolation only; opt-in for prod.** |
| `NOUS_RECALL_MAX_PARENT_EPISODES` | `2` | F067 cap on parent episode summaries appended (deduplicated). |
| `NOUS_RECALL_PARENT_EPISODE_TRUNCATE` | `500` | F067 per-parent-episode summary char truncation. |
| `NOUS_ATTACHMENTS_ENABLED` | `true` | F024 master switch for inbound image/PDF/text-file attachments (Telegram + REST). On by default — requires a vision-capable `NOUS_MODEL` (the default `claude-sonnet-4-6` qualifies). Set `false` to disable. |
| `NOUS_ATTACHMENTS_DIR` | *(empty → `<workspace_dir>/attachments`)* | F024 on-disk root for saved attachment originals. |
| `NOUS_ATTACHMENTS_MAX_PER_MESSAGE` | `5` | F024 max attachments accepted per message. |
| `NOUS_ATTACHMENTS_PERSIST` | `true` | F024 save originals to disk + record a Heart fact reference. |
| `NOUS_ATTACHMENTS_INGEST_TEXT_FILES` | `true` | F024 chunk text/code file bodies into episode_chunks for recall. |
| `NOUS_ATTACHMENTS_INGEST_PDFS` | `true` | F024.1 extract + chunk-ingest PDF text into episode_chunks for recall (pypdf, with a Claude transcription fallback for scanned PDFs). |
| `NOUS_ATTACHMENTS_PDF_TRANSCRIPTION_MODEL` | `claude-haiku-4-5-20251001` | F024.1 model for the scanned-PDF transcription fallback. |
| `NOUS_ATTACHMENTS_PDF_MAX_TRANSCRIPTION_TOKENS` | `8000` | F024.1 output cap for the transcription fallback (long scanned PDFs truncate; paging deferred). |
| `NOUS_ATTACHMENTS_DEFAULT_PROMPT` | `What can you tell me about this?` | F024 prompt used when an attachment arrives with no caption. |
| `NOUS_RECALL_EXCLUDE_CONTEXT_IDS` | `false` | F071 master switch. When true, `recall_deep` filters out results whose IDs are already loaded into the current turn's system prompt (`recalled_fact_ids`, `recalled_decision_ids`, `recalled_episode_ids`, `recalled_procedure_ids` on `TurnContext`). Wire is forward-compatible — F072 adds a `chunk` key without changing this contract. Land dark; flip in dev for measurement via `PipelineStats.excluded_in_context`. |
| `NOUS_CHUNK_CONSOLIDATION_ENABLED` | `false` | F070 master switch. When true, `GraphDensifier.run_backfill_cycle` (and the standalone `scripts/backfill_f070_chunks.py`) build chunk→episode `part_of`, chunk→fact `summarized_by` (same-episode), and chunk↔chunk `related_to` edges. Required to populate the chunk-graph that Path A's Stage 2b consumes. |
| `NOUS_GRAPH_BACKFILL_MAX_CHUNKS` | `100` | F070 per-cycle cap on orphan chunks the densifier picks up. Used as the default batch size by `backfill_orphan_chunks` when no `max_count` override is passed. |
| `NOUS_GRAPH_THRESHOLD_CHUNK_FACT` | `0.55` | F070 cosine threshold for chunk→fact `summarized_by` edges (same-episode only in v1). |
| `NOUS_GRAPH_THRESHOLD_CHUNK_CHUNK_INTRA` | `0.70` | F070 cosine threshold for non-adjacent intra-episode chunk↔chunk edges. Adjacent chunks (chunk_index ± 1) always link at structural weight 1.0 regardless. |
| `NOUS_GRAPH_THRESHOLD_CHUNK_CHUNK_CROSS` | `0.85` | F070.1 cosine threshold for cross-episode chunk↔chunk edges (written by the sleep-cycle cross-episode pass; see also `NOUS_GRAPH_THRESHOLD_CHUNK_FACT_CROSS`). |
| `NOUS_HEART_GRAPH_ALL_TYPES_ENABLED` | `false` | F070 Path A master switch. When true, `run_recall_pipeline` Stage 2b expands fact/episode/chunk seeds to neighbors of all non-decision types (fact, episode, chunk, procedure) — required to activate F022 cross-type + F040 + F070 edges that today have no other consumer. Stage 2 (decision-only) is unaffected. |
| `NOUS_HEART_GRAPH_NEIGHBORS_PER_SEED` | `3` | Path A per-(seed, neighbor_type) LIMIT for Stage 2b's `brain.neighbors` fan-out. Each fact/episode/chunk seed pulls up to N rows of each of {fact, episode, chunk, procedure} via separate calls (mirrors the SQL-pushdown discipline from Stage 2). |
| `NOUS_SESSION_GROUP_HEART_SECTION` | `false` | When true, `_format_pipeline_text` groups Heart Memory items by source session_id with `-- Session abc12345 --` headers. Helps multi-session LLM synthesis. `_attach_fact_source_episodes` only fires when this flag is on (otherwise the source-episode lookup is a wasted DB roundtrip). |
| `NOUS_GRAPH_ADJACENCY_BOOST_ENABLED` | `false` | When true, `run_recall_pipeline` applies a gbrain-style multiplicative boost to candidates connected via `brain.graph_edges` to other candidates in the same batch. Excludes `contradicts` edges. Inert until enough cross-candidate edges exist (F040/F070/F075 backfill is the prereq). **F075 Layer 2 (`happened_before` edges) requires this flag to be `true` for the edges to affect retrieval ranking — operators flip this post-deploy.** |
| `NOUS_GRAPH_ADJACENCY_BOOST_ALPHA` | `0.15` | Max boost as a fraction of original score for the most-connected candidate. |
| `NOUS_GRAPH_ADJACENCY_BOOST_EXCLUDE_DETERMINISTIC` | `false` | N3 (2026-08-02): exclude `extraction_method='deterministic'` edges from the adjacency-boost degree sum — the clause its sibling `_record_recall_reactivation` has always carried (`retrieval_pipeline.py:1553`) but the boost query at `:1468` never did. Measured on a 60-query prod clone across 4 disjoint strata: **recall@10 +0.0900** (p=2.7e-5), MRR +0.0239 (p=3.3e-6), nDCG@10 +0.0709 (p=1.2e-6), all strata directionally positive; verified against a "it just perturbs less" explanation three ways incl. matched top-10 churn (harm-per-churn 0.0054 vs 0.0320). **Frame as removing a known regression, not an improvement** — the gain over disabling the boost *entirely* is NOT established (off ≈ filtered ≈ matched-control are statistically tied; only current prod is clearly worst). **The mechanism is NOT established**: the intuitive "weight-1.0 structural stars dominate the degree sum" account was tested and FAILED (degree concentration 0.511 with vs 0.497 without). Rejected after measurement — do not re-propose: `autobehavior_exclusion_sql()` (scores *worse* than current prod), `RETRIEVAL_EXCLUDED_RELATIONS` (byte-identical no-op), lowering `alpha` (dominated by the filter at 7.5× the alpha). Default OFF pending a second-corpus A/B; **monitor degree-distribution drift when flipping** — LTP reinforcement plus a degree-based boost is a rich-get-richer loop no frozen-corpus eval can observe. |
| `NOUS_TINYHIPPO_LITE_ENABLED` | `false` | F044 (PR #531) master switch for STC (Synaptic Tagging & Capture) consolidation. **Telemetry-only when on alone** (after the round-2 flag-decoupling): reinforces `ltp_count`, promotes edges to `consolidation_state='consolidated'` at the PRP threshold, emits `f044_*` sleep stats — **no weight or ranking change**. Requires migration `061_f044_tinyhippo_stc.sql` (auto-applied on startup; adds `consolidation_state`/`ltp_count`/`last_ltp_at` to `brain.graph_edges`). Effect is **longitudinal** (reinforcement accrues over many sleep cycles). Safe first step in prod. |
| `NOUS_TINYHIPPO_PRP_THRESHOLD` | `3` | F044 promotion gate: a tagged edge consolidates once `ltp_count >= this`. Validated `ge=1` (0/negative would promote the whole graph on the first sleep). |
| `NOUS_TINYHIPPO_RECALL_TOUCH_ENABLED` | `true` | F044 v1.1 reinforcement source: edges among co-retrieved results are buffered as reactivations (retrieval == reactivation) and flushed to `ltp_count` at sleep. Only active when `tinyhippo_lite_enabled`. Buffer is agent-scoped; deterministic/structural edges are excluded at all reinforcement touchpoints. |
| `NOUS_TINYHIPPO_CONSOLIDATED_BOOST_ENABLED` | `false` | F044 **active ranking mechanism** — opt-in, sibling to downscale (round-2 decoupling). When true (AND `NOUS_GRAPH_ADJACENCY_BOOST_ENABLED=true`), consolidated edges get `tinyhippo_consolidated_boost_factor` extra adjacency weight in `run_recall_pipeline`. Kept OFF so master-alone stays telemetry. **Not validated for a prod win** (the boost was dead/negative on BEAM + prod) — A/B before flipping. |
| `NOUS_TINYHIPPO_CONSOLIDATED_BOOST_FACTOR` | `2.0` | F044 multiplier applied to a consolidated edge's adjacency-degree contribution (only when the boost flag above is on). |
| `NOUS_TINYHIPPO_ALPHA` | `0.75` | F044 Phase 8d homeostatic α-downscale factor: each sleep multiplies TAGGED, non-deterministic edge weights by this (consolidated + structural exempt). Validated `(0.0, 1.0]` (rejects typos like `75`/negative). Spec band `[0.50, 0.90]`; experiments run as low as `0.42`. |
| `NOUS_TINYHIPPO_DOWNSCALE_ENABLED` | `false` | F044 **active ranking mechanism** — master switch for the Phase 8d α-downscale (the real retrieval mechanism). Default OFF: `tinyhippo_lite_enabled` alone stays telemetry-only. **Not yet prod-validated** (+5.1pp on prod graph-targeted qrels but n=22, not significant) — A/B on the eval DB before flipping. |
| `NOUS_EXTRACTION_INPUT_HARDENING_ENABLED` | `true` | S2 hardening (2026-07-02 MAB audit): wraps the transcript/conversation fed to the episode summarizer + knowledge extractor in `<transcript>`/`<conversation>` delimiters, appends a DATA/INSTRUCTION boundary guard (embedded bulk data IS episode content — extract it, bounded at ≤40 facts/chunk so the JSON never truncates), and drops candidate facts that verbatim-echo (6-word shingle) the extraction prompt itself. Also salvages fact-shaped bare-array LLM responses that PR #525 discarded wholesale. Replay-validated on the contaminated 274k MAB transcript: 7-echo/1-content → 0-echo/81-content. Default ON — kill-switch only. |
| `NOUS_TEMPORAL_EXTRACTION_ENABLED` | `false` | F075: enable date-anchored event extraction in the episode summarizer + fact extractor prompts. When true, the summarizer's `candidate_facts` schema accepts an optional `event_date` field and producer paths stamp `event_date_classified_at` on `FactInput`. Default off (dark-launch); flip after measurement on the LME baseline. |
| `NOUS_CANDIDATE_FACTS_EVENT_LIMIT` | `30` | F075: per-episode cap on date-anchored candidate facts merged across chunks (before FactExtractor). Stable facts stay capped at 5. Default 30 covers BEAM-100K-shaped multi-day projects with daily check-ins. Two-cap split prevents date-event truncation on long transcripts. |
| `NOUS_TEMPORAL_BACKFILL_DEFAULT_TOKEN_BUDGET` | `50000` | F075: default Haiku token cap for the retrofit backfill script when `--token-budget` is not supplied. (Backfill script ships in F075.1 follow-up PR.) |
| `NOUS_DATE_AWARE_BOOST_ENABLED` | `false` | F075 Layer 3 (deferred to F075.x): multiplicative boost on facts with `event_date` in query's inferred date window. Ships disabled until Layer 1+2 measurement shows it's needed. |
| `NOUS_DATE_AWARE_BOOST_FACTOR` | `1.20` | F075 Layer 3: multiplier applied to in-window facts. 1.0 = no boost. |
| `NOUS_DATE_AWARE_BOOST_WINDOW_PAD_DAYS` | `30` | F075 Layer 3: pad days around the inferred query date window. |
| `NOUS_EPISTEMIC_GATE_ENABLED` | `false` | §2 master switch — Haiku three-way epistemic routing (grounded / world_knowledge / abstain). When true, an `EpistemicClassifier` tags each turn in `pre_turn` and `ContextEngine.build` injects an Epistemic Routing instruction sibling to the anti-hallucination block. Fail-open: timeout/error/budget => softened abstain prose that PERMITS base-model knowledge. Default OFF (dark-launch). NOT BEAM-measurable (BEAM bypasses intent/layer/context). |
| `NOUS_EPISTEMIC_GATE_MODEL` | `claude-haiku-4-5-20251001` | §2 — Haiku model id for epistemic classification. |
| `NOUS_EPISTEMIC_GATE_TIMEOUT_SECONDS` | `2.0` | §2 — per-call Haiku timeout (`asyncio.wait_for`). Blown timeout fails open to softened prose. |
| `NOUS_EPISTEMIC_GATE_MAX_PER_HOUR` | `500` | §2 — in-process sliding-window budget cap on Haiku calls (asyncio.Lock-serialized counter, mirrors F050). Breach => fail open + WARN-once. |
| `NOUS_RECENCY_RESOLVER_ENABLED` | `false` | §1 — event_date-only recency conflict resolver. After retrieval (in `run_recall_pipeline`, after `_attach_contradictions`), same-subject facts that conflict on a value AND both carry a non-null, DIFFERING `event_date` are resolved: newer => `[current YYYY-MM]`, older => `[superseded YYYY-MM]` + down-ranked `*0.3` (never deleted). Inert until `NOUS_TEMPORAL_EXTRACTION_ENABLED` populates `event_date`. Default OFF. |
| `NOUS_RECENCY_RESOLVER_SIMILARITY_FLOOR` | `0.55` | §1 — `difflib.SequenceMatcher` ratio above which two same-subject facts are treated as the SAME attribute restated/changed (so a differing `event_date` = supersession). Below this => different attributes => no trigger. |
| `NOUS_FOLLOWUP_EPISODE_BUDGET_ENABLED` | `true` | F083 A1 kill-switch (branch `feat/F083-follow-up-association`, not yet merged). When true, the `conversation` frame gets a non-zero episode retrieval budget (600) so semantic episode recall fires for ordinary follow-ups instead of being suppressed (`episodes=0`). The temporal-recency rescue then lifts it to 1000 on a detected follow-up. Set false to restore the old `episodes=0`. |
| `NOUS_FOLLOWUP_DEICTIC_DETECTION_ENABLED` | `true` | F083 C1 kill-switch. When true, on the FIRST turn of a new session a cross-session deictic/continuation follow-up ("continue what we were doing", "the second option you mentioned", "did that fix work?") raises `temporal_recency`, flipping the episode-budget rescue + `temporal_boost`. First-turn-gated so same-session references never pull cross-session episodes. |
| `NOUS_RECALL_BEFORE_CLARIFY_PROMPT` | `true` | F083 C2. When true, inject a static system-prompt instruction telling the agent to call `recall_deep`/`recall_recent` to resolve a referent before asking the user to clarify. |
| `NOUS_FOLLOWUP_FIRST_TURN_EPISODE` | `false` | F083 A2 (**land-dark**, pending local A/B). When true, on a verified first turn of a new session the temporal tier injects the most-recent episode's FULL `structured_summary.summary` (+ `open_threads`, truncated to `recall_parent_episode_truncate`) instead of titles-only. First-turn signal = `session_id not in _active_episodes` (survives LRU eviction). |
| `NOUS_EPISODE_OPEN_THREADS` | `false` | F083 B (**land-dark**, pending local A/B). When true, the episode summarizer extracts a top-level `open_threads` array (unfinished items / next steps) into `structured_summary`; raises summary `max_tokens` to 3000 so the extra field doesn't truncate the JSON. Consumed by A2. |
| `NOUS_FACT_FORMAT_MAX_CHARS` | `200` | Per-fact char cap in pre-turn context rendering (was hardcoded 200). Shared by Relevant Facts AND User Profile sections. |
| `NOUS_FACT_FORMAT_FULL_TOP_N` | `0` | Render the top-N Relevant Facts untruncated (0 = all capped). |
| `NOUS_FACT_PIN_TOP_K` | `0` | Pin top-K post-recency-resolve fact hits into pre-turn context past diversity/dedup/relevance demotion (0 = off; superseded-tagged facts never pinned). Counterfactual-injection fix, 2026-07-13 plan; flip gated on prod-generator A/B. |
| `NOUS_SUPERSESSION_LINEAGE_MODE` | `off` | Annotate injected facts that supersede an earlier fact: `tag` (generic marker) / `named` (quotes stale value — anchoring risk, A/B first) / `off`. |
| `NOUS_RECALL_BACKSTOP_ENABLED` | `false` | Inject a "call recall_deep before answering" instruction when pre-turn fact retrieval returns zero facts. |
| `NOUS_PROFILE_EXCLUDE_SOURCES` | `["reflection", "sleep_reflection"]` | Fact sources excluded (SQL-level, NULL-source facts always kept) from the Tier-1 User Profile section AND `GET /profile/facts`. Reflection lessons are lessons, not user profile data — 1,148 conf-1.0 reflection facts filled all 20 profile slots (2026-07-24 diagnosis). Empty list disables. Write paths no longer emit category `rule` from reflections. |
| `NOUS_PROFILE_CORE_ENABLED` | `false` | **Land-dark.** Core/intent profile split: the User Profile section renders ONLY `profile_core`-tagged facts (curated via dashboard/`POST /facts/{id}/core`) plus a probation window; tagged facts BYPASS the identity dedup (explicit curation outranks the heuristic). Zero tagged+probation facts → legacy top-N fallback. Flip post-deploy after initial tagging. |
| `NOUS_PROFILE_CORE_LIMIT` | `12` | Max facts in the curated core render (tagged first, then probation). |
| `NOUS_PROFILE_CORE_PROBATION_DAYS` | `14` | Untagged tier-1 facts learned within this window join the core render automatically (so a new universal preference never silently vanishes pre-curation). `0` disables probation. |
| `NOUS_PROFILE_INTENT_LEG_ENABLED` | `false` | **Land-dark.** Session Profile leg: hybrid search restricted to tier-1 categories on the turn's resolved query, staleness+relevance-filtered, rendered as a `dynamic`-tier `## Session Profile` section (never part of the cached prefix) with core/profile IDs excluded. Surfaces domain facts (trading, sailing) only on domain turns. |
| `NOUS_PROFILE_INTENT_LEG_LIMIT` | `5` | Max facts fetched/rendered by the Session Profile leg. |
| `NOUS_PROFILE_INTENT_LEG_BUDGET` | `300` | Token budget for the Session Profile section (line-aware truncation). `0` disables the leg. |
| `NOUS_PROFILE_INTENT_LEG_MIN_SCORE` | `0.7` | Absolute RRF-score floor for the Session Profile leg, applied before the adaptive relevance filter (which pads to ≥3 and can never return empty). 0.7 excludes vector-only nearest-neighbor noise (single-leg rank-1 tops out ~0.69 at default weights) while keeping dual-leg domain matches (~0.98). `0` disables the gate. |
| `NOUS_PROFILE_IDENTITY_DEDUP_SCOPE` | `line` | User Profile vs identity dedup (2026-07-23): `line` = directional per-line coverage (suppress a fact only when ONE identity line covers ≥75% of its meaningful words — fixes the P1 over-suppression that hid every post-initiation preference/person/rule fact; corrections sharing ~67% scaffolding words with the bullet they correct now survive); `blob` = legacy whole-identity overlap at 0.6 (kill-switch; also the fallback for unknown values). On a single-line prose identity the two modes coincide (deliberate no-op — prod identity verified multi-line bulleted 2026-07-23). |
| `NOUS_PROFILE_FACT_LIMIT` | `20` | Max preference/person/rule facts fetched for the Tier-1 User Profile section (was hardcoded 20). Section budget still applies after formatting; overflow now drops whole fact lines (never a mid-word slice), newest-first retained within equal confidence. |
| `NOUS_PROFILE_RECENCY_ENABLED` | `false` | **Land-dark.** Apply the pre-turn recency resolver (`_resolve_recency`: current/superseded tags + demotion sort on event_date conflicts) to Tier-1 User Profile facts. Requires `NOUS_RECENCY_RESOLVER_ENABLED=true` as well (already true in prod) — gated separately precisely so the Tier-1 pass does NOT go live on deploy day alongside the dedup fix. Flip after observing the dedup fix in prod. |
| `NOUS_EXTRACTION_ENUMERATIVE_ENABLED` | `false` | F084 R1 master switch. When true, episodes whose transcript passes the density heuristic route fact extraction through the enumerative leg (raw-transcript chunked extraction) INSTEAD of the lossy summarize-then-extract path. Modal routing — the two paths never run together for the same episode. |
| `NOUS_ENUMERATIVE_DENSITY_THRESHOLD` | `0.6` | F084 R1: statement-per-line density (0.0–1.0) above which a transcript is classified as enumerable. 0.6 is conservative — only clear list/table/log-form documents qualify. |
| `NOUS_ENUMERATIVE_MAX_FACTS_PER_EPISODE` | `1000` | F084 R1.3: hard cap on enumerative facts stored per episode. Truncation logs WARNING (never silent). 0 = unlimited. |
| `NOUS_ENUMERATIVE_MAX_CHUNKS_PER_EPISODE` | `200` | F084 R1: hard bound on extraction LLM calls per episode (one per chunk). Prevents runaway cost on pathologically large transcripts. Truncation logs WARNING. 0 = unlimited. |
| `NOUS_ENUMERATIVE_EXTRACTION_MAX_PER_HOUR` | `1000` | F084 R1: hourly in-process cap on enumerative extraction LLM calls (mirrors `*_max_per_hour` budget pattern). Budget spent → remaining chunks deferred to next episode. 0 = unlimited. |
| `NOUS_ENUMERATIVE_CLASSIFIER` | `heuristic` | F084 R1: density mode selection. `heuristic` = cheap regex-based density score (no LLM). `off` = never classify as enumerable (disables R1 without unsetting the master flag). `llm` reserved for v2. |
| `NOUS_ENUMERATIVE_MIN_CONTENT_CHARS` | `15` | F084 R1: min-content floor for `source='enumerative_extractor'` facts. Atomic statements are often <30 chars so the global 30-char floor is relaxed here. Source-aware: affects only the enumerative path. |
| `NOUS_SUPERSESSION_KEY_RESOLUTION_ENABLED` | `false` | F084 R2.1 master switch. When true, `_learn` performs an exact `(subject_key, attribute_key)` conflict lookup after each enumerative-keyed fact insert, and the sleep handler runs `_phase_sweep_key_conflicts`. |
| `NOUS_SUPERSESSION_POLICY` | `ordinal` | F084 R2.2 winner rule for UPDATE conflicts: `ordinal` (higher `source_ordinal` wins, same-episode only; falls back to `recency`) or `recency` (later `learned_at` wins). CONTRADICTION resolution always resolves by statement order (`_pick_contradiction_winner`: same-episode ordinal → later `learned_at` → KEEP-BOTH+flag) — the classifier's `current_fact` verdict is advisory only for CONTRADICTION and never picks the winner (Gate-1 D1, 2026-07-18: a memory store records what was said, not what the model believes is true). `authority` reserved. |
| `NOUS_SUPERSESSION_KEY_CANDIDATES_CAP` | `8` | F084 R2.1/RC-3: max same-key active candidates examined per insert (newest first). Bounds per-fact classifier cost. |
| `NOUS_SUPERSESSION_CLASSIFIER_MAX_PER_HOUR` | `500` | F084 R2.1/RC-5: hourly in-process cap on key-conflict F027 classifier (Haiku) calls. Budget spent → fail-open to KEEP-BOTH, defer to sleep sweep. 0 = unlimited (use for offline backfill). |
| `NOUS_SUPERSESSION_SWEEP_MAX_PAIRS` | `25` | F084 R2 sleep sweep: max same-key conflict pairs resolved per sleep cycle. Pairs beyond the cap wait for the next cycle (resumable: resolution deactivates losers so unprocessed pairs re-surface). |
| `NOUS_SAME_SLOT_CONFLICT_ROUTING_ENABLED` | `true` | F085 Gate-1 D2 kill-switch: same-`(subject_key, attribute_key)` pairs with differing values route to conflict resolution instead of dedup-drop. Default ON (correctness fix) — the pre-fix blind dedup-confirm at `fact_native_cosine_threshold` (0.95) was silently swallowing same-slot value updates before R2/the sleep sweep ever saw them. |
| `NOUS_OVERRIDE_PRIOR_MARKING_ENABLED` | `false` | F084 R2.4 (independently flippable). When true, facts with `overrides_prior=true` are prefixed `[memory override — trust this over general knowledge]` in pre-turn context rendering. Evidence: 12/12 MAB CR flip-failures were parametric fallbacks; the inoculation must sit AT the fact. Flip this first (DC-4 rollout order). |
| `NOUS_ENTITY_KEYS_MAX_PER_FACT` | `8` | F085 R3.1: max entity-key index rows per fact (subject key always included). |
| `NOUS_ENTITY_KEY_MIN_CHARS` | `3` | F085 R3.1: stop-policy floor - normalized entity keys shorter than this are not indexed (applies to subject keys too). |
| `NOUS_KEYED_FACT_LEG_ENABLED` | `false` | F085 R3.3 master switch: exact entity-key retrieval leg in `run_recall_pipeline`. Land-dark. |
| `NOUS_KEYED_FACT_LEG_K` | `8` | F085 R3.3: bounded allotment - max keyed facts merged per query. |
| `NOUS_KEYED_FACT_LEG_SCORE` | `0.55` | F085 R3.3: score band ceiling for keyed hits (RRF [0,1] scale, below the direct-hit head). |
| `NOUS_KEYED_FACT_LEG_ROUNDS` | `1` | F085 R3v2: keyed-leg retrieval rounds. 1 = v1 behavior (byte-identical); 2 enables the bounded iterative round (multi-hop composition). Land-dark. |
| `NOUS_KEYED_FACT_LEG_K2` | `8` | F085 R3v2: round-2 allotment - max round-2 keyed facts merged per query. |
| `NOUS_KEYED_FACT_LEG_R2_MAX_KEYS` | `32` | F085 R3v2 fan-out guard: max round-2 keys examined (truncation is counted, never silent). |
| `NOUS_KEYED_FACT_LEG_R2_MAX_CANDIDATES` | `256` | F085 R3v2 fan-out guard: hard cap on round-2 candidates fetched before ranking (the p90-587 lesson). |
| `NOUS_EXEMPLAR_EXTRACTION_ENABLED` | `false` | F086 write-path master switch: parse-only exemplar extraction of `utterance\nlabel: N` streams into individually-embedded facts (source='exemplar_extractor'). Zero LLM. |
| `NOUS_EXEMPLAR_DENSITY_THRESHOLD` | `0.8` | F086 exemplar_density score at/above which a transcript routes to exemplar extraction (checked before R1). |
| `NOUS_EXEMPLAR_MAX_PER_EPISODE` | `5000` | F086 cap on exemplar facts stored per episode; truncation logs WARNING (never silent). |
| `NOUS_EXEMPLAR_MIN_CONTENT_CHARS` | `5` | F086 source-aware min-content floor for exemplar facts (labels/utterances are short; the global 30-char floor would reject them). |
| `NOUS_EXEMPLAR_MODE_ENABLED` | `false` | F086 read-path master switch: exemplar retrieval leg in `run_recall_pipeline` (land-dark). |
| `NOUS_EXEMPLAR_TOP_K` | `25` | F086 max exemplars fetched/injected per query. |
| `NOUS_EXEMPLAR_LEG_SCORE` | `0.55` | F086 score-band ceiling for exemplar hits (below the RRF direct-hit head; per-rank decay 0.005). |
| `NOUS_EXEMPLAR_MIN_SIMILARITY` | `0.30` | F086 cosine floor — exemplars below this similarity are not merged (bounds false-trigger displacement, gate 2). |
| `NOUS_EXEMPLAR_MAX_QUERY_WORDS` | `64` | F086 trigger gate: queries longer than this many words are not classification-shaped. |

### Dashboard (Svelte v2)

The dashboard is a Svelte SPA under `dashboard-app/`. Build with `cd dashboard-app && npm run build` (or via the Docker `dashboard` build stage); output lands in `static/dashboard-v2/dist/` and is served at `/dashboard/v2/`. Visiting `/dashboard` or `/dashboard/` redirects there. The legacy vanilla-JS dashboard (`static/dashboard/js/`, `css/`, `index.html`) was retired 2026-06-19.

### REST Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/chat` | Send message, get response |
| POST | `/chat/stream` | SSE streaming chat |
| DELETE | `/chat/{session_id}` | End conversation |
| GET | `/status` | Agent status + memory stats + calibration |
| GET | `/decisions` | List recent decisions |
| GET | `/decisions/unreviewed` | Unreviewed decisions |
| POST | `/decisions/{id}/review` | Review a decision |
| GET | `/decisions/{id}` | Decision detail |
| GET | `/episodes` | List recent episodes |
| GET | `/facts?q=query` | Search facts |
| PUT | `/facts/{fact_id}` | Edit a Tier-1 fact via supersession (new versioned fact, re-embedded; reports merged_into_existing on dedup-swallow) |
| DELETE | `/facts/{fact_id}` | Deactivate (soft-delete) a Tier-1 fact |
| GET | `/profile/facts` | Tier-1 user-profile facts (preference/person/rule), prompt-order; `?core=true` filters to the curated core set |
| POST | `/facts/{fact_id}/core` | Toggle the `profile_core` curation tag on a Tier-1 fact (`{"core": true|false}`) |
| GET | `/censors` | Active censors |
| PUT | `/censors/{id}` | Update censor fields (trigger_action, action_instruction, unblock_pattern) |
| GET | `/procedures` | List procedures |
| GET | `/frames` | Available cognitive frames |
| GET | `/calibration` | Calibration report |
| GET | `/identity` | Get agent identity |
| PUT | `/identity/{section}` | Update identity section |
| POST | `/reinitiate` | Re-run initiation protocol |
| GET | `/health` | Health check |
| POST | `/sleep/trigger` | Trigger sleep cycle |
| GET | `/subtasks` | List subtasks |
| GET | `/subtasks/{id}` | Subtask detail |
| DELETE | `/subtasks/{id}` | Cancel a subtask |
| GET | `/schedules` | List schedules |
| POST | `/schedules` | Create a schedule |
| DELETE | `/schedules/{id}` | Deactivate a schedule |
| GET | `/admin/search-weights` | Get search weights |
| POST | `/admin/search-weights` | Set search weights |
| GET | `/rubric` | Current rubric |
| GET | `/rubric/history` | Rubric version history |
| GET | `/rubric/signals` | Outcome signals |
| GET | `/rubric/proposals` | List dimension proposals |
| POST | `/rubric/propose-dimension` | Propose a new dimension |
| POST | `/rubric/proposals/{id}/approve` | Approve a proposal |
| POST | `/rubric/rollback` | Rollback rubric version |
| POST | `/rubric/evolve` | Trigger rubric evolution |
| GET | `/dashboard/graph` | Graph visualization data |
| GET | `/dashboard/calibration` | Calibration dashboard data |
| GET | `/dashboard/activity` | Activity dashboard data |
| GET | `/dashboard/health` | Health dashboard data |
| GET | `/dashboard/rubric` | Rubric dashboard data |
| GET | `/dashboard/admission` | Admission control dashboard |
| GET | `/dashboard/admission/rejected` | Rejected admission entries |
| GET | `/dashboard/ledger` | Execution ledger dashboard data |
| GET | `/dashboard/heartbeat` | Heartbeat dashboard data |
| GET | `/dashboard/density` | Graph density dashboard data (F040) |
| GET | `/dashboard/retrieval` | F091 recent retrievals + window-level disposition/leg rollup |
| GET | `/dashboard/retrieval/{entry_id}` | F091 one retrieval's candidates (grouped by disposition) + graph-expansion edges |
| GET | `/dashboard/consolidation` | F035.6 recent consolidation cycles (sleep audit diff) |
| GET | `/dashboard/consolidation/{cycle_id}` | F035.6 one cycle's per-action diffs |
| GET | `/heartbeat/status` | Heartbeat status, checks, budget |
| POST | `/heartbeat/trigger` | Force immediate heartbeat tick |
| PUT | `/heartbeat/config` | Update heartbeat intervals/budget at runtime |
| POST | `/heartbeat/check/{name}/trigger` | Force a specific check to run |
| POST | `/heartbeat/check/{name}/reset` | Reset circuit breaker for a failed check |
| GET | `/heartbeat/findings` | All tracked findings with state/age |
| POST | `/heartbeat/findings/{fingerprint}/acknowledge` | Acknowledge a finding |
| POST | `/heartbeat/findings/{fingerprint}/resolve` | Resolve a finding |
| POST | `/heartbeat/findings/{fingerprint}/dismiss` | Dismiss a finding (strong negative) |
| PUT | `/heartbeat/escalation-policy` | Update escalation thresholds |
| GET | `/heartbeat/tuning-report` | Latest tuning report |
| POST | `/heartbeat/tune` | Force a tuning pass |
| GET | `/heartbeat/checks/dynamic` | List all dynamic checks |
| POST | `/heartbeat/checks/dynamic` | Create a new dynamic check |
| PATCH | `/heartbeat/checks/dynamic/{name}` | Update a dynamic check |
| DELETE | `/heartbeat/checks/dynamic/{name}` | Delete a dynamic check |
| POST | `/heartbeat/checks/dynamic/{name}/trigger` | Force-run a dynamic check |

### Agent Tools

| Tool | Frame Access | Description |
|------|-------------|-------------|
| `record_decision` | decision, task, debug, conversation, question | Record a decision with confidence + reasoning |
| `recall_deep` | all | Search memory (decisions, facts, episodes) |
| `recall_recent` | all | Retrieve recent memory items |
| `learn_fact` | conversation, question, creative, task | Store a new fact |
| `learn_skill` | conversation, question, task | Register a skill from URL, local path, or inline markdown |
| `ingest_document` | conversation, question, task, debug | F069: chunk & persist a full document body (arxiv, PDF/.docx text, long markdown) to heart.episode_chunks with source_kind='document'. Use after extracting text yourself via run_python / web_fetch. |
| `get_procedure` | all | Retrieve a specific procedure by ID |
| `create_censor` | all | Create a guardrail censor |
| `cache_retrieve` | all | Retrieve original content from SmartCompressed results |
| `bash` | task, debug, conversation, question | Execute shell commands |
| `read_file` | task, debug, question | Read file contents |
| `write_file` | task, creative | Write/create files |
| `spawn_task` | conversation, debug | Spawn a background subtask |
| `schedule_task` | conversation, debug | Schedule a recurring/one-shot task |
| `list_tasks` | conversation, question, decision, debug | List subtasks and schedules |
| `cancel_task` | conversation, question, decision, debug | Cancel a subtask or schedule |
| `web_search` | all | Search via multi-tier routing (Tavily/Exa/Brave) |
| `web_fetch` | all | Fetch and extract web content |
| `run_python` | conversation, question, debug, task | Execute Python with memory functions in scope |
| `send_file` | task, conversation, debug | Send files to Telegram (images as photos, rest as documents) |
| `heartbeat_check_create` | conversation, debug | Create a new dynamic heartbeat check (supports on_complete callback) |
| `heartbeat_check_manage` | conversation, debug | List, enable, disable, delete, or update dynamic checks |

## Git Workflow

- Work on feature branches, not main
- Commit messages: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`
- Keep commits focused — one logical change per commit
- All PRs need code review before merge

## References

- [Feature Index](docs/features/INDEX.md) — Current status of all features
- [Society of Mind](docs/research/002-minsky-mapping.md) — How Minsky maps to Nous
- [Database Design](docs/research/008-database-design.md) — Complete SQL for all tables
- [Storage Architecture](docs/research/004-storage-architecture.md) — Why Postgres + pgvector
- [Cognitive Layer](docs/research/005-cognitive-layer.md) — The seven systems
- [Automation Pipeline](docs/research/012-automation-pipeline.md) — Event bus design
