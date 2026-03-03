# F013 Frame Splitting — 3-Agent Review

**Date**: 2025-03-03
**Spec Reviewed**: F013-frame-splitting.md (Draft)
**Review Format**: Three specialized agent perspectives

---

## Agent 1: Architecture Reviewer

**Focus**: System design, Minsky alignment, existing patterns, scalability

### Strengths

1. **Clean Minsky alignment** — Chapter 18 (Parallel Bundles) directly supports this pattern. The idea that multiple agencies work simultaneously on different facets of a problem is core to Society of Mind. Frame Splitting operationalizes this.

2. **Natural infrastructure extension** — Builds on two proven systems (F003 frames, F009 subtasks) rather than introducing entirely new infrastructure. The SubtaskWorkerPool, FRAME_TOOLS map, and _get_frame_instructions() all exist and can be reused directly.

3. **Context isolation is correct** — Preventing sub-agents from seeing sibling state avoids the "cognitive contamination" problem. Each frame operates independently, producing cleaner outputs.

4. **Bounded by default** — Width limits, depth limits, and timeouts prevent runaway splits. Good defensive design.

### Concerns

1. **Sync barrier is a paradigm shift** — Current subtasks are fire-and-forget (spawn_task). Frame Splits require blocking-within-turn semantics (the parent tool call blocks while sub-agents execute). This is fundamentally different and adds complexity to the runner's tool execution model. The tool execution timeout needs to accommodate multiple sub-agent turns.

2. **Synthesis is the weakest link** — The spec offers three synthesis modes but doesn't deeply address the hardest problem: how to merge potentially contradictory or overlapping outputs. "Inline" synthesis (parent LLM just gets all results) works but means the parent does another expensive LLM call with potentially large context. This is the "reduce" in map-reduce and deserves more design attention.

3. **Frame selection fidelity** — The parent agent must correctly assign frame types to sub-tasks. If it assigns "question" to a task that needs "decision" tools, the sub-agent is handicapped. There's no self-correction mechanism. Consider allowing sub-agents to request a frame change.

4. **Over-engineering risk** — Many tasks that *could* be split don't *need* to be split. Serial processing in a single frame is often adequate and cheaper. Without clear heuristics for when splitting adds value, it may be used unnecessarily.

5. **Memory write semantics** — The spec says facts and decisions are additive (no conflicts), but semantic conflicts are possible. Two sub-agents could learn contradictory facts or make contradictory decisions from different perspectives. Need a coherence check during synthesis.

### Score: 7.0 / 10

### Recommendations
- Start with inline synthesis only. Agent synthesis and template merge can come later.
- Add a "split plan review" step where the parent agent evaluates its own decomposition before executing (self-check).
- Consider a frame negotiation mechanism where sub-agents can flag frame misfit.
- Implement cost tracking per split to build empirical data on value-vs-cost.

---

## Agent 2: Implementation Reviewer

**Focus**: Code complexity, existing infrastructure reuse, testing strategy, migration path

### Strengths

1. **High code reuse** — The core pieces exist:
   - `SubtaskWorkerPool` handles worker lifecycle and concurrency
   - `FRAME_TOOLS` maps frame_id → tool list
   - `_get_frame_instructions()` generates frame-specific prompts
   - `ContextAssembler` builds prompts with identity, censors, working memory
   - `AgentRunner.run_turn()` already handles the full cognitive loop

2. **Clear schema changes** — Adding `frame_type` and `split_id` to the Subtask model is a simple Alembic migration. The FrameSplitResult dataclass maps cleanly to existing patterns.

3. **Worker modification is contained** — The subtask_worker.py changes are isolated: read frame_type from subtask, apply frame config when building prompt. The worker already calls `AgentRunner.run_turn()` which handles everything downstream.

4. **Testing strategy is clear** — Unit test frame assignment, integration test parallel execution with mock workers, E2E test a full split-execute-synthesize cycle.

### Concerns

