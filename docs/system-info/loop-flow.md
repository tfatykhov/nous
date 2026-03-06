# Nous Loop Flow — Message Processing Pipeline

> How a single user message flows through Nous from receipt to response,
> and what happens after.

---

## Overview

Every user message passes through a **4-phase pipeline**:

```
PHASE 1: PRE-TURN        — Cognitive Layer prepares context for the LLM
PHASE 2: LLM EXECUTION   — Claude processes the prompt and may use tools
PHASE 3: POST-TURN       — Cognitive Layer processes the LLM's response
PHASE 4: BACKGROUND       — Event handlers learn from the conversation
```

---

## Phase 1: Pre-Turn (CognitiveLayer.pre_turn)

```
User Message
     │
     ▼
┌─────────────────────────────────────────────────────┐
│  1a. FRAME SELECTION (FrameEngine)                  │
│                                                     │
│  Pattern-match input text against frame definitions │
│  No LLM call — pure regex/keyword matching (<10ms)  │
│                                                     │
│  Frames: conversation, question, task, decision,    │
│          debug, creative                            │
│                                                     │
│  Each frame configures:                             │
│  • Available tools (frame-gated)                    │
│  • Token budget allocations                         │
│  • System prompt instructions                       │
│  • Retrieval priorities                             │
└─────────────────┬───────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────┐
│  1b. INTENT CLASSIFICATION (IntentClassifier)       │
│                                                     │
│  Determines what kind of retrieval the user needs:  │
│  • conversation — social chat, no deep retrieval    │
│  • recall       — "what did we discuss", memory Q   │
│  • factual      — needs facts/knowledge             │
│  • task         — needs procedures, past decisions  │
│  • decision     — needs past decisions + outcomes   │
│  • creative     — minimal retrieval, max freedom    │
│                                                     │
│  Generates a RetrievalPlan:                         │
│  • queries: what to search for                      │
│  • memory_types: which stores to search             │
│  • min_results: how many to fetch per type          │
└─────────────────┬───────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────┐
│  1c. CONTEXT ASSEMBLY (ContextEngine)               │
│                                                     │
│  Builds the system prompt in tiered priority order  │
│  within token budgets:                              │
│                                                     │
│  TIER 0: Identity (always loaded, ~500-1000 tokens) │
│  ├── Character, Values, Protocols, Preferences      │
│  ├── User profile (facts about the user)            │
│  ├── Active censors                                 │
│  └── Frame instructions                             │
│                                                     │
│  TIER 1: Always-on facts by category (~300 tokens)  │
│  ├── preference facts                               │
│  ├── person facts                                   │
│  └── rule facts                                     │
│                                                     │
│  TIER 2: Working memory threads (~200 tokens)       │
│  └── Current task, active threads                   │
│                                                     │
│  TIER 3: Retrieval results (~2000 tokens)           │
│  ├── Related decisions (from Brain)                 │
│  ├── Similar episodes (from Heart)                  │
│  ├── Relevant facts (from Heart)                    │
│  ├── Relevant procedures (from Heart)               │
│  └── Deduped + scored + ranked by relevance         │
│                                                     │
│  TIER 4: Recent conversations (~500 tokens)         │
│  └── Recent episode summaries for temporal context  │
│                                                     │
│  Each section respects its token budget. If total   │
│  exceeds limit, lower tiers are truncated first.    │
│                                                     │
│  Deduplication removes items that overlap >80%      │
│  with identity or higher-tier content.              │
└─────────────────┬───────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────┐
│  1d. CENSOR CHECK (CensorManager)                   │
│                                                     │
│  Check user input against active censors:           │
│  • warn   — append warning to system prompt         │
│  • block  — reject message before LLM sees it       │
│  • absolute — hard block, no override possible      │
│                                                     │
│  Also checks pending tool results for censors.      │
└─────────────────┬───────────────────────────────────┘
                  │
                  ▼
     System Prompt + Tool Definitions assembled
```

---

## Phase 2: LLM Execution (AgentRunner)

