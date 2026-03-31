# F024 Amendment — DAG Decomposition & Wave Scheduling

> **Status:** Draft v3
> **Amends:** F024 Critic Agent v3 — Phase 1 (Parallel Spawn + Pick Winner)
> **Priority:** P1
> **Author:** Nous + Tim
> **Date:** 2026-03-31
> **Research basis:** CAID (CMU, arXiv:2603.21489), DynTaskMAS (ICAPS 2025, arXiv:2503.07675v2), GAP (Tsinghua/CMU), DRAMA (arXiv:2508.04332), Atomix (arXiv:2503.18890), M1-Parallel (Microsoft ICML 2025, arXiv:2507.08944)

---

## Phase Restructure: 1a / 1b Split

The original F024 Phase 1 ("Parallel Spawn + Pick Winner") bundled two fundamentally different capabilities:

1. **DAG-based task decomposition** — break complex tasks into dependent subtasks, execute in waves
2. **Transactional competing execution** — run N approaches to the same problem, pick winner, rollback losers

These have different prerequisites, different risk profiles, and different value propositions:

| Dimension | Phase 1a (DAG) | Phase 1b (Transactions) |
|---|---|---|
| **What** | Decompose → schedule → assemble | Compete → evaluate → pick winner |
| **Pattern** | "Break this into steps" | "Try 3 approaches, pick best" |
| **Infrastructure needed** | TaskController + existing subtasks | CognitiveTransaction + journal buffer + interceptor |
| **Side effects** | All committed (real work) | Winner committed, losers rolled back |
| **Risk** | Low — additive on existing subtask system | Medium — requires isolation guarantees |
| **Value coverage** | ~80% of complex tasks | ~20% (adversarial/creative tasks) |
| **Effort** | ~16 hours | ~12 hours |
| **Dependencies** | Phase 0 (Critic) ✅ done | Phase 1a (DAG) + new transaction infra |

**Decision: Phase 1a (DAG) ships first. Phase 1b (Transactions) builds on top of it.**

This means complex multi-step tasks get immediate improvement, while the harder transactional isolation work follows with a proven orchestration layer already in place.

---

## Phase 1a: DAG + Task Controller

### What Ships

- Critic produces dependency graphs for multi-step tasks
- TaskController orchestrates wave-based execution
- Predecessor context injection between waves
- Quality gates between waves (Critic evaluates)
- Failure propagation (blocked dependents)
- Budget tracking (token/cost caps)
- Progress reporting
- Integration with existing subtask infrastructure (no redesign)

### What Does NOT Ship (deferred to 1b)

- CognitiveTransaction (journal buffer, commit/rollback)
- TransactionInterceptor (tool wrapping for isolation)
- Competing parallel approaches (same problem, multiple strategies)
- Read-through layer for pending facts
- bash/spawn blocking in parallel mode
- Winner selection among competing leaf nodes

### Why Transactions Aren't Needed for 1a

In DAG execution, every node produces **real work that gets kept**:
- Node A researches topic X → that research is valuable regardless
- Node B analyzes codebase Y → that analysis is real
- Node C combines A+B into a recommendation → that's the final output

There's no "loser" to roll back. Every node contributes to the final result. Side effects (learned facts, recorded decisions) from every node are legitimate and should be committed.

This is fundamentally different from the competing pattern:
- Instance 1 tries approach A → might be wrong
- Instance 2 tries approach B → might be wrong
- Instance 3 tries approach C → might be wrong
- Only the winner's side effects should persist → need transactions

### Subtask Integration (Additive, No Redesign)

The existing subtask system is preserved entirely. The TaskController sits on top:

**Current flow (unchanged):**
```
User → spawn_task → SubtaskManager.create() → Worker picks up → done
```

**DAG flow (new, additive):**
```
Critic → TaskController → SubtaskManager.create() × N → Workers pick up wave by wave → done
```

