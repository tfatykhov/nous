# Nous Feature Index

## v0.1.0 — "The Thinking Agent"

### P0: Core Architecture
| Feature | Name | Status | Description |
|---------|------|--------|-------------|
| F001 | [Brain Module](F001-brain-module.md) | ✅ Shipped | Decision intelligence — recording, deliberation, calibration, guardrails, graph |
| F002 | [Heart Module](F002-heart-module.md) | ✅ Shipped | Memory system — episodic, semantic, procedural, working, censors |
| F003 | [Cognitive Layer](F003-cognitive-layer.md) | ✅ Shipped | The Nous Loop — frames, recall, deliberation, monitoring, end-of-session reflection |
| F004 | [Runtime](F004-runtime.md) | ✅ Shipped | Docker container, REST API, MCP interface, Telegram bot |
| F005 | [Context Engine](F005-context-engine.md) | ✅ Shipped | Frame-adaptive context assembly (implemented as `cognitive/context.py`) |
| F006 | [Event Bus](F006-event-bus.md) | ✅ Shipped | In-process async event bus, automated handlers |

### P1: Intelligence & Measurement
| Feature | Name | Status | Description |
|---------|------|--------|-------------|
| F007 | [Metrics & Growth](F007-metrics-growth.md) | Planned | 5-level measurement framework, weekly growth reports, automatic tracking |
| F008 | [Memory Lifecycle](F008-memory-lifecycle.md) | Planned | Auto lifecycle for all memory types — confirm, trim, archive, escalate, retire, **generalize** |

### P1: Capabilities
| Feature | Name | Status | Description |
|---------|------|--------|-------------|
| F009 | [Async Subtasks](F009-async-subtasks.md) | ✅ Shipped | Background task queue — parallel execution, non-blocking chat, Postgres-backed workers, scheduled/recurring tasks |
| F010 | [Memory Improvements](F010-memory-improvements.md) | ✅ Shipped | Episode summaries, clean decision descriptions, proactive fact learning, user-tagged episodes |
| F011 | [Skill Discovery](F011-skill-discovery.md) | ✅ Shipped | `learn_skill` tool acquires skills from URL/marketplace/local — registered as procedures, auto-surface in RECALL |
| F012 | [K-Line Learning](F012-kline-learning.md) | ✅ Shipped | Auto-create procedures from decision clusters, episode lessons, error recovery. 3 pathways: sleep-cycle clustering + real-time monitor recovery |
| F015 | [Subtask Hardening](F015-subtask-hardening.md) | ✅ Shipped | Timeout limits, concurrent limits, tool call limits, worker pool configuration |
| F020 | [Tool Output Intelligence](F020-tool-output-intelligence.md) | ✅ Shipped | SmartCompress (ingestion-time statistical compression) + ReversibleCache (Postgres-backed tool result caching) + `cache_retrieve` tool |
| F022 | [Graph-Augmented Recall](F022-graph-augmented-recall.md) | ✅ Shipped | Polymorphic graph edges, cross-type linking, contradiction bridge, density-gated spreading activation |
| F030 | [MMR Diversity Reranking](F030-mmr-diversity-reranking.md) | ✅ Shipped | Maximal Marginal Relevance diversity re-ranking in recall_deep, configurable relevance/diversity weight |
| F031 | Censor Middleware | ✅ Shipped | Censors execute read-only tools, conditional unblock, action payloads, censor update API |
| F033 | Multi-Tier Search Routing | ✅ Shipped | Tavily primary + Exa research + Brave fallback, query classification router |
| F025 | [Amnesia Prevention](F025-amnesia-prevention.md) | ✅ Shipped | Staleness exemptions, budget scaling, transcript 16K, dedup 0.92, source text passthrough, chunked summarization, transcript persistence |
| F051 | [Retrieval Eval Harness](F051-retrieval-eval-harness.md) | ✅ Shipped | Local-first retrieval evaluation: pipeline refactor + per-source qrels + paired A/B configs + persistent eval-DB Docker image + F050 gate-decision logic |
| F050 | [Multi-Query Expansion](F050-multi-query-expansion.md) | 🌑 Phase 1 (dark) | Haiku-driven query expansion behind `NOUS_QUERY_EXPANSION_ENABLED=false`. Module + cache + wiring + 64 tests landed; Phase 3 flag-flip gated on F051 harness MRR +7% |
| F064 | [Symphony Orchestration Adoptions](F064-symphony-orchestration-adoptions.md) | 🟡 v1 partial | Six DAG/skill orchestrator primitives from openai/symphony. **Shipped:** F064.1 stall detection (`last_activity_at` + 3-site activity ping incl. heartbeat-during-tool-dispatch), F064.2 per-frame-type DAG dispatch caps (subtask-only enforcement, scoped to current DAG), F064.3 workspace safety (sanitize-at-insert + unconditional read-time containment + hash-suffix collision-protection), F064.6 work-queue ingress (file_jsonl adapter + atomic claim + 5-min reconciler). **🟡 v1 partial:** F064.4 manifest persistence only — orchestrator consumer enforcement deferred to F064.4-v2; F064.5 Episode reuse only — LLM thread continuity deferred to F064.5-v2 (`runner.end_conversation` pops the in-memory Conversation between fires). All sub-features gated by per-flag env vars defaulting to off. PR #425. |

