# F016 — Context Pruning Review & Anti-Hallucination Hardening

**Status:** Draft (v2 — revised after code analysis)
**Author:** Emerson (analysis & spec), Tim (requirements)
**Created:** 2026-03-06
**Revised:** 2026-03-07
**Priority:** Critical
**Trigger:** Nous hallucinating on long-running sessions. Root cause confirmed: tool pruning hard-clear destroying context the model needs.

---

## Problem

Nous starts hallucinating during extended sessions — producing responses that reference non-existent prior context or confuse details from different parts of the conversation. The model loses critical context through aggressive tool result pruning while retaining enough fragments to create false associations.

---

## Current Architecture (Code Analysis)

### Production Configuration

| Setting | Code Default | Production Value | Source |
|---------|-------------|-----------------|--------|
| `compaction_enabled` | `False` | **`True`** | `NOUS_COMPACTION_ENABLED` env var |
| `compaction_threshold` | 100,000 | 100,000 | default |
| `keep_recent_tokens` | 20,000 | 20,000 | default |
| `tool_pruning_enabled` | `True` | `True` | default |
| `tool_hard_clear_after` | 6 | 6 | default |
| `keep_last_tool_results` | 2 | 2 | default |
| `tool_soft_trim_chars` | 4,000 | 4,000 | default |
| `tool_soft_trim_head` | 1,500 | 1,500 | default |
| `tool_soft_trim_tail` | 1,500 | 1,500 | default |
| `max_turns` | 10 | 10 | default (tool loop iterations) |
| `background_model` | claude-sonnet-4-5 | claude-sonnet-4-5 | default |

### Context Management Layers (in execution order)

**Layer 0: History Window (runner.py:1334)**
- Compaction ENABLED (production): ALL messages kept → compaction manages size
- Compaction DISABLED (code default): `messages[-20:]` hard window
- **Status: NOT the problem** — compaction is on in production

**Layer 1: Tool Output Pruning (compaction.py, per-turn) ← PRIMARY SUSPECT**
Applied after EACH tool execution cycle, before the next API call. Mutates the in-memory messages list.

Pruning logic (from `prune_tool_results()`):
1. Find all tool result message indices in the messages list
2. Protect the last `keep_last_tool_results` (2) tool result messages
3. For unprotected results, calculate `age = len(tool_indices) - position`
4. If `age > tool_hard_clear_after` (6): **replace content with generic placeholder**
5. If `len(content) > tool_soft_trim_chars` (4000): keep first 1500 + last 1500 chars

**Layer 2: History Compaction (compaction.py, pre-turn)**
LLM-powered summarization when `system_tokens + history_tokens > compaction_threshold` (100K):
- Finds cut point keeping `keep_recent_tokens` (20K) of recent messages
- Summarizes old messages via LLM (uses `background_model`)
- Replaces old messages with `[Previous conversation summary]\n\n{summary}` + ack
- Validated: requires 2+ of 3 section patterns (Goal, Progress, Critical Context)
- Fallback: truncation if summary fails validation

**Layer 3: System Prompt Context Budget (schemas.py, per-turn)**
Frame-specific token budgets for context assembly (identity, facts, decisions, episodes):

| Frame | Total | Decisions | Facts | Procedures | Episodes | Conv Window |
|-------|-------|-----------|-------|------------|----------|-------------|
| conversation | 3,000 | 500 | 500 | 0 | 0 | 3 |
| question | 6,000 | 1,000 | 1,500 | 500 | 500 | 5 |
| task | 8,000 | 2,000 | 1,500 | 1,500 | 1,000 | 5 |
| decision | 12,000 | 3,000 | 2,000 | 2,000 | 1,000 | 8 |
| debug | 10,000 | 1,500 | 1,000 | 2,500 | 1,000 | 6 |
| creative | 6,000 | 1,000 | 1,500 | 500 | 500 | 4 |

---

## Root Cause: Confirmed

### Tool Output Hard-Clear (HIGH CONFIDENCE — CONFIRMED BY CODE ANALYSIS)

**The mechanism (simulated 10-turn debug session, 2 tool calls per turn):**

```
Turn 1: tool_results[0,1] — read_file(database.py), bash(grep auth)
Turn 2: tool_results[2,3] — read_file(middleware.py), recall_deep(auth)
Turn 3: tool_results[4,5] — bash(test), read_file(config.py)
Turn 4: tool_results[6,7] — added. Results 0,1 now age=7 → HARD-CLEARED
         database.py content: "[Tool output cleared - content was processed in earlier turns]"
Turn 5: tool_results[8,9] — Results 2,3 age=7 → HARD-CLEARED
         middleware.py content gone
...
Turn 9: User asks "What did we see in database.py earlier?"
         Model's context: placeholder text only. ZERO information about file contents.
         Result: Model must GUESS → HALLUCINATION
```

