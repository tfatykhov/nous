# F024 Amendment — DAG Decomposition & Wave Scheduling

> **Status:** Draft v1
> **Amends:** F024 Critic Agent v3 — Phase 1 (Parallel Spawn + Pick Winner)
> **Priority:** P1
> **Author:** Nous + Tim
> **Date:** 2026-03-30
> **Research basis:** CAID (CMU, arXiv:2603.21489), DynTaskMAS (ICAPS 2025, arXiv:2503.07675v2), GAP (Tsinghua/CMU), DRAMA (arXiv:2508.04332), Atomix (arXiv:2503.18890), M1-Parallel (Microsoft ICML 2025, arXiv:2507.08944)

---

## Problem

F024 Phase 1 has two structural gaps identified by cross-referencing CAID, DynTaskMAS, and DRAMA research:

**Gap 1: No dependency modeling between parallel branches.**
The current spec assumes all parallel instances are independent — spawn N, pick winner. But real complex tasks have **dependencies**: "research X, then compare X with Y, then write summary." Spawning all three simultaneously wastes compute (instance 2 can't compare without instance 1's results) and produces lower quality (instance 2 hallucinates what X found instead of using real results).

CAID demonstrated this empirically: missing `autodiff.py` as a dependency caused a **26-point swing** (8.7% vs 34.3%) between runs. The dependency graph IS the architecture — without it, quality is random.

**Gap 2: Fire-and-forget spawning wastes early finishers.**
When a subtask completes ahead of others, that information sits idle. No successor tasks are unblocked. No freed capacity is reallocated. DRAMA showed that event-driven reallocation yields **17% runtime improvement** and **13% resource reduction** over static scheduling.

---

## Solution: DAG Decomposition Layer

Add a **dependency analysis step** between Critic classification and instance spawning. The Critic already decomposes complex tasks — this amendment structures that decomposition as an explicit directed acyclic graph (DAG) with topological wave scheduling.

### Architecture Change

**Current F024 Phase 1 Flow:**
```
User Message → Critic → spawn(A, B, C) simultaneously → evaluate → pick winner
```

**Amended Flow:**
```
User Message → Critic → DAG Builder → Wave Scheduler → spawn waves → evaluate → response
                           │
                           ▼
                    ┌─────────────┐
                    │  Task DAG   │
                    │  G = (V, E) │
                    │             │
                    │  A ──→ C    │
                    │  B ──→ C    │
                    │  A ∥ B      │   (A and B are parallel, C depends on both)
                    └─────────────┘
                           │
                           ▼
                    Wave 1: spawn(A, B) — parallel
                    Wave 2: spawn(C) — after A,B complete
```

### When DAG Applies vs. Doesn't

The DAG layer activates **only** when the Critic routes to `parallel-select` or `parallel-merge`. The existing routing modes map as follows:

| Routing Mode | DAG? | Why |
|---|---|---|
| Passthrough | No | No Critic, no spawning |
| Single-Advised | No | Single instance, no dependencies possible |
| Parallel-Select (independent) | No | Instances are alternative approaches to same problem — no dependencies |
| Parallel-Select (phased) | **Yes** | Instances build on each other's outputs — has dependencies |
| Parallel-Merge | **Yes** | Complementary subtasks often have shared prerequisites |
| Parallel-Evaluate | No | Same prompt K times — inherently independent |

The Critic's decomposition now produces one additional field: `dependency_type`:
- `"independent"` — existing behavior, all instances spawned simultaneously
- `"phased"` — DAG decomposition, wave scheduling activated

---

## Data Structures

### TaskNode

```python
@dataclass
class TaskNode:
    """A single subtask in the dependency graph."""
    id: str                          # Unique node ID (e.g., "research_caid")
    description: str                 # What this subtask does
    frame_type: str                  # Cognitive frame to use
    skills: list[str]                # Skills to activate
    instructions: str                # Specific instructions for this instance
    estimated_complexity: str        # "light" | "moderate" | "heavy"
    
    # Dependency info (populated by DAG builder)
    depends_on: list[str] = field(default_factory=list)   # Node IDs this depends on
    produces: str = ""               # What output this node creates (for successors)
    
    # Runtime state (populated by scheduler)
    status: str = "pending"          # "pending" | "queued" | "running" | "completed" | "failed"
    transaction: Optional[CognitiveTransaction] = None
    result: Optional[str] = None     # Output text when completed
    duration_ms: int = 0
```

### TaskDAG

