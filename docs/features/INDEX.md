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
| F009 | [Async Subtasks](F009-async-subtasks.md) | ✅ Shipped | Background/inline tasks, schedules, result delivery, worker guardrails |
| F010 | [Web Browsing](F010-web-browsing.md) | Planned | Deep web browsing, multi-page research, content extraction |
| F011 | [Proactive Intelligence](F011-proactive.md) | Planned | Schedule-driven monitoring, news watching, autonomous research |
| F012 | [A2A Protocol](F012-a2a-protocol.md) | Planned | Google A2A for multi-agent communication |
| F013 | [Frame Splitting](F013-frame-splitting.md) | Partial | Full spec deferred; 012.2 shipped subtask-level frame_type as lightweight alternative |
| F018 | [Agent Identity](F018-agent-identity.md) | ✅ Shipped | Identity tiering, values, protocols, preferences, initiation ceremony |
| F019 | [Nous Website](F019-nous-website.md) | 📋 Specced | Public website, Minsky-first narrative, interactive demos |

---

## Current Stats

| Metric | Value |
|--------|-------|
| Source code | ~19,600 lines Python (source) · ~60,100 total with tests |
| Tests | 1,052 tests across 51 files |
| Database | 22 tables, 3 schemas (`brain`, `heart`, `nous_system`) |
| LLM Tools | 15 (`record_decision`, `recall_deep`, `recall_recent`, `learn_fact`, `create_censor`, `run_python`, `spawn_task`, `schedule_task`, `list_tasks`, `cancel_task`, `bash`, `read_file`, `write_file`, `web_search`, `web_fetch`) |
| REST endpoints | 23 |
| Docs | 14 feature docs · 38 implementation specs · 17 research notes |

---

## Implementation Roadmap

### Phase 1: Foundation (Shipped)