### P0: Identity & Context
| Feature | Name | Status | Description |
|---------|------|--------|-------------|
| F016 | [Context Pruning](F016-context-pruning-review.md) | ✅ Shipped | 4-tier tool pruning, anti-hallucination prompt, model-aware compaction, content-type decay profiles, pre-prune fact extraction |
| F017 | [Context Quality Gate](F017-context-quality-gate.md) | ✅ Shipped | Relevance floor, diminishing returns cutoff, staleness penalty, model-aware budget scaling, usage tracking |
| F018 | [Agent Identity](F018-agent-identity.md) | ✅ Shipped | DB-backed identity — initiation protocol, versioned sections, tiered context model |

### Implementation Specs

All shipped implementation specs with PR references:

| Spec | Name | Status | PR |
|------|------|--------|-----|
| 001 | Postgres Scaffold | ✅ Shipped | #1 |
| 002 | Brain Module | ✅ Shipped | #2 |
| 003 | Heart Module | ✅ Shipped | #3 |
| 003.1 | Heart Enhancements | ✅ Shipped | #6 |
| 003.2 | Frame-Tagged Encoding | ✅ Shipped | — |
| 004 | Cognitive Layer | ✅ Shipped | #10 |
| 004.1 | CEL Guardrails | ✅ Shipped | #10 |
| 005 | Runtime (REST + MCP + Runner) | ✅ Shipped | — |
| 005.1 | Smart Context Preparation | ✅ Shipped | — |
| 005.2 | Direct API Rewrite | ✅ Shipped | #15 |
| 005.3 | Web Tools | ✅ Shipped | #16 |
| 005.4 | Streaming Responses | ✅ Shipped | #23 |
| 005.5 | Noise Reduction | ✅ Shipped | #20 |
| 006 | Event Bus | ✅ Shipped | — |
| 006.2 | Context Quality | ✅ Shipped | #31 |
| 007 | Extended Thinking | ✅ Shipped | — |
| 007.1 | Thinking Indicators | ✅ Shipped | #53 |
| 007.2 | Topic-Aware Recall | ✅ Shipped | #55 |
| 007.3 | Improve _is_informational() | ✅ Shipped | #55 |
| 007.4 | Fix Unpopulated Columns | ✅ Shipped | #55 |
| 007.5 | Recall Min Threshold | ⏸ Reverted | #59 — superseded by 008 |
| 008 | Agent Identity & Tiered Context | ✅ Shipped | #60, #61, #62 — F018 identity + tiered context + API |
| 008.1-P1 | Tool Output Pruning + Token Estimation | ✅ Shipped | #69 |
| 008.1-P2 | History Compaction Core | ✅ Shipped | #70 |
| 008.1-P3 | Durable Integration (persistence, events, knowledge extraction) | ✅ Shipped | #71 |
| 008.1-P4 | Adaptive Compaction | 📋 Specced | — |
| 008.2 | Topic-Aware Recall v2 | 📋 Specced | — full spec deferred; spike merged |
| 008.3 | Episode Summary Backfill & Lifecycle | ✅ Shipped | #79 — backfill unsummarized episodes, active flag lifecycle |
| 008.4 | Episode Summary Quality | ✅ Shipped | — enhanced prompt, candidate_facts, smart truncation, decision context |
| 008.5 | Decision Review Loop | ✅ Shipped | #81 — auto-review signals, REST endpoints, calibration snapshots |
| 009.1-009.4 | Memory Lifecycle Implementation | 📦 Shelved | — system too young (53 facts, 86 episodes at time of assessment) |
| 008.6 | Temporal Recall | ✅ Shipped | — dual-path retrieval: time-based + semantic. Fixes cross-domain recall gap |
| 009.5 | Decision Quality Gate | ✅ Shipped | #92 — 3-layer filter: source filtering, dedup window, quality gate. Fixes 43% noise rate |
| 010.1 | Health Dashboard (F007 Phase 1) | ✅ Shipped | — implemented as part of F021 dashboard (/dashboard/health endpoint) |
| — | Streaming Keepalive + Tool Timeout | ✅ Shipped | #73 — keepalive during Anthropic wait, `NOUS_TOOL_TIMEOUT` |
| — | Typing Indicator Fix | ✅ Shipped | — continuous typing via background task |
| — | Topic Persistence Spike | ✅ Shipped | #75 — `_resolve_focus_text()` follow-up detection |
| — | Deliberation Thinking Capture | ✅ Shipped | #76 — extended thinking blocks → `brain.thoughts`, garbage cleanup |
| — | Phase 1 Voice | ✅ Shipped | — 3 procedures (send_email, notify_tim, talk_to_emerson) + 2 censors |
| — | RRF Score Fix | ✅ Shipped | #64 — use original hybrid scores instead of RRF ranking |
| — | Query Deduplication Fix | ✅ Shipped | — prevent doubled query when topic = input |
| — | Tier 3 Threshold Tuning | ✅ Shipped | #66 — decision threshold 0.3→0.20 |
| — | Timezone Fix (ORM models) | ✅ Shipped | #87 — DateTime(timezone=True) on all 27 timestamp columns |
| — | /new Session Ending | ✅ Shipped | #88 — /new now calls DELETE /chat/{session_id}, fires session_ended for all handlers |
| — | Periodic Decision Sweep | ✅ Shipped | #89 — background asyncio loop, configurable interval (default 1hr) |
| 011.1 | Subtasks & Scheduling | ✅ Shipped | #85 — F009: subtask queue, worker pool, scheduling, time parser, 4 tools, 6 endpoints |
| 011.2 | Subtask Result Delivery | ✅ Shipped | — subtask results auto-injected into parent session context, skip_episode for workers, delivered tracking |
| 014.1 | Context Quality Engine (F016+F017) | ✅ Shipped | #122 — 4-tier pruning, relevance floor, staleness penalty, model-aware thresholds, usage tracking, pre-prune extraction |
| 014.2 | Tool Output Intelligence (F020) | ✅ Shipped | #124 — SmartCompress ingestion-time compression + ReversibleCache Postgres-backed tool result storage |
| 015 | Graph-Augmented Recall (F022) | ✅ Shipped | — polymorphic edges, 1-hop expansion, cross-type linking, contradiction bridge, spreading activation |
| F011 | Skill Discovery v2 | ✅ Shipped | — learn_skill tool, SkillParser, bootstrap, FRAME_TOOLS wiring |
| F012 | K-Line Learning | ✅ Shipped | #134 — auto-create procedures from decision clusters, episode lessons, error recovery |
| F021 | Memory Dashboard | ✅ Shipped | #159 — SPA with overview, browser, graph, calibration, activity, health panels |
| F021.1 | Admission Dashboard | ✅ Shipped | #165 — admission analytics, scoring breakdown, histogram, rejected facts browser, threshold simulator |
| F024 | Critic Agent Phase 0 | ✅ Shipped | #192 — smart frame selector, LLM classification, 6 diagnostic critics |
| F024-3b | Self-Modifying Rubrics | ✅ Shipped | #196 — outcome signals, dimension proposals, rubric evolution, dashboard |
| F026 | Execution Integrity | ✅ Shipped | #183 — execution ledger, tiered action gating, claim verification |
| F030 | MMR Diversity Reranking | ✅ Shipped | #205 — Maximal Marginal Relevance in recall_deep |
| F031 | Censor Middleware | ✅ Shipped | #208 — censor action payloads, read-only tool execution, conditional unblock |
| F031-b | Consolidation Orient & Resolve | ✅ Shipped | #232 — orient context injection in sleep reflection, contradiction resolution, fact supersession |
| F032 | Execution Ledger Dashboard | ✅ Shipped | — per-action visibility, status filtering, side-effect classification |
| F033 | Multi-Tier Search Routing | ✅ Shipped | — Tavily + Exa + Brave, query classification router |
| F034 | Heartbeat Monitoring | ✅ Shipped | #236 — tick loop, health/email/self-initiated checks, triage |
| F034.1 | Finding Lifecycle | ✅ Shipped | #241 — fingerprint dedup, state machine, escalation, daily digest |
| F034.2 | Intelligent Checks | ✅ Shipped | #241 — embedding search, LLM email classification, tunable params |
| F034.3 | Self-Tuning Heartbeat | ✅ Shipped | #241 — outcome-driven adjustment, cross-cycle rollback, pinned params |
| F034.4 | Heartbeat Completions | 📋 Proposed | #242 — consolidates remaining F034.1–F034.3 gaps: suppression TTL, FindingStore persistence, email dedup migration |
| F034.5 | Dynamic Heartbeat Checks | ✅ Shipped | #252 — prompt-driven checks, conversational creation, tool_filter, REST CRUD, periodic sync |
| F034.6 | on_complete Callback | ✅ Shipped | #275 — callback prompt on self-disable, 3-layer failure handling, background execution |
| F036 | Prompt Cache Optimization | ✅ Shipped | #253 — 3-tier system prompt split, cache break detection, single breakpoint, tool schema cache |
| F038 | Memory Quality Fixes | ✅ Shipped | #258 — quality gate 0.55, fact 30-char min, procedure floor, episode recency, admission bonus, task synthesis, context dedup, bash hints |
| 012.3 | Programmatic Tool Calling | ✅ Shipped | — run_python tool with memory functions in scope |
| 011.2 | Multimodal File Support | 📋 Draft | — image/document processing across input channels |
| 012.1 | Frame Splitting | 📋 Specced | — parallel cognitive frames via sub-agents (deferred to F024) |
| 012.2 | Subtask Enhancements Light | 📋 Specced | — replaces 012.1 with lighter subtask improvements |

