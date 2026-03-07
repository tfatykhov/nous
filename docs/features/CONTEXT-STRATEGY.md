# Nous Context Strategy — Write, Select, Compress, Isolate

**Status:** Reference Document
**Author:** Emerson
**Created:** 2026-03-07
**Covers:** F014, F015, F016, F017

---

## Overview

Nous's context management is implemented across four feature specs that together form a coherent strategy. This document maps how they work together using the **Write-Select-Compress-Isolate** framework identified in context engineering research (Anthropic, Google ADK, Stanford ACE).

The core insight: **300 focused tokens beats 113K unfocused ones.** Context quality matters more than context quantity, even with 1M token windows.

---

## The Four Strategies

```
┌─────────────────────────────────────────────────────────────┐
│                    NOUS CONTEXT STRATEGY                     │
│                                                              │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐    │
│  │  WRITE   │  │  SELECT  │  │ COMPRESS │  │ ISOLATE  │    │
│  │  (F016)  │  │  (F017)  │  │  (F016)  │  │  (F015)  │    │
│  │          │  │          │  │          │  │          │    │
│  │ Pre-prune│  │ Quality  │  │ 4-tier   │  │ Subtask  │    │
│  │ fact     │  │ gate     │  │ pruning  │  │ isolation│    │
│  │ extract  │  │ Relevance│  │ Metadata │  │ Per-frame│    │
│  │          │  │ floor    │  │ degrade  │  │ budgets  │    │
│  │ Offload  │  │ Score    │  │ Compactn │  │ Tool     │    │
│  │ before   │  │ cutoff   │  │ Model-   │  │ limits   │    │
│  │ compress │  │ Staleness│  │ aware    │  │          │    │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘    │
│                                                              │
│  Cross-cutting: F014 Frame Scaffolds (what frame am I in?)  │
└─────────────────────────────────────────────────────────────┘
```

### 1. WRITE — Preserve before losing (F016 Phase 4)

**Principle:** "Offload before compress." — Anthropic

Before context is destroyed, extract and store what matters.

| Mechanism | Spec | What it does |
|-----------|------|-------------|
| Pre-prune fact extraction | F016 §4.0.1 | Regex-extract URLs, paths, key-values from tool results before hard-clear. Store as `confidence=0.3` facts in Heart. |
| Context pressure warning | F016 §5.1 | System message at 40K tool tokens: "summarize findings before reading more files." Nudges the model to write. |
| Anti-hallucination prompt | F016 §0 | "Don't guess, re-fetch." Prevents the model from fabricating lost context — makes it write or re-read instead. |

**When it fires:** During tool loop, before pruning destroys content.

**Key interaction:** Extracted facts are tagged `source=pre_prune_extraction` and exempt from F017's relevance floor (F017 §1, `FLOOR_EXEMPT_SOURCES`). Without this exemption, the quality gate would block the very facts that Write saved.

### 2. SELECT — Only retrieve what's relevant (F017)

**Principle:** "Budget is a ceiling, not a target." — F017

The context assembly pipeline retrieves memory (facts, decisions, episodes, procedures) and filters aggressively for quality.

| Mechanism | Spec | What it does |
|-----------|------|-------------|
| Relevance floor | F017 §1 | Minimum score per memory type (0.35-0.50). Nothing below the floor gets in. |
| Diminishing returns cutoff | F017 §2 | Stop at sharp score drops (>40% decrease). Finds natural relevance boundaries. |
| Staleness penalty | F017 §5 | 14-day half-life on scores. Fresh content wins over equally-relevant stale content. Rules/preferences exempt. |
| Model-aware budget scaling | F017 §3 | 2.5x ceilings on 1M models — but only fills with content passing the floor. |
| ACE-style usage tracking | F017 §6 | Track what the model actually references. Boost used items, penalize assembled-but-ignored. |

**Pipeline order:**
```
retrieve → staleness_penalty → frame_boost → dedup → usage_boost
  → relevance_floor → diminishing_cutoff → truncate → log
```

**When it fires:** Pre-turn, during context assembly (system prompt construction).

**Key interaction with F014:** Frame selection determines which `ContextBudget` is used. A `debug` frame gets 10K total with heavy procedure allocation. A `conversation` frame gets 3K with no procedures. The frame is the first quality decision.

### 3. COMPRESS — Degrade gracefully (F016 Phases 1-3)

**Principle:** "Simple masking matches LLM summarization at half cost." — JetBrains

When context must shrink, lose information gradually, not all at once.