1. **Timeout calculus is complex** — We have: per-sub-agent timeout, full split timeout, worker pool limits, and the Anthropic API response timeout (~10 min). These interact:
   - If 3 sub-agents each take 60s, the split takes ~60s (parallel) + synthesis overhead
   - But if a sub-agent is mid-tool-call when timeout hits, cleanup is messy
   - The parent's tool call blocks for the full duration — Anthropic's API must not timeout first

2. **Context assembly for sub-agents needs refactoring** — The current subtask_worker builds a minimal prompt:
   ```python
   system_prompt = f"You are Nous, completing a background task.\n\nTask: {task.instruction}"
   ```
   For frame-aware sub-agents, we need proper context assembly: identity, censors, frame instructions, scoped working memory. This means either:
   - Calling ContextAssembler from the worker (tight coupling)
   - Building a lighter "sub-agent context builder" (new code)

3. **Result enrichment** — Current subtask result is a text string. FrameSplitSubResult needs structured data (confidence, artifacts, tool_calls_count, duration). The worker needs to collect this during execution and serialize it.

4. **Tool registration** — `split_frames` is a new tool that needs to be registered in the builtin_tools registry, added to FRAME_TOOLS for appropriate frames (which frames can split?), and have a handler implemented. The handler is complex: it creates subtasks, submits to pool, awaits results, and formats output.

5. **Streaming during split** — The spec mentions this as an open question, but users will wonder what's happening during a 60-120s wait. Even a simple "working on 3 parallel tasks..." status would require streaming support changes.

### Score: 6.5 / 10

### Recommendations

**Phase the implementation:**

- **Phase 1** (~4-6h): Add frame_type to Subtask model. Modify worker to apply frame config. No new tool yet — test via direct DB/API.
- **Phase 2** (~3-4h): Implement split_frames tool with sync barrier. Basic inline synthesis (results returned as tool output).
- **Phase 3** (~2-3h): Result enrichment (structured FrameSplitSubResult), cost tracking, status messages during execution.

**Cut from v1:**
- Streaming sub-agent progress
- Recursive splits (depth > 1)
- Agent synthesis mode
- Template synthesis mode
- Model selection per sub-agent
- Cross-agent dependencies (DAG)

**Total estimated effort: 10-13 hours across 3 phases**

---

## Agent 3: Research Reviewer

**Focus**: Comparison with literature, best practices, novel contribution

### Strengths

1. **AgentOS alignment (2025)** — AgentOS proposes treating agent tasks as OS processes with scheduling, isolation, and resource management. Frame Splitting maps directly: each sub-agent is a "process" with its own environment (frame), isolated memory (working memory scope), and shared filesystem (Heart/Brain). The sync barrier is analogous to process join/wait.

2. **SCL validation (Structured Cognitive Layer)** — SCL advocates for modular cognition where specialized modules handle different cognitive functions. Frame types *are* cognitive modules. Frame Splitting enables parallel module activation, which SCL identifies as key for complex reasoning.

3. **MIRIX memory model match** — MIRIX's multi-agent memory architecture recommends shared long-term memory with isolated working memory. F013's design exactly matches: sub-agents share Heart/Brain (long-term) but have isolated working memory (short-term). MIRIX found this pattern prevents interference while enabling knowledge sharing.

4. **A-MEM (Agentic Memory)** — A-MEM's memory indexing approach supports the context_hints mechanism. Sub-agents can use targeted recall (semantic search) against shared memory, retrieving only relevant context for their specific sub-task.

5. **Society of Mind grounding** — Beyond Ch. 18, this connects to Ch. 13 (Reformulation) — breaking a problem into parts handled by different agencies — and Ch. 15 (Diplomats and Compromises) — the synthesis step where frame outputs are negotiated into a coherent response.

### Concerns

