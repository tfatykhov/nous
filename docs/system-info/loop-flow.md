# Nous Cognitive Loop & Data Flow

> How a message flows through the system, from input to response to background processing.

## Overview

Every user message triggers a **4-phase pipeline**, followed by **asynchronous background processing**:

```
User Message
     │
     ▼
┌─────────────────────────────────────────────────────┐
│ PHASE 1: PRE-TURN                                   │
│ ┌─────────────┐  ┌──────────┐  ┌────────────────┐  │
│ │ Frame       │→ │ Intent   │→ │ Context        │  │
│ │ Detection   │  │ Analysis │  │ Assembly       │  │
│ │ (5 modes)   │  │          │  │ (T0→T4 tiers)  │  │
│ └─────────────┘  └──────────┘  └────────────────┘  │
└─────────────────────────┬───────────────────────────┘
                          │ system_prompt + tools
                          ▼
┌─────────────────────────────────────────────────────┐
│ PHASE 2: LLM EXECUTION                             │
│ ┌──────────────────────────────────────────────┐    │
│ │ Anthropic Messages API                       │    │
│ │ model + system_prompt + history + message    │    │
│ └──────────────────┬───────────────────────────┘    │
│                    │                                │
│         ┌─────────▼──────────┐                      │
│         │ Response contains  │                      │
│         │ tool_use blocks?   │                      │
│         └────┬──────────┬────┘                      │
│          yes │          │ no                         │
│    ┌─────────▼───┐      │                           │
│    │ Tool Loop   │      │                           │
│    │ (max 25     │      │                           │
│    │  iterations)│      │                           │
│    │             │      │                           │
│    │ For each tool_use: │                           │
│    │ 1. Frame gate check│                           │
│    │ 2. Censor check    │                           │
│    │ 3. Execute handler │                           │
│    │ 4. Return result   │                           │
│    │ 5. Call LLM again  │                           │
│    └─────────────┘      │                           │
│              ▼          │                           │
│         Final text ◄────┘                           │
└─────────────────────────┬───────────────────────────┘
                          │ response_text
                          ▼
┌─────────────────────────────────────────────────────┐
│ PHASE 3: POST-TURN                                  │
│ ┌────────────────┐  ┌──────────┐  ┌─────────────┐  │
│ │ Working Memory │  │ Episode  │  │ Compaction   │  │
│ │ Update         │  │ Tracking │  │ Check        │  │
│ │ (task, frame)  │  │          │  │ (threshold?) │  │
│ └────────────────┘  └──────────┘  └──────┬──────┘  │
│                                          │         │
│                               ┌──────────▼───────┐ │
│                               │ If over threshold│ │
│                               │ → Layer 1 prune  │ │
│                               │ → Layer 2 compact│ │
│                               └──────────────────┘ │
└─────────────────────────┬───────────────────────────┘
                          │
                          ▼
               Return response to user
                          │
                          ▼
┌─────────────────────────────────────────────────────┐
│ PHASE 4: BACKGROUND (async, non-blocking)           │
│                                                     │
│ SessionMonitor (60s check)                          │
│    └─→ session_ended event                          │
│         ├─→ EpisodeSummarizer                       │
│         │    └─→ episode_summarized event            │
│         │         └─→ FactExtractor                  │
│         └─→ DecisionReviewer                         │
│                                                     │
│ On compaction:                                       │
│    └─→ conversation_compacting event                 │
│         └─→ KnowledgeExtractor                       │
│                                                     │
│ SubtaskWorkerPool (parallel agent workers)           │
│ TaskScheduler (30s check for due tasks)              │
└─────────────────────────────────────────────────────┘
```

---

## Phase 1: Pre-Turn — Context Assembly

### 1.1 Frame Detection

The `FrameDetector` classifies the user's message into a cognitive frame:

| Frame | Trigger Patterns | Tool Access |
|-------|-----------------|-------------|
| `conversation` | greetings, questions, casual chat | memory, web, tasks |
| `task` | "create", "fix", "build", "update" | all tools including bash, file ops |
| `research` | "search", "find", "look up" | memory, web, file read |
| `decision` | "should I", "choose", "decide" | memory, web |
| `debug` | "error", "broken", "not working" | all tools including bash |

The frame persists in working memory — it only changes when a new message clearly indicates a different mode.

### 1.2 Intent Analysis

The `IntentEngine` extracts structured intent without an LLM call:
- **Action:** what the user wants done
- **Entities:** mentioned names, files, concepts
- **Urgency:** normal, high, critical
- Uses keyword matching and regex patterns