### P1: Cognitive Enhancement
| Feature | Name | Status | Description |
|---------|------|--------|-------------|
| F023 | [Memory Admission Control](F023-memory-admission-control.md) | ✅ Shipped | 5-dimension scoring (utility, confidence, novelty, recency, type_prior), LLM-based utility scoring. Running in shadow mode. |
| F024 | [Critic Agent](F024-critic-agent.md) | Phase 0 ✅ | Smart frame selector (B-Brain) — LLM classification, 6 diagnostic critics, shadow mode. Phase 1+ (parallelism) planned. |
| F024-3b | [Self-Modifying Rubrics](F024-phase3b-self-modifying-rubrics.md) | ✅ Shipped | Data-driven rubric evolution — outcome signals, dimension proposals, approval flow, rollback, dashboard tab |
| F026 | [Execution Integrity](F026-execution-integrity.md) | ✅ Shipped | Execution ledger, tiered action gating (read/write/external/irreversible), claim verification, ghost planning detection |
| F032 | [Execution Ledger Dashboard](F032-execution-ledger-dashboard.md) | ✅ Shipped | Per-action visibility, status filtering, timeline view, side-effect classification |

### P1: Observability & Dashboard
| Feature | Name | Status | Description |
|---------|------|--------|-------------|
| F021 | [Memory Dashboard](F021-memory-dashboard.md) | ✅ Shipped | Full SPA — overview, memory browser, graph visualization, calibration, activity timeline, health metrics |
| F021.1 | [Admission Dashboard](F021-admission-dashboard.md) | ✅ Shipped | Admission analytics panel — shadow mode visibility, scoring breakdown, histogram, rejected facts browser, threshold simulator |
| F036 | [Prompt Cache Optimization](F036-prompt-cache-optimization.md) | ✅ Shipped | 3-tier system prompt split, cache break detection, single breakpoint strategy, tool schema caching per frame |