**Subtask model additions (3 nullable fields):**
- `dag_id: Optional[str]` — which DAG this belongs to (null = standalone subtask)
- `wave_number: Optional[int]` — which wave in the DAG
- `predecessor_ids: Optional[list[str]]` — subtask IDs this depends on (JSONB)

**SubtaskManager additions (2 new queries):**
- `get_completed_for_dag(dag_id, wave)` — fetch completed results from a specific wave
- `get_pending_for_dag(dag_id)` — check overall DAG completion status

**Workers remain unaware of DAGs.** They dequeue subtasks and execute them exactly as today. The only difference is that DAG subtasks arrive with predecessor context already injected into their instructions.

---

## Phase 1b: Transactional Competing Execution

### What Ships (after 1a is stable)

- `CognitiveTransaction` — journaled side effects with read-through
- `TransactionInterceptor` — wraps tool execution in competing mode
- Parallel-Select routing — spawn 2-3 competing approaches to same problem
- Parallel-Evaluate routing — spawn K=3-5 identical prompts for self-consistency (v3)
- Winner selection by Critic — evaluate competing outputs, pick best
- Loser rollback — clean removal of non-winner side effects
- bash/spawn/schedule blocking in competing instances
- recall_deep access count suppression in competing instances

### Why 1b Depends on 1a

1. **TaskController is reused.** Competing instances are just a special case of DAG execution — a single wave with multiple independent leaf nodes and no predecessors. The TaskController manages their lifecycle.
2. **Quality gate becomes winner selection.** The same Critic evaluation mechanism used for quality gates in 1a is extended to compare competing outputs in 1b.
3. **Budget tracking carries over.** Competing execution is expensive (2-3× cost). The budget enforcement from 1a prevents runaway spending.
4. **Mixed patterns become possible.** With both 1a and 1b, a DAG can have prerequisite nodes (1a pattern) feeding into competing leaf nodes (1b pattern). Example:
   ```
   Wave 1: Research topic (1a — single node, real work)
   Wave 2: Write article approach A vs approach B (1b — competing, pick winner)
   ```

### Transaction Architecture (1b-specific)

```python
class CognitiveTransaction:
    """Journals all side effects for later commit or rollback."""
    
    # Buffered side effects
    pending_facts: list[FactEntry]
    pending_decisions: list[DecisionEntry]
    pending_files: dict[str, str]  # path → content
    
    # Read-through: instances can recall their own pending facts
    def recall_with_pending(self, query) -> list[Result]:
        """Merge real recall results with pending facts from this transaction."""
        ...
    
    def commit(self) -> CommitResult:
        """Persist all buffered side effects to real storage."""
        ...
    
    def rollback(self):
        """Discard all buffered side effects."""
        ...
    
    def cherry_pick(self, fact_ids: list[str]) -> CommitResult:
        """Commit only selected facts (Phase 2 merge capability)."""
        ...
```

**Transaction policy for mixed DAG+competing execution:**
- Prerequisite nodes (non-leaf): always commit on DAG success (real work)
- Competing leaf nodes: winner commits, losers roll back
- If DAG fails (quality gate): all transactions roll back

---

## Component Architecture

### Separation of Concerns (applies to both 1a and 1b)

```
┌──────────────┐    ┌──────────────────┐    ┌──────────────────┐    ┌──────────────┐
│    Critic     │───▶│  Task Controller  │───▶│  Subtask Workers │───▶│  Main Agent   │
│  (Planner)   │    │  (Orchestrator)   │    │  (Executors)     │    │  (Assembler)  │
└──────────────┘    └──────────────────┘    └──────────────────┘    └──────────────┘
                                │                     │
                                │   monitors/retries  │
                                │◀────────────────────│
```

**Critic (Planner)** — Produces the DAG. Evaluates outputs between waves. Picks winner in competing mode (1b).
- Input: User message + conversation context
- Output: `CriticResult` with `dependency_type` and `task_graph`
- Does NOT execute anything
- Does NOT talk to user
- Invoked by Task Controller for quality gates (1a) and winner selection (1b)

