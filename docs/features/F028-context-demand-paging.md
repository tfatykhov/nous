# F028 — Context Demand Paging

> **Status:** Draft v1
> **Priority:** P1
> **Depends on:** F005 (Context Engine), F016 (Context Pruning), F017 (Context Quality Gate), F020 (Tool Output Intelligence)
> **Research:** Pichay (arXiv:2603.09023), SleepGate (arXiv:2603.14517), SuperLocalMemory V3 (arXiv:2603.14588)
> **Fills:** Gap in L1-L2 memory hierarchy (context window management)

---

## Problem Statement

Nous treats the context window as a bottomless container. Content loaded in turn 1 (tool results, retrieved episodes, recalled facts) persists for the entire session. There is no eviction policy, no working set management, and no mechanism to distinguish "actively needed" from "was needed 10 turns ago."

**Pichay's production finding:** 21.8% of context tokens are structural waste:
- 11.0% — unused tool schemas (18 tools defined, median 3 used per session)
- 8.7% — stale tool results (median amplification factor: 84.4× — each byte reprocessed 84 times)
- 2.2% — duplicated content

**The inverted cost model:** Keeping a 5K-token block in context for 20 turns costs 100K tokens of attention computation. Re-reading it once costs 5K tokens. **Break-even is 1 turn.** Anything not referenced in the next turn should be evicted.

**Current state in Nous:**
- F016 (Context Pruning) applies 4-tier tool output pruning — but only compresses, never evicts
- F017 (Context Quality Gate) applies relevance floors — but only for recalled memories, not for accumulated tool results
- F020 (SmartCompress) compresses at ingestion time — but compressed content still persists
- No mechanism tracks which content was referenced by the model after injection
- No retrieval handle system — evicted content has no recovery path

**The hierarchy gap:** Nous has L4 storage (Heart stores) and L1 (context window). Missing: L2 (working set with demand paging) and L3 (session history compression).

---

## Solution: OS-Inspired Context Hierarchy

### The Four-Level Memory Hierarchy for Nous

```
L1 — Active Context Window
     What the model sees right now. Hard-bounded by token limit.
     Policy: aggressive eviction, demand loading.

L2 — Working Set (Session Cache)
     Content evicted from L1 but likely needed again this session.
     Stored as full content in session-scoped cache.
     Policy: fault-driven pinning, FIFO age eviction.

L3 — Session History (Compressed)
     Model-authored summaries of completed sub-tasks.
     Replaces raw tool outputs and scaffolding dialogue.
     Policy: append-only within session, compaction on session close.

L4 — Persistent Memory (Heart Stores)
     Episodes, facts, procedures, decisions.
     Retrieved by recall_deep, recall_recent.
     Policy: admission-controlled (A-MAC), lifecycle-managed (F008).
```

### Mechanism 1: Retrieval Handles

When content is evicted from L1, replace it with a self-describing stub:

```
[📄 Paged out: read_file("/src/cognitive/context.py") — 287 lines, 8,192 bytes.
 Re-read the file if you need its content.]
```

```
[📋 Paged out: Episode "F024 Phase Review" — 847 tokens.
 Use recall_deep("F024 phase review") to restore.]
```

```
[🔍 Paged out: web_search("agent memory architecture") — 5 results.
 Re-search if you need the results.]
```

**Handle properties:**
- Self-describing: contains what was removed, size, and recovery instruction
- Recovery-aware: includes the exact tool call needed to restore content
- Compact: ~30-50 tokens per handle vs hundreds/thousands for original content
- Stale-proof: re-fetching gets current content, not cached (files may have changed)

**Key insight from Pichay:** Models recognize these handles without instruction and cooperatively re-request content when needed. No special prompting required.

### Mechanism 2: Age-Based Eviction Policy

**FIFO by user-turn age with size threshold:**

```python
EVICTION_CONFIG = {
    "turn_age_threshold": 4,       # τ — evict after 4 turns without reference
    "min_block_size": 500,         # s_min — only evict blocks > 500 bytes
    "pressure_zones": {
        "normal":     {"max_tokens": 60_000, "action": "observe"},
        "advisory":   {"max_tokens": 80_000, "action": "inject_pressure_signal"},
        "involuntary":{"max_tokens": 100_000,"action": "auto_evict"},
        "aggressive": {"max_tokens": 120_000,"action": "emergency_evict"},
    }
}
```

**Eviction candidate scoring:**