### P1: Proactive Autonomy
| Feature | Name | Status | Description |
|---------|------|--------|-------------|
| F034 | [Heartbeat Monitoring](F034-heartbeat-proactive-monitoring.md) | ✅ Shipped | Proactive tick loop, health/email/self-initiated checks, triage, Telegram alerts |
| F034.1 | [Finding Lifecycle](F034.1-finding-lifecycle.md) | ✅ Shipped | Fingerprint dedup, state machine (new→ack→resolved), escalation, daily digest, outcome signals |
| F034.2 | [Intelligent Checks](F034.2-intelligent-checks.md) | ✅ Shipped | Embedding search, LLM email classification, drive significance, tunable params |
| F034.3 | [Self-Tuning Heartbeat](F034.3-self-tuning-heartbeat.md) | ✅ Shipped | Outcome-driven parameter adjustment, cross-cycle rollback, pinned params |
| F034.4 | [Heartbeat Completions](F034.4-heartbeat-completions.md) | 📋 Proposed | Consolidates remaining F034.1–F034.3 gaps: suppression TTL, FindingStore persistence, email→FindingStore migration, rollback threshold fix |
| F034.5 | [Dynamic Heartbeat Checks](F034.5-dynamic-heartbeat-checks.md) | ✅ Shipped | Prompt-driven checks, conversational creation/management, DB-backed persistence, full lifecycle integration |
| F034.6 | on_complete Callback | ✅ Shipped | Callback prompt executes on self-disable, 3-layer failure handling (retry, Telegram, Finding), background execution |

