# F038 — Unified DAG Orchestration

> **Status:** Draft v1
> **Priority:** P1
> **Author:** Nous + Tim
> **Date:** 2026-04-09
> **Depends on:** F009 (Subtasks), F034.5 (Dynamic Checks), F024 (DAG Decomposition spec)
> **Research basis:** DynTaskMAS (ICAPS 2025), GAP (Tsinghua/CMU), IBM ACG Survey (arXiv:2603.22386), DRAMA (arXiv:2508.04332), CORPGEN (Microsoft, 2026)

---

## Problem Statement

Nous currently has **three independent execution primitives** with no coordination layer:

1. **Subtasks** (F009) — Fire-and-forget background execution via worker pool
2. **Dynamic Checks** (F034.5) — Polling/monitoring agents with self-disable and callbacks
3. **Schedules** — Time-based triggers that create subtasks

These are connected today through **ad-hoc chaining**: a dynamic check monitors a job, spawns a follow-up check on completion, which monitors the next job, etc. This pattern emerged organically and has proven useful but fragile:

### Observed Problems (Real Examples)

1. **Orphaned monitors** — `pr-fixes-monitor` spawned `pr281-ci-fix-monitor` before being disabled. Disabling the parent didn't cascade to the child. Tim had to manually find and disable the orphan.

2. **No cascade operations** — When a Claude Code job fails, its monitor check continues polling a dead job. Dependent checks that were spawned keep running. There's no way to say "cancel everything related to this pipeline."

3. **Invisible dependency chains** — The relationship between `ci-schema-sync-monitor` → `pr-fixes-monitor` → `pr281-ci-fix-monitor` exists only in human knowledge. The system has no record that these are part of the same workflow.

4. **Brittle state handoff** — Checks pass context to follow-up checks via prompt text. If the chain gets complex, context degrades at each hop.

5. **No parallel branches** — Current chaining is strictly linear (A → B → C). There's no way to express "A completes, then B and C run in parallel, then D runs after both finish."

6. **Manual teardown** — Tim confirmed: "When a Claude Code job completes, associated monitors and censors are NOT automatically torn down." This is a known gap filed as a fact.

### Root Cause

The three primitives evolved independently. Each has its own lifecycle, storage, and execution model. There is no shared concept of "these things belong together and depend on each other."

---

## Solution: DAG Orchestration Layer

A **lightweight coordination layer** that sits on top of existing primitives. Both dynamic checks (sensors) and subtasks (actuators) keep their distinct execution models, but a new **ExecutionDAG** tracks dependencies and manages lifecycle.

### Design Philosophy

- **Additive, not replacement** — Subtasks, checks, and schedules continue working exactly as today for standalone use
- **DAG is optional** — Only created when there are actual dependencies between execution units
- **Primitives stay primitive** — Workers and checks don't know about DAGs. The DAG layer coordinates from above
- **Sensors and actuators unified** — A DAG node can be a check (polling/monitoring) OR a subtask (doing work). The DAG doesn't care
- **Checks are first-class** — Unlike F024 which only considered subtasks as nodes, F038 treats dynamic checks as equal DAG citizens

### Relationship to F024

F024 DAG Decomposition describes Critic-driven task decomposition for complex user requests. F038 is the **execution infrastructure** that F024 needs, but broader:

- **F024** answers: "How does the Critic break a complex request into a dependency graph?"
- **F038** answers: "How does the system execute, track, and manage any graph of dependent work?"

F038 subsumes F024's TaskController concept and generalizes it to handle the monitor-chain pattern (not just Critic-decomposed subtasks).

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                  DAG Orchestrator                     │
│                                                       │
│  ┌───────────┐  ┌───────────┐  ┌──────────────────┐ │
│  │ DAG Store │  │ Lifecycle │  │ Event Processor   │ │
│  │ (DB)      │  │ Manager   │  │ (completion/fail) │ │
│  └───────────┘  └───────────┘  └──────────────────┘ │
└──────────┬──────────────┬──────────────┬─────────────┘
           │              │              │
     ┌─────▼─────┐ ┌─────▼─────┐ ┌─────▼─────┐
     │  Subtask   │ │  Dynamic   │ │  Schedule  │
     │  Workers   │ │  Checks    │ │  Triggers  │
     └───────────┘ └───────────┘ └───────────┘