### 1.3 Context Assembly (Tiered)

The `ContextEngine` builds the system prompt using a **priority tier system**:

```
T0 (Critical) — Always included, never trimmed
├── Agent identity (character, values, protocols)
├── Active censors (guardrails)
├── Working memory (current task, frame)
├── User profile (preferences, rules)
└── Token budget: ~8,000 tokens

T1 (High) — Included if budget allows
├── Preference rules
├── Tool instructions for current frame
├── Output formatting rules (e.g., Telegram)
└── Token budget: ~4,000 tokens

T2 (Medium) — Semantic decisions
├── Related decisions from Brain (query by message embedding)
├── Recent decisions from current session
└── Token budget: ~3,000 tokens

T3 (Standard) — Semantic memory
├── Similar episodes (embedding search)
├── Relevant facts (hybrid search)
├── Related procedures
└── Token budget: ~4,000 tokens

T4 (Low) — Temporal context
├── Recent episodes (last 48 hours)
├── Recent conversation summaries
└── Token budget: ~2,000 tokens
```

**Token budget management:**
- Total budget: `max_context_tokens` (180,000)
- Tiers are filled top-down
- If budget is exceeded, lower tiers are trimmed first
- T0 is never trimmed

### 1.4 Deduplication

The `DedupEngine` runs after context assembly:
- Checks each context item against conversation history
- Removes facts/episodes already discussed in current session
- Uses embedding similarity (threshold: 0.85)
- Prevents the agent from repeating the same context

---

## Phase 2: LLM Execution

### 2.1 API Call

```python
response = anthropic.messages.create(
    model=settings.model,           # claude-sonnet-4-20250514
    max_tokens=16384,
    system=system_prompt,           # from Phase 1
    messages=conversation_history,  # includes current message
    tools=available_tools,          # filtered by frame
)
```

### 2.2 Tool Loop

If the response contains `tool_use` blocks:

```
For each tool_use in response (max 25 rounds total):
│
├── 1. Frame Gate Check
│   └── Is this tool allowed in current frame?
│       ├── Yes → continue
│       └── No → return "tool not available in this mode"
│
├── 2. Censor Check
│   └── Run tool input through GuardrailEngine
│       ├── Pass → continue
│       ├── Warn → log warning, continue
│       ├── Block → return block message, skip tool
│       └── Absolute → hard stop
│
├── 3. Execute Tool Handler
│   └── ToolDispatcher.dispatch(name, args, session_id)
│       └── Returns result string (may be truncated)
│
├── 4. Append Result to History
│   └── tool_result content block added
│
└── 5. Call LLM Again
    └── LLM sees tool result, may call more tools or generate text
```

**Tool loop terminates when:**
- LLM returns text without tool_use blocks
- Max rounds (25) reached
- Error occurs

### 2.3 Decision Quality Gate

If any `record_decision` tool calls were made during the turn:

```
Decision → QualityScorer
│
├── Signal 1: Stakes Alignment (0.0-1.0)
│   └── Are stakes appropriate for this type of decision?
│
├── Signal 2: Confidence Calibration (0.0-1.0)
│   └── Is confidence level realistic given the evidence?
│
├── Signal 3: Reasoning Diversity (0.0-1.0)
│   └── Are multiple reasoning types used?
│
├── Signal 4: Context Richness (0.0-1.0)
│   └── Is sufficient context provided?
│
└── Signal 5: Tag Specificity (0.0-1.0)
    └── Are tags specific enough to be useful?

Composite Score = weighted average of signals
├── Score ≥ 0.5 → Decision recorded normally
└── Score < 0.5 → Decision flagged for review
```

---

## Phase 3: Post-Turn Processing

### 3.1 Working Memory Update

After each turn, the `WorkingMemoryManager` updates:
- **Current task** — extracted from conversation context
- **Frame** — current cognitive frame
- **Notes** — any important context for next turn

### 3.2 Episode Tracking

The `AgentRunner` tracks conversation episodes:
- Creates episode on first message in session
- Updates episode with each turn (topics, message count)
- Episode is summarized when session ends

### 3.3 Compaction Check

If conversation history exceeds `compaction_threshold` (120,000 tokens):

```
Layer 1: Tool Output Pruning
├── Scan history for tool_result blocks
├── Truncate results > 2000 chars to summary
└── Typically saves 30-50% of token usage

If still over threshold:

Layer 2: History Compaction
├── Fire conversation_compacting event
│   └── KnowledgeExtractor extracts facts first
├── Send old turns to Claude for summarization
├── Replace old turns with compact summary
└── Preserve last 4-6 turns in full
```

