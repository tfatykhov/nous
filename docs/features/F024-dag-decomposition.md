# F024 Amendment — DAG Decomposition & Wave Scheduling

> **Status:** Draft v2
> **Amends:** F024 Critic Agent v3 — Phase 1 (Parallel Spawn + Pick Winner)
> **Priority:** P1
> **Author:** Nous + Tim
> **Date:** 2026-03-31
> **Research basis:** CAID (CMU, arXiv:2603.21489), DynTaskMAS (ICAPS 2025, arXiv:2503.07675v2), GAP (Tsinghua/CMU), DRAMA (arXiv:2508.04332), Atomix (arXiv:2503.18890), M1-Parallel (Microsoft ICML 2025, arXiv:2507.08944)

---

## Problem

F024 Phase 1 has two structural gaps identified by cross-referencing CAID, DynTaskMAS, and DRAMA research:

**Gap 1: No dependency modeling between parallel branches.**
The current spec assumes all parallel instances are independent — spawn N, pick winner. But real complex tasks have **dependencies**: "research X, then compare X with Y, then write summary." Spawning all three simultaneously wastes compute (instance 2 can't compare without instance 1's results) and produces lower quality (instance 2 hallucinates what X found instead of using real results).

CAID demonstrated this empirically: missing `autodiff.py` as a dependency caused a **26-point swing** (8.7% vs 34.3%) between runs. The dependency graph IS the architecture — without it, quality is random.

**Gap 2: Fire-and-forget spawning wastes early finishers.**
When a subtask completes ahead of others, that information sits idle. No successor tasks are unblocked. No freed capacity is reallocated. DRAMA showed that event-driven reallocation yields **17% runtime improvement** and **13% resource reduction** over static scheduling.

**Gap 3: No orchestration layer between Critic and execution.**
The current architecture has the Critic producing a routing decision and the Main Agent executing it. For DAG-based execution, we need a **stateful controller** that tracks node states, manages wave progression, handles failures, and feeds results back to the Main Agent for final assembly. Without this, the DAG is just a data structure with no one to drive it.

---

## Solution: Task Controller + DAG Decomposition Layer

Add a **Task Controller** component and a **dependency analysis step** between Critic classification and instance spawning. The Critic already decomposes complex tasks — this amendment structures that decomposition as an explicit directed acyclic graph (DAG) and introduces a dedicated controller to execute it.

---

## Component Architecture

### Separation of Concerns

The DAG execution flow involves four distinct roles, each with a clear boundary:

```
┌──────────────┐    ┌──────────────────┐    ┌──────────────────┐    ┌──────────────┐
│    Critic     │───▶│  Task Controller  │───▶│  Subtask Workers │───▶│  Main Agent   │
│  (Planner)   │    │  (Orchestrator)   │    │  (Executors)     │    │  (Assembler)  │
└──────────────┘    └──────────────────┘    └──────────────────┘    └──────────────┘
                                │                     │
                                │   monitors/retries  │
                                │◀────────────────────│
```

**Critic (Planner)** — Produces the DAG. Evaluates outputs between waves.
- Input: User message + conversation context
- Output: `CriticResult` with `dependency_type` and `task_graph`
- Does NOT execute anything
- Does NOT talk to user
- Invoked by Task Controller between waves for quality gates

**Task Controller (Orchestrator)** — Drives DAG execution. Stateful, rule-based + Critic-delegated decisions.
- Input: `TaskDAG` from Critic
- Output: Ordered collection of node results
- Manages node lifecycle: pending → queued → running → completed/failed/blocked
- Schedules waves, enforces concurrency caps
- Injects predecessor context into successor prompts
- Handles failure propagation and (Phase 2) retries
- Tracks cost/token budget across all nodes
- Reports progress to Main Agent
- **Is NOT an LLM agent** — it's a stateful async controller with decision points delegated to Critic

**Subtask Workers (Executors)** — Individual Nous instances executing one node each.
- Input: Node instructions + injected predecessor context
- Output: Response text + transaction journal
- Each runs in its own `CognitiveTransaction`
- No awareness of the DAG — just sees its instructions and context
- Existing `spawn_task` / `execute_in_transaction` machinery

**Main Agent (Assembler)** — Owns the user conversation. Assembles final response.
- Input: All completed node results from Task Controller
- Output: User-facing response
- Knows user's tone, preferences, conversation history
- Synthesizes multi-node outputs into coherent reply
- Same agent that handles non-DAG conversations today

