# F016 — Context Pruning Review & Anti-Hallucination Hardening

**Status:** Draft
**Author:** Emerson (analysis & spec), Tim (requirements)
**Created:** 2026-03-06
**Priority:** Critical
**Trigger:** Nous hallucinating on long-running sessions. Suspected root cause: context pruning destroying information the model needs.

---

## Problem

Nous starts hallucinating during extended sessions — producing responses that reference non-existent prior context or confuse details from different parts of the conversation. This is consistent with the model losing critical context through aggressive pruning while retaining enough fragments to create false associations.

---

## Current Architecture (Code Analysis)

### Three Independent Context Management Layers

**Layer 0: History Window (runner.py:1334)**
- When compaction is DISABLED: `messages[-MAX_HISTORY_MESSAGES:]` where `MAX_HISTORY_MESSAGES = 20`
- When compaction is ENABLED: ALL messages are kept (compaction manages size)
- `compaction_enabled` defaults to **`False`** → so the active behavior is a hard 20-message window
- **Critical:** This is a message count, not a turn count. In a tool-heavy turn, a single user request generates 1 user message + 1 assistant message + N tool result messages. A 10-turn session with 3 tool calls per turn = 70+ messages, but only the last 20 are kept.

**Layer 1: Tool Output Pruning (compaction.py, per-turn)**
Applied after EACH tool execution cycle, before the next API call:

| Setting | Default | Effect |
|---------|---------|--------|
| `tool_soft_trim_chars` | 4,000 | Results > 4K chars get head+tail trimmed |
| `tool_soft_trim_head` | 1,500 | Keep first 1,500 chars |
| `tool_soft_trim_tail` | 1,500 | Keep last 1,500 chars |
| `tool_hard_clear_after` | 6 | After 6 newer tool results, old ones are fully replaced with placeholder |
| `keep_last_tool_results` | 2 | Last 2 tool results are protected from any pruning |

**Layer 2: History Compaction (compaction.py, pre-turn)**
LLM-powered summarization when total tokens exceed threshold:
- `compaction_enabled`: **`False` (disabled by default)**
- `compaction_threshold`: 100,000 tokens
- `keep_recent_tokens`: 20,000
- Generates structured summary via LLM, replaces old messages with summary + recent messages

**Layer 3: Context Budget (schemas.py, per-turn)**
Frame-specific token budgets for the SYSTEM PROMPT context assembly:

| Frame | Total Budget | Decisions | Facts | Episodes | Conv Window |
|-------|-------------|-----------|-------|----------|-------------|
| conversation | 3,000 | 500 | 500 | 0 | 3 turns |
| question | 6,000 | 1,000 | 1,500 | 500 | 5 turns |
| task | 8,000 | 2,000 | 1,500 | 1,000 | 5 turns |
| decision | 12,000 | 3,000 | 2,000 | 1,000 | 8 turns |
| debug | 10,000 | 1,500 | 1,000 | 1,000 | 6 turns |
| creative | 6,000 | 1,000 | 1,500 | 500 | 4 turns |

---

## Root Cause Analysis

### Hypothesis 1: Hard History Window + Tool-Heavy Turns (HIGH CONFIDENCE)

**The 20-message hard window is the most likely hallucination source.**

Consider a debugging session where each turn involves 3 tool calls:
- Turn 1: user(1) + assistant(1) + tool_results(1) + assistant(1) + tool_results(1) + assistant(1) + tool_results(1) + assistant(1) = 8 messages
- Turn 2: same = 8 messages
- Turn 3: same = 8 messages → total 24 messages

By turn 3, the first 4 messages of turn 1 are already gone. The user's original problem statement, the first diagnostic results, and the initial assistant reasoning are silently dropped. The model sees tool results from turn 2 onward but has no context for WHY those tools were called.

**The model doesn't know what it doesn't know.** It sees partial tool results and infers (hallucinates) what the earlier context must have been.

### Hypothesis 2: Tool Output Pruning Destroys Critical Middle Content (MEDIUM CONFIDENCE)

The head+tail trim strategy (keep first 1,500 + last 1,500, drop the middle) is reasonable for log-like outputs but destructive for structured data:
- JSON objects: first 1,500 chars has keys/structure, last 1,500 has closing braces. The actual data is in the middle.
- Code files: first 1,500 chars has imports/headers, last 1,500 has the end. The function the model was looking at is in the middle.
- Database results: first rows and last rows survive, but the relevant row might be in the middle.

After 6 newer tool calls, old results are replaced entirely with `"[Tool output cleared - content was processed in earlier turns]"`. The model sees that a tool was called but has zero information about what it returned. It has to guess — and guessing is hallucinating.

### Hypothesis 3: Compaction Disabled = No Safety Net (MEDIUM CONFIDENCE)