---

## Phase 4: Background Processing

All background processing is event-driven via the `EventBus`.

### 4.1 Session Lifecycle

```
User active
     │ (no message for 30 min)
     ▼
SessionMonitor fires session_ended
     │
     ├──▶ EpisodeSummarizer
     │    ├── Retrieves full conversation history
     │    ├── Calls Claude to generate structured summary
     │    │   ├── Topics discussed
     │    │   ├── Key decisions made
     │    │   ├── Outcomes/results
     │    │   └── Participants
     │    ├── Stores episode with embedding
     │    └── Fires episode_summarized
     │              │
     │              ▼
     │         FactExtractor
     │         ├── Calls Claude to extract facts from summary
     │         ├── Categories: preference, technical, person, etc.
     │         ├── Deduplicates against existing facts
     │         └── Stores new facts with source attribution
     │
     └──▶ DecisionReviewer
          ├── Reviews all decisions from session
          ├── QualityScorer pipeline
          ├── Confidence calibration
          └── Flags low-quality for review
```

### 4.2 Compaction Knowledge Extraction

```
Compaction triggered
     │
     ├── Fire conversation_compacting event
     │         │
     │         ▼
     │    KnowledgeExtractor
     │    ├── Reads conversation turns being compacted
     │    ├── Extracts critical facts (more aggressive than FactExtractor)
     │    └── Stores facts before history is lost
     │
     └── Proceed with compaction
```

### 4.3 Subtask Execution

```
spawn_task called
     │
     ▼
SubtaskWorkerPool
├── Check pending count < max_pending (5)
├── Create subtask record (status: pending)
├── Spawn async worker
│   ├── Create dedicated AgentRunner
│   ├── Configure: model override, frame_type, tool_call_limit (20)
│   ├── Run task as single-turn conversation
│   ├── Store result in subtask record
│   └── Status → completed/failed
│
├── If await_result=true:
│   └── Block caller until worker completes, return result inline
│
└── If await_result=false (default):
    └── Return subtask ID immediately, worker runs in background
```

### 4.4 Scheduled Task Execution

```
TaskScheduler (checks every 30 seconds)
     │
     ├── Query active schedules where fire_at <= now
     │
     ├── For each due schedule:
     │   ├── Spawn subtask with schedule's task text
     │   ├── Increment fire_count
     │   ├── Compute next fire_at (for recurring)
     │   │   ├── Interval: now + interval_seconds
     │   │   └── Cron: next match from cron_expr
     │   ├── If max_fires reached → deactivate
     │   └── If notify=true → push result to user
     │
     └── One-shot schedules: deactivate after single fire
```

### 4.5 Sleep Cycle

```
POST /admin/sleep  (or scheduled)
     │
     ▼
Fire sleep_start event
     │
     ▼
SleepHandler (5 phases, each calls Claude independently)
│
├── Phase 1: REPLAY
│   ├── Retrieve recent episodes (last 24h)
│   ├── Retrieve recent decisions
│   └── Build consolidated timeline
│
├── Phase 2: CONSOLIDATE
│   ├── Find related facts (embedding similarity)
│   ├── Merge duplicates
│   ├── Strengthen frequently-confirmed facts
│   └── Create composite facts
│
├── Phase 3: PRUNE
│   ├── Identify stale facts (old, low confidence)
│   ├── Identify contradicted facts
│   ├── Reduce confidence on stale items
│   └── Deactivate contradicted items
│
├── Phase 4: REFLECT
│   ├── Self-assessment: what went well, what didn't
│   ├── Identify blind spots
│   ├── Generate improvement insights
│   └── Store reflections as facts
│
└── Phase 5: GENERALIZE
    ├── Review specific decisions
    ├── Extract abstract patterns
    ├── Store patterns as procedures
    └── Link patterns to source decisions
```

---

## Startup & Shutdown

### Startup Sequence

```
main.py → lifespan()
│
├── 1. Settings — load from NOUS_* env vars
├── 2. Database — create async engine, verify connection
├── 3. Migrator — run pending migrations
├── 4. EmbeddingProvider — initialize Voyage AI client
├── 5. Brain — create facade (quality, guardrails, calibration, bridge)
├── 6. Heart — create facade (facts, episodes, censors, procedures, working_memory, subtasks, schedules, search)
├── 7. EventBus — create queue, start poll loop
├── 8. IdentityManager — load identity, check initiation
├── 9. CognitiveLayer — create sub-engines (context, frames, deliberation, intent, dedup, monitor, usage)
├── 10. Handlers — create and register:
│       FactExtractor, EpisodeSummarizer, DecisionReviewer,
│       KnowledgeExtractor, SessionMonitor, SleepHandler
├── 11. ToolDispatcher — register 15 tools with schemas
├── 12. AgentRunner — create with all dependencies
├── 13. SubtaskWorkerPool — create worker pool
├── 14. TaskScheduler — start schedule checker
├── 15. REST API — mount routes, start Starlette
└── 16. Telegram Bot — start polling (if configured)
```