```

### Core Concept: DAG Node Types

A DAG node wraps an existing execution primitive:

```python
class DAGNodeType(str, Enum):
    SUBTASK = "subtask"       # Fire-and-forget work (existing SubtaskManager)
    CHECK = "check"           # Polling/monitoring (existing DynamicCheckLoader)  
    GATE = "gate"             # Quality evaluation before proceeding (Critic call)
    CALLBACK = "callback"     # Notification/action on completion
```

**SUBTASK nodes** — Do actual work. Created via SubtaskManager, executed by worker pool. Complete when the subtask finishes.

**CHECK nodes** — Monitor external state. Created via DynamicCheckLoader. Complete when the check self-disables (success condition met) or a configurable completion condition is triggered.

**GATE nodes** — Quality checkpoints between waves. Evaluate whether predecessor outputs are sufficient. Implemented as a Critic evaluation call. No external execution — runs inline in the orchestrator.

**CALLBACK nodes** — Terminal actions (notify Tim, create PR, send summary). Leaf nodes that fire after all predecessors complete.

---

## Data Model

### ExecutionDAG (new table: `nous_system.execution_dags`)

```python
class ExecutionDAG(Base):
    __tablename__ = "execution_dags"
    __table_args__ = {"schema": "nous_system"}
    
    id: UUID                        # DAG identifier
    agent_id: str                   # Always "nous"
    name: str                       # Human-readable name (e.g., "pr-281-fix-pipeline")
    description: str                # What this DAG accomplishes
    status: str                     # "pending" | "running" | "completed" | "failed" | "cancelled" | "partial"
    source: str                     # "conversation" | "critic" | "heartbeat" | "schedule"
    
    # Metadata
    original_request: str | None    # User message or trigger that created this DAG
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    
    # Budget
    token_budget: int | None        # Max tokens across all nodes
    tokens_consumed: int            # Running total
    
    # Results
    result_summary: str | None      # Final assembled result
    postmortem: dict | None         # JSONB — failure analysis if partial/failed
    
    # Relationships
    nodes: list["DAGNode"]          # All nodes in this DAG
```

### DAGNode (new table: `nous_system.dag_nodes`)

```python
class DAGNode(Base):
    __tablename__ = "dag_nodes"
    __table_args__ = {"schema": "nous_system"}
    
    id: UUID                        # Node identifier
    dag_id: UUID                    # FK → execution_dags.id
    
    # Identity
    name: str                       # Short name (e.g., "monitor-ci", "fix-schema")
    description: str                # What this node does
    node_type: str                  # "subtask" | "check" | "gate" | "callback"
    
    # Execution reference — links to the primitive
    subtask_id: UUID | None         # FK → heart.subtasks.id (if node_type=subtask)
    check_name: str | None          # FK → nous_system.dynamic_checks.name (if node_type=check)
    
    # DAG position
    wave: int                       # Computed wave number (0-indexed)
    
    # State
    status: str                     # "pending" | "ready" | "running" | "completed" | "failed" | "blocked" | "cancelled"
    
    # Configuration
    instructions: str | None        # Task prompt (for subtask/check creation)
    tools: list[str] | None         # Tool filter
    frame_type: str | None          # Cognitive frame
    model: str | None               # Model override
    timeout_seconds: int            # Per-node timeout
    
    # Completion condition (for CHECK nodes)
    completion_condition: str | None # "self_disable" | "finding_match:<pattern>" | "max_runs:<n>"
    
    # Results
    result: str | None              # Output text
    error: str | None               # Error message if failed
    tokens_used: int                # Token consumption
    started_at: datetime | None
    completed_at: datetime | None
    
    # Context injection
    injected_context: str | None    # Predecessor context that was injected
