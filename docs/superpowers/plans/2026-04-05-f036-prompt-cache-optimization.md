# Implementation Plan: F036 Prompt Cache Optimization

**Feature:** F036 — Prompt Cache Optimization  
**Date:** 2026-04-05  
**Spec:** docs/features/F036-prompt-cache-optimization.md  

---

## Phase Overview

After analyzing the 5 components and their interdependencies, I'm collapsing into **2 phases** instead of the spec's implied 5 sequential steps. Components 1-5 are independent enough to implement in parallel within logical groupings.

### Phase 1: Foundation (Components 1, 2, 5 — parallel)
- Component 1: CacheBreakDetector (new file + wiring)
- Component 2: System prompt 3-tier split (context.py + runner.py refactor)
- Component 5: Tool schema caching (tools.py)

### Phase 2: API Integration (Components 3, 4 — parallel)
- Component 3: Single breakpoint strategy (runner.py)
- Component 4: Cache reference tagging (runner.py)

Phase 2 depends on Phase 1 because the single breakpoint strategy needs the 3-tier split in place.

---

## Phase 1: Foundation

### Task 1.1: Config additions (nous/config.py)

Add 5 new config fields after the existing SmartCompress section (~line 174):

```python
# F036: Prompt Cache Optimization
cache_break_detection_enabled: bool = Field(
    default=True, validation_alias="NOUS_CACHE_BREAK_DETECTION_ENABLED"
)
cache_split_system_prompt: bool = Field(
    default=True, validation_alias="NOUS_CACHE_SPLIT_SYSTEM_PROMPT"
)
cache_single_breakpoint: bool = Field(
    default=True, validation_alias="NOUS_CACHE_SINGLE_BREAKPOINT"
)
cache_reference_enabled: bool = Field(
    default=True, validation_alias="NOUS_CACHE_REFERENCE_ENABLED"
)
tool_schema_cache_enabled: bool = Field(
    default=True, validation_alias="NOUS_TOOL_SCHEMA_CACHE_ENABLED"
)
```

### Task 1.2: Schema changes (nous/cognitive/schemas.py)

Add `tier` field to `ContextSection`:

```python
class ContextSection(BaseModel):
    """A section of assembled context."""
    priority: int
    label: str
    content: str
    token_estimate: int
    tier: str = "dynamic"  # "static", "semi_stable", or "dynamic"
```

Add `sections_by_tier` to `BuildResult`:

```python
class BuildResult(BaseModel):
    system_prompt: str
    sections: list[ContextSection] = Field(default_factory=list)
    recalled_ids: dict[str, list[str]] = Field(default_factory=dict)
    recalled_content_map: dict[str, str] = Field(default_factory=dict)
    recalled_score_map: dict[str, float] = Field(default_factory=dict)
    sections_by_tier: dict[str, str] = Field(default_factory=dict)  # tier -> joined text
```

### Task 1.3: Context engine tier classification (nous/cognitive/context.py)

In `ContextEngine.build()`, assign tier to each section at creation time:

**Tier mapping:**
| Section label | Tier |
|---|---|
| Identity | static |
| Context Safety (anti-halluc) | static |
| Current Date/Time | dynamic |
| User Profile | semi_stable |
| Active Censors | semi_stable |
| Current Frame | semi_stable |
| Cached Results | dynamic |
| Working Memory | dynamic |
| Related Decisions | dynamic |
| Relevant Facts | dynamic |
| Relevant Procedures | dynamic |
| Recent Conversations | dynamic |

After assembling all sections, build `sections_by_tier`:

```python
# Group sections by tier, sorted by priority within each tier
tier_groups: dict[str, list[str]] = {"static": [], "semi_stable": [], "dynamic": []}
for section in sorted(sections, key=lambda s: s.priority):
    tier_groups[section.tier].append(f"## {section.label}\n\n{section.content}")

sections_by_tier = {
    tier: "\n\n".join(parts)
    for tier, parts in tier_groups.items()
    if parts
}
```

The existing `system_prompt` field continues to contain the full joined prompt (for backward compatibility). `sections_by_tier` is additive.

### Task 1.4: CacheBreakDetector (NEW: nous/api/cache_optimizer.py)

New module with:

```python
@dataclass
class CacheHashState:
    """Hashes from the previous API call for comparison."""
    static_hash: str = ""
    semi_stable_hash: str = ""
    dynamic_hash: str = ""
    tools_hash: str = ""
    model_hash: str = ""

@dataclass
class CacheBreakInfo:
    """Detected cache invalidation between consecutive API calls."""
    components_changed: list[str]
    estimated_tokens_lost: int
    previous_hashes: dict[str, str]
    current_hashes: dict[str, str]

class CacheBreakDetector:
    """Detects prompt cache invalidations between consecutive API calls."""
    
    def __init__(self) -> None:
        self._previous: CacheHashState | None = None
    
    def check(
        self,
        static_text: str,
        semi_stable_text: str,
        dynamic_text: str,
        tools_json: str,
        model: str,
    ) -> CacheBreakInfo | None:
        """Compare current request hashes against previous. Return info if break detected."""
        current = CacheHashState(
            static_hash=_hash(static_text),
            semi_stable_hash=_hash(semi_stable_text),
            dynamic_hash=_hash(dynamic_text),
            tools_hash=_hash(tools_json),
            model_hash=_hash(model),
        )
        
        if self._previous is None:
            self._previous = current
            return None  # First call, no comparison
        
        changed: list[str] = []
        tokens_lost = 0
        
        if current.static_hash != self._previous.static_hash:
            changed.append("static_identity")
            tokens_lost += len(static_text) // 4
        if current.semi_stable_hash != self._previous.semi_stable_hash:
            changed.append("semi_stable_context")
            tokens_lost += len(semi_stable_text) // 4
        if current.tools_hash != self._previous.tools_hash:
            changed.append("tools")
            tokens_lost += len(tools_json) // 4
        if current.model_hash != self._previous.model_hash:
            changed.append("model")
        
        # Dynamic always changes — don't count it as a "break"
        # Only report if stable/semi-stable components changed
        
        self._previous = current
        
        if not changed:
            return None
        
        return CacheBreakInfo(
            components_changed=changed,
            estimated_tokens_lost=tokens_lost,
            previous_hashes=asdict(self._previous) if self._previous else {},
            current_hashes=asdict(current),
        )
    
    def reset(self) -> None:
        """Reset state (e.g., on session end)."""
        self._previous = None

def _hash(text: str) -> str:
    """SHA256 truncated to 16 hex chars."""
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()[:16]
```

### Task 1.5: Tool schema caching (nous/api/tools.py)

Add frame-keyed cache to `ToolDispatcher`:

```python
class ToolDispatcher:
    def __init__(self) -> None:
        self._handlers: dict[str, Callable[..., Any]] = {}
        self._schemas: dict[str, dict[str, Any]] = {}
        self._tool_schema_cache: dict[str, list[dict[str, Any]]] = {}  # F036

    def register(self, name, handler, schema):
        self._handlers[name] = handler
        self._schemas[name] = schema
        self._tool_schema_cache.clear()  # Invalidate on registration

    def available_tools(self, frame_id: str) -> list[dict[str, Any]]:
        # Check cache first
        if frame_id in self._tool_schema_cache:
            return self._tool_schema_cache[frame_id]
        
        # Build (existing logic)
        result = ... # existing build logic
        
        # Cache and return
        self._tool_schema_cache[frame_id] = result
        return result
```

### Task 1.6: Wire into runner (nous/api/runner.py)

In `AgentRunner.__init__`, create `CacheBreakDetector` instance.

Refactor `_build_api_payload()`:
1. Accept optional `BuildResult` (or its `sections_by_tier`) 
2. When `cache_split_system_prompt` enabled, build 3 system blocks from tiers
3. When disabled, fall back to current 2-block behavior
4. Run CacheBreakDetector.check() and log result
5. Add cache_break info to F035.4 context log

### Task 1.7: Context logger update (nous/observability/context_logger.py)

Add fields to `ContextLogEntry`:

```python
cache_break: bool = False
cache_break_components: list[str] = field(default_factory=list)
cache_break_tokens_lost: int = 0
```

Update `to_dict()` to include these fields.

### Task 1.8: Tests for Phase 1