### P1: Memory Quality
| Feature | Name | Status | Description |
|---------|------|--------|-------------|
| F020 | [Tool Output Intelligence](F020-tool-output-intelligence.md) | ✅ Shipped | SmartCompress (ingestion-time statistical compression) + ReversibleCache (Postgres-backed tool result caching) + `cache_retrieve` tool |
| F031-b | [Consolidation Orient & Resolve](F031-consolidation-orient-resolve.md) | ✅ Shipped | Orient context injection in sleep reflection — checks existing facts before extracting. Contradiction resolution phase with fact supersession |
| F038 | Memory Quality & Context Loading Fixes | ✅ Shipped | Quality gate 0.55, fact 30-char min, procedure floor 0.40, episode recency weighting, user_direct admission bonus, task synthesis, context dedup, bash batching hints |
| F040 | [Graph Densification](F040-graph-densification.md) | ✅ Shipped | Orphan backfill engine, reverse linking (decision/procedure/episode), per-relation thresholds, edge confidence scoring, cluster discovery, density dashboard |
| F042 | [Cross-Encoder Reranking](F042-cross-encoder-reranking.md) | ✅ Shipped | Cross-encoder reranking stage in recall_deep — sigmoid-normalized scores, async executor, head-truncation, feature-flagged, optional sentence-transformers dep |
| F043 | [CE Rerank Sleep Backfill](F043-ce-rerank-sleep-backfill.md) | ✅ Shipped | Cross-encoder reranking applied to F040 graph backfill during sleep — precision pre-filter before cosine gate, reuses F042 reranker, feature-flagged, `_ce_stats` telemetry |
| F044 | [tinyHippo-Lite: Algorithmic Sleep Consolidation](F044-tinyhippo-lite.md) | 📝 Draft | Pure-Python lift of tinyHippo STC + homeostatic downscale (α=0.75, PRP≥3) onto Nous graph. Two-tier edge state (tagged/consolidated), three new sleep phases (8c promote, 8d downscale, 8e replay telemetry), falsification harness (Run A/B/C), optional HDF5 output schema-compatible with Max's MareNostrum5 runs. Gap-independent: no NEST, no .h5 input, no topology mapping. Sibling to F041, not sub-feature. |
 [CE-Aware Thresholds](F045-ce-aware-thresholds.md) | ✅ Shipped | Relaxed per-relation cosine thresholds + 80-char content guard for CE backfill. `fact_fact=0.65` empirically validated at 80% LLM-judged precision on 2026-04-14 A/B. Routes to CE-mode thresholds only when `ce_backfill_enabled=True`. |