```

### DAGEdge (new table: `nous_system.dag_edges`)

```python
class DAGEdge(Base):
    __tablename__ = "dag_edges"
    __table_args__ = {"schema": "nous_system"}
    
    id: UUID
    dag_id: UUID                    # FK → execution_dags.id
    from_node_id: UUID              # FK → dag_nodes.id
    to_node_id: UUID                # FK → dag_nodes.id
    edge_type: str                  # "dependency" | "cancel_cascade" | "context_flow"
```

### Edge Types

- **dependency** — `to_node` cannot start until `from_node` completes successfully
- **cancel_cascade** — If `from_node` is cancelled/failed, `to_node` is also cancelled (solves orphan problem)
- **context_flow** — `from_node`'s result is injected into `to_node`'s instructions (implies dependency)

---

## DAG Orchestrator

### Core Loop

The orchestrator runs as part of the heartbeat tick (piggybacks on existing infrastructure, no new background loop):

```python
class DAGOrchestrator:
    """Manages lifecycle of all active ExecutionDAGs.
    
    Runs on each heartbeat tick. Checks node completion status,
    advances ready nodes, propagates failures, and completes DAGs.
    Not an LLM agent — purely mechanical state management.
    """
    
    async def tick(self) -> None:
        """Called each heartbeat cycle. Advances all active DAGs."""
        active_dags = await self._store.get_active_dags()
        
        for dag in active_dags:
            await self._advance_dag(dag)
    
    async def _advance_dag(self, dag: ExecutionDAG) -> None:
        """Process one DAG: check completions, advance ready nodes, handle failures."""
        
        # 1. Sync node statuses from underlying primitives
        await self._sync_node_statuses(dag)
        
        # 2. Propagate failures (block dependents of failed nodes)
        self._propagate_failures(dag)
        
        # 3. Find newly ready nodes (all predecessors completed)
        ready_nodes = self._find_ready_nodes(dag)
        
        # 4. Launch ready nodes via their respective primitives
        for node in ready_nodes:
            await self._launch_node(node)
        
        # 5. Check DAG completion
        if self._is_dag_complete(dag):
            await self._finalize_dag(dag)
    
    async def _sync_node_statuses(self, dag: ExecutionDAG) -> None:
        """Pull completion status from subtasks and checks back into DAG nodes."""
        for node in dag.nodes:
            if node.status != "running":
                continue
            
            if node.node_type == "subtask" and node.subtask_id:
                subtask = await self._subtask_mgr.get(node.subtask_id)
                if subtask.status == "completed":
                    node.status = "completed"
                    node.result = subtask.result
                    node.completed_at = subtask.completed_at
                elif subtask.status == "failed":
                    node.status = "failed"
                    node.error = subtask.error
            
            elif node.node_type == "check" and node.check_name:
                check_info = await self._check_loader.get_check_info(node.check_name)
                if not check_info.enabled:  # Self-disabled = completed
                    node.status = "completed"
                    node.result = check_info.last_result
                    node.completed_at = datetime.now(UTC)
    
    async def _launch_node(self, node: DAGNode) -> None:
        """Create the underlying primitive for a ready node."""
        
        # Build predecessor context
        context = await self._build_predecessor_context(node)
        augmented_instructions = context + (node.instructions or "")
        
        if node.node_type == "subtask":
            subtask = await self._subtask_mgr.create(
                task=augmented_instructions,
                frame_type=node.frame_type,
                model=node.model,
                timeout=node.timeout_seconds,
                metadata={"dag_id": str(node.dag_id), "dag_node_id": str(node.id)},
            )
            node.subtask_id = subtask.id
            node.status = "running"
        
        elif node.node_type == "check":
            check = await self._check_loader.create(
                name=f"dag-{node.dag_id.hex[:8]}-{node.name}",
                description=node.description,
                prompt=augmented_instructions,
                tools=node.tools or ["bash"],
                interval_seconds=300,
                timeout_seconds=node.timeout_seconds,
                metadata={"dag_id": str(node.dag_id), "dag_node_id": str(node.id)},
            )
            node.check_name = check.name
            node.status = "running"
        
        elif node.node_type == "gate":
            # Gates run inline — evaluate predecessor quality
            gate_passed = await self._evaluate_gate(node)
            node.status = "completed" if gate_passed else "failed"
            node.result = "gate_passed" if gate_passed else "gate_failed"
            node.completed_at = datetime.now(UTC)
        
        elif node.node_type == "callback":
            # Callbacks are simple notifications/actions
            await self._execute_callback(node, augmented_instructions)
            node.status = "completed"
            node.completed_at = datetime.now(UTC)
        
        node.started_at = datetime.now(UTC)
