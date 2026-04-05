# F036 — Prompt Cache Optimization

**Status:** IN PROGRESS  
**Priority:** P0 (direct cost reduction + latency improvement)  
**Depends on:** F035.4 (Context Visibility — for measurement)  
**Author:** Nous + Tim  
**Date:** 2026-04-05  

---

## Problem

Nous uses Anthropic's prompt caching (`cache_control: {"type": "ephemeral"}`) but in a naive way that undermines its effectiveness:

1. **Multiple cache markers compete** — Both system prompt blocks AND the last user message all have `cache_control`, but Anthropic only guarantees one cached prefix position per turn-to-turn eviction.
2. **System prompt changes every turn** — `ContextEngine.build()` reassembles decisions, facts, procedures, and working memory on every turn. Even small changes in recalled items invalidate the entire system prompt cache.
3. **No cache break visibility** — When the cache is invalidated, there's zero signal. No logging, no metrics, no way to optimize.
4. **Tool results break the prefix** — In-place tool pruning (soft-trim, hard-clear) modifies message content, invalidating the cached prefix for everything before it.
5. **No scope differentiation** — The static identity prompt ("You are Claude Code...") could use `global` scope (shared across all sessions) but is treated identically to the dynamic context that changes every turn.

**Cost impact:** With typical 50K-200K token system prompts, cache misses mean re-processing the entire prefix on every API call. At $15/MTok for Opus input, a session with 20 turns paying full price for a 100K prefix costs ~$30 vs ~$3 with 90% cache hits.

---

## Solution: Five Interlocking Optimizations

These five changes form a coherent system. Items 1-2 are measurement + architecture prerequisites; items 3-5 build on them.

### Component 1: Cache Break Detection (Measurement Foundation)

**What:** Hash system prompt blocks, tool schemas, model, headers, and cache_control config before each API call. Compare with previous request. Log cache breaks with token impact.

**Why first:** You can't optimize what you can't measure. This makes all other optimizations' impact visible through F035.4 context logging.

**How it works:**
- Before each `_build_api_payload()` call, compute SHA256 hashes of:
  - Static identity block text
  - Dynamic context block text
  - Serialized tool schemas list
  - Model name + beta headers + cache_control config
- Compare with previous call's hashes (stored on AgentRunner instance)
- On mismatch: log which component changed and estimated token impact
- Integrate with F035.4 ContextLog — add `cache_break` field to context entries

```python
@dataclass
class CacheBreakInfo:
    """Detected cache invalidation between consecutive API calls."""
    components_changed: list[str]  # e.g. ["dynamic_context", "tools"]
    estimated_tokens_lost: int     # tokens that must be re-processed
    previous_hashes: dict[str, str]
    current_hashes: dict[str, str]
```

### Component 2: Session-Stable System Prompt Splitting

**What:** Split the system prompt from 2 blocks into 3, separating content by volatility:

| Block | Content | Changes | Cache Strategy |
|-------|---------|---------|----------------|
| Block 0 | Static identity ("You are Claude Code...") + anti-hallucination + platform rules | Never within session | `cache_control: ephemeral` |
| Block 1 | Semi-stable context: frame description, censors, user profile | Rarely (frame changes) | `cache_control: ephemeral` |
| Block 2 | Dynamic context: decisions, facts, procedures, episodes, working memory, ledger | Every turn | NO cache_control |

**Why:** Currently both system blocks have `cache_control`, so every turn the dynamic context changes and invalidates both blocks. By separating stable from volatile content, Blocks 0-1 remain cache hits across turns.

**Implementation:**
- `ContextEngine.build()` returns `BuildResult` with a new `sections_by_tier` dict grouping sections into `static`, `semi_stable`, and `dynamic` tiers
- `AgentRunner._build_api_payload()` constructs 3 system blocks from these tiers
- Only blocks 0 and 1 get `cache_control: {"type": "ephemeral"}`

**ContextEngine section tier mapping:**

| Section | Tier |
|---------|------|
| Identity prompt | static |
| Anti-hallucination | static |
| Current date/time | dynamic |
| User profile facts | semi_stable |
| Active censors | semi_stable |
| Frame description + questions | semi_stable |
| Cache hints | dynamic |
| Working memory | dynamic |
| Decisions | dynamic |
| Facts | dynamic |
| Procedures | dynamic |
| Episodes | dynamic |
| Execution ledger | dynamic |
| Diagnostic nudges | dynamic |
| Pending corrections | dynamic |
| Temporal context | dynamic |

### Component 3: Single Cache Breakpoint Strategy

**What:** Use exactly ONE `cache_control` marker on the message array (on the last user message), following Claude Code's "single marker rule." Remove the redundant marker on the second system block.

**Why:** Anthropic's turn-to-turn eviction protects only the last `cache_control` position. Multiple markers create unpredictable eviction behavior. The single-marker approach ensures the conversation prefix up to the last user message stays cached.

**Implementation:**
- System blocks: Only Block 0 (static identity) gets `cache_control`
- Block 1 (semi-stable): Gets `cache_control` only if it changed since last turn (detected by Component 1)
- Block 2 (dynamic): Never gets `cache_control`
- Messages: Last user message gets `cache_control` (existing behavior, kept)
- Net: 2 markers maximum (1 on stable system prefix, 1 on last user message)

### Component 4: Cache Reference Tagging