| F046 | [DAG Node Timeout Config](F046-dag-node-timeout-config.md) | ✅ Shipped | Env-var-driven DAG node timeouts: `NOUS_DAG_NODE_DEFAULT_TIMEOUT=600`, `NOUS_DAG_NODE_MAX_TIMEOUT=7200`. `DAGNodeSpec.timeout_seconds` becomes `int \| None`; Settings injected into `DAGStore` + `DAGOrchestrator`; defensive clamp at all 3 orchestrator read sites. Unblocks long-running DAG nodes (Claude Code, deep-research) previously capped at 600 s. Closes #327. |
| F047 | [Actionability Classification](F047-actionability-classification.md) | ✅ Shipped | Learn-time classifier persists `actionable: bool \| None` + `actionable_confidence` on `heart.facts`, replacing the `_OBSERVATION_PATTERNS` arms-race at heartbeat read time. Three tiers: category/tag hard filter → positive-wins heuristic → Haiku LLM. Backfill handler with PG advisory lock + supervision wrapper. Heartbeat now prefers persisted verdict with positive-wins fallback for NULL rows, fixing the PR #335 short-circuit bug. Supersedes PR #335. |
| F048 | [Background Streaming + TCP Keep-Alive](F048-background-streaming-keepalive.md) | ✅ Shipped | Subtask and heartbeat turns now stream under the hood via `call_streaming_aggregated` on both `HttpxAnthropicClient` and `SdkAnthropicClient` — incremental SSE bytes keep the TCP socket warm so long (multi-minute) background generations no longer hit idle-connection drops on proxies/load-balancers. `httpx.AsyncHTTPTransport` gains `SO_KEEPALIVE` + platform-appropriate `TCP_KEEP{IDLE,INTVL,CNT}` via a new `_build_socket_options` helper (Linux `TCP_KEEPIDLE`, macOS `TCP_KEEPALIVE`, Windows SO_KEEPALIVE-only with warning). `AgentRunner.run_turn(is_background=True)` threads through `_tool_loop` to every `_call_api` iteration; routed to streaming-aggregated on 5 call sites: subtask worker, heartbeat cognitive triage, on_complete callback, dynamic check run, and inline `spawn_task(await_result)`. Truncated-stream detection raises `RuntimeError` instead of silently returning empty content. Gated by `NOUS_API_BACKGROUND_STREAMING_ENABLED=true` + `NOUS_API_SOCKET_KEEPALIVE_ENABLED=true`. Also bumped `NOUS_SUBTASK_DEFAULT_TIMEOUT` 120→600 and `NOUS_SUBTASK_MAX_TIMEOUT` 900→3600 to match the 600s per-chunk read budget. Fixed pre-existing censor-block 2-tuple return bug at `runner.py:249`. |
| F049 | [Session & Memory Lifecycle Hygiene](F049-session-lifecycle-hygiene.md) | ✅ Shipped | Closes #187 (primary) + scoped #166 (safety net). Mechanism B: `_execute_subtask` wraps body in `try/finally` calling `end_conversation` via `asyncio.shield(asyncio.wait_for(..., NOUS_SUBTASK_CLEANUP_TIMEOUT_SECONDS=30))` with three distinct except branches (`TimeoutError`/`CancelledError`/`Exception`) at ERROR severity — no more leaked subtask sessions, bounded cleanup. Mechanism A: new `WorkingMemoryManager.cleanup_stale()` deletes stale `heart.working_memory` rows via `ctid IN (SELECT … LIMIT N)` batched DELETE under `pg_try_advisory_xact_lock` keyed on SHA-256 of `agent_id` (cross-process stable; builtin `hash()` would be per-process randomized). `SessionTimeoutMonitor.__init__` grows `heart: Heart \| None` kwarg; sweep invoked from `_check_timeouts` at most once per `NOUS_WORKING_MEMORY_SWEEP_INTERVAL_SECONDS`. 3-agent review (architecture + correctness + silent-failure) caught: dropped Mechanism C (`NousTelegramBot` is out-of-process, cannot share `EventBus`); added bounded `wait_for` + `shield` + ERROR logging; added advisory lock + LIMIT batching. 13 new tests; targets 86/87 stale rows observed in 2026-04-20 audit. #184 deferred — existing lazy TTL (PR #185) is the de-facto fix. |