### Shutdown Sequence

```
SIGTERM/SIGINT received
│
├── 1. TaskScheduler.stop() — stop checking schedules
├── 2. SubtaskWorkerPool.stop() — cancel running subtasks
├── 3. EventBus.stop() — stop poll loop, drain queue
├── 4. SessionMonitor — fire session_ended for active sessions
├── 5. Heart.close() — flush pending writes
├── 6. Brain.close() — flush pending writes
└── 7. Database.close() — close connection pool
```

---

## Search Pipeline

### recall_deep (Hybrid Search)

```
User query: "how did we handle the API migration?"
     │
     ▼
1. Generate embedding for query (Voyage AI)
     │
     ▼
2. Parallel search across memory types:
   ├── Facts: keyword (trigram) + embedding similarity
   ├── Episodes: keyword + embedding similarity
   ├── Decisions: keyword + embedding similarity
   ├── Procedures: keyword + embedding similarity
   └── Censors: keyword match only
     │
     ▼
3. Score fusion:
   ├── keyword_score × 0.3
   └── embedding_score × 0.7
   = hybrid_score per result
     │
     ▼
4. Rank by hybrid_score, return top N
     │
     ▼
5. Format results by type with metadata
```

### recall_recent (Temporal)

```
Query: last 48 hours, limit 10
     │
     ▼
1. SELECT from heart.episodes
   WHERE created_at > now() - interval '48 hours'
   ORDER BY created_at DESC
   LIMIT 10
     │
     ▼
2. Return formatted episode summaries with timestamps
```

---

## Data Flow Diagrams

### Fact Lifecycle

```
Source: conversation, compaction, sleep, manual
     │
     ▼
learn_fact / FactExtractor / KnowledgeExtractor
     │
     ├── Generate embedding (Voyage AI)
     ├── Deduplicate (embedding similarity > 0.9)
     │   ├── Duplicate found → update confidence, skip
     │   └── New fact → insert
     ├── Store in heart.facts + heart.fact_embeddings
     └── Available via recall_deep
```

### Decision Lifecycle

```
record_decision (agent tool call)
     │
     ├── Generate embedding
     ├── Quality gate scoring (5 signals)
     │   ├── Pass → status: active
     │   └── Fail → status: pending_review
     ├── Guardrail check (censors)
     │   ├── Pass → continue
     │   └── Block → reject with reason
     ├── Store in brain.decisions + brain.decision_embeddings
     ├── Auto-link to similar past decisions
     │
     └── On session_ended:
         └── DecisionReviewer
             ├── Re-score quality
             ├── Calibration check
             └── Update status if needed
```

### Context Assembly Flow

```
New user message arrives
     │
     ▼
CognitiveLayer.prepare_turn()
     │
     ├── FrameDetector.detect(message) → frame
     │
     ├── ContextEngine.assemble(message, session_id)
     │   │
     │   ├── T0: Identity
     │   │   ├── IdentityManager.get_current() (cached 60s)
     │   │   ├── Heart.get_active_censors()
     │   │   ├── WorkingMemory.get(session_id)
     │   │   └── User profile (from identity preferences)
     │   │
     │   ├── T1: Rules
     │   │   ├── Protocol rules from identity
     │   │   ├── Tool instructions for frame
     │   │   └── Output formatting (e.g., Telegram rules)
     │   │
     │   ├── T2: Decisions
     │   │   ├── Brain.query(message_embedding, limit=5)
     │   │   └── Recent session decisions
     │   │
     │   ├── T3: Semantic
     │   │   ├── Heart.search_facts(message, limit=10)
     │   │   ├── Heart.search_episodes(message, limit=5)
     │   │   └── Heart.search_procedures(message, limit=3)
     │   │
     │   └── T4: Temporal
     │       └── Heart.get_recent_episodes(hours=48, limit=5)
     │
     ├── DedupEngine.deduplicate(context_items, history)
     │
     └── Build system_prompt string
         └── Return to AgentRunner
```
