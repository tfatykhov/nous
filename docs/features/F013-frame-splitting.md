# F013: Frame Splitting — Parallel Cognitive Frames via Sub-Agents

## Status: Draft
## Priority: Medium
## Dependencies: F003 (Cognitive Layer), F009 (Async Subtasks)

## Summary

Enable Nous to decompose complex multi-faceted tasks into parallel cognitive sub-tasks, each running in a purpose-specific frame with appropriate tools, recall patterns, and working memory scope. Results from parallel frames are synchronized and synthesized into a unified response.

## Motivation

### Problem

Nous currently operates in a single cognitive frame per turn. Complex tasks requiring multiple cognitive modes (research + analysis + writing, or investigation + decision + implementation) are processed serially within one frame. This creates:

1. **Mode interference** — Research context pollutes decision-making, creative output is constrained by analytical framing
2. **Serial latency** — Multi-step tasks that could run in parallel are bottlenecked
3. **Cognitive dilution** — A single frame can't optimize for multiple task types simultaneously
4. **Underutilized infrastructure** — F009 subtasks exist but lack cognitive frame awareness

### Solution

Frame Splitting introduces a **Split → Execute → Synthesize** pattern:
- The parent agent identifies sub-problems and maps each to the most appropriate cognitive frame
- Sub-agents execute in parallel via the existing subtask worker pool, each with frame-specific configuration
- Results are collected at a synchronization barrier and synthesized into a coherent response

### Minsky Alignment

Society of Mind, Chapter 18 (Parallel Bundles): Multiple agencies work simultaneously on different aspects of a problem. Each agency operates within its own frame of reference, contributing partial solutions that are combined by higher-level processes.

Chapter 19 (Words and Ideas): The meaning of a concept emerges from the interaction of multiple partial representations — each frame contributes a perspective that no single frame could provide alone.

## Design

### Core Concepts

#### Frame Split
A cognitive operation where the parent agent decomposes a task into N parallel sub-tasks, each assigned to a specific frame type. A Frame Split is:
- **Planned** — The parent agent explicitly decides the decomposition
- **Frame-typed** — Each sub-task carries a target frame (question, decision, creative, task, debug)
- **Scoped** — Each sub-agent receives filtered context, not the full conversation
- **Bounded** — Maximum split width and depth are configurable

#### Frame-Aware Subtask
An extension of F009 subtasks that carries:
- Explicit frame type assignment (overrides auto-detection)
- Frame-specific tool set (from FRAME_TOOLS)
- Frame-specific system prompt overlay (from _get_frame_instructions)
- Scoped context (working memory subset, relevant recalls)
- Frame-specific recall boost patterns (frame_boost from CognitiveLayer)

#### Sync Barrier
A coordination point where the parent agent waits for all sub-agents in a split to complete before proceeding. Unlike fire-and-forget subtasks (spawn_task), Frame Splits use a blocking-within-turn pattern with configurable timeout.

#### Synthesis
The merge step where parallel frame outputs are combined into a coherent response. Three modes:
1. **Inline** — Parent agent receives all results as tool output and synthesizes in the same turn
2. **Agent** — A dedicated synthesis sub-agent evaluates and combines (more expensive, higher quality)
3. **Template** — Structured concatenation based on frame type ordering (cheapest, lowest quality)

### Architecture

```
┌─────────────────────────────────────┐
│         Parent Agent Turn           │
│                                     │
│  1. Identify multi-faceted task     │
│  2. Plan frame decomposition        │
│  3. Call split_frames tool          │
└──────────────┬──────────────────────┘
               │
    ┌──────────┼──────────┐
    ▼          ▼          ▼
┌─────────┐ ┌─────────┐ ┌─────────┐
│ Question│ │Decision │ │  Task   │
│  Frame  │ │ Frame   │ │  Frame  │
│         │ │         │ │         │
│Research │ │Evaluate │ │ Build   │
│& recall │ │& decide │ │& create │
└────┬────┘ └────┬────┘ └────┬────┘
     │           │           │
     └───────────┼───────────┘
                 ▼
    ┌────────────────────────┐
    │   Sync Barrier         │
    │   (await all results)  │
    └───────────┬────────────┘
                ▼
    ┌────────────────────────┐
    │   Synthesis Step       │
    │   (merge results)      │
    └───────────┬────────────┘
                ▼
    ┌────────────────────────┐
    │   Response to User     │
    └────────────────────────┘
```