**What:** Tag every `tool_result` block with `cache_reference: tool_use_id` when building the messages array. This enables future cache_edits support and improves Anthropic's server-side cache management.

**Why:** Without cache_reference, Anthropic treats the entire message array as an opaque prefix. With it, the server can track individual tool results within the cached prefix, enabling more granular cache management. This is also the prerequisite for Component 5.

**Implementation:**
- In `_build_api_payload()` message formatting: when iterating messages, add `cache_reference` to every `tool_result` block
- The `tool_use_id` is already present on tool_result blocks as the `tool_use_id` field

```python
# In message formatting, for each tool_result block:
if block.get("type") == "tool_result":
    block["cache_reference"] = block["tool_use_id"]
```

### Component 5: Tool Schema Caching Per Frame

**What:** Cache the serialized tool schema list per `frame_id`. When the frame doesn't change between turns, reuse the exact same tool list to preserve Anthropic's server-side tool definition cache.

**Why:** `ToolDispatcher.available_tools(frame_id)` rebuilds the schema list every turn. Even though the schemas themselves don't change, re-serialization can produce subtly different ordering, breaking the tool cache key. Caching by frame_id guarantees byte-identical tool lists across turns.

**Implementation:**
- Add `_tool_schema_cache: dict[str, list[dict]]` on `ToolDispatcher`
- `available_tools(frame_id)` checks cache first, builds and caches on miss
- Cache invalidated on tool registration/deregistration (rare — only at startup)

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    AgentRunner._build_api_payload()              │
│                                                                   │
│  1. CacheBreakDetector.check()     ← Component 1 (measurement)  │
│  2. system = [                                                    │
│       Block 0: static_identity     ← Component 2 (split)         │
│       Block 1: semi_stable_context ← Component 2 (split)         │
│       Block 2: dynamic_context     ← Component 2 (no cache_ctrl) │
│     ]                                                             │
│  3. Single breakpoint strategy     ← Component 3 (markers)       │
│  4. cache_reference on tool_results ← Component 4 (tagging)      │
│  5. Cached tool schemas per frame   ← Component 5 (tools)        │
│                                                                   │
│  → F035.4 ContextLog entry with cache_break info                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `NOUS_CACHE_BREAK_DETECTION_ENABLED` | `true` | Enable cache break detection logging |
| `NOUS_CACHE_SPLIT_SYSTEM_PROMPT` | `true` | Enable 3-tier system prompt splitting |
| `NOUS_CACHE_SINGLE_BREAKPOINT` | `true` | Use single cache breakpoint strategy |
| `NOUS_CACHE_REFERENCE_ENABLED` | `true` | Add cache_reference to tool_result blocks |
| `NOUS_TOOL_SCHEMA_CACHE_ENABLED` | `true` | Cache tool schemas per frame |

All flags default to `true` (active). Individual flags allow disabling any component if it causes issues, enabling incremental rollout.

---

## Files Changed

| File | Change |
|------|--------|
| `nous/api/cache_optimizer.py` | **NEW** — CacheBreakDetector, CacheHashState, CacheBreakInfo |
| `nous/api/runner.py` | Wire CacheBreakDetector into _build_api_payload; 3-block system prompt; cache_reference tagging; single breakpoint logic |
| `nous/api/tools.py` | Add _tool_schema_cache to ToolDispatcher |
| `nous/cognitive/context.py` | Add section tier classification to BuildResult |
| `nous/cognitive/schemas.py` | Add `tier` field to ContextSection; update BuildResult |
| `nous/config.py` | Add 5 new config flags |
| `nous/observability/context_logger.py` | Add cache_break field to ContextLogEntry |
| `sql/migrations/017_cache_optimization.sql` | Add cache_break_count, cache_break_components columns to context_log table |
| `sql/init.sql` | Add same columns to base schema |
| `tests/test_f036_*.py` | Tests for all components |

---

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| 3-block split changes system prompt byte layout, causing one-time cache miss | Expected; cache recovers on next turn. Break detector will log it. |
| cache_reference not supported on all API versions | Guard behind config flag; field is ignored by API if unsupported |
| Tool schema caching returns stale schemas after dynamic tool registration | Invalidate cache on register/deregister; startup-only registration makes this safe |
| Semi-stable tier classification wrong (e.g., censors change mid-session) | Censors change rarely; frame changes trigger cache rebuild; break detector catches it |

---

## Success Metrics

Measured via F035.4 context logging:
- `cache_read_input_tokens / total_input_tokens` ratio per session (target: >60% after turn 2)
- Cache break frequency per session (target: <30% of turns trigger system block break)
- Average tokens re-processed per cache break (target: <20K)
- Cost per session reduction (target: 40-60% reduction in input token costs)

---

## Non-Goals

- **Cache reference tagging (Component 4 from original analysis)**: Dropped during implementation. `cache_reference` is not a documented Anthropic API field — risks payload rejection. Deferred to F036.1 if/when Anthropic adds official support.
- **Cache edits (delete)**: Requires deeper integration with tool pruning pipeline. Deferred to F036.1.
- **Global/org scope**: Requires Anthropic API support for scope parameter. Tagged for future when available.
- **1-hour TTL**: Requires eligibility check with Anthropic. Out of scope.
- **Embedding LRU cache**: Separate concern (not prompt caching). Track as F037.
- **Non-streaming fallback**: Separate concern (reliability, not caching). Track as F038.
- **Stream idle watchdog**: Separate concern (reliability). Track as F038.