### Phase 2 — Quality (next to build)

| Feature | Name | Priority | Description |
|---------|------|----------|-------------|
| #38 | _is_informational() Phase 2 | P1 | Partially addressed by PR #76 (delete instead of abandon). Further tuning possible. |
| #52 | Topic-Aware Recall v2 | P1 | Spike merged (#75). Full 008.2 spec exists if spike proves insufficient. |
| F025 | [Amnesia Prevention](F025-amnesia-prevention.md) | P1 | 7 root causes identified (over-filtered retrieval, tiny limits, naive grouping). Partial mitigations in context.py. |
| F027 | [Supersession Detection](F027-supersession-detection.md) | Partial | Basic subject-based supersession shipped. Missing: retrieval-time suppression, periodic conflict scanning, LLM conflict classification. |
| F033-b | [Subtask Completion Validation](F033-subtask-completion-validation.md) | P1 | Prevent false "completed" status — validation gates before marking subtasks done. |

### Phase 3 — Growth

| Feature | Name | Priority | Description |
|---------|------|----------|-------------|
| F007 | Metrics & Growth | P2 | Calibration, Brier scores, outcome tracking. Decision data now clean (27 real decisions). |
| F008 | Memory Lifecycle | P2 | Shelved — system too young. Revisit when data grows. Specs 009.1-009.4 written. |
| 008.1-P4 | Adaptive Compaction | P2 | LLM-powered summarization with configurable triggers. Spec written. |

### Future

| Feature | Name | Description |
|---------|------|-------------|
| F013 | Frame Splitting | Parallel cognitive frames via sub-agents |
| F014 | Model Router | LLM portability via proxy layer |
| F019 | [Nous Website](F019-nous-website.md) | Developer-first open-source framework site (mem-brain.ai) |
| F024.1 | [DAG Decomposition](F024-dag-decomposition.md) | Phase 1a/1b: task decomposition + competing execution for critic agent |
| F028 | [Context Demand Paging](F028-context-demand-paging.md) | OS-inspired 4-level memory hierarchy with retrieval handles and demand loading |
| F029 | [Trajectory Learning](F029-trajectory-learning.md) | Post-execution tip extraction from failure traces and optimization patterns |

## Stats