- `tests/test_f036_cache_optimizer.py` — CacheBreakDetector unit tests
  - First call returns None
  - Identical calls return None
  - Changed static text detected
  - Changed tools detected
  - Changed model detected
  - Dynamic changes NOT reported as breaks
  - Reset clears state
  - Token loss estimation

- `tests/test_f036_schema_tier.py` — Schema + context tier tests
  - ContextSection accepts tier field
  - BuildResult has sections_by_tier
  - Context engine assigns correct tiers to sections

- `tests/test_f036_tool_cache.py` — Tool schema cache tests
  - Cache hit on repeated frame
  - Cache miss on different frame
  - Cache invalidation on register

- `tests/test_f036_runner.py` — Runner integration tests
  - 3-block system prompt when enabled
  - 2-block fallback when disabled
  - Cache break detection logged
  - Context log entry has cache_break fields

---

## Phase 2: API Integration

### Task 2.1: Single breakpoint strategy (nous/api/runner.py)

Modify `_build_api_payload()` cache_control assignment:

**Current behavior:** Both system blocks + last user message get `cache_control: ephemeral`.

**New behavior when `cache_single_breakpoint` enabled:**
- System Block 0 (static): `cache_control: {"type": "ephemeral"}` — always
- System Block 1 (semi_stable): `cache_control: {"type": "ephemeral"}` ONLY if it hasn't changed since last call (detected by CacheBreakDetector)
- System Block 2 (dynamic): NO cache_control
- Last user message: `cache_control: {"type": "ephemeral"}` — always (existing)

Net effect: stable prefix stays cached. Dynamic content doesn't compete for cache slots.

### Task 2.2: Cache reference tagging (nous/api/runner.py)

In `_build_api_payload()`, after building `cached_messages`, iterate and tag tool_result blocks:

```python
if self._settings.cache_reference_enabled:
    for msg in cached_messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tool_use_id = block.get("tool_use_id")
                    if tool_use_id:
                        block["cache_reference"] = tool_use_id
```

### Task 2.3: Tests for Phase 2

- `tests/test_f036_breakpoint.py` — Single breakpoint tests
  - Only static block gets cache_control when semi_stable changed
  - Both static + semi_stable get cache_control when semi_stable is stable
  - Dynamic block never gets cache_control
  - Last user message always gets cache_control
  - Disabled flag reverts to old behavior

- `tests/test_f036_cache_ref.py` — Cache reference tests
  - tool_result blocks get cache_reference
  - Non-tool_result blocks untouched
  - Disabled flag skips tagging
  - Handles string content gracefully

---

## File Change Summary

| File | Type | Lines (est.) |
|------|------|-------------|
| `nous/config.py` | Edit | +15 |
| `nous/cognitive/schemas.py` | Edit | +8 |
| `nous/cognitive/context.py` | Edit | +25 |
| `nous/api/cache_optimizer.py` | New | ~120 |
| `nous/api/tools.py` | Edit | +15 |
| `nous/api/runner.py` | Edit | +80 |
| `nous/observability/context_logger.py` | Edit | +10 |
| `tests/test_f036_cache_optimizer.py` | New | ~150 |
| `tests/test_f036_schema_tier.py` | New | ~80 |
| `tests/test_f036_tool_cache.py` | New | ~60 |
| `tests/test_f036_runner.py` | New | ~120 |
| `tests/test_f036_breakpoint.py` | New | ~100 |
| `tests/test_f036_cache_ref.py` | New | ~60 |
| **Total** | | **~845 lines** |

No database schema changes needed — cache_break fields are in-memory on ContextLogEntry only (not persisted to DB). The F035.4 context log table doesn't store individual fields, it stores the full dict.

---

## Execution Strategy

Phase 1 tasks are independent and can be dispatched to 3 parallel subagents:
- Agent A: Config + schemas + context engine tiers (Tasks 1.1, 1.2, 1.3)
- Agent B: CacheBreakDetector + context logger update (Tasks 1.4, 1.7)
- Agent C: Tool schema caching (Task 1.5)

Then a sequential agent for Task 1.6 (runner wiring — depends on A, B, C outputs).

Phase 2 tasks can be 2 parallel subagents:
- Agent D: Single breakpoint strategy (Task 2.1)
- Agent E: Cache reference tagging (Task 2.2)

Tests run after each phase with dedicated test agents.