```python
@dataclass
class TaskDAG:
    """Dependency graph of subtasks."""
    nodes: dict[str, TaskNode]       # node_id -> TaskNode
    edges: list[tuple[str, str]]     # (from_id, to_id) — "from must complete before to"
    
    # Metadata
    original_request: str            # User's original message
    decomposition_rationale: str     # Why the Critic decomposed this way
    total_waves: int = 0             # Computed after topological sort
    
    def topological_waves(self) -> list[list[str]]:
        """
        Group nodes into waves for parallel execution.
        Wave N+1 only starts after all of Wave N completes.
        Nodes within a wave have no mutual dependencies.
        """
        in_degree = {nid: 0 for nid in self.nodes}
        for (src, dst) in self.edges:
            in_degree[dst] += 1
        
        waves = []
        remaining = set(self.nodes.keys())
        
        while remaining:
            # Nodes with no unmet dependencies form the next wave
            wave = [nid for nid in remaining if in_degree[nid] == 0]
            if not wave:
                raise CyclicDependencyError(
                    f"Cycle detected in task DAG. Remaining: {remaining}"
                )
            waves.append(wave)
            
            # Remove completed wave, update in-degrees
            for nid in wave:
                remaining.remove(nid)
                for (src, dst) in self.edges:
                    if src == nid and dst in remaining:
                        in_degree[dst] -= 1
        
        self.total_waves = len(waves)
        return waves
    
    def validate(self) -> list[str]:
        """Validate DAG constraints. Returns list of issues."""
        issues = []
        
        # Max nodes (hard cap from F024)
        if len(self.nodes) > 6:
            issues.append(f"Too many nodes ({len(self.nodes)}). Max 6 per decomposition.")
        
        # Max depth (waves)
        try:
            waves = self.topological_waves()
            if len(waves) > 3:
                issues.append(f"Too deep ({len(waves)} waves). Max 3 sequential waves.")
        except CyclicDependencyError as e:
            issues.append(str(e))
        
        # All edge targets exist
        node_ids = set(self.nodes.keys())
        for (src, dst) in self.edges:
            if src not in node_ids:
                issues.append(f"Edge source '{src}' not in nodes.")
            if dst not in node_ids:
                issues.append(f"Edge target '{dst}' not in nodes.")
        
        # No self-loops
        for (src, dst) in self.edges:
            if src == dst:
                issues.append(f"Self-loop on '{src}'.")
        
        return issues
    
    def inject_predecessor_context(self, node_id: str) -> str:
        """
        Build context string from completed predecessor outputs.
        Injected into successor's instructions so it has access to
        upstream results without needing cross-instance communication.
        """
        predecessors = [src for (src, dst) in self.edges if dst == node_id]
        if not predecessors:
            return ""
        
        context_parts = []
        for pred_id in predecessors:
            pred = self.nodes[pred_id]
            if pred.status == "completed" and pred.result:
                context_parts.append(
                    f"=== Output from '{pred.description}' ===\n{pred.result}\n"
                )
        
        if context_parts:
            return (
                "\n\n--- CONTEXT FROM COMPLETED PREREQUISITES ---\n"
                + "\n".join(context_parts)
                + "--- END PREREQUISITES ---\n\n"
            )
        return ""
```

---

## Critic Prompt Amendment

The existing pre-turn classification prompt (F024 §Critic Agent Prompt Design) gains an additional output field when routing to parallel modes:

```
...existing Critic prompt fields...

If routing is "parallel-select" or "parallel-merge", also decide:

6. dependency_type: "independent" | "phased"
   - "independent": all instances tackle the same problem from different angles (no dependencies)
   - "phased": instances build on each other; produce a dependency graph

7. If dependency_type is "phased", produce a task_graph:
{
  "nodes": [
    {
      "id": "unique_short_id",
      "description": "what this subtask does",
      "frame": "research|task|decision|...",
      "skills": ["skill-name"],
      "instructions": "specific focus for this subtask",
      "depends_on": ["other_node_id"],
      "produces": "description of what this outputs for successors"
    }
  ]
}

CONSTRAINTS on task_graph:
- Maximum 6 nodes
- Maximum 3 sequential waves (depth)
- No cycles
- Each node must have a clear, actionable instruction
- Leaf nodes (no dependents) produce the final user-facing output
- Predecessor outputs will be injected into dependent nodes automatically

Example decomposition:
User: "Research how CAID handles parallelism, compare with our F024, and draft amendment recommendations"
→ dependency_type: "phased"
→ task_graph:
  Node A: "Research CAID parallelism approach" (research frame, depends_on: [])
  Node B: "Analyze current F024 parallel execution spec" (task frame, depends_on: [])
  Node C: "Compare CAID approach with F024 and draft recommendations" (decision frame, depends_on: [A, B])

Wave 1: A ∥ B (parallel — independent research)
Wave 2: C (sequential — needs A and B outputs)
```