```

### Cascade Operations

The key improvement over current ad-hoc chaining:

```python
async def cancel_dag(self, dag_id: UUID, reason: str = "user_cancelled") -> None:
    """Cancel an entire DAG and all its running primitives."""
    dag = await self._store.get_dag(dag_id)
    dag.status = "cancelled"
    
    for node in dag.nodes:
        if node.status in ("pending", "ready", "running"):
            node.status = "cancelled"
            
            # Cancel the underlying primitive
            if node.node_type == "subtask" and node.subtask_id:
                await self._subtask_mgr.cancel(node.subtask_id)
            elif node.node_type == "check" and node.check_name:
                await self._check_loader.manage(
                    action="disable", name=node.check_name
                )
    
    await self._store.save_dag(dag)

async def cascade_failure(self, node: DAGNode) -> None:
    """When a node fails, block all transitive dependents."""
    dependents = self._get_dependents(node)
    for dep in dependents:
        if dep.status in ("pending", "ready"):
            dep.status = "blocked"
            dep.error = f"Blocked: predecessor '{node.name}' failed"
            # Recurse to block transitive dependents
            await self.cascade_failure(dep)
```

---

## Tool Interface

### New Tools

**`dag_create`** — Create a new DAG with nodes and edges

```python
dag_create(
    name="pr-281-fix-pipeline",
    description="Fix PR #281 CI failures end-to-end",
    nodes=[
        {"name": "fix-schema", "type": "subtask", "instructions": "...", "tools": ["bash"]},
        {"name": "monitor-ci", "type": "check", "instructions": "...", "completion_condition": "self_disable"},
        {"name": "notify", "type": "callback", "instructions": "Notify Tim that PR #281 is green"},
    ],
    edges=[
        {"from": "fix-schema", "to": "monitor-ci", "type": "dependency"},
        {"from": "monitor-ci", "to": "notify", "type": "dependency"},
        {"from": "fix-schema", "to": "notify", "type": "cancel_cascade"},
    ]
)
```

**`dag_manage`** — List, cancel, inspect DAGs

```python
dag_manage(action="list")                    # List active DAGs
dag_manage(action="status", dag_id="...")    # Detailed status with node states
dag_manage(action="cancel", dag_id="...")    # Cancel entire DAG + all primitives
dag_manage(action="retry_node", dag_id="...", node_name="fix-schema")  # Retry a failed node
```

### Existing Tools — Enhanced

**`spawn_task`** — Gains optional `dag_id` and `depends_on` parameters:

```python
# Standalone (unchanged)
spawn_task(task="Do something")

# As part of a DAG
spawn_task(task="Do something", dag_id="...", depends_on=["other-node"])
```

**`heartbeat_check_create`** — Gains optional `dag_id` and `depends_on` parameters:

```python
# Standalone (unchanged)
heartbeat_check_create(name="my-check", prompt="...")

# As part of a DAG
heartbeat_check_create(name="my-check", prompt="...", dag_id="...", depends_on=["fix-job"])
```

### Backward Compatibility

All existing tools work exactly as before when `dag_id` is not specified. DAGs are opt-in.

---

## Common Patterns

### Pattern 1: Monitor Chain (replaces current ad-hoc chaining)

**Before (current — fragile):**
```
heartbeat_check_create("pr-fixes-monitor", on_complete_prompt="create next check...")
  → pr-fixes-monitor spawns ci-fix-monitor via on_complete callback
    → ci-fix-monitor spawns pr281-ci-fix-monitor via on_complete callback
      → orphans everywhere