```python
def eviction_score(block: ContextBlock, current_turn: int) -> float:
    """Higher score = more evictable."""
    age = current_turn - block.last_referenced_turn
    if age < TURN_AGE_THRESHOLD:
        return 0.0  # Too young to evict
    if block.is_pinned:
        return 0.0  # Fault-pinned, don't evict
    
    age_score = min(age / 10.0, 1.0)           # Normalize age to [0,1]
    size_score = min(block.tokens / 5000, 1.0)  # Larger blocks more evictable
    type_score = CONTENT_TYPE_EVICTABILITY[block.content_type]
    # bash_output: 0.9, search_results: 0.8, file_read: 0.6, 
    # recalled_episode: 0.5, recalled_fact: 0.3, system_context: 0.1
    
    return age_score * 0.4 + size_score * 0.3 + type_score * 0.3
```

### Mechanism 3: Fault Detection & Pinning

**Page fault:** The model re-requests content that was previously evicted.

**Detection:**
```python
class FaultTracker:
    def __init__(self):
        self.evicted: dict[str, EvictedBlock] = {}  # key → block
        self.fault_history: set[str] = set()          # keys that have faulted
    
    def check_fault(self, tool_call: ToolCall) -> Optional[EvictedBlock]:
        """Check if a tool call is requesting evicted content."""
        key = self._make_key(tool_call.name, tool_call.args)
        if key in self.evicted:
            block = self.evicted.pop(key)
            self.fault_history.add(key)  # Pin: don't evict this again
            return block
        return None
    
    def should_pin(self, key: str) -> bool:
        """Content that faulted once is pinned for the session."""
        return key in self.fault_history
```

**Fault-driven pinning:** After one fault, content is permanently pinned for the session. This prevents thrashing — the pathological evict→fault→evict cycle that Pichay observed at 97% fault rate in long sessions.

### Mechanism 4: Garbage Collection vs. Paging

Distinguish two categories of tool output:

**Garbage-collectable (ephemeral):**
- Bash command outputs (ls, grep, git status)
- Search result snippets
- Directory listings
- Error messages

These have no stable identity — they can't be re-requested with the same tool call to get the same result. Evict aggressively, no retrieval handle needed (just a note: "Earlier bash output removed").

**Pageable (addressable):**
- File reads (path is a stable identifier)
- Web fetches (URL is a stable identifier)
- Recalled episodes/facts (IDs are stable)

These can be re-fetched. Eviction creates a retrieval handle with recovery instructions.

### Mechanism 5: Cooperative Memory Protocol

Leverage the model's ability to participate in memory management:

**Advisory pressure injection** (at advisory zone):
```
[⚠️ Context Pressure: 78% full (93,600/120,000 tokens).
 Largest blocks: read_file("/src/context.py") 3,200 tokens (turn 2),
                 web_fetch("arxiv.org/...") 2,800 tokens (turn 3).
 Consider: release stale content, summarize completed sub-tasks.]
```

**Model-initiated release:** The model can signal that content is no longer needed:
```python
# Detected in model output via lightweight parser
# "I've finished analyzing context.py, the key findings are..."
# → context.py block becomes high-priority eviction candidate
```

**Model-initiated compaction:** When the model summarizes a sub-task, the summary replaces the raw scaffolding:
```
# Model output: "Summary: The context engine uses 4-tier pruning with..."
# → Raw tool outputs from that sub-task compressed to just the summary (L3)
```

---

## Integration with Existing Context Engine

### Changes to `cognitive/context.py`

The context engine currently assembles context in `prepare_context()`. Changes:

1. **Add ContextBlock metadata tracking:**
```python
@dataclass
class ContextBlock:
    content: str
    content_type: str          # "file_read", "bash_output", "recalled_fact", etc.
    source_key: Optional[str]  # Stable identifier for re-fetch (file path, URL, fact ID)
    inserted_turn: int
    last_referenced_turn: int  # Updated when model references this content
    tokens: int
    is_pinned: bool = False
```

2. **Add eviction pass before context assembly:**
```python
async def prepare_context(self, ...):
    # NEW: Run eviction pass
    evicted = self._run_eviction_pass(current_turn)
    for block in evicted:
        self._replace_with_handle(block)
    
    # Existing: assemble context
    ...
```

3. **Add fault detection in tool result processing:**
```python
async def process_tool_result(self, tool_call, result):
    # NEW: Check if this is a page fault
    faulted_block = self.fault_tracker.check_fault(tool_call)
    if faulted_block:
        logger.info(f"Page fault: {tool_call.name}({tool_call.args})")
        # Pin this content for the session
        result_block.is_pinned = True
    
    # Existing: process and add to context
    ...
```