---

## Wave Scheduler

### Core Logic

```python
class WaveScheduler:
    """
    Executes a TaskDAG in topological waves.
    Nodes within each wave run in parallel.
    Wave N+1 starts only after all Wave N nodes complete.
    """
    
    def __init__(self, dag: TaskDAG, max_parallel: int = 3):
        self.dag = dag
        self.max_parallel = max_parallel  # F024 hard cap still applies per wave
    
    async def execute(self) -> DAGExecutionResult:
        """Execute the full DAG, wave by wave."""
        waves = self.dag.topological_waves()
        all_results = []
        
        for wave_idx, wave_node_ids in enumerate(waves):
            # Respect max_parallel: if wave has more nodes than cap,
            # split into sub-waves (rare — max 6 nodes, max 3 parallel)
            sub_waves = self._chunk(wave_node_ids, self.max_parallel)
            
            for sub_wave in sub_waves:
                wave_results = await self._execute_wave(sub_wave, wave_idx)
                all_results.extend(wave_results)
                
                # Check for failures — if a node fails, mark all dependents as blocked
                for result in wave_results:
                    if result.status == "failed":
                        self._propagate_failure(result.node_id)
        
        return DAGExecutionResult(
            dag=self.dag,
            node_results=all_results,
            total_waves=len(waves)
        )
    
    async def _execute_wave(self, node_ids: list[str], wave_idx: int) -> list[NodeResult]:
        """Execute all nodes in a wave concurrently."""
        tasks = []
        for node_id in node_ids:
            node = self.dag.nodes[node_id]
            if node.status == "blocked":
                continue  # Skip nodes blocked by upstream failure
            
            # Inject predecessor outputs into this node's context
            predecessor_context = self.dag.inject_predecessor_context(node_id)
            augmented_instructions = predecessor_context + node.instructions
            
            node.status = "running"
            task = self._spawn_instance(node, augmented_instructions)
            tasks.append(task)
        
        # await all instances in this wave
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Process results
        node_results = []
        for node_id, result in zip(node_ids, results):
            node = self.dag.nodes[node_id]
            if isinstance(result, Exception):
                node.status = "failed"
                node_results.append(NodeResult(node_id=node_id, status="failed", error=str(result)))
            else:
                node.status = "completed"
                node.result = result.response_text
                node.duration_ms = result.duration_ms
                node.transaction = result.transaction
                node_results.append(NodeResult(node_id=node_id, status="completed", output=result))
        
        return node_results
    
    async def _spawn_instance(self, node: TaskNode, instructions: str):
        """Spawn a single Nous instance for this node."""
        # Uses existing F024 transactional execution infrastructure
        transaction = CognitiveTransaction(
            instance_id=node.id,
            frame_type=node.frame_type,
        )
        return await execute_in_transaction(
            frame=node.frame_type,
            skills=node.skills,
            instructions=instructions,
            transaction=transaction,
        )
    
    def _propagate_failure(self, failed_node_id: str):
        """Mark all transitive dependents of a failed node as blocked."""
        dependents = [dst for (src, dst) in self.dag.edges if src == failed_node_id]
        for dep_id in dependents:
            self.dag.nodes[dep_id].status = "blocked"
            self._propagate_failure(dep_id)  # Recursive — propagate to their dependents
    
    @staticmethod
    def _chunk(items: list, size: int) -> list[list]:
        return [items[i:i+size] for i in range(0, len(items), size)]
```

### Failure Handling Strategy

When a node fails in a wave:

1. **Fail-fast propagation** — All dependents are marked `blocked` (they can't produce useful output without their prerequisite)
2. **Independent nodes continue** — Other nodes in the same wave that don't depend on the failed node still complete normally
3. **Partial DAG results** — The Critic evaluates whatever completed successfully. A 2-of-3 wave result may still produce a useful response
4. **Retry option (Phase 2)** — Failed nodes can be retried once with a different prompt or model. Not in Phase 1 scope

---

## Predecessor Context Injection

The key mechanism that makes phased execution work: **predecessor outputs are injected into successor prompts as structured context.**

This is how CAID's engineers receive task context — via structured data, not shared memory. Our approach is similar but uses prompt injection rather than file-system artifacts:

```
=== CONTEXT FROM COMPLETED PREREQUISITES ===

=== Output from 'Research CAID parallelism approach' ===
CAID uses a Manager + N Engineer architecture with physical git worktree 
isolation. Key findings: soft isolation degrades performance below single-agent.
Optimal parallelism is 2-4 engineers depending on task type...
[full research output from Node A]

=== Output from 'Analyze F024 parallel execution spec' ===
F024 v3 supports five routing modes: passthrough, single-advised, 
parallel-select, parallel-merge, parallel-evaluate. Current gap: no dependency 
modeling between parallel branches...
[full analysis output from Node B]

--- END PREREQUISITES ---

Your task: Compare the CAID approach with F024 and draft specific amendment 
recommendations. Use the prerequisite outputs above as your source material.
```

### Context Size Management

Predecessor outputs can be large. Mitigation:

1. **Summarization gate** — If a predecessor output exceeds 2000 tokens, summarize it before injection (using the same LLMSummarizingCondenser approach as CAID)
2. **Relevance filtering** — Only inject predecessors that are direct dependencies (not transitive — if A→B→C, C gets B's output which already incorporates A's findings)
3. **Token budget** — Total injected context capped at 4000 tokens. If exceeded, summarize all predecessors

---

## Transaction Handling for DAG Execution

### Side Effect Commitment Strategy

With wave scheduling, the commitment model changes from F024's simple "pick winner, commit their journal":

**Wave nodes that are prerequisites (non-leaf):**
- Their transactions are **held open** (not committed) until the full DAG completes
- Their outputs are used as context injection, not as final responses
- Side effects (facts learned during research) are candidates for commitment

**Leaf nodes (final output):**
- Same as current F024 — Critic evaluates leaf outputs, picks winner or merges
- Winner's transaction commits
- Losers roll back

**Cross-wave side effect handling:**
- Facts learned by prerequisite nodes are **always committed** if the DAG succeeds (they represent real research/work, not speculative alternatives)
- Facts from non-selected leaf nodes are rolled back (same as current F024)
- This differs from pure F024 Phase 1 where ALL non-winner transactions roll back

```python
class DAGTransactionManager:
    """Manages transaction lifecycle across a DAG execution."""
    
    async def commit_dag_results(
        self, 
        dag: TaskDAG, 
        selected_leaf_id: str
    ) -> CommitResult:
        """
        Commit strategy:
        - All non-leaf (prerequisite) node transactions: COMMIT
        - Selected leaf node transaction: COMMIT  
        - Non-selected leaf node transactions: ROLLBACK
        """
        results = CommitResult()
        leaf_ids = self._get_leaf_ids(dag)
        
        for node_id, node in dag.nodes.items():
            if node.status != "completed" or node.transaction is None:
                continue
            
            if node_id not in leaf_ids:
                # Prerequisite node — always commit (real work)
                r = await node.transaction.commit()
                results.merge(r)
            elif node_id == selected_leaf_id:
                # Selected leaf — commit
                r = await node.transaction.commit()
                results.merge(r)
            else:
                # Non-selected leaf — rollback
                node.transaction.rollback()
        
        return results
    
    def _get_leaf_ids(self, dag: TaskDAG) -> set[str]:
        """Nodes with no outgoing edges (no dependents)."""
        sources = {src for (src, dst) in dag.edges}
        return set(dag.nodes.keys()) - sources
```

---

## Hard Constraints (Amendment-Specific)

1. **Max 6 nodes per DAG.** Prevents over-decomposition. (DynTaskMAS caps recursive decomposition at 3 reflection iterations; we cap total nodes.)
2. **Max 3 sequential waves.** Deeper chains add latency without proportional quality gain. If a task needs 4+ waves, it should be a multi-turn conversation instead.
3. **Max 3 parallel nodes per wave.** Inherits F024's hard cap on parallel instances.
4. **No dynamic DAG updates in Phase 1.** The graph is static once built. DynTaskMAS's G(t+1)=U(G(t),Δ(t)) is Phase 2+ scope.
5. **Predecessor context injection is the ONLY cross-wave communication.** No shared memory, no message passing, no file system artifacts. Strict prompt-injection interface.
6. **DAG validation runs before execution.** Cycle detection, depth check, node count check. Invalid DAGs fall back to independent parallel execution.
7. **Prerequisite node transactions commit on DAG success.** This is a policy difference from pure F024 Phase 1 (where only the winner commits). Justified: prerequisite outputs represent real work (research, analysis), not speculative alternatives.

---

## Interaction with Existing F024 Modes

### Compatibility Matrix

- **Passthrough** → No change. No Critic, no DAG.
- **Single-Advised** → No change. Single instance, no DAG needed.
- **Parallel-Select (independent)** → No change. Existing behavior preserved. `dependency_type: "independent"`.
- **Parallel-Select (phased)** → **NEW.** Critic produces DAG with `dependency_type: "phased"`. Wave scheduler executes. Critic evaluates leaf outputs.
- **Parallel-Merge (phased)** → **NEW.** Same as above but Critic synthesizes leaf outputs instead of picking winner.
- **Parallel-Evaluate** → No change. Same prompt K times — inherently independent, no dependencies.

### Backward Compatibility

All existing F024 behavior is preserved. The DAG layer is **additive** — it only activates when the Critic explicitly produces `dependency_type: "phased"`. The default remains `"independent"`.

---

## Phase 2 Extensions (Future — Not in Scope)

### Dynamic DAG Updates (from DynTaskMAS)
- Mid-execution graph modifications: G(t+1) = U(G(t), Δ(t))
- If a prerequisite reveals the task is simpler than expected, prune remaining nodes
- If a prerequisite reveals new sub-problems, add nodes dynamically
- Requires event-driven architecture changes

### Completion-Triggered Unblocking (from DRAMA)
- Instead of waiting for ALL Wave N to complete, unblock individual successors as soon as ALL of their specific predecessors complete
- More efficient for unbalanced DAGs where one node finishes much faster
- Requires per-node dependency tracking rather than wave-level batching

### Worker Reallocation (from DRAMA)
- When a node completes early, freed capacity can be assigned to help other nodes
- Requires DRAMA-style resource object abstraction for agents
- Agent dropout recovery: if a node fails, another agent can take over its work

### Learned Decomposition (from GAP)
- Train the Critic to produce better DAGs via SFT + RL
- Reward signal: execution efficiency, output quality, cost
- Dependency modeling becomes a native capability instead of prompt engineering

---

## Implementation Plan

### Files to Create

- `nous/cognitive/dag.py` — `TaskNode`, `TaskDAG`, `DAGExecutionResult`, validation logic
- `nous/cognitive/wave_scheduler.py` — `WaveScheduler`, wave execution, failure propagation
- `nous/cognitive/dag_transaction.py` — `DAGTransactionManager`, cross-wave commit policy

### Files to Modify

- `nous/cognitive/critic.py` — Add `dependency_type` and `task_graph` to Critic output schema. Add DAG construction from Critic response. Route to WaveScheduler when `dependency_type == "phased"`.
- `nous/cognitive/critic_schemas.py` — Add `TaskGraphSchema`, `DependencyType` enum
- `nous/cognitive/transaction.py` — Add `hold_open()` method for prerequisite nodes (defer commit/rollback)

### Estimated Effort

- DAG data structures + validation: ~3 hours
- Wave scheduler: ~4 hours
- Predecessor context injection + summarization: ~3 hours
- Critic prompt amendment + schema: ~2 hours
- DAG transaction manager: ~3 hours
- Integration tests: ~4 hours
- **Total: ~19 hours** (within Phase 1's existing 15-20h estimate — absorbs into it)

### Success Criteria

1. Critic produces valid DAGs for multi-step tasks >80% of the time (no cycles, reasonable decomposition)
2. Wave-scheduled execution produces better results than independent parallel on phased tasks (human judged, >60%)
3. Predecessor context injection preserves information from upstream nodes (no hallucination of prerequisites)
4. DAG validation catches all malformed graphs before execution
5. Prerequisite node facts are committed, non-selected leaf facts are rolled back (transaction integrity)
6. Total DAG execution latency < sum of all node latencies × 0.7 (parallelism provides meaningful speedup)

---

## References

1. Geng & Neubig. "Effective Strategies for Asynchronous Software Engineering Agents." CMU LTI, arXiv:2603.21489, March 2026.
2. Yu, Ding, Sato. "DynTaskMAS: Dynamic Task Graph-driven Framework for Asynchronous and Parallel LLM-based Multi-Agent Systems." ICAPS 2025, arXiv:2503.07675v2.
3. GAP: Learning dependency analysis via SFT+RL. Tsinghua/CMU.
4. DRAMA: Event-driven multi-agent reallocation. arXiv:2508.04332.
5. Chen et al. "Atomix: Transactional Isolation for AI Agent Interactions." arXiv:2503.18890, March 2025.
6. Zhang et al. "M1-Parallel: Optimizing Sequential Multi-Step Tasks with Parallel LLM Agents." Microsoft, ICML 2025, arXiv:2507.08944.