### New Tool: split_frames

```python
def split_frames(
    subtasks: list[FrameSplitTask],
    synthesis_mode: str = "inline",  # inline | agent | template
    timeout_seconds: int = 120,
) -> FrameSplitResult
```

Each FrameSplitTask:
```python
{
    "task": str,           # What this sub-agent should do
    "frame_type": str,     # question | decision | creative | task | debug
    "context_hints": list[str],  # Working memory items / keywords to include
    "max_turns": int,      # Max tool-use loops for this sub-agent (default 5)
}
```

### Context Scoping

Each sub-agent receives:
- The specific sub-task instruction (as user message)
- Core identity (from character config, always included)
- Active censors (always enforced)
- Relevant working memory items (filtered by context_hints)
- Access to shared memory (Brain, Heart) for recall
- **No access** to sibling sub-agent state (isolation)
- **No access** to parent conversation history (clean slate)

### Result Schema

```python
@dataclass
class FrameSplitResult:
    split_id: str
    subtasks: list[FrameSplitSubResult]
    synthesis: str | None  # Populated if synthesis_mode is "agent" or "template"
    total_duration_ms: int

@dataclass
class FrameSplitSubResult:
    task_id: str
    frame_type: str
    task: str
    result: str
    confidence: float | None
    artifacts: list[str]  # files created, decisions recorded, facts learned
    duration_ms: int
    tool_calls_count: int
    status: str  # completed | failed | timeout
```

### Safety & Limits

| Constraint | Default | Configurable |
|---|---|---|
| Max split width | 5 sub-agents | Yes |
| Max split depth | 1 (no recursive splits) | Future (max 3) |
| Max turns per sub-agent | 5 | Yes, per sub-task |
| Timeout per sub-agent | 60s | Yes |
| Timeout for full split | 120s | Yes |
| Max concurrent sub-agents | 3 | Via worker pool |

### Memory Coherence

Sub-agents share read access to Heart/Brain but have isolated working memory:
- **Facts**: Sub-agents can create facts (additive, no conflicts)
- **Decisions**: Sub-agents can record decisions (additive, no conflicts)
- **Episodes**: Each sub-agent creates its own episode
- **Censors**: Sub-agents cannot create censors (parent only)
- **Working memory**: Isolated per sub-agent (no cross-contamination)

## Integration Points

### F003 Cognitive Layer
- Frame selection for sub-agents uses fixed assignment (overrides auto-detection)
- Pre/post turn hooks still execute per sub-agent
- Deliberation traces recorded per sub-agent

### F009 Async Subtasks
- Frame Splits use the existing SubtaskWorkerPool
- New subtask_type: "frame_split" (vs existing "spawn")
- Split_id groups related subtasks for barrier synchronization

### F006 Event Bus
- New events: frame_split.started, frame_split.completed, frame_split.failed
- Per-sub-agent events: frame_split.subtask.started, frame_split.subtask.completed

## Open Questions

1. **Automatic splitting**: Should Nous auto-detect split-worthy tasks, or always require explicit tool call?
2. **Cost governance**: Per-turn cost ceiling for splits? Budget parameter?
3. **Streaming**: Can sub-agent progress be streamed to user during execution?
4. **Partial failure**: If one sub-agent fails, retry just that one or abort all?
5. **Dependencies**: Should sub-tasks be able to depend on each other (DAG vs parallel)?
6. **Model selection**: Should sub-agents use cheaper models (Haiku) for simple frames?

## Risks

1. **Cost explosion** — 5 parallel sub-agents × 5 turns = 25 API calls per split
2. **Complexity** — Significant architectural addition
3. **Synthesis quality** — Merging parallel outputs coherently is hard
4. **Debugging** — Parallel execution harder to trace than serial
5. **Latency** — Sync barrier + synthesis adds overhead despite parallelism