| Mechanism | Spec | What it does |
|-----------|------|-------------|
| 4-tier pruning pipeline | F016 §1 | Full → soft-trim → metadata degrade → hard-clear. Each tier preserves less but never drops to zero info suddenly. |
| Content-type-aware profiles | F016 §4 | Different tools decay at different rates. Code (`preserve`) stays full longest. Web search (`conservative`) gets fact-extracted first. |
| Re-fetch hints | F016 §1 | Metadata traces include `↺ re-fetchable` so the model knows it can call the tool again. |
| LLM compaction | F016 §2 | At 60% of context window, LLM summarizes old conversation. Dynamic thresholds per model (600K for 1M models). |
| Tool Results Digest | F016 OQ#1 | Compaction summary includes a "Key Tool Results" section so tool findings survive summarization. |

**Decay profiles:**

| Profile | Tools | Soft-trim | Metadata | Hard-clear | Philosophy |
|---------|-------|-----------|----------|------------|-----------|
| preserve | read_file | age 8 | _(skip)_ | age 20 | Code is too dense for lossy compression. Trust compaction. |
| aggressive | list_files, recall_deep | age 2 | age 4 | age 8 | Re-readable or already stored. |
| standard | bash, run_python | age 3 | age 8 | age 12 | Default progression. |
| conservative | web_search | age 5 | age 10 | age 15 | Extract facts first (Write strategy). |

**When it fires:** After each tool execution (pruning) and pre-turn when context exceeds threshold (compaction).

**Key interaction with F017:** Compress operates on conversation history (tool results). Select operates on system prompt (memory retrieval). They are independent layers — Compress can't affect what Select retrieves, and Select can't affect what Compress prunes. This separation is by design (confirmed by architecture review).

### 4. ISOLATE — Contain context per concern (F015)

**Principle:** "Use sub-sessions so no single session gets overloaded." — Claude Code pattern

When a task needs heavy context (deep code analysis, multi-file debugging), isolate it rather than polluting the main session.

| Mechanism | Spec | What it does |
|-----------|------|-------------|
| Subtask isolation | F015 | Per-frame timeout and tool limits. Debug gets more tool calls, research gets fewer. |
| Tool budgets | F016 §5.2 | Soft per-frame limits on `read_file` (e.g., 10 for task, 12 for debug). Warning, not block. |
| Frame-adaptive windows | F016 §4.0.2 | `keep_last_tool_results` varies by frame. Debug keeps 4, research keeps 1. |
| Session budgets | F015 | Total token tracking per session. Prevents runaway subtasks. |

**When it fires:** At tool call time (budgets) and at subtask spawn (isolation).

**Key interaction:** Isolation is the escape valve when the other three strategies aren't enough. A 100-file codebase analysis can't be solved by better pruning (Compress), better retrieval (Select), or better extraction (Write). It needs to be broken into sub-sessions that each handle a subset. F016's scope boundary explicitly calls this out.

---

## How They Interact Per Turn

```
User sends message
  │
  ├─ F014: Classify frame (task/debug/decision/conversation/...)
  │
  ├─ F017 SELECT: Assemble system prompt context
  │   ├─ Retrieve from Heart (facts, procedures, episodes)
  │   ├─ Retrieve from Brain (decisions)
  │   ├─ Apply staleness → frame_boost → dedup → usage_boost
  │   ├─ Apply relevance floor → diminishing cutoff
  │   └─ Truncate to scaled budget, log fill ratio
  │
  ├─ F016 COMPRESS (pre-turn): Check compaction threshold
  │   └─ If > 60% of context window: LLM summarize old messages
  │
  ├─ API call (system prompt + conversation history)
  │
  ├─ Tool loop (if tool calls):
  │   ├─ Execute tool
  │   ├─ F016 COMPRESS: Prune tool results (4-tier pipeline)
  │   ├─ F016 WRITE: Extract facts before hard-clear (conservative tools)
  │   ├─ F016 ISOLATE: Check tool budgets, inject pressure warning
  │   └─ Next API call
  │
  ├─ F017 SELECT (post-response): Detect context usage (Phase 6)
  │   └─ Update usage tracker (boost used, penalize unused)
  │
  └─ F015 ISOLATE: If subtask needed, spawn isolated session
```

---

## Failure Modes & Safeguards

### Double-reduction scenario
**Risk:** F016 compaction reduces conversation AND F017 floor filters memory → model has almost no context.
**Safeguard:** These operate on different content. Compaction affects conversation history. Floor affects system prompt retrieval. Even in worst case, the model has: identity prompt + anti-hallucination prompt + whatever passes the floor + recent conversation (keep_recent_tokens).