```

**After (DAG-managed):**
```python
dag_create(
    name="pr-281-pipeline",
    nodes=[
        {"name": "fix-lint", "type": "subtask", "instructions": "Run lint fixes..."},
        {"name": "monitor-lint-ci", "type": "check", "instructions": "Watch CI for lint fix...",
         "completion_condition": "self_disable"},
        {"name": "fix-schema", "type": "subtask", "instructions": "Fix schema drift..."},
        {"name": "monitor-schema-ci", "type": "check", "instructions": "Watch CI for schema fix...",
         "completion_condition": "self_disable"},
        {"name": "notify-complete", "type": "callback", "instructions": "Tell Tim all green"},
    ],
    edges=[
        {"from": "fix-lint", "to": "monitor-lint-ci"},
        {"from": "monitor-lint-ci", "to": "fix-schema"},
        {"from": "fix-schema", "to": "monitor-schema-ci"},
        {"from": "monitor-schema-ci", "to": "notify-complete"},
    ]
)
```

Benefits:
- Cancel "pr-281-pipeline" → everything stops
- Any node fails → dependents blocked automatically
- Full visibility: `dag_manage(action="status")` shows the entire chain
- No orphans possible

### Pattern 2: Parallel Branches with Join

```python
dag_create(
    name="research-and-implement",
    nodes=[
        {"name": "research-papers", "type": "subtask", "frame": "research"},
        {"name": "analyze-codebase", "type": "subtask", "frame": "task"},
        {"name": "quality-gate", "type": "gate"},
        {"name": "implement", "type": "subtask", "frame": "task"},
    ],
    edges=[
        {"from": "research-papers", "to": "quality-gate"},
        {"from": "analyze-codebase", "to": "quality-gate"},
        {"from": "quality-gate", "to": "implement"},
    ]
)
```

Wave 0: research-papers ∥ analyze-codebase (parallel)
Wave 1: quality-gate (evaluates both outputs)
Wave 2: implement (gets context from both predecessors)

### Pattern 3: Claude Code Job Pipeline

The most common current use case — launching a Claude Code job and monitoring it:

```python
dag_create(
    name="feature-implementation",
    nodes=[
        {"name": "code-job", "type": "subtask",
         "instructions": "Launch Claude Code job: implement F038..."},
        {"name": "monitor-job", "type": "check",
         "instructions": "Monitor Claude Code job {job_id}. Report on completion.",
         "completion_condition": "self_disable"},
        {"name": "review-pr", "type": "subtask",
         "instructions": "Review the PR created by the job. Check for issues."},
        {"name": "notify", "type": "callback",
         "instructions": "Send Tim the PR link and review summary"},
    ],
    edges=[
        {"from": "code-job", "to": "monitor-job", "type": "context_flow"},
        {"from": "monitor-job", "to": "review-pr", "type": "context_flow"},
        {"from": "review-pr", "to": "notify", "type": "dependency"},
    ]
)
```

### Pattern 4: Critic-Driven Decomposition (F024 Integration)

When the Critic produces a `dependency_type: "phased"` result, the cognitive layer automatically creates a DAG:

```python
# In cognitive/layer.py — after Critic decomposition
if critic_result.dependency_type == "phased":
    dag = await self._dag_orchestrator.create_from_critic(
        critic_result.task_graph,
        original_request=user_message,
    )
    # DAG orchestrator takes over execution
    # Main agent assembles results when DAG completes