- **Total source:** ~35,200 lines of production Python + ~36,800 lines of tests (~72K total)
- **Test count:** 2,006 tests across 106 test files
- **Database:** 28 tables across 3 schemas (brain, heart, nous_system), 20 migrations
- **Tools:** 23 agent tools (record_decision, recall_deep, recall_recent, learn_fact, learn_skill, get_procedure, create_censor, cache_retrieve, spawn_task, schedule_task, list_tasks, cancel_task, run_python, bash, read_file, write_file, web_search, web_fetch, send_file, store_identity, complete_initiation, heartbeat_check_create, heartbeat_check_manage)
- **Endpoints:** 62 REST endpoints + 5 MCP tools + Telegram bot
- **Event handlers:** 13 automated handlers (decision review, episode summary, fact extraction, knowledge extraction, fact graph linking, outcome detection, procedure learning, rubric evolution, session monitoring, sleep/reflection, subtask workers, task scheduling, time parsing)
- **Feature specs:** 41 feature docs + 19 research notes
- **Voice:** 3 communication procedures (email, Telegram, A2A) + 2 censors
- **Modules:** 85 Python modules across 10 packages (api, brain, cognitive, handlers, heart, heartbeat, identity, integrations, skills, storage)

## Research Notes

| # | Title | Key Topic |
|---|-------|-----------|
| [001](../research/001-foundations.md) | Foundations | Problem statement, Nous hypothesis |
| [002](../research/002-minsky-mapping.md) | Minsky Mapping | 14 chapters → Nous components |
| [003](../research/003-runtime-decision.md) | Runtime Decision | Claude Agent SDK + model router |
| [004](../research/004-storage-architecture.md) | Storage Architecture | Postgres + pgvector, swappable backends |
| [005](../research/005-cognitive-layer.md) | Cognitive Layer | The seven systems |
| [006](../research/006-v01-features.md) | v0.1.0 Features | Initial feature plan |
| [007](../research/007-memory-integration.md) | Memory Integration | 5 memory types, CE integration |
| [008](../research/008-database-design.md) | Database Design | 27 tables, 3 schemas, full SQL |
| [009](../research/009-context-management.md) | Context Management | Token budgets, relevance scoring |
| [010](../research/010-summarization-strategy.md) | Summarization | 3-tier compression, episode lifecycle |
| [011](../research/011-measuring-success.md) | Measuring Success | 5-level metrics, growth reports |
| [012](../research/012-automation-pipeline.md) | Automation Pipeline | Event bus, 13 handlers, full wiring |
| [013](../research/013-langchain-memory-lessons.md) | LangChain Memory Lessons | 5 takeaways: reflection, generalization, validation, approval gates |
| [014a](../research/014-acteon-action-gateway.md) | Acteon | Action gateway for agent coordination |
| [014b](../research/014-group-evolving-agents.md) | GEA | Experience sharing for open-ended self-improvement |
| [015](../research/015-deep-thinking-ratio.md) | DTR | Measuring real reasoning effort, not token count |
| [016](../research/016-agent-memory-synthesis.md) | Agent Memory Synthesis | 9 papers on LLM agent memory (2025-2026) — retrieval, consolidation, generalization |
| [017](../research/017-agent-memory-march2026.md) | Agent Memory Update | March 2026 field update — latest memory research |
| [018](../research/memristor-actor-critic-simulation-spec.md) | Memristor Actor-Critic | Analogue memristor actor-critic simulation spec (Lammie et al. 2025) |

## Architecture Summary

![Nous Architecture](../nous-architecture.png)

## Database: 28 Tables, 3 Schemas, 20 Migrations

| Schema | Tables | Purpose |
|--------|--------|---------|
| `brain` (8) | decisions, decision_tags, decision_reasons, decision_bridge, thoughts, graph_edges, guardrails, calibration_snapshots | Decision intelligence |
| `heart` (13) | episodes, episode_decisions, episode_procedures, facts, procedures, rubric_versions, outcome_signals, censors, working_memory, conversation_state, subtasks, schedules, tool_cache | Memory system |
| `nous_system` (7) | agents, agent_identity, config, dynamic_checks, events, frames, schema_migrations | System infrastructure |

Migrations (006→029): event bus, agent identity, conversation state, decision review, subtasks/schedules, subtask delivery, frame typing, tool cache, notification defaults, schedule frames, polymorphic graph edges, admission control, dashboard indexes, admission scores, episode compaction, config table, rubric/outcome signals, procedure search, censor action payloads, episode transcript, observability, dynamic checks, quality guardrail threshold, on_complete callback.