### What the Task Controller IS and IS NOT

**IS:**
- A stateful async orchestrator (event loop + state machine)
- A rule engine for scheduling decisions (wave ordering, concurrency caps, failure propagation)
- A delegation point (calls Critic for quality gates, calls workers for execution)
- A budget tracker (tokens, cost, wall-clock time across all nodes)
- A progress reporter (Main Agent can query: "how far along?")

**IS NOT:**
- An LLM agent (no model calls of its own — delegates to Critic for intelligence)
- A response generator (never produces text for the user)
- A decision maker for task decomposition (that's the Critic's job)
- A replacement for the Main Agent (Main Agent still owns the conversation)

### Task Controller State Machine

Each DAG execution follows this lifecycle:

```
                    ┌─────────────────────────────────┐
                    │         INITIALIZING             │
                    │  Validate DAG, set up state      │
                    └───────────────┬─────────────────┘
                                    │
                    ┌───────────────▼─────────────────┐
                    │         EXECUTING                │
                    │  Loop: schedule wave → await     │
                    │  completion → quality gate →     │◀──┐
                    │  inject context → next wave      │   │ (retry wave on
                    └───────────────┬─────────────────┘   │  quality gate fail)
                                    │                     │
                            ┌───────┴───────┐             │
                            │               │             │
                    ┌───────▼──────┐  ┌─────▼──────┐     │
                    │  ALL DONE    │  │  WAVE FAIL  │─────┘ (Phase 2 only)
                    └───────┬──────┘  └─────┬──────┘
                            │               │
                    ┌───────▼──────┐  ┌─────▼──────┐
                    │  ASSEMBLING  │  │  PARTIAL    │
                    │  Pass to     │  │  RESULT     │
                    │  Main Agent  │  │  Best-effort│
                    └──────────────┘  └─────────────┘
```

Node-level states:
```
pending → queued → running → completed
                           → failed → (dependents: blocked)
```

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
    
    # Runtime state (populated by Task Controller)
    status: str = "pending"          # "pending" | "queued" | "running" | "completed" | "failed" | "blocked"
    transaction: Optional[CognitiveTransaction] = None
    result: Optional[str] = None     # Output text when completed
    duration_ms: int = 0
    tokens_used: int = 0             # Token consumption for budget tracking
    retry_count: int = 0             # Number of retries (Phase 2)
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

### TaskController