With `compaction_enabled=False` (the default), there is NO mechanism to summarize old conversation context. The system relies entirely on the 20-message window. When that window slides past critical information, it's gone forever for that session.

Compaction would at least preserve a structured summary. Without it, the model has an abrupt amnesia boundary.

### Hypothesis 4: Context Assembly Dedup Removes Relevant Content (LOW CONFIDENCE)

PR #101 added identity overlap filtering (`text_overlap > 0.6` → filter out). If this is too aggressive, relevant facts could be silently dropped from context. The `_is_system_episode()` filter could also exclude episode context that provides important session history.

However, these filters operate on the system prompt context, not on conversation history. They're unlikely to cause the kind of mid-conversation hallucination Tim is seeing.

### Hypothesis 5: Frame Switching Resets Context Incorrectly (LOW CONFIDENCE)

When the frame changes mid-conversation (e.g., conversation → debug), the context budget changes dramatically (3K → 10K). The system prompt is rebuilt from scratch with different retrieval priorities. If the frame detector oscillates between frames on consecutive turns, the model gets inconsistent context each turn.

---

## Proposed Changes

### Phase 1: Fix the Hard Window (Critical)

#### 1.1 Enable Compaction by Default

Change `compaction_enabled` default from `False` to `True`. This is the single highest-impact change.

```python
compaction_enabled: bool = Field(
    default=True, validation_alias="NOUS_COMPACTION_ENABLED"
)
```

With compaction enabled, `_format_messages()` returns ALL messages (compaction manages size via summarization). The abrupt 20-message amnesia boundary is replaced with a structured summary.

#### 1.2 Increase MAX_HISTORY_MESSAGES for Non-Compaction Mode

If compaction remains disabled, increase `MAX_HISTORY_MESSAGES` from 20 to at least 40, and make it configurable:

```python
max_history_messages: int = Field(
    default=40, validation_alias="NOUS_MAX_HISTORY_MESSAGES"
)
```

#### 1.3 Count by Turns, Not Messages

The fundamental issue is that message count != turn count. Replace the raw message window with a turn-aware window:

```python
def _format_messages(self, conversation: Conversation) -> list[dict[str, Any]]:
    if self._settings.compaction_enabled:
        return [{"role": m.role, "content": m.content} for m in conversation.messages]

    # Keep last N *turns* (user+assistant+tools = 1 turn)
    max_turns = self._settings.max_history_turns  # default 10
    messages = [{"role": m.role, "content": m.content} for m in conversation.messages]

    # Walk backwards counting user messages (= turn boundaries)
    turn_count = 0
    cut_index = len(messages)
    for i in range(len(messages) - 1, -1, -1):
        if messages[i]["role"] == "user" and isinstance(messages[i]["content"], str):
            turn_count += 1
            if turn_count > max_turns:
                cut_index = i
                break

    return messages[cut_index:]
```

### Phase 2: Smarter Tool Pruning

#### 2.1 Content-Aware Trimming

Instead of blind head+tail, detect content type and trim accordingly:

```python
def _smart_trim(self, text: str, max_chars: int) -> str:
    """Content-aware trimming that preserves structure."""
    if len(text) <= max_chars:
        return text

    # JSON: try to parse and truncate values, keeping keys
    if text.lstrip().startswith('{') or text.lstrip().startswith('['):
        return self._trim_json(text, max_chars)

    # Code: keep function signatures and key lines
    if any(kw in text[:200] for kw in ['def ', 'class ', 'function ', 'import ']):
        return self._trim_code(text, max_chars)

    # Default: head + tail (existing behavior)
    head = max_chars // 2
    tail = max_chars // 2
    return f"{text[:head]}\n\n--- trimmed ({len(text)} chars) ---\n\n{text[-tail:]}"
```

#### 2.2 Gradual Degradation Instead of Hard Clear

Replace the binary hard-clear (full content → placeholder) with progressive summarization:

| Age (newer results after) | Action |
|--------------------------|--------|
| 0-2 | Full content (protected) |
| 3-5 | Soft-trim to head+tail |
| 6-8 | Compress to 500 char summary |
| 9+ | Single-line description: `"[recall_deep('user email'): returned 3 facts about email preferences]"` |

The key insight: even a single-line description of WHAT a tool returned is vastly better than a generic placeholder. It prevents the model from hallucinating what the result was.

```python
# In prune_tool_results(), replace hard-clear with:
if age > self._settings.tool_summary_after:  # new setting, default 8
    for item in content:
        text = item.get("content", "")
        if isinstance(text, str) and len(text) > 200:
            # Keep first line + character count as metadata
            first_line = text.split('\n')[0][:150]
            item["content"] = (
                f"[Tool output summarized - {len(text)} chars, "
                f"first line: {first_line}]"
            )
elif age > self._settings.tool_hard_clear_after:
    # Existing soft-trim behavior (head + tail)
    ...
```

