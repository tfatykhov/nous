# F016 — Context Pruning Review & Anti-Hallucination Hardening

**Status:** Draft (v4 — context pressure signaling + scope boundaries)
**Author:** Emerson (analysis & spec), Tim (requirements), Nous (research)
**Created:** 2026-03-06
**Revised:** 2026-03-07
**Priority:** Critical
**Trigger:** Nous hallucinating on long-running sessions. Root cause confirmed: tool pruning hard-clear destroying context the model needs.
**Reviews:** Architecture review (no P1s), Correctness review (1 P1, 4 P2s) — all addressed in v3.
**v4 additions:** Context pressure warning, soft tool budgets, large codebase scope boundary.
**v5 additions:** P0 anti-hallucination system prompt, re-fetch hints in metadata traces (from modern techniques review).
**v6 additions:** Model-aware context limits — dynamic compaction thresholds based on model context window (1M for Sonnet/Opus 4.6).
**v7 fixes:** P1 #2 (pre-prune facts survive F017 floor via source tag), P1 #4 (_metadata_degrade small content handling), P2 #1 (explicit env var detection replaces sentinel), P2 #2 (Heart dependency via runner.py caller), P2 #4 (constants centralized in schemas.py).

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

### Phase 0: Anti-Hallucination System Prompt (P0 — Zero Cost)

> **Source:** Modern techniques review. Zero implementation cost, highest impact. The model hallucinates because it tries to be helpful by reconstructing cleared content. Tell it not to.

Add to the identity/system prompt (injected via context assembly):

```
When you encounter a cleared or degraded tool result (marked with [tool result cleared]
or showing only metadata), do NOT attempt to reconstruct or guess the original content.
Instead, say "I'd need to re-read that file" or "Let me fetch that again" and call the
tool again. Results marked with "↺ re-fetchable" can be retrieved by calling the same
tool with the same arguments.
```

**Implementation:** Add as a new section in `cognitive/context.py` context assembly, after the identity prompt. Controlled by a config flag:

```python
anti_hallucination_prompt: bool = Field(
    default=True, validation_alias="NOUS_ANTI_HALLUCINATION_PROMPT"
)
```

**Why this works:** The model already knows the content is gone (it sees the placeholder). It hallucinates because Claude is trained to be helpful — saying "I can't see that anymore" feels like a cop-out. Explicit permission to re-fetch removes that pressure.

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
    if not isinstance(text, str):
        return
    if len(text) < 200:
        return  # Small results: keep as-is (already compact enough).
        # NOTE (v7 fix P1 #4): Items < 200 chars stay at full content
        # until hard-clear age, when they get the standard placeholder.
        # The caller must NOT skip hard-clear for these items.

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

    # Re-fetch hint based on tool type
    refetchable = tool_name in {"read_file", "list_files", "bash", "run_python"}
    refetch_hint = " | ↺ re-fetchable" if refetchable else ""

    item["content"] = (
        f"[{tool_name}({args_summary}): {line_count} lines, "
        f"{len(text)} chars | first: {first_line}{refetch_hint}]"
    )
```

**Updated pruning pipeline (4 tiers instead of 2):**

| Age | Action | Info Preserved |
|-----|--------|----------------|
| 0-2 | **Full content** (protected) | 100% |
| 3-7 | **Soft-trim** (head 1500 + tail 1500) | ~75% for large results |
| 8-11 | **Metadata degradation** (tool+args+first line) | Key reference info |
| 12+ | **Hard-clear** (minimal placeholder) | Tool name only |

> **v3 fix:** Tier boundaries use `>=` comparison. Age 3-7 means `age >= 3 and age < 8`.
> Previous draft had an off-by-one: `>` operator with defaults 8/12 would produce tiers 3-8/9-12/13+.

New settings:
```python
tool_metadata_degrade_after: int = Field(
    default=8, validation_alias="NOUS_TOOL_METADATA_DEGRADE_AFTER"
)
tool_hard_clear_after: int = Field(
    default=12, validation_alias="NOUS_TOOL_HARD_CLEAR_AFTER"  # was 6
)
```

**Config validation (v3 addition):**
```python
@model_validator(mode="after")
def _validate_pruning_tiers(self) -> "Settings":
    if self.tool_metadata_degrade_after >= self.tool_hard_clear_after:
        raise ValueError(
            f"tool_metadata_degrade_after ({self.tool_metadata_degrade_after}) "
            f"must be < tool_hard_clear_after ({self.tool_hard_clear_after})"
        )
    return self