1. **Map-reduce limitations** — Research on multi-agent task decomposition (Khot et al., 2023; Anthropic, 2024) shows map-reduce patterns work well for independent subtasks but poorly for interdependent ones. The spec assumes task independence, but many real tasks have dependencies:
   - "Research options, then decide" — decision depends on research
   - "Analyze code, then write tests" — tests depend on analysis
   - Purely parallel tasks (research A, research B, research C) are less common

   **This limits the practical applicability of v1.** True Frame Splitting may need DAG support eventually.

2. **Synthesis quality gap** — ACC (Agent Computer Collaboration) research identifies result merging as the hardest challenge in multi-agent collaboration. Key findings:
   - Simple concatenation produces incoherent, redundant output
   - Agent-based synthesis adds its own error modes (hallucination, information loss)
   - Best results come from structured output formats that facilitate merging
   - Recommendation: enforce a common result schema for all sub-agents

3. **Decomposition quality** — The parent agent's ability to decompose tasks correctly is critical but untested. Research on self-decomposition (task planning) shows LLMs tend to:
   - Over-decompose simple tasks (waste resources)
   - Under-decompose complex tasks (miss aspects)
   - Misassign subtask granularity
   Consider a decomposition evaluation step before execution.

4. **Cost-quality empirics** — No existing research establishes when parallel frames outperform serial processing for LLM agents. The spec should include a measurement framework to validate that splits actually improve quality/speed vs. serial processing.

### Score: 7.5 / 10

### Recommendations

- **Explicit-only for v1** — Always require the agent to call split_frames intentionally. No auto-detection.
- **Structured sub-agent output** — Enforce a common result schema (summary, key_findings, confidence, artifacts) to facilitate synthesis.
- **Decomposition review** — Add a self-check step where the parent evaluates its split plan before executing.
- **Measurement framework** — Track metrics per split: total cost, total latency, quality (user satisfaction or self-rated), vs. estimated serial cost/latency. Build empirical data.
- **DAG support in roadmap** — Even if v1 is parallel-only, design the data model to support dependencies later (precedence field on FrameSplitTask).

---

## Composite Assessment

### Scores
- Architecture: **7.0 / 10**
- Implementation: **6.5 / 10**
- Research: **7.5 / 10**
- **Composite: 7.0 / 10**

### Cross-Cutting Recommendations (Consensus)

1. **Phase the implementation** — All three reviewers agree: start minimal, iterate. Phase 1 = frame-aware subtasks, Phase 2 = sync barrier + tool, Phase 3 = synthesis + measurement.

2. **Explicit invocation only** — No auto-splitting in v1. The agent must call split_frames intentionally.

3. **Inline synthesis first** — Parent agent synthesizes results in its turn. Skip agent/template synthesis for v1.

4. **Cut aggressive scope** — Remove from v1:
   - Recursive splits (depth > 1)
   - Streaming progress
   - Agent synthesis mode
   - Model selection per sub-agent
   - DAG dependencies
   - Auto-detection

5. **Enforce structured output** — Sub-agents should return results in a common schema to aid synthesis quality.

6. **Build measurement from day 1** — Track cost, latency, quality per split. This data informs whether and when to expand the feature.

7. **Address dependency gap** — Design the data model to support task dependencies even if v1 doesn't implement them. Add a `depends_on` field to FrameSplitTask that's ignored in v1 but available for v2.

### Open Decisions for 012.1

1. **New tool vs. extended spawn_task** — Architecture says extend, Implementation says new tool (different semantics). Recommendation: **new tool** — the blocking/sync semantics are fundamentally different from fire-and-forget spawn_task.

2. **Which frames can split?** — Should all frames have access to split_frames, or only certain ones (task, conversation)? Recommendation: **task and conversation only** — these are the frames that handle complex, multi-faceted requests.

3. **Sub-agent episode handling** — Should each sub-agent create a full episode, or should all sub-agents share the parent's episode? Recommendation: **sub-agents create child episodes linked to parent** — maintains audit trail.

4. **Censor enforcement in sub-agents** — Must sub-agents respect all parent censors? Recommendation: **yes, always** — censors are safety-critical and must propagate.