```python
class TaskController:
    """
    Stateful orchestrator for DAG execution.
    Drives wave scheduling, failure handling, quality gates, 
    and budget tracking. Not an LLM agent — delegates intelligence
    to Critic and execution to subtask workers.
    """
    
    def __init__(
        self,
        dag: TaskDAG,
        critic: CriticAgent,
        max_parallel: int = 3,
        token_budget: int = 100_000,
        timeout_per_node_ms: int = 120_000,
    ):
        self.dag = dag
        self.critic = critic
        self.max_parallel = max_parallel
        self.token_budget = token_budget
        self.timeout_per_node_ms = timeout_per_node_ms
        
        # Runtime state
        self.state: ControllerState = ControllerState.INITIALIZING
        self.tokens_consumed: int = 0
        self.start_time_ms: int = 0
        self.wave_history: list[WaveResult] = []
    
    # ─── Lifecycle ──────────────────────────────────────────────
    
    async def execute(self) -> DAGExecutionResult:
        """Main execution loop. Returns all node results."""
        
        # Phase: INITIALIZING
        issues = self.dag.validate()
        if issues:
            return DAGExecutionResult(
                dag=self.dag,
                status="validation_failed",
                errors=issues,
            )
        
        waves = self.dag.topological_waves()
        self.state = ControllerState.EXECUTING
        self.start_time_ms = _now_ms()
        
        # Phase: EXECUTING — wave by wave
        for wave_idx, wave_node_ids in enumerate(waves):
            
            # Budget check before starting wave
            if self.tokens_consumed >= self.token_budget:
                self.state = ControllerState.BUDGET_EXCEEDED
                break
            
            # Execute wave (respecting max_parallel)
            wave_result = await self._execute_wave(wave_node_ids, wave_idx)
            self.wave_history.append(wave_result)
            
            # Quality gate between waves (Critic evaluates outputs)
            if wave_idx < len(waves) - 1:  # Not the last wave
                gate_passed = await self._quality_gate(wave_result, wave_idx)
                if not gate_passed:
                    # Phase 1: stop and return partial results
                    # Phase 2: retry wave with adjusted prompts
                    self.state = ControllerState.PARTIAL_RESULT
                    break
        
        # Phase: ASSEMBLING — collect all results
        if self.state == ControllerState.EXECUTING:
            self.state = ControllerState.COMPLETED
        
        return DAGExecutionResult(
            dag=self.dag,
            status=self.state.value,
            wave_history=self.wave_history,
            tokens_consumed=self.tokens_consumed,
            duration_ms=_now_ms() - self.start_time_ms,
        )
    
    # ─── Wave Execution ─────────────────────────────────────────
    
    async def _execute_wave(
        self, node_ids: list[str], wave_idx: int
    ) -> WaveResult:
        """Execute all nodes in a wave concurrently."""
        
        # Filter out blocked nodes
        active_ids = [
            nid for nid in node_ids 
            if self.dag.nodes[nid].status != "blocked"
        ]
        
        # Respect max_parallel: chunk if needed
        sub_waves = _chunk(active_ids, self.max_parallel)
        node_results = []
        
        for sub_wave in sub_waves:
            tasks = []
            for node_id in sub_wave:
                node = self.dag.nodes[node_id]
                
                # Inject predecessor context
                predecessor_context = self.dag.inject_predecessor_context(node_id)
                augmented_instructions = predecessor_context + node.instructions
                
                node.status = "running"
                task = self._spawn_worker(node, augmented_instructions)
                tasks.append((node_id, task))
            
            # Await all in sub-wave
            for node_id, task in tasks:
                try:
                    result = await asyncio.wait_for(
                        task, 
                        timeout=self.timeout_per_node_ms / 1000
                    )
                    node = self.dag.nodes[node_id]
                    node.status = "completed"
                    node.result = result.response_text
                    node.duration_ms = result.duration_ms
                    node.tokens_used = result.tokens_used
                    node.transaction = result.transaction
                    self.tokens_consumed += result.tokens_used
                    node_results.append(NodeResult(
                        node_id=node_id, status="completed", output=result
                    ))
                except (asyncio.TimeoutError, Exception) as e:
                    node = self.dag.nodes[node_id]
                    node.status = "failed"
                    self._propagate_failure(node_id)
                    node_results.append(NodeResult(
                        node_id=node_id, status="failed", error=str(e)
                    ))
        
        return WaveResult(
            wave_index=wave_idx,
            node_results=node_results,
            tokens_used=sum(nr.output.tokens_used for nr in node_results if nr.output),
        )
    
    # ─── Quality Gate ────────────────────────────────────────────
    
    async def _quality_gate(
        self, wave_result: WaveResult, wave_idx: int
    ) -> bool:
        """
        Ask Critic to evaluate wave outputs before proceeding.
        Returns True if outputs are sufficient for successor nodes.
        
        Phase 1: binary pass/fail — fail means stop with partial results.
        Phase 2: fail can trigger retry with adjusted prompts.
        """
        completed_outputs = {
            nr.node_id: nr.output.response_text 
            for nr in wave_result.node_results 
            if nr.status == "completed"
        }
        
        if not completed_outputs:
            return False  # Entire wave failed
        
        # Get successor nodes that need these outputs
        successors = set()
        for node_id in completed_outputs:
            for (src, dst) in self.dag.edges:
                if src == node_id:
                    successors.add(dst)
        
        if not successors:
            return True  # No successors — this was the last wave
        
        # Ask Critic: are these outputs sufficient?
        gate_result = await self.critic.evaluate_wave_quality(
            wave_outputs=completed_outputs,
            successor_requirements={
                sid: self.dag.nodes[sid].description 
                for sid in successors
            },
        )
        
        return gate_result.sufficient
    
    # ─── Failure Handling ────────────────────────────────────────
    
    def _propagate_failure(self, failed_node_id: str):
        """Mark all transitive dependents of a failed node as blocked."""
        dependents = [dst for (src, dst) in self.dag.edges if src == failed_node_id]
        for dep_id in dependents:
            node = self.dag.nodes[dep_id]
            if node.status == "pending":  # Don't block already-completed nodes
                node.status = "blocked"
                self._propagate_failure(dep_id)
    
    # ─── Worker Spawning ─────────────────────────────────────────
    
    async def _spawn_worker(self, node: TaskNode, instructions: str):
        """Spawn a single Nous instance for this node."""
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
    
    # ─── Status / Progress ───────────────────────────────────────
    
    def get_progress(self) -> dict:
        """Return current execution progress for Main Agent queries."""
        total = len(self.dag.nodes)
        completed = sum(1 for n in self.dag.nodes.values() if n.status == "completed")
        failed = sum(1 for n in self.dag.nodes.values() if n.status == "failed")
        blocked = sum(1 for n in self.dag.nodes.values() if n.status == "blocked")
        running = sum(1 for n in self.dag.nodes.values() if n.status == "running")
        
        return {
            "state": self.state.value,
            "total_nodes": total,
            "completed": completed,
            "running": running,
            "failed": failed,
            "blocked": blocked,
            "pending": total - completed - failed - blocked - running,
            "tokens_consumed": self.tokens_consumed,
            "token_budget": self.token_budget,
            "waves_completed": len(self.wave_history),
            "total_waves": self.dag.total_waves,
        }


class ControllerState(Enum):
    INITIALIZING = "initializing"
    EXECUTING = "executing"
    COMPLETED = "completed"
    PARTIAL_RESULT = "partial_result"
    BUDGET_EXCEEDED = "budget_exceeded"
    VALIDATION_FAILED = "validation_failed"
```