#### 2.3 Tool Result Tagging

Tag each tool result with its turn number so the model can understand the temporal sequence:

```python
tool_results_for_message.append({
    "type": "tool_result",
    "tool_use_id": tc["id"],
    "content": f"[Turn {turns+1}] {result_text}",
    "is_error": is_error,
})
```

### Phase 3: Compaction Quality Improvements

#### 3.1 Richer Summary Prompt

The current summary prompt produces 800-1200 word summaries. For tool-heavy sessions, this misses critical details. Add:

```
## Tool Results Summary
- [List key tool calls and their essential results]
- [Include file paths, error messages, API responses that were acted on]
```

#### 3.2 Incremental Summaries

Instead of one big summary that replaces everything, maintain a rolling summary that's updated every N turns:

```python
compaction_interval: int = Field(
    default=10, validation_alias="NOUS_COMPACTION_INTERVAL"
)  # Compact every N turns instead of waiting for threshold
```

This prevents the "cliff" where a massive amount of context is suddenly compressed.

#### 3.3 Summary Validation Enhancement

Current validation only checks length and section presence. Add:
- Verify key entity preservation (file paths, function names, error messages from old context appear in summary)
- Verify user's original question/goal is preserved
- If validation fails, fall back to keeping more raw messages instead of truncating

### Phase 4: Observability

#### 4.1 Context Health Logging

Log context health metrics per turn at INFO level:

```python
logger.info(
    "Context health: messages=%d, turns=%d, pruned=%d, "
    "compacted=%d, system_tokens=%d, history_tokens=%d, "
    "frame=%s, budget=%d",
    len(messages), turn_count, pruned_count,
    conversation.compaction_count, system_tokens,
    history_tokens, frame_id, budget.total,
)
```

#### 4.2 Hallucination Detection Heuristic

After each assistant response, check for references to non-existent context:

```python
async def _check_context_coherence(self, response: str, messages: list) -> bool:
    """Detect potential hallucination from lost context.

    Heuristic: if the response references specific details (file paths,
    function names, error messages) that don't appear in ANY message
    in the current window, flag it.
    """
    # Extract specific references from response
    # Compare against all available context
    # Log warning if orphaned references found
```

This is a heuristic, not a guarantee — but it provides early warning.

---

## Affected Files

| File | Change | Phase |
|------|--------|-------|
| `nous/config.py` | New settings, enable compaction default | 1 |
| `nous/api/runner.py` | Turn-aware history window, `MAX_HISTORY_MESSAGES` configurable | 1 |
| `nous/api/compaction.py` | Smart trim, gradual degradation, richer summary | 2-3 |
| `nous/cognitive/context.py` | Context health logging | 4 |
| `nous/cognitive/schemas.py` | Context health metrics model | 4 |

---

## Implementation Priority

1. **Enable compaction by default** — single line change, highest impact
2. **Turn-aware history window** — prevents tool-call message explosion from eating context
3. **Gradual tool result degradation** — replace hard-clear with descriptive placeholders
4. **Context health logging** — observability to diagnose future issues
5. **Content-aware trimming** — nice-to-have, improves quality for structured data
6. **Hallucination detection** — experimental, helps measure improvement

---

## Success Metrics

- No reported hallucinations during 10+ turn debug sessions
- Tool result pruning preserves enough information for the model to reference prior results accurately
- Compaction summaries preserve the user's original goal and key decisions
- Context health metrics visible in INFO logs for every turn

---

## Risk Assessment

| Change | Risk | Mitigation |
|--------|------|------------|
| Enable compaction | LLM summary quality varies; bad summary = context corruption | Validation + fallback to truncation (already implemented) |
| Turn-aware window | Larger window = more tokens = higher cost | Bounded by compaction threshold; cost increase is modest (~20%) |
| Gradual degradation | More preserved content = slightly larger message payloads | Summaries are short (< 200 chars each); net effect is small |
| Content-aware trim | JSON/code parsers could fail on malformed input | Fallback to head+tail (existing behavior) on any parse error |

---

## Open Questions

1. **Should compaction use a different model?** Currently uses `background_model`. Summarization quality is critical — might warrant a stronger model for compaction specifically.
2. **Should tool results from the current turn ever be pruned?** Currently only cross-turn pruning happens. Within a single turn, all tool results accumulate without limit.
3. **Is the 100K token compaction threshold too high?** Claude's context is 200K, but quality degrades well before that. Consider lowering to 60-80K.
4. **Should the conversation summary be stored in the DB?** Currently it's in-memory only (`Conversation.summary`). If the container restarts mid-session, the summary is lost and the model starts fresh with no history.