**Task Controller (Orchestrator)** — Drives DAG execution. Stateful, rule-based + Critic-delegated decisions.
- Input: `TaskDAG` from Critic
- Output: Ordered collection of node results
- Manages node lifecycle: pending → queued → running → completed/failed/blocked
- Schedules waves, enforces concurrency caps
- Injects predecessor context into successor prompts
- Handles failure propagation and (Phase 2) retries
- Tracks cost/token budget across all nodes
- Reports progress to Main Agent
- **Is NOT an LLM agent** — zero model calls, all intelligence delegated

**Subtask Workers (Executors)** — Individual Nous instances executing one node each.
- Input: Node instructions + injected predecessor context
- Output: Response text (+ transaction journal in 1b competing mode)
- Each runs via existing `spawn_task` / subtask worker pool
- No awareness of the DAG — just sees its instructions and context

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
    
    # Execution mode (Phase 1b)
    execution_mode: str = "standard" # "standard" (1a) | "competing" (1b)
    
    # Runtime state (populated by Task Controller)
    status: str = "pending"          # "pending" | "queued" | "running" | "completed" | "failed" | "blocked"
    subtask_id: Optional[str] = None # Links to existing Subtask model
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
    dag_id: str                      # Unique ID for this DAG execution
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
        
        # Max nodes (hard cap)
        if len(self.nodes) > 6:
            issues.append(f"Too many nodes ({len(self.nodes)}). Max 6 per decomposition.")
        
        # Max depth (waves)
        try:
            waves = self.topological_waves()
            if len(waves) > 3:
                issues.append(f"Too deep ({len(waves)} waves). Max 3 sequential waves.")
            # Max parallel per wave
            for i, wave in enumerate(waves):
                if len(wave) > 3:
                    issues.append(f"Wave {i} has {len(wave)} nodes. Max 3 parallel per wave.")
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
def _now_ms() -> int:
    """Current time in milliseconds."""
    import time
    return int(time.time() * 1000)


def _chunk(items: list, size: int) -> list[list]:
    """Split a list into chunks of at most `size`."""
    return [items[i:i + size] for i in range(0, len(items), size)]