```

**To resolve tool_use context for metadata:** Build a lookup dict at the start of `prune_tool_results()` mapping `tool_use_id → tool_use block`. This is O(N) once instead of O(N²) per degradation.

> **v3 change:** Replaced backward linear search with pre-built dict (architecture review P2). Compaction runs pre-turn, so tool_use blocks are never deleted during the tool loop — the dict is always complete.

```python
def _build_tool_use_index(
    self, messages: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Build tool_use_id → tool_use block index. O(N) once."""
    index: dict[str, dict[str, Any]] = {}
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tool_id = block.get("id")
                if tool_id:
                    index[tool_id] = block
    return index
```

Usage in `prune_tool_results()`:
```python
tool_use_index = self._build_tool_use_index(messages)
# ...
tool_use_block = tool_use_index.get(tool_use_id)
```

### Phase 2: Model-Aware Context Limits

> **v6 revision:** Original v2-v4 proposed lowering compaction threshold from 100K → 60K. This was based on the assumption that Claude's context window was 200K. Claude Sonnet 4.6 and Opus 4.6 support **1M tokens**. At 1M, compacting at 100K means Nous uses only 10% of the available window — the aggressive pruning is largely self-inflicted.

#### 2.1 Model Context Window Registry

```python
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    # 1M context models
    "claude-sonnet-4-6": 1_000_000,
    "claude-opus-4-6": 1_000_000,
    # 200K context models
    "claude-sonnet-4-5": 200_000,
    "claude-opus-4-5": 200_000,
    # 128K context models
    "gpt-4o": 128_000,
    "gpt-4-turbo": 128_000,
}

def _get_context_window(self, model: str) -> int:
    """Look up context window for model. Default 200K for unknown models."""
    for key, window in MODEL_CONTEXT_WINDOWS.items():
        if key in model:
            return window
    return 200_000
```

#### 2.2 Dynamic Compaction Thresholds

Scale compaction thresholds as a percentage of the model's context window:

```python
# Compaction fires at 60% of context window
COMPACTION_THRESHOLD_RATIO = 0.60
# Keep 20% of context window as recent context during compaction
KEEP_RECENT_RATIO = 0.20
```

| Model | Context Window | Compaction Threshold (60%) | Keep Recent (20%) | Usable Before Compaction |
|-------|---------------|---------------------------|-------------------|-------------------------|
| Sonnet/Opus 4.6 | 1,000,000 | 600,000 | 200,000 | **600K tokens** |
| Sonnet/Opus 4.5 | 200,000 | 120,000 | 40,000 | 120K tokens |
| GPT-4o | 128,000 | 76,800 | 25,600 | 76K tokens |

```python
# Track whether values were explicitly set via env var
_compaction_threshold_explicit: bool = False
_keep_recent_explicit: bool = False

@model_validator(mode="after")
def _detect_explicit_overrides(self) -> "Settings":
    """Detect if compaction settings were explicitly provided via env vars.

    v7 fix (P2 #1): The old approach compared against sentinel values
    (e.g., != 100_000), which broke if someone explicitly set the value
    to the default. Instead, check if the env var is actually set.
    """
    import os
    self._compaction_threshold_explicit = (
        "NOUS_COMPACTION_THRESHOLD" in os.environ
    )
    self._keep_recent_explicit = (
        "NOUS_KEEP_RECENT_TOKENS" in os.environ
    )
    return self

@property
def effective_compaction_threshold(self) -> int:
    """Dynamic threshold based on model context window."""
    if self._compaction_threshold_explicit:
        return self.compaction_threshold  # Explicit override takes priority
    window = self._get_context_window(self.model)
    return int(window * COMPACTION_THRESHOLD_RATIO)

@property
def effective_keep_recent(self) -> int:
    """Dynamic keep_recent based on model context window."""
    if self._keep_recent_explicit:
        return self.keep_recent_tokens  # Explicit override takes priority
    window = self._get_context_window(self.model)
    return int(window * KEEP_RECENT_RATIO)
```

**Impact for Sonnet/Opus 4.6 (1M context):**
- Compaction doesn't fire until **600K tokens** (~150-200 turns with heavy tool use)
- Most sessions will never hit compaction at all
- The 4-tier pruning pipeline (Phase 1) still applies but kicks in much later
- Tool results can stay at full content for 10x longer

**Backward compatibility:** If `NOUS_COMPACTION_THRESHOLD` is explicitly set, it takes priority over the dynamic calculation. Existing deployments are unaffected.

> **⚠️ Cost consideration:** 1M context = higher per-request cost. Anthropic charges per input token. A session using 500K tokens of context costs ~5x more than one using 100K. The dynamic threshold should be paired with context health logging (Phase 3) so operators can monitor actual usage and tune the ratio if needed.

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

### Phase 4: Content-Type-Aware Pruning (v3 — from Nous research)

> **Source:** Nous's independent research. JetBrains paper confirms simple masking matches LLM summarization at half cost. Anthropic guidance: "offload before compress."

Different tools should decay at different rates based on re-fetchability:

```python
TOOL_DECAY_PROFILES: dict[str, str] = {
    # Preserve — code/source files: skip metadata degradation entirely,
    # trust compaction to summarize. Too information-dense for lossy pruning.
    "read_file": "preserve",
    # Aggressive — content is re-readable on demand or already stored
    "list_files": "aggressive",
    "recall_deep": "aggressive",  # already in DB
    # Standard — not easily re-fetched, use default tiers
    "bash": "standard",
    "run_python": "standard",
    # Conservative — extract facts BEFORE clearing (not re-fetchable)
    "web_search": "conservative",
}
```

| Profile | Soft-trim age | Metadata age | Hard-clear age | Rationale |
|---------|--------------|--------------|----------------|-----------|
| preserve | 8 | _(skipped)_ | 20 | Code is too dense for lossy metadata traces. Keep full (soft-trimmed) content long enough for compaction to summarize it properly. |
| aggressive | 2 | 4 | 8 | Re-readable or already persisted |
| standard | 3 | 8 | 12 | Default tier progression |
| conservative | 5 | 10 | 15 | Extract facts before clearing |

> **v6 note:** These ages are defaults for 200K context models. With 1M context (Sonnet/Opus 4.6), the 4-tier pipeline still applies but matters less — at 600K compaction threshold, most sessions won't accumulate enough tool results for even the `standard` profile to hit hard-clear. Consider scaling ages proportionally to context window in a future iteration, but the current defaults are a safe floor.

#### 4.0.1 Pre-Prune Fact Extraction (~30 LOC)

Before hard-clearing `conservative` tools, regex-extract key information and store as low-confidence facts. Zero LLM cost.

```python
import re

def _extract_facts_before_clear(self, tool_name: str, content: str) -> list[str]:
    """Extract URLs, paths, numbers, and names before hard-clearing."""
    facts = []
    # URLs
    facts.extend(re.findall(r'https?://[^\s\'"<>]+', content))
    # File paths
    facts.extend(re.findall(r'(?:/[\w.-]+){2,}', content))
    # Key-value patterns (e.g., "version: 3.2.1")
    facts.extend(re.findall(r'\b\w+:\s+[\w.-]+', content)[:5])
    return facts[:10]  # Cap at 10 facts per result
```

> **v7 note (P2 #2 — Heart dependency):** `_extract_facts_before_clear()` needs to store facts in Heart, but `compaction.py` currently has no Heart dependency. Implementation approach: `prune_tool_results()` returns a list of extracted facts. The caller in `runner.py` (which already has Heart access) stores them. This keeps compaction.py stateless and avoids injecting a new dependency.

```python
# In runner.py (caller), after prune_tool_results():
extracted_facts = self._compaction.prune_tool_results(messages, frame)
if extracted_facts:
    for fact_text in extracted_facts:
        await self._heart.store_fact(
            content=fact_text,
            category="technical",
            confidence=0.3,
            source="pre_prune_extraction",
        )
```

These extracted facts are stored via Heart as `confidence=0.3` facts with `source="pre_prune_extraction"` tag (auto-expire, won't clutter context).

> **v7 fix (P1 #2 — F016/F017 interaction):** Extracted facts at `confidence=0.3` would be blocked by F017's relevance floor (0.45). Fix: extracted facts are tagged with `source="pre_prune_extraction"`. F017's relevance floor exempts this source tag — these facts exist specifically because the original content was destroyed, so filtering them defeats the purpose. They still compete on score for budget space and auto-expire via Heart's cleanup.

#### 4.0.2 Frame-Adaptive Window Sizes (~20 LOC)

Override `keep_last_tool_results` based on active frame:

```python
FRAME_TOOL_WINDOWS: dict[str, int] = {
    "debug": 4,          # Need error traces
    "decision": 3,       # Need evidence
    "task": 2,           # Default
    "conversation": 2,   # Default
    "research": 1,       # Results go to memory
    "creative": 1,       # Minimal tool context needed
}
```

### Phase 5: Context Pressure Signaling (v4)

The `preserve` profile keeps source code in context much longer (hard-clear at age 20), which works for normal sessions (5-10 file reads). But for deep code analysis (20+ files), the model will exhaust context without realizing it. Pruning is reactive - it cleans up *after* the model has already read too many files. We need to intervene *before* the next tool call.

#### 5.1 Context Pressure Warning

Inject a system-level warning into the conversation when tool result tokens exceed a threshold:

```python
TOOL_CONTENT_WARNING_THRESHOLD = 40_000  # tokens

def _check_context_pressure(self, messages: list[dict[str, Any]]) -> str | None:
    """Return warning message if tool results are consuming too much context."""
    tool_tokens = sum(
        self._estimate_tokens(msg.get("content", ""))
        for msg in messages
        if msg.get("role") == "user"
        and isinstance(msg.get("content"), list)
        and any(
            b.get("type") == "tool_result"
            for b in msg["content"]
            if isinstance(b, dict)
        )
    )
    if tool_tokens > TOOL_CONTENT_WARNING_THRESHOLD:
        return (
            "⚠️ Context pressure: tool results are consuming "
            f"~{tool_tokens:,} tokens. Summarize key findings to memory "
            "before reading more files. Use write_file or record_fact "
            "to offload what you've learned."
        )
    return None
```

Injected as a system message before the next API call in `run_turn()`. The model sees it and can decide to offload.

#### 5.2 Tool Call Budget (Cross-ref F015)

F015 already specs per-frame tool budgets. For `read_file` specifically, enforce a soft limit:

```python
FRAME_TOOL_LIMITS: dict[str, dict[str, int]] = {
    "debug": {"read_file": 12, "bash": 20},
    "task": {"read_file": 10, "bash": 15},
    "research": {"read_file": 8, "bash": 10},
    "conversation": {"read_file": 4, "bash": 5},
}
```

When limit is hit, the tool returns a warning instead of an error:
```
⚠️ read_file limit reached (10/10 for task frame). You can still call read_file,
but consider: have you stored your findings? Use write_file to save a summary
of what you've learned before reading more files.
```

This is a soft limit (warning, not block) because sometimes the model genuinely needs more files. But the friction forces a conscious decision.

#### 5.3 Scope Boundary: Large Codebase Analysis

> **F016 does NOT solve large codebase analysis (100+ files).** No pruning strategy can keep 100 source files in a 200K context window. That problem requires **retrieval** (semantic code index + targeted chunk retrieval), not **retention** (keeping everything in context).
>
> A future spec (code indexing / semantic code search) should address:
> - Embedding functions/classes/modules into Heart
> - `recall_code("authentication middleware")` → returns relevant chunks
> - `read_file` becomes a targeted follow-up, not a scanning tool
> - Task definitions reference repos; Nous indexes on first use
>
> F016's scope: prevent hallucination from context loss in normal sessions (5-20 file reads). The context pressure warning (Phase 5.1) and tool budgets (Phase 5.2) serve as a bridge — they nudge the model toward offload-and-retrieve patterns even without a dedicated code index.

### Phase 6: Safety Net Improvements (Lower Priority)

#### 6.1 Enable Compaction by Default

Change code default to match production:
```python
compaction_enabled: bool = Field(
    default=True, validation_alias="NOUS_COMPACTION_ENABLED"
)
```

#### 6.2 Turn-Aware History Window (Fallback Mode)

For cases where compaction is disabled, count turns instead of messages:

```python
max_history_turns: int = Field(
    default=10, validation_alias="NOUS_MAX_HISTORY_TURNS"
)
```

Walk backwards counting user text messages (not tool_result messages) as turn boundaries.

#### 6.3 Content-Aware Soft Trimming

Detect content type before applying head+tail trim:
- JSON: preserve keys/structure, truncate values
- Code: keep signatures and relevant functions
- Logs/output: head+tail (current behavior — already optimal for this type)

Fallback to head+tail on any parse error.

---

## Affected Files

| File | Change | Phase |
|------|--------|-------|
| `nous/api/compaction.py` | `_build_tool_use_index()`, `_metadata_degrade()` (with re-fetch hints), updated `prune_tool_results()` with 4-tier pipeline (returns extracted facts list) | 1, 4 |
| `nous/cognitive/context.py` | Anti-hallucination prompt injection in context assembly | 0 |
| `nous/cognitive/schemas.py` | `TOOL_DECAY_PROFILES`, `FRAME_TOOL_WINDOWS`, `FRAME_TOOL_LIMITS`, `MODEL_CONTEXT_WINDOWS` constants (v7 P2 #4: all constants centralized here) | 4, 5 |
| `nous/config.py` | `anti_hallucination_prompt`, `tool_metadata_degrade_after`, `tool_hard_clear_after`, `compaction_enabled` default, `model_validator` for tier ordering, `_detect_explicit_overrides` for dynamic thresholds | 0-2, 5 |
| `nous/api/runner.py` | Context health logging, context pressure warning, turn-aware window | 3, 5, 6 |
| `nous/cognitive/schemas.py` | `FRAME_TOOL_WINDOWS`, `FRAME_TOOL_LIMITS` | 4, 5 |
| `nous/api/tools.py` | Soft tool budget warning in `read_file` response | 5 |

---

## Implementation Priority

1. **Anti-hallucination system prompt** — tell the model to re-fetch instead of guessing (P0, zero cost)
2. **Metadata-based tool degradation** — 4-tier pruning pipeline with re-fetch hints (PRIMARY FIX)
3. **Increase hard-clear age** from 6 → 12 (gives metadata tier room to work)
4. **Config validation** — ensure `degrade_after < hard_clear_after`
5. **Model-aware compaction thresholds** — dynamic based on context window (⚠️ breaking change, document in changelog)
6. **Context health logging** — observability per turn
7. **Content-type-aware pruning** — per-tool decay profiles
8. **Pre-prune fact extraction** — regex capture before hard-clear (conservative tools)
9. **Frame-adaptive window sizes** — override tool result protection per frame
10. **Context pressure warning** — system message when tool tokens > 40K
11. **Soft tool budgets** — per-frame read_file limits with warning (cross-ref F015)
12. **Enable compaction default** — align code with production
13. **Turn-aware history window** — safety net for non-compaction mode
14. **Content-aware trimming** — nice-to-have

---

## Token Cost Analysis

| Change | Impact |
|--------|--------|
| Metadata traces (~150 chars each) replacing placeholders (~60 chars) | +~90 chars × N degraded results = **+500-1500 tokens per long session** |
| Anti-hallucination system prompt | ~80 tokens per request (negligible) |
| Re-fetch hints in metadata | ~5 tokens per degraded result (negligible) |
| Context health logging | Zero token cost (server-side only) |
| **Model-aware thresholds (1M context)** | **⚠️ Significant cost increase.** Sessions that previously compacted at 100K will now run to 600K. At Anthropic's pricing, a 500K input request costs ~5x more than 100K. Operators should monitor via context health logs and tune `COMPACTION_THRESHOLD_RATIO` if costs are too high. |
| **Total estimated increase** | **Variable: negligible for short sessions, up to 5x for long sessions on 1M models** |

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
| Metadata degradation | Index miss if tool_use_id not found | Graceful fallback: `"[tool result: N chars]"` if no match in index |
| Dynamic compaction thresholds | **Breaking change** — thresholds scale with model context window. 1M models go from 100K → 600K (less compaction, higher cost) | Explicit `NOUS_COMPACTION_THRESHOLD` overrides dynamic. Existing deployments unaffected if env var is set. |
| Hard-clear age 12 | More tool results in context | Metadata-degraded results are ~150 chars each; 6 extra × 150 = 900 chars total |
| Content-type profiles | Maintenance burden of per-tool config | Sane `standard` default; only override tools with clear re-fetchability characteristics |
| Pre-prune fact extraction | Low-confidence facts polluting Heart | Capped at 10 per result, `confidence=0.3` auto-expires via existing cleanup. Tagged `source=pre_prune_extraction` for F017 floor exemption. |

---

## Migration Path (v7 — P2 #7)

**Upgrading existing deployments:**

1. **Phase 0 (anti-hallucination prompt):** ON by default. Disable: `NOUS_ANTI_HALLUCINATION_PROMPT=false`. No risk — it's a system prompt addition.

2. **Phase 1 (4-tier pruning):** Takes effect immediately. Existing sessions get new pruning behavior on next tool call. No migration needed — pruning is stateless.

3. **Phase 2 (dynamic thresholds):** **Breaking change.** Deployments that don't set `NOUS_COMPACTION_THRESHOLD` will see thresholds jump from 100K to 600K on 1M models (less compaction, higher cost per request). To preserve old behavior: `NOUS_COMPACTION_THRESHOLD=100000`.

4. **Sessions in progress:** All changes are per-turn. No session restart needed. A session that was at 90K tokens (near old threshold) will now have 510K of headroom on a 1M model.

5. **Rollback:** Each phase has a config flag. Disable individually, takes effect on next turn.

6. **Recommended rollout:**
   - Day 1: Phase 0 (prompt) + Phase 3 (logging) — zero risk, gives observability
   - Day 3: Phase 1 (4-tier pruning) — monitor via logs
   - Week 2: Phase 2 (dynamic thresholds) — monitor cost impact
   - Week 3: Phases 4-5 (content-type profiles, pressure warning)

---

## Open Questions

1. ~~Should compaction summaries include a "Tool Results Digest" section listing key tool calls and their essential outputs?~~ **RESOLVED (v3):** Yes. The compaction summary prompt should request a "Key Tool Results" section. Combined with pre-prune fact extraction (Phase 4), this ensures tool findings survive compaction. Without it, compaction summaries lose tool-specific details even when metadata traces are present.

2. ~~Should the metadata degradation format be customizable per tool?~~ **RESOLVED (v3):** Yes, via content-type-aware pruning profiles (Phase 4). `read_file` shows filename + line count, `web_search` extracts facts first, `bash` shows command + exit code. Implemented as `TOOL_DECAY_PROFILES` dict.

3. ~~Should `_find_tool_use_block()` search be cached?~~ **RESOLVED (v3):** Yes. Replaced with `_build_tool_use_index()` that builds a `tool_use_id → block` dict once per `prune_tool_results()` call. O(N) build, O(1) lookups. Architecture review confirmed compaction runs pre-turn, so the index is always complete.

4. **(NEW)** Should the "Tool Results Digest" in compaction summaries be generated from the metadata traces (cheap, available) or from original content before compaction (expensive, more complete)? Recommendation: use metadata traces — they're designed to preserve the essential info.