### Pre-prune facts blocked by quality gate
**Risk:** F016 extracts facts at confidence=0.3, F017 floor at 0.45 blocks them.
**Safeguard:** `FLOOR_EXEMPT_SOURCES = {"pre_prune_extraction"}`. Extracted facts bypass the floor.

### Stale but critical context
**Risk:** Staleness penalty decays a foundational decision to below the floor.
**Safeguard:** `rule` and `preference` categories are exempt from staleness. 30% floor on decay prevents complete disappearance. Usage_boost rescues items the model keeps referencing.

### Code files pruned too aggressively
**Risk:** Source code from `read_file` gets metadata-degraded, losing structural info.
**Safeguard:** `preserve` profile skips metadata degradation entirely. Hard-clear at age 20. By then, compaction should have summarized with full code context available.

### Context pressure ignored
**Risk:** Model ignores the 40K pressure warning and keeps reading files.
**Safeguard:** Soft tool budgets (F016 §5.2) provide a second signal. Anti-hallucination prompt provides a third. If all three fail, the 4-tier pruning pipeline handles the cleanup gracefully.

---

## Spec Dependencies

```
F014 (Frame Scaffolds)
  │
  ├── F015 (Subtask Hardening) ─── ISOLATE
  │     └── per-frame timeout/tool config
  │
  ├── F016 (Context Pruning) ──── WRITE + COMPRESS
  │     ├── 4-tier pruning pipeline
  │     ├── content-type profiles
  │     ├── pre-prune fact extraction
  │     ├── model-aware compaction
  │     ├── anti-hallucination prompt
  │     └── context pressure warning
  │
  └── F017 (Context Quality Gate) ─ SELECT
        ├── relevance floor
        ├── diminishing returns cutoff
        ├── staleness penalty
        ├── model-aware budget scaling
        └── ACE-style usage tracking
```

**F014 is the foundation.** Frame classification determines budgets (F017), tool limits (F015), decay profiles (F016), and conversation window sizes. Without correct frame detection, the downstream specs optimize for the wrong context.

---

## Implementation Order

The specs should be implemented in this order to avoid regressions:

| Order | What | Why |
|-------|------|-----|
| 1 | F016 Phase 0 (anti-hallucination prompt) | Zero cost, immediate impact, no dependencies |
| 2 | F016 Phase 1 (4-tier pruning + re-fetch hints) | Primary hallucination fix |
| 3 | F017 Phase 1-2 (floor + cutoff) | Prevents quality regression when budgets scale |
| 4 | F016 Phase 2 (model-aware thresholds) | Unlocks 1M context window |
| 5 | F017 Phase 3 (budget scaling) | Only safe after floor is in place |
| 6 | F016 Phase 3 (context health logging) | Observability for tuning |
| 7 | F017 Phase 4 (fill ratio logging) | Observability for tuning |
| 8 | F016 Phase 4 (content-type profiles) | Refinement |
| 9 | F016 Phase 5 (pressure warning + tool budgets) | Refinement |
| 10 | F017 Phase 5 (staleness penalty) | Refinement, needs data |
| 11 | F017 Phase 6 (ACE usage tracking) | Advanced, needs usage data |
| 12 | F015 (subtask hardening) | Independent, can ship anytime |

**Critical constraint:** F017 Phase 3 (budget scaling) MUST NOT ship before F017 Phase 1 (floor). Scaling budgets without a quality gate will flood context with noise.

---

## Research Backing

| Source | Key Finding | Applied In |
|--------|------------|-----------|
| Anthropic (context engineering) | "Safest, lightest-touch compaction" = tool result clearing | F016 metadata degradation |
| Google ADK | "Compiled view" processor pipeline | F017 assembly pipeline |
| Stanford ACE (ICLR 2026) | Evolving playbooks: +10.6% on benchmarks | F017 Phase 6 (usage tracking) |
| JetBrains | Simple masking ≈ LLM summarization at half cost | F016 metadata traces |
| Anthropic guidance | "Offload before compress" | F016 pre-prune extraction |
| SWE-Agent | Better tools reduce context needs more than better pruning | F015 tool design |
| Context engineering research | 300 focused tokens > 113K unfocused | F017 relevance floor |
| Context rot research | Sharp cliffs, not gradual decline | F017 diminishing returns cutoff |

---

*This document is a map, not a plan. The individual specs (F014-F017) contain the implementation details. This shows how they fit together.*