| Spec Area | Status | Key Deliverables |
|-----------|--------|-----------------|
| Decision Intelligence (001-002) | ✅ Shipped | Brain schema, recording, calibration, guardrails, graph edges |
| Episodic Memory (003) | ✅ Shipped | Episodes, facts, procedures, censors, frame encoding |
| Cognitive Frames (004-004.1) | ✅ Shipped | 8 frame types, CEL guardrails, mode-specific behavior |
| Runtime & API (005) | ✅ Shipped | Docker, REST, MCP, direct API, smart context prep |
| Event System & Observability (006) | ✅ Shipped | In-process event bus, 7 handlers, observability hooks |
| Context Quality (006.2) | ✅ Shipped | Dedup, budget tracking, quality scoring |
| Extended Thinking (007) | ✅ Shipped | `thinking` blocks in prompts, thinking indicators |
| Context Recall (007.2-007.4) | ✅ Shipped | Topic-aware recall, is_informational, unpopulated cleanup |
| Agent Identity (008/F018) | ✅ Shipped | Identity system, tiered context, initiation ceremony |
| Conversation Compaction (008.1) | ✅ Shipped | Rolling compaction, summary insertion |
| Streaming & Reliability | ✅ Shipped | SSE streaming, backpressure, graceful errors |
| Topic Persistence | ✅ Shipped | Topic-aware recall v2 (008.2), multi-scope recall |
| Deliberation Capture | ✅ Shipped | `thinking` block extraction, structured traces |
| Episode Summary Quality (008.3-008.4) | ✅ Shipped | Backfill + enhanced prompt, candidate_facts, smart truncation, decision context |
| Decision Review Loop (008.5) | ✅ Shipped | Periodic review sweeps, outcome tracking |
| Temporal Recall (008.6) | ✅ Shipped | `recall_recent` tool, time-based episode retrieval |
| Decision Quality Gate (009.5) | ✅ Shipped | Pre-record validation, confidence checks, duplicate detection |
| Context Dedup & Cleanup | ✅ Shipped | Fact category cleanup, context deduplication (PR #101) |
| Exa.ai Fallback Search | ✅ Shipped | Exa.ai as fallback when Brave Search unavailable |

### Phase 2: Capabilities (In Progress)

| Spec Area | Status | Key Deliverables |
|-----------|--------|-----------------|
| **Subtasks (011.1)** | ✅ Shipped | spawn_task, schedule_task, worker pool, result delivery |
| **Subtask Result Delivery** | ✅ Shipped | await_result inline, push via Telegram/email |
| **Subtask Enhancements (012.2)** | ✅ Shipped | frame_type per subtask, model override, shared prefix builder, worker guardrails (no-nesting, tool limit, timeout) |
| **Programmatic Tool Calling (012.3)** | ✅ Shipped | `run_python` tool, sandboxed execution, stdlib whitelist, async bridge |
| **Working Memory Threads** | ✅ Shipped | Thread-linked working memory items (PR #100) |
| **Scripting Indicator** | ✅ Shipped | 💻 indicator for run_python execution (PR #112) |
| Web Browsing (F010) | Planned | Multi-page research, content pipelines |
| Multimodal File Support (011.2) | 📋 Specced | Image/PDF/audio upload, extraction, memory storage |
| Proactive Intelligence (F011) | Planned | Autonomous monitoring, news watching |
| A2A Protocol (F012) | Planned | Google A2A, agent card, task negotiation |

### Phase 3: Growth & Measurement

| Spec Area | Status | Key Deliverables |
|-----------|--------|-----------------|
| Memory Lifecycle (F008) | Specced | 009.1-009.4: reactive lifecycle, maintenance engine, episode enhancements, fact generalization |
| Metrics & Growth (F007) | Planned | 5-level measurement, weekly reports |
| Health Dashboard (010.1) | 📋 Specced | System health, memory stats, decision analytics |

### Future

| Spec Area | Status | Notes |
|-----------|--------|-------|
| Frame Splitting (012.1/F013) | Deferred | Full spec written; 012.2 shipped lightweight alternative (frame_type per subtask) |
| Nous Website (F019) | 📋 Specced | Public site, Minsky narrative, interactive demos |

---

## Implementation Specs

| # | Spec | Status | PR |
|---|------|--------|----|
| 001 | [Postgres Scaffold](../implementation/001-postgres-scaffold.md) | ✅ Shipped | #1 |
| 002 | [Brain Module](../implementation/002-brain-module.md) | ✅ Shipped | #4 |
| 003 | [Heart Module](../implementation/003-heart-module.md) | ✅ Shipped | #9 |
| 003.1 | [Heart Enhancements](../implementation/003.1-heart-enhancements.md) | ✅ Shipped | #19 |
| 003.2 | [Frame-Tagged Encoding](../implementation/003.2-frame-tagged-encoding.md) | ✅ Shipped | #22 |
| 004 | [Cognitive Layer](../implementation/004-cognitive-layer.md) | ✅ Shipped | #12 |
| 004.1 | [CEL Guardrails](../implementation/004.1-cel-guardrails.md) | ✅ Shipped | #14 |
| 005 | [Runtime](../implementation/005-runtime.md) | ✅ Shipped | #16 |
| 005.1 | [Smart Context Prep](../implementation/005.1-smart-context-preparation.md) | ✅ Shipped | #25 |
| 005.2 | [Direct API Rewrite](../implementation/005.2-direct-api-rewrite.md) | ✅ Shipped | #27 |
| 005.3 | [Web Tools](../implementation/005.3-web-tools.md) | ✅ Shipped | #31 |
| 005.4 | [Streaming Responses](../implementation/005.4-streaming-responses.md) | ✅ Shipped | #33 |
| 005.5 | [Noise Reduction](../implementation/005.5-noise-reduction.md) | ✅ Shipped | #52 |
| 006 | [Event Bus](../implementation/006-event-bus.md) | ✅ Shipped | #36 |
| 006.1 | [Event Bus Observability](../implementation/006.1-event-bus-observability.md) | ✅ Shipped | #40 |
| 006.2 | [Context Quality](../implementation/006.2-context-quality.md) | ✅ Shipped | #54 |
| 007.1 | [Thinking Indicators](../implementation/007.1-thinking-indicators.md) | ✅ Shipped | #43 |
| 007.2 | [Topic-Aware Recall](../implementation/007.2-topic-aware-recall.md) | ✅ Shipped | #45 |
| 007.3 | [Is Informational](../implementation/007.3-is-informational.md) | ✅ Shipped | #47 |
| 007.4 | [Unpopulated Columns](../implementation/007.4-unpopulated-columns.md) | ✅ Shipped | #48 |
| 008 | [Identity & Tiered Context](../implementation/008-identity-and-tiered-context.md) | ✅ Shipped | #56 |
| 008.1 | [Conversation Compaction](../implementation/008.1-conversation-compaction.md) | ✅ Shipped | #60 |
| 008.2 | [Topic Recall v2](../implementation/008.2-topic-recall-v2.md) | ✅ Shipped | #63 |
| 008.3 | [Episode Summary Backfill](../implementation/008.3-episode-summary-backfill.md) | ✅ Shipped | #72 |
| 008.4 | [Summary Quality](../implementation/008.4-summary-quality.md) | ✅ Shipped | #72 |
| 008.5 | [Decision Review Loop](../implementation/008.5-decision-review-loop.md) | ✅ Shipped | #76 |
| 008.6 | [Temporal Recall](../implementation/008.6-temporal-recall.md) | ✅ Shipped | #78 |
| 009.1 | [Reactive Memory Lifecycle](../implementation/009.1-reactive-memory-lifecycle.md) | 📋 Specced | — |
| 009.2 | [Maintenance Engine](../implementation/009.2-maintenance-engine.md) | 📋 Specced | — |
| 009.3 | [Episode Enhancements](../implementation/009.3-episode-enhancements.md) | 📋 Specced | — |
| 009.4 | [Fact Generalization](../implementation/009.4-fact-generalization.md) | 📋 Specced | — |
| 009.5 | [Decision Quality Gate](../implementation/009.5-decision-quality-gate.md) | ✅ Shipped | #92 |
| 010.1 | [Health Dashboard](../implementation/010.1-health-dashboard.md) | 📋 Specced | — |
| 011.1 | [Subtasks & Scheduling](../implementation/011.1-subtasks-and-scheduling.md) | ✅ Shipped | #69 |
| 011.2 | [Multimodal File Support](../implementation/011.2-multimodal-file-support.md) | 📋 Specced | — |
| 012.1 | [Frame Splitting](../implementation/012.1-frame-splitting.md) | 📋 Deferred | — |
| 012.2 | [Subtask Enhancements Light](../implementation/012.2-subtask-enhancements-light.md) | ✅ Shipped | #104 |
| 012.3 | [Programmatic Tool Calling](../implementation/012.3-programmatic-tool-calling.md) | ✅ Shipped | #107 |

---

## Research Notes

| # | Topic | Summary |
|---|-------|---------|
| [001](../research/001-foundations.md) | Foundations | Minsky's Society of Mind → dual-module architecture |
| [002](../research/002-minsky-mapping.md) | Minsky Mapping | K-lines, frames, censors → concrete system design |
| [003](../research/003-runtime-decision.md) | Runtime Decision | Docker, GPU, orchestration tradeoffs |
| [004](../research/004-storage-architecture.md) | Storage Architecture | Postgres + pgvector, schema decisions |
| [005](../research/005-cognitive-layer.md) | Cognitive Layer | How Brain + Heart compose into thinking |
| [006](../research/006-v01-features.md) | v0.1 Features | Feature scoping, MVP decisions |
| [007](../research/007-memory-integration.md) | Memory Integration | Episodic ↔ semantic ↔ procedural flows |
| [008](../research/008-database-design.md) | Database Design | 22 tables, 3 schemas, full SQL |
| [009](../research/009-context-management.md) | Context Management | Token budgets, relevance scoring |
| [010](../research/010-summarization-strategy.md) | Summarization | 3-tier compression, episode lifecycle |
| [011](../research/011-measuring-success.md) | Measuring Success | 5-level metrics, growth reports |
| [012](../research/012-automation-pipeline.md) | Automation Pipeline | Event bus, 7 handlers, full wiring |
| [013](../research/013-langchain-memory-lessons.md) | LangChain Memory Lessons | 5 takeaways: reflection, generalization, validation, approval gates |
| [014a](../research/014-group-evolving-agents.md) | GEA | Experience sharing for open-ended self-improvement |
| [014b](../research/014-acteon-action-gateway.md) | Acteon | Action gateway for agent coordination, approval gates |
| [015](../research/015-deep-thinking-ratio.md) | DTR | Measuring real reasoning effort, not token count |
| [016](../research/016-agent-memory-synthesis.md) | Agent Memory Synthesis | 9 papers on LLM agent memory (2025-2026) — retrieval, consolidation, generalization |

## Architecture Summary

![Nous Architecture](../nous-architecture.png)

## Database: 22 Tables, 3 Schemas

| Schema | Tables | Purpose |
|--------|--------|---------|
| `nous_system` (4) | agents, agent_identity, frames, events | Core identity & coordination |
| `brain` (8) | decisions, decision_tags, decision_reasons, decision_bridge, thoughts, graph_edges, guardrails, calibration_snapshots | Decision intelligence |
| `heart` (10) | episodes, episode_decisions, episode_procedures, facts, procedures, censors, working_memory, conversation_state, subtasks, schedules | Memory system |