**Why the model hallucinates instead of saying "I don't remember":**
- The model sees the tool_use block (it knows read_file was called with path=database.py)
- It sees the placeholder response (so it knows a response existed)
- It has semantic memory of being "in the middle of debugging auth" from recent context
- It reconstructs plausible but fabricated content for what the file "must have contained"
- Claude is trained to be helpful — saying "I can't see that anymore" feels like a cop-out

### Ruled Out Hypotheses

| Hypothesis | Status | Reason |
|-----------|--------|--------|
| 20-message hard window | **RULED OUT** | Compaction enabled in prod, window not active |
| Compaction disabled | **RULED OUT** | `NOUS_COMPACTION_ENABLED=true` in prod |
| Context assembly dedup (PR #101) | LOW | Operates on system prompt, not conversation history |
| Frame switching | LOW | Possible minor contributor, recommend logging |
| Compaction summary quality | LOW | Triggers too late (~turn 30-40) to cause early-session hallucination |

---

## Proposed Changes

### Phase 1: Metadata-Based Tool Degradation (Critical — Primary Fix)

**Replace the generic hard-clear placeholder with a descriptive metadata trace.**

Instead of:
```
[Tool output cleared - content was processed in earlier turns]
```

Generate from tool name + input args + first line of output:
```
[read_file(app/database.py): 85 lines | first: import psycopg2 from contextlib import contextmanager]
[recall_deep('user email preference'): 3 results | first: User prefers HTML email format]
[bash(grep -rn "auth" src/): 12 lines | first: src/middleware.py:45: def check_auth(request):]
```

**Implementation in `compaction.py`:**

```python
def _metadata_degrade(self, item: dict[str, Any], tool_use_block: dict[str, Any] | None) -> None:
    """Replace tool result with descriptive metadata trace.

    Preserves: tool name, input args, result size, first meaningful line.
    Cost: ~100-200 chars per degraded result (vs 0 info in hard-clear).
    Latency: <1ms (string manipulation only, no LLM).
    """
    text = item.get("content", "")
    if not isinstance(text, str) or len(text) < 200:
        return  # Don't degrade tiny results

    # Extract tool context from the preceding assistant message's tool_use block
    tool_name = tool_use_block.get("name", "tool") if tool_use_block else "tool"
    tool_input = tool_use_block.get("input", {}) if tool_use_block else {}

    # Build args summary (key=value for dicts, truncated)
    args_parts = []
    for k, v in (tool_input.items() if isinstance(tool_input, dict) else []):
        v_str = str(v)[:80]
        args_parts.append(f"{k}={v_str}")
    args_summary = ", ".join(args_parts[:3])  # max 3 args shown

    # Extract first meaningful line
    first_line = ""
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped and len(stripped) > 5:
            first_line = stripped[:120]
            break

    # Count lines/items
    line_count = text.count("\n") + 1

    item["content"] = (
        f"[{tool_name}({args_summary}): {line_count} lines, "
        f"{len(text)} chars | first: {first_line}]"
    )
```

**Updated pruning pipeline (4 tiers instead of 2):**

| Age (newer results after) | Action | Info Preserved |
|--------------------------|--------|----------------|
| 0-2 | **Full content** (protected) | 100% |
| 3-7 | **Soft-trim** (head 1500 + tail 1500) | ~75% for large results |
| 8-11 | **Metadata degradation** (tool+args+first line) | Key reference info |
| 12+ | **Hard-clear** (minimal placeholder) | Tool name only |

New settings:
```python
tool_metadata_degrade_after: int = Field(
    default=8, validation_alias="NOUS_TOOL_METADATA_DEGRADE_AFTER"
)
tool_hard_clear_after: int = Field(
    default=12, validation_alias="NOUS_TOOL_HARD_CLEAR_AFTER"  # was 6
)
```

**To resolve tool_use context for metadata:** Walk backwards from the tool_result message to find the preceding assistant message containing the matching `tool_use` block (matched by `tool_use_id`). This is already available in the messages list.

```python
def _find_tool_use_block(
    self, messages: list[dict[str, Any]], tool_result_idx: int, tool_use_id: str
) -> dict[str, Any] | None:
    """Find the tool_use block that generated this tool_result."""
    for i in range(tool_result_idx - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if (isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("id") == tool_use_id):
                return block
    return None
```

### Phase 2: Lower Compaction Threshold

Reduce `compaction_threshold` from 100K → 60K tokens:

```python
compaction_threshold: int = Field(
    default=60_000, validation_alias="NOUS_COMPACTION_THRESHOLD"
)
```

**Rationale:** At 100K, compaction doesn't fire until ~turn 30-40 in tool-heavy sessions. At 60K, it fires around turn 15-20 — early enough that the structured summary preserves information BEFORE metadata degradation has replaced too many results.

Also increase `keep_recent_tokens` from 20K → 30K to protect more recent context during compaction:

```python
keep_recent_tokens: int = Field(
    default=30_000, validation_alias="NOUS_KEEP_RECENT_TOKENS"
)
```

### Phase 3: Context Health Logging

Add per-turn observability at INFO level in the tool loop:

```python
if soft_trimmed or hard_cleared or metadata_degraded:
    logger.info(
        "Tool pruning: soft_trimmed=%d, metadata_degraded=%d, hard_cleared=%d, "
        "total_tool_msgs=%d, protected=%d, oldest_age=%d",
        soft_trimmed, metadata_degraded, hard_cleared,
        len(tool_indices), len(protected), max_age,
    )
```

Add pre-turn context health in `run_turn()`:

```python
logger.info(
    "Context health: messages=%d, tool_results=%d, "
    "compactions=%d, system_tokens~=%d, history_tokens~=%d, "
    "frame=%s",
    len(messages), tool_result_count,
    conversation.compaction_count, system_tokens, history_tokens,
    turn_context.frame.frame_id,
)
```

### Phase 4: Safety Net Improvements (Lower Priority)

#### 4.1 Enable Compaction by Default

Change code default to match production:
```python
compaction_enabled: bool = Field(
    default=True, validation_alias="NOUS_COMPACTION_ENABLED"
)
```

#### 4.2 Turn-Aware History Window (Fallback Mode)

For cases where compaction is disabled, count turns instead of messages:

```python
max_history_turns: int = Field(
    default=10, validation_alias="NOUS_MAX_HISTORY_TURNS"
)
```

Walk backwards counting user text messages (not tool_result messages) as turn boundaries.

#### 4.3 Content-Aware Soft Trimming

Detect content type before applying head+tail trim:
- JSON: preserve keys/structure, truncate values
- Code: keep signatures and relevant functions
- Logs/output: head+tail (current behavior — already optimal for this type)

Fallback to head+tail on any parse error.

---

## Affected Files

| File | Change | Phase |
|------|--------|-------|
| `nous/api/compaction.py` | `_metadata_degrade()`, `_find_tool_use_block()`, updated `prune_tool_results()` with 4-tier pipeline | 1 |
| `nous/config.py` | `tool_metadata_degrade_after=8`, `tool_hard_clear_after=12`, `compaction_threshold=60000`, `keep_recent_tokens=30000`, `compaction_enabled=True` default | 1-2 |
| `nous/api/runner.py` | Context health logging, turn-aware window (phase 4) | 3-4 |

---

## Implementation Priority

1. **Metadata-based tool degradation** — 4-tier pruning pipeline replacing hard-clear (PRIMARY FIX)
2. **Increase hard-clear age** from 6 → 12 (gives metadata tier room to work)
3. **Lower compaction threshold** — 100K → 60K tokens
4. **Context health logging** — observability per turn
5. **Enable compaction default** — align code with production
6. **Turn-aware history window** — safety net for non-compaction mode
7. **Content-aware trimming** — nice-to-have

---

## Token Cost Analysis

| Change | Impact |
|--------|--------|
| Metadata traces (~150 chars each) replacing placeholders (~60 chars) | +~90 chars × N degraded results = **+500-1500 tokens per long session** |
| Lower compaction threshold (60K vs 100K) | Compaction fires earlier → more frequent LLM summary calls (~1 extra per long session) |
| Context health logging | Zero token cost (server-side only) |
| **Total estimated increase** | **< 10% per session** |

---

## Success Metrics

- No reported hallucinations during 10+ turn debug/task sessions
- Tool result metadata traces preserve enough info for model to accurately reference prior results
- Compaction summaries fire before turn 20 in tool-heavy sessions
- Context health logs visible at INFO level for every turn
- Zero false references to non-existent context in model responses

---

## Risk Assessment

| Change | Risk | Mitigation |
|--------|------|------------|
| Metadata degradation | `_find_tool_use_block()` may not find match (deleted by compaction) | Graceful fallback: use generic `"[tool result: N chars]"` if no tool_use block found |
| Lower compaction threshold | More frequent LLM summarization calls | Cost is one background-model call per compaction; ~$0.01-0.02 per session |
| Hard-clear age 12 | More tool results in context = larger payloads | Metadata-degraded results are ~150 chars each; 6 extra × 150 = 900 chars total |

---

## Open Questions

1. Should compaction summaries include a "Tool Results Digest" section listing key tool calls and their essential outputs? Currently the summary prompt doesn't specifically ask for tool result preservation.
2. Should the metadata degradation format be customizable per tool? e.g., `read_file` shows filename + line count, `recall_deep` shows query + result count, `bash` shows command + exit code.
3. Should `_find_tool_use_block()` search be cached? In a long session with many messages, walking backwards each time could be slow. A dict mapping `tool_use_id → block` built during the tool loop would be O(1).