```

---

## Constraints

### Hard Limits
1. **Max 10 nodes per DAG** — Prevents over-decomposition
2. **Max 4 waves** — Keeps sequential depth manageable
3. **Max 4 parallel nodes per wave** — Respects subtask worker pool capacity
4. **Max 5 active DAGs** — Prevents resource exhaustion
5. **No cycles** — Validated at creation time via topological sort
6. **No nested DAGs** — A DAG node cannot itself be a DAG (Phase 2 consideration)

### Timing
- DAG orchestrator tick runs every heartbeat cycle (default: 5 minutes for checks)
- For subtask nodes, completion is also detected by the subtask worker pool's event system
- CHECK nodes are polled on the heartbeat cycle for self-disable detection

### Budget
- Each DAG has an optional token budget
- Budget is tracked across all nodes (subtask tokens + check tokens)
- When budget >80% consumed: warning logged
- When budget exceeded: pending/ready nodes cancelled, DAG status → "partial"

---

## Migration Path

### Phase 1: Foundation (MVP)

**New files:**
- `nous/dag/models.py` — ExecutionDAG, DAGNode, DAGEdge SQLAlchemy models
- `nous/dag/store.py` — CRUD operations for DAGs
- `nous/dag/orchestrator.py` — DAGOrchestrator with tick(), advance, cascade
- `nous/api/tools_dag.py` — dag_create, dag_manage tool implementations

**Modified files:**
- `nous/heartbeat/runner.py` — Call `dag_orchestrator.tick()` on each heartbeat cycle
- `nous/api/tools.py` — Register new dag_create, dag_manage tools
- `nous/storage/models.py` — Add new models
- Alembic migration for new tables

**What works after Phase 1:**
- Create DAGs with subtask and check nodes
- Automatic dependency resolution and wave scheduling
- Cascade cancel/failure propagation
- Status visibility via dag_manage
- Backward compatible — standalone subtasks/checks unchanged

**Estimated effort:** ~12-16 hours

### Phase 2: Critic Integration

**Modified files:**
- `nous/cognitive/layer.py` — Auto-create DAG from Critic's phased decomposition
- `nous/cognitive/critic.py` — Add task_graph output to Critic prompt

**What works after Phase 2:**
- Critic can decompose complex requests into DAGs automatically
- Quality gate nodes evaluated by Critic
- Predecessor context injection for phased execution
- F024's TaskController concept fully realized

**Estimated effort:** ~8-10 hours

### Phase 3: Dashboard & Observability

**New files:**
- `static/dashboard/js/dag.js` — DAG dashboard tab (D3 node graph + status panels)
- Query function `get_dag_dashboard_data()` in `nous/api/dashboard_queries.py`

**Modified files:**
- `nous/api/rest.py` — `GET /dashboard/dag` endpoint + route registration
- `static/dashboard/index.html` — Nav link + view container + script include
- `static/dashboard/css/dashboard.css` — DAG-specific styles (node colors, edge paths)

**Dashboard Sections:**

1. **Status Banner** — Active DAGs count, total nodes running, budget utilization pill
2. **Stat Cards** (4-grid):
   - Active DAGs (with running/pending breakdown)
   - Nodes Completed (24h)
   - Success Rate (completed / total finished DAGs)
   - Avg Completion Time
3. **Active DAG List** — Table with columns: Name, Status (badge), Source, Nodes (progress bar: completed/total), Created, Actions (view graph, cancel)
4. **DAG Detail View** — D3 force-directed node graph:
   - Nodes colored by status: pending (gray), ready (blue), running (amber pulse), completed (green), failed (red), blocked (dark red), cancelled (muted)
   - Nodes shaped by type: circle (subtask), diamond (check), hexagon (gate), triangle (callback)
   - Edges styled by type: solid (dependency), dashed (cancel_cascade), dotted with arrow (context_flow)
   - Wave lanes (horizontal grouping by wave number)
   - Click node → side panel with: name, type, status, instructions preview, result/error, timing, linked primitive ID
5. **DAG History** — Recent completed/failed/cancelled DAGs with expandable post-mortem
6. **Budget Chart** — Doughnut chart showing token consumption vs budget per active DAG

**Auto-refresh:** 15 seconds (DAGs advance on heartbeat ticks, faster refresh than heartbeat's 30s)

**REST Endpoint:**
```
GET /dashboard/dag
Response: {
  "active_dags": [...],       // Currently running DAGs with full node/edge data
  "recent_dags": [...],       // Last 20 completed/failed/cancelled DAGs
  "stats": {
    "active_count": int,
    "nodes_completed_24h": int,
    "success_rate": float,
    "avg_completion_seconds": float
  }
}
```

**Estimated effort:** ~6-8 hours

### Phase 4: Smart Features

- Dynamic DAG updates (add/remove nodes mid-execution, from DynTaskMAS)
- Retry failed nodes with adjusted prompts
- Learning from DAG outcomes (which decompositions work well)
- Parallel competing execution within a DAG (F024 Phase 1b)

---

## What This Replaces vs. Preserves

### Replaces
- **on_complete_prompt/tools** on dynamic checks — DAG edges replace callbacks for multi-step workflows
- **Self-chaining monitor pattern** — DAG manages the chain explicitly
- **Manual teardown** — cascade cancel handles it

### Preserves
- **Standalone subtasks** — `spawn_task("do thing")` still works, no DAG needed
- **Standalone checks** — `heartbeat_check_create("monitor-x")` still works
- **Schedules** — `schedule_task()` still works independently
- **on_complete for simple cases** — Single check with a callback doesn't need a DAG

### Deprecates (Phase 2+)
- **on_complete_prompt/tools** for multi-step chains — Soft deprecation, recommend DAG instead
- **Manual monitor chaining** — Anti-pattern once DAGs are available

---

## Success Criteria

1. **No orphaned monitors** — All nodes in a cancelled/failed DAG are properly torn down
2. **Cascade operations work** — Cancel a DAG → all running subtasks cancelled, all checks disabled
3. **Parallel branches** — Two independent nodes in the same wave execute concurrently
4. **Dependency ordering** — Nodes only start when all predecessors are completed
5. **Context flows** — Predecessor results are injected into successor instructions
6. **Visibility** — `dag_manage(action="status")` shows real-time state of all nodes
7. **Backward compatible** — All existing standalone subtask/check usage unchanged
8. **Budget enforcement** — DAGs respect token budget limits
9. **Critic integration** (Phase 2) — Complex requests automatically become DAGs

---

## Observability

### Log Events (via `nous.dag.orchestrator`)

- `dag.created` — INFO — DAG validated and created
- `dag.advanced` — DEBUG — DAG tick processed, nodes advanced  
- `dag.node.launched` — INFO — Node's primitive created and started
- `dag.node.completed` — INFO — Node finished successfully
- `dag.node.failed` — WARNING — Node failed, cascading to dependents
- `dag.node.blocked` — INFO — Node blocked due to predecessor failure
- `dag.completed` — INFO — All nodes resolved, DAG finished
- `dag.cancelled` — INFO — DAG cancelled (user or cascade)
- `dag.budget.warning` — WARNING — Token budget >80%
- `dag.budget.exceeded` — ERROR — Budget exceeded, pending nodes cancelled

### Metrics

- Active DAGs count
- Nodes per DAG (avg, p95)
- DAG completion rate (completed / total)
- Parallelism efficiency (actual speedup vs. sequential)
- Budget utilization per DAG

---

## References

1. Yu, Ding, Sato. "DynTaskMAS: Dynamic Task Graph-driven Framework." ICAPS 2025.
2. Wu et al. "GAP: Graph-based Agent Planning with Parallel Tool Use." Tsinghua/CMU.
3. Yue et al. "From Static Templates to Dynamic Runtime Graphs." IBM, arXiv:2603.22386, 2026.
4. DRAMA. "Event-driven Multi-Agent Reallocation." arXiv:2508.04332.
5. CORPGEN. "Multi-Horizon Task Environments." Microsoft Research, 2026.
6. Tomašev et al. "Intelligent Delegation." DeepMind, arXiv:2602.11865, 2026.
7. F024 DAG Decomposition & Wave Scheduling spec (internal).
8. F034.5 Dynamic Heartbeat Checks spec (internal).