class TaskController:
    """
    Stateful orchestrator for DAG execution.
    Drives wave scheduling, failure handling, quality gates, 
    and budget tracking. Not an LLM agent — delegates intelligence
    to Critic and execution to subtask workers.
    
    Phase 1a: Manages DAG waves via existing subtask infrastructure.
    Phase 1b: Additionally manages competing instances with transactions.
    """
    
    def __init__(
        self,
        dag: TaskDAG,
        critic: CriticAgent,
        subtask_manager: SubtaskManager,  # Existing — no new class needed
        max_parallel: int = 3,
        token_budget: int = 100_000,
        timeout_per_node_ms: int = 120_000,
    ):
        self.dag = dag
        self.critic = critic
        self.subtask_manager = subtask_manager
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
        
        # Degenerate case: single-node DAG → skip orchestration overhead,
        # fall through to normal single-advised execution
        if len(self.dag.nodes) == 1:
            return DAGExecutionResult(
                dag=self.dag,
                status="degenerate_single_node",
                errors=[],
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
                    # Phase 1a: stop and return partial results
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
        """Execute all nodes in a wave via existing subtask system."""
        
        # Filter out blocked nodes
        active_ids = [
            nid for nid in node_ids 
            if self.dag.nodes[nid].status != "blocked"
        ]
        
        # Respect max_parallel: chunk if needed
        sub_waves = _chunk(active_ids, self.max_parallel)
        node_results = []
        
        for sub_wave in sub_waves:
            # Create subtasks via existing SubtaskManager
            subtask_ids = []
            for node_id in sub_wave:
                node = self.dag.nodes[node_id]
                
                # Inject predecessor context
                predecessor_context = self.dag.inject_predecessor_context(node_id)
                augmented_instructions = predecessor_context + node.instructions
                
                # Create subtask through existing system
                subtask = await self.subtask_manager.create(
                    task=augmented_instructions,
                    frame_type=node.frame_type,
                    dag_id=self.dag.dag_id,
                    wave_number=wave_idx,
                    predecessor_ids=[
                        self.dag.nodes[dep].subtask_id 
                        for dep in node.depends_on
                        if self.dag.nodes[dep].subtask_id
                    ],
                )
                node.subtask_id = subtask.id
                node.status = "queued"
                subtask_ids.append((node_id, subtask.id))
            
            # Await completion of all subtasks in this sub-wave
            for node_id, subtask_id in subtask_ids:
                try:
                    result = await self._await_subtask(
                        subtask_id, 
                        timeout_ms=self.timeout_per_node_ms
                    )
                    node = self.dag.nodes[node_id]
                    node.status = "completed"
                    node.result = result.response_text
                    node.duration_ms = result.duration_ms
                    node.tokens_used = result.tokens_used
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
            tokens_used=sum(
                nr.output.tokens_used for nr in node_results 
                if nr.output and hasattr(nr.output, 'tokens_used')
            ),
        )
    
    async def _await_subtask(self, subtask_id: str, timeout_ms: int):
        """Poll subtask completion via SubtaskManager."""
        deadline = _now_ms() + timeout_ms
        while _now_ms() < deadline:
            subtask = await self.subtask_manager.get(subtask_id)
            if subtask.status == "completed":
                return subtask.result
            if subtask.status == "failed":
                raise SubtaskFailedError(subtask.error)
            await asyncio.sleep(1)  # Poll interval — TODO Phase 2: replace with asyncio.Event/callback for event-driven completion notification
        raise asyncio.TimeoutError(f"Subtask {subtask_id} timed out after {timeout_ms}ms")
    
    # ─── Quality Gate ────────────────────────────────────────────
    
    async def _quality_gate(
        self, wave_result: WaveResult, wave_idx: int
    ) -> bool:
        """
        Ask Critic to evaluate wave outputs before proceeding.
        Returns True if outputs are sufficient for successor nodes.
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
            if node.status == "pending":
                node.status = "blocked"
                self._propagate_failure(dep_id)
    
    # ─── Status / Progress ───────────────────────────────────────
    
    def get_progress(self) -> dict:
        """Return current execution progress for Main Agent queries."""
        total = len(self.dag.nodes)
        completed = sum(1 for n in self.dag.nodes.values() if n.status == "completed")
        failed = sum(1 for n in self.dag.nodes.values() if n.status == "failed")
        blocked = sum(1 for n in self.dag.nodes.values() if n.status == "blocked")
        running = sum(1 for n in self.dag.nodes.values() if n.status in ("queued", "running"))
        
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

The DAG layer activates **only** when the Critic produces `dependency_type: "phased"`. Mapping:

- **Passthrough** → No DAG. No Critic, no spawning.
- **Single-Advised** → No DAG. Single instance, no dependencies possible.
- **Parallel-Select (independent)** → No DAG. Existing behavior. (Phase 1b: add transactions for competing)
- **Parallel-Select (phased)** → **DAG (Phase 1a).** Critic produces TaskDAG, Task Controller drives execution.
- **Parallel-Merge (phased)** → **DAG (Phase 1a).** Same as above, Main Agent merges leaf outputs.
- **Parallel-Evaluate** → No DAG. Same prompt K times — inherently independent. (Phase 1b scope)

---

## Critic Prompt Amendment

The existing pre-turn classification prompt gains additional output fields:

```
...existing Critic prompt fields...

If the task has multiple dependent steps, also decide:

6. dependency_type: "independent" | "phased"
   - "independent": single task or parallel alternatives (no dependencies)
   - "phased": multiple steps that build on each other; produce a dependency graph

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
- Maximum 3 parallel nodes per wave
- No cycles
- Each node must have a clear, actionable instruction
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

```
=== CONTEXT FROM COMPLETED PREREQUISITES ===

=== Output from 'Research CAID parallelism approach' ===
CAID uses a Manager + N Engineer architecture with physical git worktree 
isolation. Key findings: soft isolation degrades performance below single-agent.
Optimal parallelism is 2-4 engineers depending on task type...

=== Output from 'Analyze F024 parallel execution spec' ===
F024 v3 supports five routing modes: passthrough, single-advised, 
parallel-select, parallel-merge, parallel-evaluate. Current gap: no dependency 
modeling between parallel branches...

--- END PREREQUISITES ---

Your task: Compare the CAID approach with F024 and draft specific amendment 
recommendations. Use the prerequisite outputs above as your source material.
```

### Subtask Result Storage

Node outputs are stored in full in `subtask.result` (TEXT column, no size limit). Summarization happens only at **injection time** — when predecessor context is built for successor prompts. This means:
- The DB always has the complete output (useful for debugging and post-mortems)
- Only the summarized version enters the successor's context window
- If a node produces very large output (>10K tokens), the full result is preserved but the successor sees a focused summary

### Context Size Management

1. **Summarization gate** — If a predecessor output exceeds 2000 tokens, summarize before injection (LLM call via Critic)
2. **Relevance filtering** — Only inject direct dependencies (not transitive — if A→B→C, C gets B's output which already incorporates A)
3. **Token budget** — Total injected context capped at 4000 tokens. If exceeded, summarize all predecessors

---

## Integration: End-to-End Flow

### Phase 1a Flow (DAG, no transactions)

```
1. User sends complex message
   │
2. CognitiveLayer.process_turn() invokes Critic
   │
3. Critic classifies → dependency_type: "phased"
   Critic produces TaskDAG with nodes and edges
   │
4. CognitiveLayer instantiates TaskController(dag, critic, subtask_manager)
   │
5. TaskController.execute() begins
   │
   ├─ 5a. Validate DAG (cycle detection, depth check, node count)
   │
   ├─ 5b. Compute topological waves
   │
   ├─ 5c. Wave 1: create subtasks via SubtaskManager for all Wave 1 nodes
   │       Each subtask gets: node.instructions (no predecessor context for Wave 1)
   │       Workers pick up subtasks via existing dequeue mechanism
   │
   ├─ 5d. Await Wave 1 completion (poll SubtaskManager)
   │       Failed nodes → propagate_failure to dependents
   │
   ├─ 5e. Quality Gate: Critic evaluates Wave 1 outputs
   │       "Are these sufficient for Wave 2 successors?"
   │       If no → stop with partial results (see Partial Result Assembly below)
   │
   ├─ 5f. Wave 2: create subtasks with predecessor context injected into instructions
   │       Workers pick up, execute, complete
   │       ... repeat 5d-5e ...
   │
   ├─ 5g. Final wave completed → all results collected
   │
6. TaskController returns DAGExecutionResult to CognitiveLayer
   │
7. CognitiveLayer passes node results to Main Agent as context
   │
8. Main Agent assembles final response → User
```

### Partial Result Assembly

When a DAG produces partial results (quality gate failure, node failure, budget exceeded), the Main Agent receives:
1. All **completed** node outputs (these are real, useful work)
2. A **status summary** listing what failed and why
3. The TaskController's `DAGPostMortem` object

The Main Agent then:
- Delivers whatever completed results are useful to the user
- Transparently explains what couldn't be completed: _"I completed the research and analysis phases, but the comparison step couldn't run because the research output was insufficient. Here's what I found so far..."_
- Does NOT fabricate results for failed/blocked nodes
- May suggest the user retry with a more specific request

### Phase 1b Flow (competing, with transactions)

```
1. User sends message suitable for competing approaches
   │
2. Critic classifies → routing: "parallel-select", dependency_type: "independent"
   │
3. CognitiveLayer creates TaskDAG with single wave, N competing leaf nodes
   Each node wrapped in CognitiveTransaction
   │
4. TaskController executes single wave — all nodes run in parallel
   │
5. All nodes complete → Critic evaluates outputs → picks winner
   │
6. Winner transaction committed, loser transactions rolled back
   │
7. Winner output → Main Agent → User
```

### Mixed Flow (1a DAG + 1b competing in same request)

```
Wave 1: [Research A] [Research B]     ← 1a pattern (real work, no transactions)
         ↓              ↓
Wave 2: [Compare using A+B results]   ← 1a pattern (real work)
         ↓
Wave 3: [Write report v1] [Write report v2]  ← 1b pattern (competing, transactions)
         ↓
         Critic picks winner → commit winner, rollback loser
```

---

### User Message During DAG Execution

**Policy:** Queue and respond after DAG completes.

If the user sends a new message while a DAG is executing:
1. The message is queued (not processed immediately)
2. The current DAG continues to completion (or partial result)
3. After the DAG result is assembled and delivered, the queued message is processed as a new turn
4. If the user message is clearly an **abort signal** ("stop", "cancel", "nevermind"), the TaskController cancels all pending/running nodes and returns partial results immediately

**Rationale:** Processing a new message mid-DAG would require either (a) injecting it into running subtasks (complex, risky) or (b) running it in parallel with the DAG (confusing for the user). Queuing is simple, predictable, and matches how subtasks work today.

---

## Hard Constraints

### Phase 1a Constraints
1. **Max 6 nodes per DAG.**
2. **Max 3 sequential waves.**
3. **Max 3 parallel nodes per wave.**
4. **No dynamic DAG updates.** Graph is static once built.
5. **Predecessor context injection is the ONLY cross-wave communication.** No shared memory, no file artifacts.
6. **DAG validation before execution.** Invalid DAGs fall back to single-advised execution.
7. **All node side effects commit on DAG success.** No transactions, no rollback — every node does real work.
8. **Task Controller makes zero LLM calls.** Intelligence delegated to Critic.

### Phase 1b Additional Constraints (inherited from F024 main spec)
9. **Max 3 competing instances per node.**
10. **bash blocked in competing instances.**
11. **spawn/schedule blocked in competing instances.**
12. **Transaction rollback must be clean.** No orphaned facts, no ghost decisions.
13. **Competing instances are mutually isolated.** No cross-instance communication.

---

---

## Observability & Logging

The TaskController is a stateful async component — failures must be debuggable after the fact. All events are logged via standard Python logging (`nous.cognitive.task_controller`).

### Required Log Events

| Event | Level | Data | When |
|---|---|---|---|
| `dag.created` | INFO | `dag_id`, node count, wave count, original request (truncated) | DAG validated and accepted |
| `dag.validation_failed` | WARNING | `dag_id`, list of issues | DAG rejected by `validate()` |
| `wave.started` | INFO | `dag_id`, wave index, node IDs in wave | Wave execution begins |
| `wave.completed` | INFO | `dag_id`, wave index, duration, tokens used, pass/fail per node | All nodes in wave resolved |
| `node.spawned` | DEBUG | `dag_id`, node ID, subtask ID, frame, injected context size | Subtask created for node |
| `node.completed` | INFO | `dag_id`, node ID, duration, tokens used, result size | Node subtask finished successfully |
| `node.failed` | WARNING | `dag_id`, node ID, error message, retry count | Node subtask failed |
| `node.blocked` | INFO | `dag_id`, node ID, blocked by (failed predecessor ID) | Failure propagated to dependent |
| `node.timeout` | WARNING | `dag_id`, node ID, elapsed ms, timeout ms | Node exceeded timeout |
| `quality_gate.passed` | INFO | `dag_id`, wave index, Critic rationale | Quality gate approved wave outputs |
| `quality_gate.failed` | WARNING | `dag_id`, wave index, Critic rationale, action taken | Quality gate rejected wave outputs |
| `budget.warning` | WARNING | `dag_id`, tokens consumed, token budget, percentage | Budget >80% consumed |
| `budget.exceeded` | ERROR | `dag_id`, tokens consumed, token budget | Budget exceeded, execution stopped |
| `dag.completed` | INFO | `dag_id`, total duration, total tokens, waves completed, nodes completed/failed/blocked | DAG execution finished |
| `dag.partial` | WARNING | `dag_id`, completed nodes, failed nodes, blocked nodes, reason | DAG finished with partial results |

### Structured Log Format

All log entries include `dag_id` as a correlation key for filtering. Example:

```python
logger.info(
    "dag.wave.completed",
    extra={
        "dag_id": self.dag.dag_id,
        "wave_index": wave_idx,
        "duration_ms": wave_duration,
        "tokens_used": wave_tokens,
        "nodes": {nid: node.status for nid in wave_node_ids},
    }
)
```

### Dashboard Integration

The Memory Dashboard (F021) should display:
- Active DAG executions with real-time progress (via `get_progress()`)
- Historical DAG execution log — searchable by `dag_id`, date, status
- Per-DAG node timeline visualization (which nodes ran when, how long, pass/fail)
- Aggregate stats: avg nodes per DAG, avg waves, failure rate, quality gate rejection rate

### Post-Mortem Support

On DAG failure or partial result, the TaskController produces a `DAGPostMortem` object:

```python
@dataclass
class DAGPostMortem:
    dag_id: str
    status: str  # "partial_result" | "budget_exceeded" | "validation_failed"
    completed_nodes: list[str]
    failed_nodes: list[str]  # with error messages
    blocked_nodes: list[str]  # with which predecessor caused the block
    quality_gate_results: list[dict]  # per-wave gate outcomes
    total_tokens: int
    total_duration_ms: int
    recommendation: str  # "retry with simpler decomposition" | "increase budget" | etc.
```

This is stored alongside the DAG execution result and available for debugging.

---

## Phase 2+ Extensions (Future — Not in Scope)

### Dynamic DAG Updates (from DynTaskMAS)
- Mid-execution graph modifications: G(t+1) = U(G(t), Δ(t))
- Prune nodes if task simpler than expected, add nodes if new sub-problems emerge
- Requires Task Controller to support graph mutation during execution

### Completion-Triggered Unblocking (from DRAMA)
- Unblock individual successors when their specific predecessors complete (not full wave)
- More efficient for unbalanced DAGs
- Task Controller tracks per-node readiness rather than wave-level batching

### Worker Reallocation (from DRAMA)
- Freed capacity reassigned to help other nodes
- Agent dropout recovery: failed node's work taken over by another agent
- Task Controller becomes a resource scheduler

### Retry with Adjusted Prompts
- Quality gate fails → Critic produces adjusted instructions → retry wave
- Max 1 retry per wave to prevent infinite loops

### Learned Decomposition (from GAP)
- Train Critic to produce better DAGs via SFT + RL
- Reward signal: execution efficiency, output quality, cost

---

## Implementation Plan

### Phase 1a — Files to Create
- `nous/cognitive/task_dag.py` — `TaskNode`, `TaskDAG`, `DAGExecutionResult`, validation logic
- `nous/cognitive/task_controller.py` — `TaskController`, `ControllerState`, wave execution, failure propagation, quality gates, budget tracking, progress reporting

### Phase 1a — Files to Modify
- `nous/storage/models.py` — Add `dag_id`, `wave_number`, `predecessor_ids` to Subtask model
- `nous/heart/subtasks.py` — Add `get_completed_for_dag()`, `get_pending_for_dag()` queries
- `nous/cognitive/critic.py` — Add `dependency_type` and `task_graph` to output schema. Add `evaluate_wave_quality()` method.
- `nous/cognitive/critic_schemas.py` — Add `TaskGraphSchema`, `DependencyType` enum
- `nous/cognitive/layer.py` — When Critic returns `dependency_type: "phased"`, instantiate TaskController, execute DAG, pass results to Main Agent

### Phase 1a — Estimated Effort
- TaskDAG data structures + validation: ~3 hours
- TaskController core (state machine, wave loop, failure propagation): ~4 hours
- Subtask model additions + queries: ~1 hour
- Quality gate integration with Critic: ~2 hours
- Predecessor context injection + summarization: ~2 hours
- Critic prompt amendment + schema: ~2 hours
- CognitiveLayer integration: ~2 hours
- **Total: ~16 hours**

### Phase 1b — Files to Create
- `nous/cognitive/transaction.py` — `CognitiveTransaction`, journal buffer, commit/rollback
- `nous/cognitive/transaction_interceptor.py` — Tool wrapping for isolation in competing mode

### Phase 1b — Files to Modify
- `nous/cognitive/task_controller.py` — Add competing mode support, winner selection flow
- `nous/cognitive/critic.py` — Add winner selection evaluation prompt
- `nous/cognitive/layer.py` — Wire up transaction lifecycle for competing instances
- `nous/handlers/subtask_worker.py` — Transaction-aware execution for competing mode

### Phase 1b — Estimated Effort
- CognitiveTransaction + journal: ~4 hours
- TransactionInterceptor: ~3 hours
- TaskController competing mode: ~2 hours
- Winner selection in Critic: ~1 hour
- CognitiveLayer transaction wiring: ~2 hours
- **Total: ~12 hours**

### Combined Phase 1 Total: ~28 hours

---

## Success Criteria

### Phase 1a
1. Critic produces **structurally valid** DAGs (pass `validate()` — no cycles, within depth/node/parallelism caps) for multi-step tasks >95% of the time. **Semantically useful** decompositions (correct dependencies, appropriate granularity) >80% of the time, measured by human review of a sample
2. Task Controller correctly schedules waves — parallel within, sequential between
3. Wave-scheduled execution produces better results than monolithic single-turn on complex tasks (human judged, >60%)
4. Predecessor context injection preserves information from upstream nodes
5. DAG validation catches all malformed graphs before execution
6. Quality gates catch insufficient outputs before wasting downstream compute (>50% of preventable failures)
7. Total DAG execution latency < sum of all node latencies × 0.7 (parallelism speedup)
8. Budget enforcement prevents runaway cost
9. Progress reporting accurately reflects real-time DAG state
10. Existing standalone subtasks continue working unchanged (backward compatibility)

### Phase 1b (additional)
11. Transaction isolation verified — no leaked side effects from discarded instances
12. Read-through works — competing instances can recall their own pending facts
13. Parallel competing route produces better responses than single-frame on >50% of suitable tasks
14. Winner selection agrees with human preference >70% of the time
15. Total cost for competing execution < 3× single turn

---

## References

1. Geng & Neubig. "Effective Strategies for Asynchronous Software Engineering Agents." CMU LTI, arXiv:2603.21489, March 2026.
2. Yu, Ding, Sato. "DynTaskMAS: Dynamic Task Graph-driven Framework for Asynchronous and Parallel LLM-based Multi-Agent Systems." ICAPS 2025, arXiv:2503.07675v2.
3. GAP: Learning dependency analysis via SFT+RL. Tsinghua/CMU.
4. DRAMA: Event-driven multi-agent reallocation. arXiv:2508.04332.
5. Chen et al. "Atomix: Transactional Isolation for AI Agent Interactions." arXiv:2503.18890, March 2025.
6. Zhang et al. "M1-Parallel: Optimizing Sequential Multi-Step Tasks with Parallel LLM Agents." Microsoft, ICML 2025, arXiv:2507.08944.