```
     System Prompt + History + User Message
                  │
                  ▼
┌─────────────────────────────────────────────────────┐
│  2a. HISTORY MANAGEMENT                             │
│                                                     │
│  • Max 20 messages in history window                │
│  • If history exceeds token limit → COMPACTION      │
│    (LLM summarizes older messages into a summary    │
│    that replaces them)                              │
│  • Conversation state persisted to DB (survives     │
│    restarts)                                        │
│  • Compaction count tracked per conversation        │
└─────────────────┬───────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────┐
│  2b. ANTHROPIC API CALL                             │
│                                                     │
│  Direct httpx POST to Messages API:                 │
│  • model: claude-sonnet-4-5-20250514 (default)      │
│  • system: assembled system prompt                  │
│  • messages: conversation history                   │
│  • tools: frame-gated tool definitions              │
│  • max_tokens: 16384 (default)                      │
│                                                     │
│  Supports both sync and SSE streaming modes.        │
└─────────────────┬───────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────┐
│  2c. TOOL USE LOOP                                  │
│                                                     │
│  If Claude returns tool_use blocks:                 │
│                                                     │
│  for each tool_use in response:                     │
│    ├── ToolDispatcher routes to handler             │
│    ├── Handler executes (may hit DB, run bash,      │
│    │   search web, spawn subtask, etc.)             │
│    ├── Censor check on tool result                  │
│    └── Result appended to messages                  │
│                                                     │
│  Then re-call Anthropic API with tool results.      │
│  Loop continues until Claude returns end_turn       │
│  (text-only response, no more tool calls).          │
│                                                     │
│  Max iterations: 25 (safety limit)                  │
│                                                     │
│  FRAME-GATED TOOLS (by frame type):                 │
│  • conversation: all 15 tools                       │
│  • question: recall, bash, read/write, web, python  │
│  • task: all 15 tools + decision nudge              │
│  • decision: all 15 tools + decision nudge          │
│  • debug: all 15 tools + decision nudge             │
│  • creative: recall, learn_fact, web, python        │
└─────────────────┬───────────────────────────────────┘
                  │
                  ▼
     Final text response from Claude
```

### Available Tools (15)

| Tool | Category | Purpose |
|------|----------|---------|
| `record_decision` | Memory (Brain) | Record a decision with reasons, confidence, category |
| `learn_fact` | Memory (Heart) | Store a fact with category, subject, tags |
| `recall_deep` | Memory (Search) | Search across all memory types |
| `recall_recent` | Memory (Search) | Recall recent episodes by time |
| `create_censor` | Memory (Heart) | Create a guardrail rule |
| `bash` | System | Execute shell commands |
| `read_file` | System | Read a file |
| `write_file` | System | Write a file |
| `web_search` | Web | Search Brave/Exa |
| `web_fetch` | Web | Fetch and extract URL content |
| `spawn_task` | Tasks | Create a background subtask |
| `schedule_task` | Tasks | Schedule a future/recurring task |
| `list_tasks` | Tasks | List subtasks and schedules |
| `cancel_task` | Tasks | Cancel a subtask or schedule |
| `run_python` | Code | Execute Python with memory API access |

---

## Phase 3: Post-Turn (CognitiveLayer.post_turn)

```
     Claude's final response
                  │
                  ▼
┌─────────────────────────────────────────────────────┐
│  3a. MONITOR (MonitorEngine)                        │
│                                                     │
│  Tracks response quality metrics:                   │
│  • Response length and token usage                  │
│  • Tool calls made and their results                │
│  • Context utilization (what % was useful)          │
│  • Error detection                                  │
└─────────────────┬───────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────┐
│  3b. USAGE TRACKING (UsageTracker)                  │
│                                                     │
│  Tracks which retrieved memories appeared in the    │
│  response (containment coefficient):                │
│  • If context was used → boost future retrieval     │
│  • If context was ignored → consider deprioritizing │
│  • Prunes stale tracking data after 30 days         │
│                                                     │
│  Uses |A∩B| / min(|A|,|B|) instead of Jaccard      │
│  for asymmetric text lengths.                       │
└─────────────────┬───────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────┐
│  3c. WORKING MEMORY UPDATE                          │
│                                                     │
│  Extracts task/topic from conversation and stores   │
│  in working memory threads for next-turn context.   │
└─────────────────┬───────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────┐
│  3d. CONVERSATION STATE PERSISTENCE                 │
│                                                     │
│  Saves current conversation to DB:                  │
│  • Messages array                                   │
│  • Summary (if compacted)                           │
│  • Compaction count                                 │
│  Allows recovery after server restart.              │
└─────────────────┬───────────────────────────────────┘
                  │
                  ▼
     Response sent to user
```

---

## Phase 4: Background Processing (Event Handlers)

After the user receives their response, background handlers process the conversation asynchronously.

### 4a. Session Timeout Flow

```
┌──────────────────────────────────────────────────┐
│  SessionMonitor (runs every 60s)                 │
│                                                  │
│  For each active conversation:                   │
│  ├── idle > 30min? → emit session_timeout        │
│  │   └── CognitiveLayer.end_session()            │
│  │       ├── Generate reflection summary         │
│  │       ├── Extract facts inline                │
│  │       └── Emit session_ended                  │
│  │                                               │
│  └── idle > 2hr?  → emit session_sleep           │
│      └── SleepHandler                            │
│          └── Generate "dream" reflection         │
│              (meta-analysis of recent activity)   │
└──────────────────────────────────────────────────┘
```