### Changes to tool output handling

Tool results currently pass through F016 (pruning) and F020 (SmartCompress). Add:
- Content type classification at ingestion
- Source key extraction (file path from read_file, URL from web_fetch)
- Token counting for size tracking
- Turn number stamping

---

## Implementation Plan

### Phase 1: Block Tracking & Metrics (~4h)
- Add ContextBlock dataclass with metadata
- Instrument context assembly to track block ages, sizes, types
- Add metrics: total tokens per type, average block age, largest blocks
- **Shadow mode:** Log what would be evicted, don't actually evict
- Measure: what % of context is >4 turns old? What's the size distribution?

### Phase 2: Retrieval Handles & Eviction (~6h)
- Implement handle generation per content type
- Implement FIFO age-based eviction with pressure zones
- Implement garbage collection for ephemeral tool outputs
- Wire into context preparation pipeline
- Add eviction event logging

### Phase 3: Fault Detection & Pinning (~4h)
- Implement FaultTracker with key-based eviction index
- Intercept tool calls for fault detection
- Implement fault-driven pinning (one fault = permanent session pin)
- Measure fault rate (target: <5% at τ=4)
- Add anti-thrashing protection (if fault rate >30%, increase τ)

### Phase 4: Cooperative Protocol (~3h)
- Implement advisory pressure injection at pressure zone boundaries
- Add lightweight parser for model release signals
- Implement L3 compaction (model-authored summaries replace raw tool outputs)
- Wire into session end handler for final compaction

### Phase 5: Evaluation (~2h)
- Measure token reduction (target: 30-50% for sessions >10 turns)
- Measure fault rate at various τ values (sweep τ ∈ {2, 4, 6, 8})
- Measure task completion accuracy pre/post (must not degrade)
- Compare latency: fewer tokens should reduce inference time

---

## Success Metrics

- **30-50% token reduction** for sessions >10 turns (Pichay achieved 37.1% with trim+compact)
- **Fault rate < 5%** at τ=4 (Pichay achieved 0.0254%)
- **Zero task completion regression** — eviction must not break functionality
- **21.8% structural waste eliminated** — unused tool schemas, stale results, duplicated content

---

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| Evicting content model needs next turn | τ=4 provides buffer; fault detection + pinning recovers gracefully |
| Thrashing in long working sessions | Fault-driven pinning + anti-thrash detector (auto-increase τ at high fault rates) |
| Handle format confuses the model | Self-describing handles work without instruction (Pichay validated). Test with current models. |
| Complexity in context assembly | Phase 1 shadow mode measures before committing. Rollback = remove eviction pass. |
| Session cache memory usage | Bound L2 cache to 2× context window. Beyond that, oldest blocks promoted to L3 summaries. |

---

## Connection to Existing Work

- **F016 (Context Pruning):** Demand paging operates upstream of pruning. Eviction removes entire blocks; pruning compresses remaining ones. They compose: evict stale blocks → prune remaining large blocks.
- **F017 (Context Quality Gate):** Quality gate filters recalled memories. Demand paging manages *all* context including tool outputs. Different domains, same goal.
- **F020 (SmartCompress):** Compressed content still persists in context. Demand paging can evict compressed blocks that are old enough. Complementary.
- **F027 (Supersession):** Superseded facts that survive into recall results are also candidates for eviction. Supersession scoring feeds into eviction scoring.
- **F024 (Critic Agent):** Context pressure metrics become observable by the Critic. A session approaching aggressive eviction zone triggers Critic intervention.

---

## Research Grounding

**Pichay (arXiv:2603.09023):**
- Context window = L1 cache, not RAM. Field has been expanding L1 instead of building virtual memory.
- FIFO age eviction (τ=4, s_min=500) achieves 0.0254% fault rate in production
- 21.8% structural waste across 857 production sessions (4.45B tokens analyzed)
- Cooperative fault protocol: models recognize retrieval handles and self-recover without instruction
- Inverted cost model: keeping is expensive (per-turn), faulting is cheap (one-time)
- Graduated pressure zones prevent cliff-edge degradation

**SleepGate (arXiv:2603.14517):**
- Soft biasing > hard eviction: `b = β·log(max(r, ε))` modifies retrieval ranking without deletion
- Graceful degradation when gate makes errors

**SuperLocalMemory V3 (arXiv:2603.14588):**
- Fisher information geometry for information-theoretic memory management
- Validates that mathematical foundations for eviction policies outperform heuristics