---

## When DAG Applies vs. Doesn't

The DAG layer activates **only** when the Critic routes to `parallel-select` or `parallel-merge` with `dependency_type: "phased"`. The existing routing modes map as follows:

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
- `"phased"` — DAG decomposition, Task Controller activated

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

## Integration: End-to-End Flow

Here is the complete flow showing how all components interact:

```
1. User sends complex message
   │
2. CognitiveLayer.process_turn() invokes Critic
   │
3. Critic classifies → routing: "parallel-merge", dependency_type: "phased"
   Critic produces TaskDAG with nodes and edges
   │
4. CognitiveLayer instantiates TaskController(dag, critic)
   │
5. TaskController.execute() begins
   │
   ├─ 5a. Validate DAG (cycle detection, depth check, node count)
   │
   ├─ 5b. Compute topological waves
   │
   ├─ 5c. Wave 1: spawn workers for all Wave 1 nodes (parallel)
   │       Each worker gets: node.instructions + predecessor_context (empty for Wave 1)
   │       Each worker runs in its own CognitiveTransaction
   │
   ├─ 5d. Await Wave 1 completion
   │       Failed nodes → propagate_failure to dependents
   │
   ├─ 5e. Quality Gate: Critic evaluates Wave 1 outputs
   │       "Are these sufficient for Wave 2 successors?"
   │       If no → stop with partial results (Phase 1) or retry (Phase 2)
   │
   ├─ 5f. Wave 2: spawn workers with predecessor context injected
   │       ... repeat 5d-5e ...
   │
   ├─ 5g. Final wave completed → all results collected
   │
6. TaskController returns DAGExecutionResult to CognitiveLayer
   │
7. CognitiveLayer passes node results to Main Agent as context
   │
8. Main Agent assembles final response → User
   │
9. DAGTransactionManager commits prerequisite transactions,
   commits selected leaf, rolls back non-selected leaves
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
8. **Task Controller makes no LLM calls.** All intelligence is delegated to Critic (quality gates) or workers (execution). The controller is pure orchestration logic.

---

## Interaction with Existing F024 Modes

### Compatibility Matrix

- **Passthrough** → No change. No Critic, no DAG, no Task Controller.
- **Single-Advised** → No change. Single instance, no DAG needed.
- **Parallel-Select (independent)** → No change. Existing behavior preserved. `dependency_type: "independent"`.
- **Parallel-Select (phased)** → **NEW.** Critic produces DAG. Task Controller drives execution. Critic evaluates leaf outputs.
- **Parallel-Merge (phased)** → **NEW.** Same as above but Critic synthesizes leaf outputs instead of picking winner.
- **Parallel-Evaluate** → No change. Same prompt K times — inherently independent, no dependencies.

### Backward Compatibility

All existing F024 behavior is preserved. The Task Controller and DAG layer are **additive** — they only activate when the Critic explicitly produces `dependency_type: "phased"`. The default remains `"independent"`.

---

## Phase 2 Extensions (Future — Not in Scope)

### Dynamic DAG Updates (from DynTaskMAS)
- Mid-execution graph modifications: G(t+1) = U(G(t), Δ(t))
- If a prerequisite reveals the task is simpler than expected, prune remaining nodes
- If a prerequisite reveals new sub-problems, add nodes dynamically
- Requires Task Controller to support graph mutation during execution

### Completion-Triggered Unblocking (from DRAMA)
- Instead of waiting for ALL Wave N to complete, unblock individual successors as soon as ALL of their specific predecessors complete
- More efficient for unbalanced DAGs where one node finishes much faster
- Task Controller tracks per-node readiness rather than wave-level batching

### Worker Reallocation (from DRAMA)
- When a node completes early, freed capacity can be assigned to help other nodes
- Requires DRAMA-style resource object abstraction for agents
- Task Controller becomes a resource scheduler, not just a wave scheduler
- Agent dropout recovery: if a node fails, another agent can take over its work

### Retry with Adjusted Prompts
- Quality gate fails → Task Controller asks Critic: "what should change?"
- Critic produces adjusted instructions for retry
- Task Controller re-spawns failed wave with new prompts
- Max 1 retry per wave to prevent infinite loops

### Learned Decomposition (from GAP)
- Train the Critic to produce better DAGs via SFT + RL
- Reward signal: execution efficiency, output quality, cost
- Dependency modeling becomes a native capability instead of prompt engineering

---

## Implementation Plan

### Files to Create

- `nous/cognitive/task_dag.py` — `TaskNode`, `TaskDAG`, `DAGExecutionResult`, validation logic
- `nous/cognitive/task_controller.py` — `TaskController`, `ControllerState`, wave execution, failure propagation, quality gates, budget tracking, progress reporting
- `nous/cognitive/dag_transaction.py` — `DAGTransactionManager`, cross-wave commit policy

### Files to Modify

- `nous/cognitive/critic.py` — Add `dependency_type` and `task_graph` to Critic output schema. Add DAG construction from Critic response. Add `evaluate_wave_quality()` method for quality gates.
- `nous/cognitive/critic_schemas.py` — Add `TaskGraphSchema`, `DependencyType` enum
- `nous/cognitive/layer.py` — Integrate Task Controller: when Critic returns `dependency_type: "phased"`, instantiate TaskController, execute DAG, pass results to Main Agent for assembly
- `nous/cognitive/transaction.py` — Add `hold_open()` method for prerequisite nodes (defer commit/rollback)

### Estimated Effort

- TaskDAG data structures + validation: ~3 hours
- TaskController core (state machine, wave loop, failure propagation): ~5 hours
- Quality gate integration with Critic: ~2 hours
- Predecessor context injection + summarization: ~3 hours
- Critic prompt amendment + schema: ~2 hours
- DAG transaction manager: ~3 hours
- CognitiveLayer integration: ~2 hours
- Integration tests: ~4 hours
- **Total: ~24 hours**

### Success Criteria

1. Critic produces valid DAGs for multi-step tasks >80% of the time (no cycles, reasonable decomposition)
2. Task Controller correctly schedules waves — parallel within, sequential between
3. Wave-scheduled execution produces better results than independent parallel on phased tasks (human judged, >60%)
4. Predecessor context injection preserves information from upstream nodes (no hallucination of prerequisites)
5. DAG validation catches all malformed graphs before execution
6. Prerequisite node facts are committed, non-selected leaf facts are rolled back (transaction integrity)
7. Total DAG execution latency < sum of all node latencies × 0.7 (parallelism provides meaningful speedup)
8. Quality gates catch insufficient outputs before wasting downstream compute (>50% of preventable failures caught)
9. Task Controller token budget enforcement prevents runaway cost
10. Progress reporting accurately reflects real-time DAG state

---

## References

1. Geng & Neubig. "Effective Strategies for Asynchronous Software Engineering Agents." CMU LTI, arXiv:2603.21489, March 2026.
2. Yu, Ding, Sato. "DynTaskMAS: Dynamic Task Graph-driven Framework for Asynchronous and Parallel LLM-based Multi-Agent Systems." ICAPS 2025, arXiv:2503.07675v2.
3. GAP: Learning dependency analysis via SFT+RL. Tsinghua/CMU.
4. DRAMA: Event-driven multi-agent reallocation. arXiv:2508.04332.
5. Chen et al. "Atomix: Transactional Isolation for AI Agent Interactions." arXiv:2503.18890, March 2025.
6. Zhang et al. "M1-Parallel: Optimizing Sequential Multi-Step Tasks with Parallel LLM Agents." Microsoft, ICML 2025, arXiv:2507.08944.