### 4b. Session End Flow

```
session_ended event fires
         │
         ├─────────────────────────────┐
         │                             │
         ▼                             ▼
┌─────────────────┐         ┌──────────────────┐
│ EpisodeSummarizer│        │  FactExtractor    │
│                  │        │                   │
│ Takes transcript │        │ Takes transcript  │
│ → LLM generates │        │ → LLM extracts    │
│   summary with   │       │   facts with       │
│   topics and     │       │   categories,      │
│   key points     │       │   subjects, tags   │
│ → Stores as      │       │ → Dedupes against  │
│   Episode in     │       │   existing facts   │
│   Heart          │       │ → Stores new facts │
│ → Generates      │       │   in Heart         │
│   embedding      │       │                    │
└─────────────────┘         └──────────────────┘
         │
         ▼
┌─────────────────┐
│DecisionReviewer  │
│                  │
│ Scans decisions  │
│ from this session│
│ → Auto-reviews   │
│   if verifiable  │
│   signal exists: │
│   • Error logs   │
│   • File exists  │
│   • GitHub PR    │
│   • Episode ref  │
│ → Updates Brain  │
│   with outcome   │
└─────────────────┘
```

### 4c. Subtask Flow

```
User says "spawn_task" or schedule fires
         │
         ▼
┌──────────────────────────────────────────────────┐
│  Heart.enqueue_subtask()                         │
│  → Creates subtask record (status: pending)      │
│  → If await_result=true, blocks until complete   │
└────────────────┬─────────────────────────────────┘
                 │
                 ▼
┌──────────────────────────────────────────────────┐
│  SubtaskWorker (continuous background loop)      │
│                                                  │
│  1. Pick oldest pending subtask                  │
│  2. Create fresh AgentRunner (separate LLM ctx)  │
│  3. Build subtask-specific system prompt:        │
│     • Reduced tool set (no spawn_task recursion) │
│     • Task instruction as user message           │
│     • Model override if specified                │
│     • Frame type if specified                    │
│  4. Run conversation (may have tool loop)        │
│  5. Store result, update status → completed      │
│  6. If parent waiting → signal completion        │
│  7. If notify=true → push to Telegram            │
│                                                  │
│  Concurrency: max 3 simultaneous subtasks        │
│  Timeout: configurable per subtask (default 120s)│
│  Guardrails: worker cannot spawn_task (no fork   │
│  bombs), limited tool access                     │
└──────────────────────────────────────────────────┘
```

### 4d. Schedule Flow

```
┌──────────────────────────────────────────────────┐
│  TaskScheduler (runs every 60s)                  │
│                                                  │
│  1. Query Heart for due schedules               │
│  2. For each due schedule:                       │
│     ├── One-shot? → deactivate after firing      │
│     └── Recurring? → compute next_fire from cron │
│  3. Enqueue as subtask in Heart                  │
│  4. SubtaskWorker picks it up (see above)        │
└──────────────────────────────────────────────────┘
```

---

## Context Assembly Detail

The system prompt is the most important part — it determines what Nous "knows" for each turn. Here's the exact assembly order:

```
System Prompt Structure:
═══════════════════════

## Identity                              ← Tier 0 (always)
Character: ...
Values: ...
Protocols: ...
Preferences: ...
Boundaries: ...

## User Profile                          ← Tier 0 (always)
• Tim lives in Silver Spring, MD
• Tim prefers Celsius
• ...

## Active Censors                        ← Tier 0 (always)
• BLOCK: api.key|token|...
• WARN: notifying Tim unnecessarily
• ...

## Current Frame                         ← Tier 0 (always)
**Task Execution**: Focused on completing...

## Working Memory                        ← Tier 2
**Current task:** ...
**Frame:** ...

## Related Decisions                     ← Tier 3 (retrieved)
- [success] Decision about X... (confidence: 0.85)
- [failure] Decision about Y... (confidence: 0.70)

## Recent Conversations                  ← Tier 4 (temporal)
- [2 hours ago] Discussion about Z...
- [yesterday] Worked on A...

## Tool Instructions                     ← Frame-specific
You are in a TASK frame. If you make a meaningful choice...

## Output Formatting                     ← Channel-specific
You are responding in Telegram. Format accordingly...
```

---

## Conversation Compaction

When conversation history grows too large:

```
Before Compaction:
  messages[0]: user "Hey, can you..."
  messages[1]: assistant "Sure, I'll..."
  messages[2]: user "What about..."
  ...
  messages[38]: user "Now do this"
  messages[39]: assistant "On it..."

After Compaction:
  summary: "Discussion covered X, Y, Z. Key decisions: A, B.
            User preferences noted: P, Q."
  messages[0]: user "Now do this"    ← only recent messages kept
  messages[1]: assistant "On it..."
  compaction_count: 1               ← tracks how many times compacted

The summary is prepended to the system prompt so context is preserved.
```

---

## Decision Quality Pipeline

When Claude calls `record_decision`:

```
record_decision call
         │
         ▼
┌─────────────────────────┐
│  Quality Gate            │
│  Scores 0.0-1.0:        │
│  • Specificity (0.3x)   │
│  • Reasoning depth (0.3x)│
│  • Confidence cal (0.2x) │
│  • Context (0.2x)        │
│                          │
│  Below 0.5? → BLOCKED    │
│  (returned to Claude     │
│   with improvement hints)│
└───────────┬─────────────┘
            │ (passed)
            ▼
┌─────────────────────────┐
│  Guardrail Engine        │
│  • Duplicate detection   │
│  • Content validation    │
└───────────┬─────────────┘
            │ (passed)
            ▼
┌─────────────────────────┐
│  Brain.record_decision() │
│  • Store in brain schema │
│  • Generate embedding    │
│  • Auto-link to similar  │
│    decisions (>0.85 sim) │
│  • Bridge to Heart       │
│    memories if relevant  │
│  • Return decision ID    │
└─────────────────────────┘
```

---

## Search Pipeline (recall_deep)

When Claude calls `recall_deep`:

```
recall_deep(query="how to deploy", memory_types=["all"])
         │
         ▼
┌─────────────────────────────────────────┐
│  SearchEngine.search()                   │
│                                          │
│  For each memory type:                   │
│  1. Keyword search (ts_vector/ILIKE)     │
│  2. Embedding search (pgvector cosine)   │
│  3. Hybrid scoring:                      │
│     score = (keyword_rank * 0.4)         │
│           + (embedding_sim * 0.6)        │
│  4. Frame boost (current frame boosts    │
│     relevant types, e.g., decision       │
│     frame boosts decision results)       │
│  5. Rank and return top N                │
│                                          │
│  Types searched:                         │
│  • episodes  (Heart)                     │
│  • facts     (Heart)                     │
│  • procedures (Heart)                    │
│  • decisions (Brain)                     │
│  • censors   (Heart)                     │
└─────────────────────────────────────────┘
```

---

## Startup Sequence

```
main()
  │
  ├── Settings() — load from env/file
  │
  ├── build_app(settings) — create Starlette app
  │
  └── uvicorn.run(app) — start ASGI server
       │
       └── lifespan startup:
            │
            ├── 1. Database — create engine + pool
            │
            ├── 2. run_migrations() — Alembic auto-migrate
            │
            ├── 3. EmbeddingProvider — init OpenAI client (optional)
            │
            ├── 4. Brain — init with DB + embeddings
            │
            ├── 5. Heart — init with DB + embeddings (shared)
            │
            ├── 6. CognitiveLayer — compose all engines
            │
            ├── 7. EventBus — create + start processing loop
            │
            ├── 8. Handlers — register on EventBus events:
            │   ├── EpisodeSummarizer → session_ended
            │   ├── FactExtractor → session_ended
            │   ├── KnowledgeExtractor → session_ended
            │   ├── DecisionReviewer → session_ended + sweep
            │   ├── SessionMonitor → start periodic check
            │   ├── SleepHandler → session_sleep
            │   ├── SubtaskWorker → start continuous loop
            │   └── TaskScheduler → start periodic check
            │
            ├── 9. AgentRunner — init with Brain + Heart + CogLayer
            │
            ├── 10. ToolDispatcher — register all 15 tools
            │
            ├── 11. REST routes — mount on /api
            │
            ├── 12. MCP server — mount on /mcp (if enabled)
            │
            └── 13. TelegramBot — start polling (if token set)
```

---

## Shutdown Sequence

```
lifespan shutdown:
  │
  ├── 1. Stop TelegramBot polling
  │
  ├── 2. Stop SessionMonitor
  │
  ├── 3. Stop TaskScheduler
  │
  ├── 4. Stop SubtaskWorker (drain queue)
  │
  ├── 5. Stop DecisionReviewer sweep
  │
  ├── 6. Stop EventBus (drain pending events)
  │
  ├── 7. Close MCP server
  │
  ├── 8. Close httpx client (Anthropic API)
  │
  └── 9. Close Database connection pool
```
