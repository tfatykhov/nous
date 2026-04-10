# F038 Unified DAG Orchestration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a DAG orchestration layer that coordinates subtasks and dynamic checks with dependency tracking, cascade cancel/failure, and a real-time dashboard for monitoring DAG execution.

**Architecture:** Three new DB tables (`execution_dags`, `dag_nodes`, `dag_edges`) in `nous_system` schema. A `nous/dag/` module with store (CRUD), orchestrator (tick-based state machine), and schemas. Two new agent tools (`dag_create`, `dag_manage`). Heartbeat runner calls `dag_orchestrator.tick()` each cycle. Dashboard tab with D3 node graph visualization.

**Tech Stack:** Python 3.12+, SQLAlchemy 2.0 async ORM, PostgreSQL 17, Starlette, D3.js for graph, Chart.js for stats, vanilla JS SPA.

---

## File Map

### New Files
| File | Responsibility |
|------|---------------|
| `sql/migrations/032_dag_orchestration.sql` | 3 new tables: `execution_dags`, `dag_nodes`, `dag_edges` |
| `nous/dag/__init__.py` | Package exports |
| `nous/dag/schemas.py` | Pydantic models: `DAGCreateRequest`, `DAGNodeSpec`, `DAGEdgeSpec`, `DAGStatus`, `DAGNodeStatus` |
| `nous/dag/store.py` | `DAGStore` — async CRUD for DAGs/nodes/edges |
| `nous/dag/orchestrator.py` | `DAGOrchestrator` — tick loop, state machine, cascade, launch |
| `tests/test_dag_schemas.py` | Schema validation tests |
| `tests/test_dag_store.py` | Store CRUD integration tests |
| `tests/test_dag_orchestrator.py` | Orchestrator state machine tests |
| `tests/test_dag_tools.py` | Tool dispatch tests |
| `tests/test_dag_dashboard.py` | Dashboard endpoint tests |
| `static/dashboard/js/dag.js` | Dashboard tab: DAG list, D3 graph, stats |

### Modified Files
| File | Changes |
|------|---------|
| `nous/storage/models.py` | Add `ExecutionDAG`, `DAGNode`, `DAGEdge` ORM models |
| `nous/api/tools.py` | Add `register_dag_tools()` function |
| `nous/api/rest.py` | Add `GET /dashboard/dag` endpoint + route |
| `nous/api/dashboard_queries.py` | Add `get_dag_dashboard_data()` query function |
| `nous/heartbeat/runner.py` | Call `dag_orchestrator.tick()` in `_loop()` |
| `nous/main.py` | Wire DAGStore + DAGOrchestrator + register tools |
| `nous/config.py` | Add `NOUS_DAG_ENABLED` setting |
| `static/dashboard/index.html` | Add nav link + view container + script tag |
| `static/dashboard/css/dashboard.css` | Add DAG-specific styles |

---

## Task 1: Database Migration

**Files:**
- Create: `sql/migrations/032_dag_orchestration.sql`

- [ ] **Step 1: Write the migration SQL**

```sql
-- F038: Unified DAG Orchestration
-- Three tables for DAG lifecycle management

BEGIN;

-- 1. ExecutionDAG — top-level orchestration unit
CREATE TABLE IF NOT EXISTS nous_system.execution_dags (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id VARCHAR(100) NOT NULL,
    name VARCHAR(200) NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status VARCHAR(20) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled', 'partial')),
    source VARCHAR(30) NOT NULL DEFAULT 'conversation'
        CHECK (source IN ('conversation', 'critic', 'heartbeat', 'schedule')),
    original_request TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    token_budget INT,
    tokens_consumed INT NOT NULL DEFAULT 0,
    result_summary TEXT,
    postmortem JSONB,
    CONSTRAINT chk_dag_budget CHECK (token_budget IS NULL OR token_budget > 0)
);

CREATE INDEX idx_dags_agent_status ON nous_system.execution_dags (agent_id, status);
CREATE INDEX idx_dags_created ON nous_system.execution_dags (created_at DESC);

-- 2. DAGNode — individual execution unit within a DAG
CREATE TABLE IF NOT EXISTS nous_system.dag_nodes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dag_id UUID NOT NULL REFERENCES nous_system.execution_dags(id) ON DELETE CASCADE,
    name VARCHAR(100) NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    node_type VARCHAR(20) NOT NULL
        CHECK (node_type IN ('subtask', 'check', 'gate', 'callback')),
    subtask_id UUID,
    check_name VARCHAR(200),
    wave INT NOT NULL DEFAULT 0,
    status VARCHAR(20) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'ready', 'running', 'completed', 'failed', 'blocked', 'cancelled')),
    instructions TEXT,
    tools JSONB,
    frame_type VARCHAR(30),
    model VARCHAR(100),
    timeout_seconds INT NOT NULL DEFAULT 120,
    completion_condition VARCHAR(100),
    result TEXT,
    error TEXT,
    tokens_used INT NOT NULL DEFAULT 0,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    injected_context TEXT,
    CONSTRAINT uq_dag_node_name UNIQUE (dag_id, name)
);

CREATE INDEX idx_dag_nodes_dag ON nous_system.dag_nodes (dag_id);
CREATE INDEX idx_dag_nodes_status ON nous_system.dag_nodes (dag_id, status);
CREATE INDEX idx_dag_nodes_subtask ON nous_system.dag_nodes (subtask_id) WHERE subtask_id IS NOT NULL;

-- 3. DAGEdge — dependency/cascade/context relationships
CREATE TABLE IF NOT EXISTS nous_system.dag_edges (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dag_id UUID NOT NULL REFERENCES nous_system.execution_dags(id) ON DELETE CASCADE,
    from_node_id UUID NOT NULL REFERENCES nous_system.dag_nodes(id) ON DELETE CASCADE,
    to_node_id UUID NOT NULL REFERENCES nous_system.dag_nodes(id) ON DELETE CASCADE,
    edge_type VARCHAR(20) NOT NULL DEFAULT 'dependency'
        CHECK (edge_type IN ('dependency', 'cancel_cascade', 'context_flow')),
    CONSTRAINT uq_dag_edge UNIQUE (dag_id, from_node_id, to_node_id, edge_type)
);

CREATE INDEX idx_dag_edges_dag ON nous_system.dag_edges (dag_id);
CREATE INDEX idx_dag_edges_to ON nous_system.dag_edges (to_node_id);

-- Migration record
INSERT INTO nous_system.migrations (version, description)
VALUES (32, 'F038: DAG orchestration tables')
ON CONFLICT DO NOTHING;

COMMIT;
```

- [ ] **Step 2: Verify migration applies cleanly**

Run: `docker exec -i nous-postgres-1 psql -U nous -d nous -f /dev/stdin < sql/migrations/032_dag_orchestration.sql`
Expected: `COMMIT` with no errors.

---

## Task 2: ORM Models

**Files:**
- Modify: `nous/storage/models.py` (append after DynamicCheckModel)

- [ ] **Step 1: Write test for model instantiation**

Create `tests/test_dag_schemas.py`:

```python
"""Tests for F038 DAG ORM models and Pydantic schemas."""

from uuid import uuid4

import pytest

from nous.storage.models import ExecutionDAG, DAGNode, DAGEdge


class TestDAGModels:
    def test_execution_dag_defaults(self):
        dag = ExecutionDAG(agent_id="nous", name="test-dag")
        assert dag.status == "pending"
        assert dag.source == "conversation"
        assert dag.tokens_consumed == 0

    def test_dag_node_defaults(self):
        node = DAGNode(
            dag_id=uuid4(), name="fix-lint", node_type="subtask",
        )
        assert node.status == "pending"
        assert node.wave == 0
        assert node.timeout_seconds == 120
        assert node.tokens_used == 0

    def test_dag_edge_creation(self):
        edge = DAGEdge(
            dag_id=uuid4(), from_node_id=uuid4(),
            to_node_id=uuid4(), edge_type="dependency",
        )
        assert edge.edge_type == "dependency"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_dag_schemas.py -v`
Expected: `ImportError: cannot import name 'ExecutionDAG'`

- [ ] **Step 3: Add ORM models to `nous/storage/models.py`**

Append after the `DynamicCheckModel` class (after the last field definition):

```python
# ---------------------------------------------------------------------------
# F038: Unified DAG Orchestration
# ---------------------------------------------------------------------------


class ExecutionDAG(Base):
    """Top-level DAG orchestration unit."""

    __tablename__ = "execution_dags"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'cancelled', 'partial')",
            name="chk_dag_status",
        ),
        {"schema": "nous_system"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    agent_id: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending")
    source: Mapped[str] = mapped_column(String(30), nullable=False, server_default="conversation")
    original_request: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    token_budget: Mapped[int | None] = mapped_column(Integer)
    tokens_consumed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    result_summary: Mapped[str | None] = mapped_column(Text)
    postmortem: Mapped[dict | None] = mapped_column(JSONB)

    nodes: Mapped[list["DAGNode"]] = relationship(
        "DAGNode", back_populates="dag", cascade="all, delete-orphan",
        order_by="DAGNode.wave, DAGNode.name",
    )
    edges: Mapped[list["DAGEdge"]] = relationship(
        "DAGEdge", back_populates="dag", cascade="all, delete-orphan",
    )


class DAGNode(Base):
    """Individual execution unit within a DAG."""

    __tablename__ = "dag_nodes"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'ready', 'running', 'completed', 'failed', 'blocked', 'cancelled')",
            name="chk_dag_node_status",
        ),
        UniqueConstraint("dag_id", "name", name="uq_dag_node_name"),
        {"schema": "nous_system"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    dag_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nous_system.execution_dags.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    node_type: Mapped[str] = mapped_column(String(20), nullable=False)
    subtask_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    check_name: Mapped[str | None] = mapped_column(String(200))
    wave: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending")
    instructions: Mapped[str | None] = mapped_column(Text)
    tools: Mapped[list | None] = mapped_column(JSONB)
    frame_type: Mapped[str | None] = mapped_column(String(30))
    model: Mapped[str | None] = mapped_column(String(100))
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="120")
    completion_condition: Mapped[str | None] = mapped_column(String(100))
    result: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    injected_context: Mapped[str | None] = mapped_column(Text)

    dag: Mapped["ExecutionDAG"] = relationship("ExecutionDAG", back_populates="nodes")


class DAGEdge(Base):
    """Dependency/cascade/context relationship between DAG nodes."""

    __tablename__ = "dag_edges"
    __table_args__ = (
        UniqueConstraint(
            "dag_id", "from_node_id", "to_node_id", "edge_type",
            name="uq_dag_edge",
        ),
        {"schema": "nous_system"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    dag_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nous_system.execution_dags.id", ondelete="CASCADE"),
        nullable=False,
    )
    from_node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nous_system.dag_nodes.id", ondelete="CASCADE"),
        nullable=False,
    )
    to_node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nous_system.dag_nodes.id", ondelete="CASCADE"),
        nullable=False,
    )
    edge_type: Mapped[str] = mapped_column(String(20), nullable=False, server_default="dependency")

    dag: Mapped["ExecutionDAG"] = relationship("ExecutionDAG", back_populates="edges")
```

Also add `relationship` to the imports at the top of the file if not already present. Check the existing imports first — if `relationship` is already imported, skip this.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_dag_schemas.py -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add sql/migrations/032_dag_orchestration.sql nous/storage/models.py tests/test_dag_schemas.py
git commit -m "feat(f038): add DAG orchestration DB migration and ORM models"
```

---

## Task 3: Pydantic Schemas

**Files:**
- Create: `nous/dag/__init__.py`
- Create: `nous/dag/schemas.py`

- [ ] **Step 1: Write schema validation tests**

Add to `tests/test_dag_schemas.py`:

```python
from nous.dag.schemas import (
    DAGNodeSpec,
    DAGEdgeSpec,
    DAGCreateRequest,
    DAGNodeType,
    DAGNodeStatus,
    DAGStatusEnum,
)


class TestDAGSchemas:
    def test_dag_create_request_valid(self):
        req = DAGCreateRequest(
            name="test-pipeline",
            description="Test",
            nodes=[
                DAGNodeSpec(name="step1", type="subtask", instructions="Do step 1"),
                DAGNodeSpec(name="step2", type="check", instructions="Monitor",
                            completion_condition="self_disable"),
            ],
            edges=[
                DAGEdgeSpec(from_node="step1", to_node="step2"),
            ],
        )
        assert len(req.nodes) == 2
        assert req.edges[0].edge_type == "dependency"

    def test_dag_create_request_rejects_cycle(self):
        with pytest.raises(ValueError, match="cycle"):
            DAGCreateRequest(
                name="cyclic",
                nodes=[
                    DAGNodeSpec(name="a", type="subtask", instructions="A"),
                    DAGNodeSpec(name="b", type="subtask", instructions="B"),
                ],
                edges=[
                    DAGEdgeSpec(from_node="a", to_node="b"),
                    DAGEdgeSpec(from_node="b", to_node="a"),
                ],
            )

    def test_dag_create_request_rejects_too_many_nodes(self):
        nodes = [DAGNodeSpec(name=f"n{i}", type="subtask", instructions=f"Task {i}") for i in range(11)]
        with pytest.raises(ValueError, match="10"):
            DAGCreateRequest(name="big", nodes=nodes, edges=[])

    def test_dag_create_request_rejects_unknown_edge_node(self):
        with pytest.raises(ValueError, match="unknown node"):
            DAGCreateRequest(
                name="bad-edge",
                nodes=[DAGNodeSpec(name="a", type="subtask", instructions="A")],
                edges=[DAGEdgeSpec(from_node="a", to_node="nonexistent")],
            )

    def test_wave_computation(self):
        req = DAGCreateRequest(
            name="waves",
            nodes=[
                DAGNodeSpec(name="a", type="subtask", instructions="A"),
                DAGNodeSpec(name="b", type="subtask", instructions="B"),
                DAGNodeSpec(name="c", type="gate", instructions="C"),
                DAGNodeSpec(name="d", type="subtask", instructions="D"),
            ],
            edges=[
                DAGEdgeSpec(from_node="a", to_node="c"),
                DAGEdgeSpec(from_node="b", to_node="c"),
                DAGEdgeSpec(from_node="c", to_node="d"),
            ],
        )
        waves = req.compute_waves()
        assert waves["a"] == 0
        assert waves["b"] == 0
        assert waves["c"] == 1
        assert waves["d"] == 2

    def test_max_waves_enforced(self):
        """Max 4 waves (0-3) allowed."""
        nodes = [DAGNodeSpec(name=f"n{i}", type="subtask", instructions=f"T{i}") for i in range(5)]
        edges = [DAGEdgeSpec(from_node=f"n{i}", to_node=f"n{i+1}") for i in range(4)]
        with pytest.raises(ValueError, match="4 waves"):
            DAGCreateRequest(name="deep", nodes=nodes, edges=edges)

    def test_max_parallel_per_wave(self):
        """Max 4 parallel nodes per wave."""
        nodes = [DAGNodeSpec(name=f"n{i}", type="subtask", instructions=f"T{i}") for i in range(6)]
        nodes.append(DAGNodeSpec(name="join", type="gate", instructions="join"))
        edges = [DAGEdgeSpec(from_node=f"n{i}", to_node="join") for i in range(6)]
        with pytest.raises(ValueError, match="4 parallel"):
            DAGCreateRequest(name="wide", nodes=nodes, edges=edges)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_dag_schemas.py::TestDAGSchemas -v`
Expected: `ImportError: No module named 'nous.dag'`

- [ ] **Step 3: Create `nous/dag/__init__.py`**

```python
"""F038: Unified DAG Orchestration."""
```

- [ ] **Step 4: Create `nous/dag/schemas.py`**

```python
"""F038: Pydantic schemas for DAG orchestration."""

from __future__ import annotations

from collections import defaultdict, deque
from enum import Enum

from pydantic import BaseModel, field_validator, model_validator


class DAGNodeType(str, Enum):
    SUBTASK = "subtask"
    CHECK = "check"
    GATE = "gate"
    CALLBACK = "callback"


class DAGStatusEnum(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PARTIAL = "partial"


class DAGNodeStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class DAGNodeSpec(BaseModel):
    """Spec for a single node in a DAG creation request."""

    name: str
    type: DAGNodeType
    instructions: str = ""
    description: str = ""
    tools: list[str] | None = None
    frame_type: str | None = None
    model: str | None = None
    timeout_seconds: int = 120
    completion_condition: str | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not v or len(v) > 100:
            raise ValueError("name must be 1-100 characters")
        return v


class DAGEdgeSpec(BaseModel):
    """Spec for an edge in a DAG creation request."""

    from_node: str
    to_node: str
    edge_type: str = "dependency"

    @field_validator("edge_type")
    @classmethod
    def validate_edge_type(cls, v: str) -> str:
        valid = {"dependency", "cancel_cascade", "context_flow"}
        if v not in valid:
            raise ValueError(f"edge_type must be one of {valid}")
        return v


_MAX_NODES = 10
_MAX_WAVES = 4
_MAX_PARALLEL_PER_WAVE = 4


class DAGCreateRequest(BaseModel):
    """Validated request to create a new DAG."""

    name: str
    description: str = ""
    source: str = "conversation"
    original_request: str | None = None
    token_budget: int | None = None
    nodes: list[DAGNodeSpec]
    edges: list[DAGEdgeSpec]

    @model_validator(mode="after")
    def validate_dag(self) -> "DAGCreateRequest":
        # Node count
        if len(self.nodes) > _MAX_NODES:
            raise ValueError(f"DAG cannot have more than {_MAX_NODES} nodes")
        if len(self.nodes) == 0:
            raise ValueError("DAG must have at least one node")

        # Unique names
        names = [n.name for n in self.nodes]
        if len(names) != len(set(names)):
            raise ValueError("node names must be unique")

        # Edge references
        name_set = set(names)
        for edge in self.edges:
            if edge.from_node not in name_set or edge.to_node not in name_set:
                raise ValueError(
                    f"edge references unknown node: "
                    f"{edge.from_node} -> {edge.to_node}"
                )
            if edge.from_node == edge.to_node:
                raise ValueError("self-loop not allowed")

        # Cycle detection via topological sort
        waves = self.compute_waves()  # raises on cycle

        # Wave depth
        max_wave = max(waves.values()) if waves else 0
        if max_wave >= _MAX_WAVES:
            raise ValueError(
                f"DAG exceeds {_MAX_WAVES} waves (has {max_wave + 1})"
            )

        # Max parallel per wave
        wave_counts: dict[int, int] = defaultdict(int)
        for w in waves.values():
            wave_counts[w] += 1
        for w, count in wave_counts.items():
            if count > _MAX_PARALLEL_PER_WAVE:
                raise ValueError(
                    f"wave {w} has {count} nodes — max {_MAX_PARALLEL_PER_WAVE} parallel"
                )

        return self

    def compute_waves(self) -> dict[str, int]:
        """Topological sort to compute wave numbers. Raises on cycle."""
        # Build adjacency for dependency + context_flow edges
        in_degree: dict[str, int] = {n.name: 0 for n in self.nodes}
        successors: dict[str, list[str]] = defaultdict(list)

        for edge in self.edges:
            if edge.edge_type in ("dependency", "context_flow"):
                in_degree[edge.to_node] += 1
                successors[edge.from_node].append(edge.to_node)

        queue: deque[str] = deque()
        for name, deg in in_degree.items():
            if deg == 0:
                queue.append(name)

        waves: dict[str, int] = {}
        processed = 0

        while queue:
            name = queue.popleft()
            waves[name] = 0
            for pred_name, pred_wave in list(waves.items()):
                if name in successors.get(pred_name, []):
                    waves[name] = max(waves[name], pred_wave + 1)
            processed += 1

            for succ in successors[name]:
                in_degree[succ] -= 1
                if in_degree[succ] == 0:
                    queue.append(succ)

        if processed != len(self.nodes):
            raise ValueError("DAG contains a cycle")

        return waves
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_dag_schemas.py -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add nous/dag/__init__.py nous/dag/schemas.py tests/test_dag_schemas.py
git commit -m "feat(f038): add DAG Pydantic schemas with validation"
```

---

## Task 4: DAG Store (CRUD)

**Files:**
- Create: `nous/dag/store.py`
- Create: `tests/test_dag_store.py`

- [ ] **Step 1: Write store integration tests**

```python
"""Integration tests for DAGStore — requires running Postgres."""

import pytest
import pytest_asyncio

from nous.dag.store import DAGStore
from nous.dag.schemas import DAGCreateRequest, DAGNodeSpec, DAGEdgeSpec
from nous.storage.database import Database


@pytest_asyncio.fixture
async def store(test_database: Database) -> DAGStore:
    return DAGStore(test_database, agent_id="test-agent")


def _simple_request() -> DAGCreateRequest:
    return DAGCreateRequest(
        name="test-pipeline",
        description="Test DAG",
        nodes=[
            DAGNodeSpec(name="step1", type="subtask", instructions="Do step 1"),
            DAGNodeSpec(name="step2", type="check", instructions="Monitor",
                        completion_condition="self_disable"),
            DAGNodeSpec(name="notify", type="callback", instructions="Done"),
        ],
        edges=[
            DAGEdgeSpec(from_node="step1", to_node="step2"),
            DAGEdgeSpec(from_node="step2", to_node="notify"),
        ],
    )


@pytest.mark.asyncio
async def test_create_dag(store: DAGStore):
    dag = await store.create(_simple_request())
    assert dag.name == "test-pipeline"
    assert dag.status == "pending"
    assert len(dag.nodes) == 3
    assert len(dag.edges) == 2
    # Wave 0 nodes should be ready
    wave0 = [n for n in dag.nodes if n.wave == 0]
    assert all(n.status == "ready" for n in wave0)


@pytest.mark.asyncio
async def test_get_dag(store: DAGStore):
    created = await store.create(_simple_request())
    fetched = await store.get_dag(created.id)
    assert fetched is not None
    assert fetched.name == created.name
    assert len(fetched.nodes) == 3


@pytest.mark.asyncio
async def test_get_active_dags(store: DAGStore):
    req = _simple_request()
    dag = await store.create(req)
    await store.update_dag_status(dag.id, "running")
    active = await store.get_active_dags()
    assert any(d.id == dag.id for d in active)


@pytest.mark.asyncio
async def test_update_node_status(store: DAGStore):
    dag = await store.create(_simple_request())
    node = dag.nodes[0]
    await store.update_node(node.id, status="running")
    refreshed = await store.get_dag(dag.id)
    updated = [n for n in refreshed.nodes if n.id == node.id][0]
    assert updated.status == "running"


@pytest.mark.asyncio
async def test_get_recent_dags(store: DAGStore):
    await store.create(_simple_request())
    recent = await store.get_recent_dags(limit=5)
    assert len(recent) >= 1


@pytest.mark.asyncio
async def test_max_active_dags_enforced(store: DAGStore):
    for i in range(5):
        dag = await store.create(DAGCreateRequest(
            name=f"dag-{i}",
            nodes=[DAGNodeSpec(name="a", type="subtask", instructions="x")],
            edges=[],
        ))
        await store.update_dag_status(dag.id, "running")

    with pytest.raises(ValueError, match="active DAG limit"):
        dag = await store.create(DAGCreateRequest(
            name="dag-6",
            nodes=[DAGNodeSpec(name="a", type="subtask", instructions="x")],
            edges=[],
        ))
        await store.update_dag_status(dag.id, "running")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_dag_store.py -v`
Expected: `ImportError: cannot import name 'DAGStore'`

- [ ] **Step 3: Implement `nous/dag/store.py`**

```python
"""F038: DAG storage — CRUD operations for DAGs, nodes, and edges."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, update

from nous.dag.schemas import DAGCreateRequest
from nous.storage.database import Database
from nous.storage.models import DAGEdge, DAGNode, ExecutionDAG

logger = logging.getLogger(__name__)

_MAX_ACTIVE_DAGS = 5


class DAGStore:
    """Async CRUD for ExecutionDAGs and their nodes/edges."""

    def __init__(self, database: Database, agent_id: str) -> None:
        self._db = database
        self._agent_id = agent_id

    async def create(self, request: DAGCreateRequest) -> ExecutionDAG:
        """Create a new DAG with nodes and edges. Returns the persisted DAG."""
        waves = request.compute_waves()

        async with self._db.session() as session:
            dag = ExecutionDAG(
                agent_id=self._agent_id,
                name=request.name,
                description=request.description,
                source=request.source,
                original_request=request.original_request,
                token_budget=request.token_budget,
            )
            session.add(dag)
            await session.flush()  # get dag.id

            # Create nodes
            name_to_node: dict[str, DAGNode] = {}
            for spec in request.nodes:
                wave = waves[spec.name]
                node = DAGNode(
                    dag_id=dag.id,
                    name=spec.name,
                    description=spec.description or spec.instructions[:200],
                    node_type=spec.type.value if hasattr(spec.type, "value") else spec.type,
                    wave=wave,
                    status="ready" if wave == 0 else "pending",
                    instructions=spec.instructions,
                    tools=spec.tools,
                    frame_type=spec.frame_type,
                    model=spec.model,
                    timeout_seconds=spec.timeout_seconds,
                    completion_condition=spec.completion_condition,
                )
                session.add(node)
                name_to_node[spec.name] = node

            await session.flush()  # get node IDs

            # Create edges
            for edge_spec in request.edges:
                edge = DAGEdge(
                    dag_id=dag.id,
                    from_node_id=name_to_node[edge_spec.from_node].id,
                    to_node_id=name_to_node[edge_spec.to_node].id,
                    edge_type=edge_spec.edge_type,
                )
                session.add(edge)

            await session.commit()
            await session.refresh(dag, ["nodes", "edges"])
            logger.info("F038: Created DAG '%s' (%s) with %d nodes", dag.name, dag.id.hex[:8], len(dag.nodes))
            return dag

    async def get_dag(self, dag_id: UUID) -> ExecutionDAG | None:
        """Fetch a DAG with all nodes and edges."""
        async with self._db.session() as session:
            result = await session.execute(
                select(ExecutionDAG)
                .where(ExecutionDAG.id == dag_id)
                .where(ExecutionDAG.agent_id == self._agent_id)
            )
            dag = result.scalar_one_or_none()
            if dag:
                # Eagerly load relationships
                await session.refresh(dag, ["nodes", "edges"])
            return dag

    async def get_active_dags(self) -> list[ExecutionDAG]:
        """Get all DAGs in running or pending status."""
        async with self._db.session() as session:
            result = await session.execute(
                select(ExecutionDAG)
                .where(ExecutionDAG.agent_id == self._agent_id)
                .where(ExecutionDAG.status.in_(["pending", "running"]))
                .order_by(ExecutionDAG.created_at)
            )
            dags = list(result.scalars().all())
            for dag in dags:
                await session.refresh(dag, ["nodes", "edges"])
            return dags

    async def get_recent_dags(self, limit: int = 20) -> list[ExecutionDAG]:
        """Get recent DAGs (any status) for dashboard."""
        async with self._db.session() as session:
            result = await session.execute(
                select(ExecutionDAG)
                .where(ExecutionDAG.agent_id == self._agent_id)
                .order_by(ExecutionDAG.created_at.desc())
                .limit(limit)
            )
            dags = list(result.scalars().all())
            for dag in dags:
                await session.refresh(dag, ["nodes", "edges"])
            return dags

    async def count_active(self) -> int:
        """Count active (pending + running) DAGs."""
        async with self._db.session() as session:
            return await session.scalar(
                select(func.count())
                .select_from(ExecutionDAG)
                .where(ExecutionDAG.agent_id == self._agent_id)
                .where(ExecutionDAG.status.in_(["pending", "running"]))
            ) or 0

    async def update_dag_status(
        self,
        dag_id: UUID,
        status: str,
        result_summary: str | None = None,
        postmortem: dict | None = None,
    ) -> None:
        """Update DAG status and optional fields."""
        async with self._db.session() as session:
            values: dict = {"status": status}
            if status == "running" and not await self._has_started(session, dag_id):
                values["started_at"] = datetime.now(UTC)
                # Check active limit
                active = await session.scalar(
                    select(func.count())
                    .select_from(ExecutionDAG)
                    .where(ExecutionDAG.agent_id == self._agent_id)
                    .where(ExecutionDAG.status.in_(["pending", "running"]))
                    .where(ExecutionDAG.id != dag_id)
                )
                if active >= _MAX_ACTIVE_DAGS:
                    raise ValueError(f"active DAG limit ({_MAX_ACTIVE_DAGS}) reached")
            if status in ("completed", "failed", "cancelled", "partial"):
                values["completed_at"] = datetime.now(UTC)
            if result_summary is not None:
                values["result_summary"] = result_summary
            if postmortem is not None:
                values["postmortem"] = postmortem

            await session.execute(
                update(ExecutionDAG)
                .where(ExecutionDAG.id == dag_id)
                .values(**values)
            )
            await session.commit()

    async def update_node(
        self,
        node_id: UUID,
        **kwargs,
    ) -> None:
        """Update node fields (status, result, error, subtask_id, check_name, etc.)."""
        async with self._db.session() as session:
            await session.execute(
                update(DAGNode)
                .where(DAGNode.id == node_id)
                .values(**kwargs)
            )
            await session.commit()

    async def update_dag_tokens(self, dag_id: UUID, tokens: int) -> None:
        """Increment tokens consumed for a DAG."""
        async with self._db.session() as session:
            await session.execute(
                update(ExecutionDAG)
                .where(ExecutionDAG.id == dag_id)
                .values(tokens_consumed=ExecutionDAG.tokens_consumed + tokens)
            )
            await session.commit()

    async def _has_started(self, session, dag_id: UUID) -> bool:
        result = await session.execute(
            select(ExecutionDAG.started_at).where(ExecutionDAG.id == dag_id)
        )
        return result.scalar_one_or_none() is not None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_dag_store.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add nous/dag/store.py tests/test_dag_store.py
git commit -m "feat(f038): add DAGStore with CRUD operations"
```

---

## Task 5: DAG Orchestrator

**Files:**
- Create: `nous/dag/orchestrator.py`
- Create: `tests/test_dag_orchestrator.py`

- [ ] **Step 1: Write orchestrator tests**

```python
"""Tests for DAGOrchestrator state machine."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio

from nous.dag.orchestrator import DAGOrchestrator
from nous.dag.schemas import DAGCreateRequest, DAGEdgeSpec, DAGNodeSpec
from nous.dag.store import DAGStore
from nous.storage.database import Database


@pytest_asyncio.fixture
async def store(test_database: Database) -> DAGStore:
    return DAGStore(test_database, agent_id="test-agent")


@pytest_asyncio.fixture
async def orchestrator(store: DAGStore) -> DAGOrchestrator:
    subtask_mgr = AsyncMock()
    dynamic_loader = AsyncMock()
    return DAGOrchestrator(
        store=store,
        subtask_mgr=subtask_mgr,
        dynamic_loader=dynamic_loader,
    )


def _pipeline_request() -> DAGCreateRequest:
    return DAGCreateRequest(
        name="test-pipeline",
        nodes=[
            DAGNodeSpec(name="work", type="subtask", instructions="Do work"),
            DAGNodeSpec(name="monitor", type="check", instructions="Watch",
                        completion_condition="self_disable"),
            DAGNodeSpec(name="notify", type="callback", instructions="Done"),
        ],
        edges=[
            DAGEdgeSpec(from_node="work", to_node="monitor"),
            DAGEdgeSpec(from_node="monitor", to_node="notify"),
        ],
    )


@pytest.mark.asyncio
async def test_create_and_start_dag(orchestrator: DAGOrchestrator, store: DAGStore):
    dag = await store.create(_pipeline_request())
    await orchestrator.start_dag(dag.id)
    refreshed = await store.get_dag(dag.id)
    assert refreshed.status == "running"
    # Wave 0 node should be launched
    work_node = [n for n in refreshed.nodes if n.name == "work"][0]
    assert work_node.status == "running"


@pytest.mark.asyncio
async def test_tick_advances_ready_nodes(orchestrator: DAGOrchestrator, store: DAGStore):
    """When a subtask completes, tick should advance dependents."""
    dag = await store.create(_pipeline_request())
    await orchestrator.start_dag(dag.id)

    # Simulate subtask completion
    refreshed = await store.get_dag(dag.id)
    work_node = [n for n in refreshed.nodes if n.name == "work"][0]
    await store.update_node(work_node.id, status="completed", result="done")

    # Mock subtask manager to return completed subtask
    mock_subtask = MagicMock()
    mock_subtask.status = "completed"
    mock_subtask.result = "done"
    mock_subtask.completed_at = datetime.now(UTC)
    orchestrator._subtask_mgr.get = AsyncMock(return_value=mock_subtask)

    await orchestrator.tick()

    refreshed = await store.get_dag(dag.id)
    monitor_node = [n for n in refreshed.nodes if n.name == "monitor"][0]
    assert monitor_node.status in ("ready", "running")


@pytest.mark.asyncio
async def test_cascade_failure(orchestrator: DAGOrchestrator, store: DAGStore):
    dag = await store.create(_pipeline_request())
    await orchestrator.start_dag(dag.id)

    refreshed = await store.get_dag(dag.id)
    work_node = [n for n in refreshed.nodes if n.name == "work"][0]
    await store.update_node(work_node.id, status="failed", error="boom")

    await orchestrator.tick()

    refreshed = await store.get_dag(dag.id)
    monitor_node = [n for n in refreshed.nodes if n.name == "monitor"][0]
    notify_node = [n for n in refreshed.nodes if n.name == "notify"][0]
    assert monitor_node.status == "blocked"
    assert notify_node.status == "blocked"
    assert refreshed.status == "failed"


@pytest.mark.asyncio
async def test_cancel_dag(orchestrator: DAGOrchestrator, store: DAGStore):
    dag = await store.create(_pipeline_request())
    await orchestrator.start_dag(dag.id)

    await orchestrator.cancel_dag(dag.id, reason="user_cancelled")

    refreshed = await store.get_dag(dag.id)
    assert refreshed.status == "cancelled"
    for node in refreshed.nodes:
        assert node.status == "cancelled"


@pytest.mark.asyncio
async def test_dag_completes_when_all_nodes_done(orchestrator: DAGOrchestrator, store: DAGStore):
    req = DAGCreateRequest(
        name="simple",
        nodes=[DAGNodeSpec(name="a", type="callback", instructions="done")],
        edges=[],
    )
    dag = await store.create(req)
    await orchestrator.start_dag(dag.id)

    # Callback nodes complete immediately in start_dag
    await orchestrator.tick()

    refreshed = await store.get_dag(dag.id)
    assert refreshed.status == "completed"


@pytest.mark.asyncio
async def test_parallel_wave_launch(orchestrator: DAGOrchestrator, store: DAGStore):
    req = DAGCreateRequest(
        name="parallel",
        nodes=[
            DAGNodeSpec(name="a", type="subtask", instructions="A"),
            DAGNodeSpec(name="b", type="subtask", instructions="B"),
            DAGNodeSpec(name="join", type="gate", instructions="check"),
        ],
        edges=[
            DAGEdgeSpec(from_node="a", to_node="join"),
            DAGEdgeSpec(from_node="b", to_node="join"),
        ],
    )
    dag = await store.create(req)
    await orchestrator.start_dag(dag.id)

    refreshed = await store.get_dag(dag.id)
    running = [n for n in refreshed.nodes if n.status == "running"]
    assert len(running) == 2  # a and b run in parallel


@pytest.mark.asyncio
async def test_budget_exceeded_cancels_pending(orchestrator: DAGOrchestrator, store: DAGStore):
    req = DAGCreateRequest(
        name="budget-test",
        token_budget=100,
        nodes=[
            DAGNodeSpec(name="a", type="subtask", instructions="A"),
            DAGNodeSpec(name="b", type="subtask", instructions="B"),
        ],
        edges=[DAGEdgeSpec(from_node="a", to_node="b")],
    )
    dag = await store.create(req)
    await orchestrator.start_dag(dag.id)

    # Simulate budget exceeded
    await store.update_dag_tokens(dag.id, 101)
    a_node = [n for n in dag.nodes if n.name == "a"][0]
    await store.update_node(a_node.id, status="completed", result="done")

    await orchestrator.tick()

    refreshed = await store.get_dag(dag.id)
    assert refreshed.status == "partial"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_dag_orchestrator.py -v`
Expected: `ImportError: cannot import name 'DAGOrchestrator'`

- [ ] **Step 3: Implement `nous/dag/orchestrator.py`**

```python
"""F038: DAG Orchestrator — tick-based state machine for DAG lifecycle."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from nous.dag.store import DAGStore
from nous.storage.models import DAGEdge, DAGNode, ExecutionDAG

if TYPE_CHECKING:
    from nous.heart.subtasks import SubtaskManager
    from nous.heartbeat.dynamic import DynamicCheckLoader

logger = logging.getLogger(__name__)


class DAGOrchestrator:
    """Manages lifecycle of all active ExecutionDAGs.

    Runs on each heartbeat tick. Checks node completion status,
    advances ready nodes, propagates failures, and completes DAGs.
    Not an LLM agent — purely mechanical state management.
    """

    def __init__(
        self,
        store: DAGStore,
        subtask_mgr: "SubtaskManager | None" = None,
        dynamic_loader: "DynamicCheckLoader | None" = None,
        bus: "Any | None" = None,
    ) -> None:
        self._store = store
        self._subtask_mgr = subtask_mgr
        self._dynamic_loader = dynamic_loader
        self._bus = bus

    async def tick(self) -> None:
        """Called each heartbeat cycle. Advances all active DAGs."""
        active_dags = await self._store.get_active_dags()
        for dag in active_dags:
            try:
                await self._advance_dag(dag)
            except Exception:
                logger.exception("F038: Error advancing DAG '%s'", dag.name)

    async def start_dag(self, dag_id: UUID) -> None:
        """Start a DAG: set status to running and launch wave-0 nodes."""
        await self._store.update_dag_status(dag_id, "running")
        dag = await self._store.get_dag(dag_id)
        if dag is None:
            return

        ready_nodes = [n for n in dag.nodes if n.status == "ready"]
        for node in ready_nodes:
            await self._launch_node(node, dag)

        logger.info("F038: Started DAG '%s' — launched %d wave-0 nodes", dag.name, len(ready_nodes))

    async def cancel_dag(self, dag_id: UUID, reason: str = "user_cancelled") -> None:
        """Cancel an entire DAG and all its running primitives."""
        dag = await self._store.get_dag(dag_id)
        if dag is None:
            return

        for node in dag.nodes:
            if node.status in ("pending", "ready", "running"):
                await self._cancel_node(node)

        await self._store.update_dag_status(
            dag_id, "cancelled",
            postmortem={"reason": reason},
        )
        logger.info("F038: Cancelled DAG '%s' — reason: %s", dag.name, reason)

    async def retry_node(self, dag_id: UUID, node_name: str) -> None:
        """Retry a failed node by resetting it to ready."""
        dag = await self._store.get_dag(dag_id)
        if dag is None:
            raise ValueError(f"DAG {dag_id} not found")

        node = next((n for n in dag.nodes if n.name == node_name), None)
        if node is None:
            raise ValueError(f"Node '{node_name}' not found in DAG")
        if node.status != "failed":
            raise ValueError(f"Node '{node_name}' is {node.status}, not failed")

        await self._store.update_node(
            node.id, status="ready", error=None,
            subtask_id=None, check_name=None,
            started_at=None, completed_at=None,
        )

        # Unblock dependents
        for dep_node in dag.nodes:
            if dep_node.status == "blocked":
                # Check if this was blocked by the retried node
                await self._store.update_node(dep_node.id, status="pending", error=None)

        # Re-run the DAG if it was marked failed
        if dag.status == "failed":
            await self._store.update_dag_status(dag_id, "running")

        logger.info("F038: Retrying node '%s' in DAG '%s'", node_name, dag.name)

    # ------------------------------------------------------------------
    # Internal — DAG advancement
    # ------------------------------------------------------------------

    async def _advance_dag(self, dag: ExecutionDAG) -> None:
        """Process one DAG: sync statuses, propagate failures, advance ready nodes."""
        # 1. Sync node statuses from underlying primitives
        await self._sync_node_statuses(dag)

        # 2. Check budget
        if dag.token_budget and dag.tokens_consumed > dag.token_budget:
            logger.warning("F038: DAG '%s' budget exceeded (%d/%d)", dag.name, dag.tokens_consumed, dag.token_budget)
            await self._handle_budget_exceeded(dag)
            return

        # 3. Propagate failures
        await self._propagate_failures(dag)

        # 4. Find newly ready nodes
        ready_nodes = self._find_ready_nodes(dag)

        # 5. Launch ready nodes
        for node in ready_nodes:
            await self._launch_node(node, dag)

        # 6. Check DAG completion
        await self._check_dag_completion(dag)

    async def _sync_node_statuses(self, dag: ExecutionDAG) -> None:
        """Pull completion status from subtasks and checks back into DAG nodes."""
        for node in dag.nodes:
            if node.status != "running":
                continue

            if node.node_type == "subtask" and node.subtask_id and self._subtask_mgr:
                try:
                    subtask = await self._subtask_mgr.get(node.subtask_id)
                    if subtask and subtask.status == "completed":
                        await self._store.update_node(
                            node.id, status="completed",
                            result=subtask.result,
                            completed_at=subtask.completed_at or datetime.now(UTC),
                        )
                        node.status = "completed"
                        node.result = subtask.result
                    elif subtask and subtask.status == "failed":
                        await self._store.update_node(
                            node.id, status="failed",
                            error=subtask.error,
                            completed_at=datetime.now(UTC),
                        )
                        node.status = "failed"
                        node.error = subtask.error
                except Exception:
                    logger.debug("F038: Error syncing subtask for node '%s'", node.name, exc_info=True)

            elif node.node_type == "check" and node.check_name and self._dynamic_loader:
                try:
                    check = self._dynamic_loader._registry.get_check(node.check_name)
                    if check is None:
                        # Check was removed — treat as completed
                        await self._store.update_node(
                            node.id, status="completed",
                            result="check_removed",
                            completed_at=datetime.now(UTC),
                        )
                        node.status = "completed"
                    elif hasattr(check, '_enabled') and not check._enabled:
                        # Self-disabled = completed
                        await self._store.update_node(
                            node.id, status="completed",
                            result="self_disabled",
                            completed_at=datetime.now(UTC),
                        )
                        node.status = "completed"
                except Exception:
                    logger.debug("F038: Error syncing check for node '%s'", node.name, exc_info=True)

    async def _propagate_failures(self, dag: ExecutionDAG) -> None:
        """Block dependents of failed nodes."""
        failed_names = {n.name for n in dag.nodes if n.status == "failed"}
        if not failed_names:
            return

        # Build dependency map: to_node -> set of from_nodes
        deps: dict[str, set[str]] = {}
        for edge in dag.edges:
            if edge.edge_type in ("dependency", "context_flow"):
                to_name = next((n.name for n in dag.nodes if n.id == edge.to_node_id), None)
                from_name = next((n.name for n in dag.nodes if n.id == edge.from_node_id), None)
                if to_name and from_name:
                    deps.setdefault(to_name, set()).add(from_name)

        # Transitively block
        changed = True
        while changed:
            changed = False
            for node in dag.nodes:
                if node.status in ("pending", "ready"):
                    preds = deps.get(node.name, set())
                    if preds & failed_names:
                        await self._store.update_node(
                            node.id, status="blocked",
                            error=f"Blocked: predecessor failed",
                        )
                        node.status = "blocked"
                        failed_names.add(node.name)
                        changed = True

    def _find_ready_nodes(self, dag: ExecutionDAG) -> list[DAGNode]:
        """Find pending nodes whose all predecessors are completed."""
        # Build predecessor map
        preds: dict[str, set[str]] = {}
        node_by_name: dict[str, DAGNode] = {n.name: n for n in dag.nodes}

        for edge in dag.edges:
            if edge.edge_type in ("dependency", "context_flow"):
                to_name = next((n.name for n in dag.nodes if n.id == edge.to_node_id), None)
                from_name = next((n.name for n in dag.nodes if n.id == edge.from_node_id), None)
                if to_name and from_name:
                    preds.setdefault(to_name, set()).add(from_name)

        ready = []
        for node in dag.nodes:
            if node.status != "pending":
                continue
            node_preds = preds.get(node.name, set())
            if all(node_by_name[p].status == "completed" for p in node_preds if p in node_by_name):
                ready.append(node)

        return ready

    async def _launch_node(self, node: DAGNode, dag: ExecutionDAG) -> None:
        """Create the underlying primitive for a ready node."""
        # Build predecessor context
        context = await self._build_predecessor_context(node, dag)
        augmented = context + (node.instructions or "")

        now = datetime.now(UTC)

        if node.node_type == "subtask" and self._subtask_mgr:
            try:
                subtask = await self._subtask_mgr.create(
                    task=augmented,
                    frame_type=node.frame_type,
                    model=node.model,
                    timeout=node.timeout_seconds,
                    metadata={"dag_id": str(dag.id), "dag_node_id": str(node.id)},
                )
                await self._store.update_node(
                    node.id, status="running",
                    subtask_id=subtask.id, started_at=now,
                    injected_context=context if context else None,
                )
                node.status = "running"
                logger.info("F038: Launched subtask node '%s' -> subtask %s", node.name, subtask.id.hex[:8])
            except Exception as e:
                await self._store.update_node(node.id, status="failed", error=str(e), started_at=now, completed_at=now)
                node.status = "failed"
                logger.error("F038: Failed to launch subtask node '%s': %s", node.name, e)

        elif node.node_type == "check" and self._dynamic_loader:
            try:
                check_name = f"dag-{dag.id.hex[:8]}-{node.name}"
                result = await self._dynamic_loader.create_check(
                    name=check_name,
                    description=node.description,
                    prompt=augmented,
                    tools=node.tools or ["bash"],
                    interval_seconds=300,
                    timeout_seconds=node.timeout_seconds,
                )
                await self._store.update_node(
                    node.id, status="running",
                    check_name=check_name, started_at=now,
                    injected_context=context if context else None,
                )
                node.status = "running"
                logger.info("F038: Launched check node '%s' -> check '%s'", node.name, check_name)
            except Exception as e:
                await self._store.update_node(node.id, status="failed", error=str(e), started_at=now, completed_at=now)
                node.status = "failed"
                logger.error("F038: Failed to launch check node '%s': %s", node.name, e)

        elif node.node_type == "gate":
            # Gates run inline — for now, auto-pass (Critic integration in Phase 2)
            await self._store.update_node(
                node.id, status="completed",
                result="gate_passed", started_at=now, completed_at=now,
            )
            node.status = "completed"
            logger.info("F038: Gate node '%s' passed (auto-pass, Phase 2 will add Critic)", node.name)

        elif node.node_type == "callback":
            # Callbacks complete immediately — result stored for downstream
            await self._store.update_node(
                node.id, status="completed",
                result=f"callback: {node.instructions or ''}",
                started_at=now, completed_at=now,
            )
            node.status = "completed"
            logger.info("F038: Callback node '%s' completed", node.name)

    async def _build_predecessor_context(self, node: DAGNode, dag: ExecutionDAG) -> str:
        """Build context from predecessor results for context_flow edges."""
        context_parts = []
        for edge in dag.edges:
            if edge.to_node_id == node.id and edge.edge_type == "context_flow":
                pred = next((n for n in dag.nodes if n.id == edge.from_node_id), None)
                if pred and pred.result:
                    context_parts.append(f"[Result from '{pred.name}']: {pred.result}\n\n")
        return "".join(context_parts)

    async def _cancel_node(self, node: DAGNode) -> None:
        """Cancel a single node and its underlying primitive."""
        if node.node_type == "subtask" and node.subtask_id and self._subtask_mgr:
            try:
                await self._subtask_mgr.cancel(node.subtask_id)
            except Exception:
                logger.debug("F038: Error cancelling subtask for node '%s'", node.name)

        elif node.node_type == "check" and node.check_name and self._dynamic_loader:
            try:
                await self._dynamic_loader.manage_check(action="disable", name=node.check_name)
            except Exception:
                logger.debug("F038: Error disabling check for node '%s'", node.name)

        await self._store.update_node(node.id, status="cancelled", completed_at=datetime.now(UTC))

    async def _check_dag_completion(self, dag: ExecutionDAG) -> None:
        """Check if DAG is complete (all nodes resolved)."""
        statuses = {n.status for n in dag.nodes}
        active = {"pending", "ready", "running"}

        if not statuses & active:
            # All nodes are in terminal state
            if all(n.status == "completed" for n in dag.nodes):
                # Assemble result summary
                results = [f"{n.name}: {n.result or 'ok'}" for n in dag.nodes if n.result]
                summary = "; ".join(results)
                await self._store.update_dag_status(dag.id, "completed", result_summary=summary)
                logger.info("F038: DAG '%s' completed successfully", dag.name)
            elif any(n.status == "failed" for n in dag.nodes):
                failed = [n.name for n in dag.nodes if n.status == "failed"]
                await self._store.update_dag_status(
                    dag.id, "failed",
                    postmortem={"failed_nodes": failed},
                )
                logger.warning("F038: DAG '%s' failed — nodes: %s", dag.name, failed)
            elif any(n.status == "cancelled" for n in dag.nodes):
                await self._store.update_dag_status(dag.id, "cancelled")

    async def _handle_budget_exceeded(self, dag: ExecutionDAG) -> None:
        """Cancel pending/ready nodes when budget exceeded."""
        for node in dag.nodes:
            if node.status in ("pending", "ready"):
                await self._store.update_node(node.id, status="cancelled", error="budget_exceeded")
                node.status = "cancelled"

        # Mark DAG as partial if some nodes completed
        completed = [n for n in dag.nodes if n.status == "completed"]
        if completed:
            await self._store.update_dag_status(
                dag.id, "partial",
                postmortem={"reason": "budget_exceeded", "completed_nodes": [n.name for n in completed]},
            )
        else:
            await self._store.update_dag_status(
                dag.id, "failed",
                postmortem={"reason": "budget_exceeded"},
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_dag_orchestrator.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add nous/dag/orchestrator.py tests/test_dag_orchestrator.py
git commit -m "feat(f038): add DAGOrchestrator state machine"
```

---

## Task 6: SubtaskManager.get() Method

The orchestrator needs `SubtaskManager.get(subtask_id)` to sync node status. Check if it exists; if not, add it.

**Files:**
- Modify: `nous/heart/subtasks.py`

- [ ] **Step 1: Write test for get method**

Add to an existing subtask test file or create inline:

```python
# In tests/test_dag_orchestrator.py or a dedicated file
@pytest.mark.asyncio
async def test_subtask_manager_get(test_database: Database):
    from nous.heart.subtasks import SubtaskManager
    mgr = SubtaskManager(test_database, "test-agent")
    subtask = await mgr.create(task="test task")
    fetched = await mgr.get(subtask.id)
    assert fetched is not None
    assert fetched.task == "test task"
```

- [ ] **Step 2: Check if `get()` exists in `nous/heart/subtasks.py`**

Search for `async def get` in the file. If it exists, skip this task. If not, add:

```python
async def get(self, subtask_id: UUID) -> Subtask | None:
    """Fetch a single subtask by ID."""
    async with self._db.session() as session:
        result = await session.execute(
            select(Subtask)
            .where(Subtask.id == subtask_id)
            .where(Subtask.agent_id == self._agent_id)
        )
        return result.scalar_one_or_none()
```

- [ ] **Step 3: Run test to verify it passes**

Run: `uv run pytest tests/test_dag_orchestrator.py::test_subtask_manager_get -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add nous/heart/subtasks.py
git commit -m "feat(f038): add SubtaskManager.get() for DAG status sync"
```

---

## Task 7: Tool Registration (dag_create, dag_manage)

**Files:**
- Modify: `nous/api/tools.py` — add `register_dag_tools()`
- Create: `tests/test_dag_tools.py`

- [ ] **Step 1: Write tool dispatch tests**

```python
"""Tests for dag_create and dag_manage tool dispatch."""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock

from nous.dag.orchestrator import DAGOrchestrator
from nous.dag.store import DAGStore
from nous.storage.database import Database


@pytest_asyncio.fixture
async def dag_store(test_database: Database) -> DAGStore:
    return DAGStore(test_database, agent_id="test-agent")


@pytest_asyncio.fixture
async def dag_orchestrator(dag_store: DAGStore) -> DAGOrchestrator:
    return DAGOrchestrator(store=dag_store, subtask_mgr=AsyncMock(), dynamic_loader=AsyncMock())


@pytest.mark.asyncio
async def test_dag_create_tool(dag_store: DAGStore, dag_orchestrator: DAGOrchestrator):
    from nous.api.tools import _dag_create_handler
    result = await _dag_create_handler(
        store=dag_store,
        orchestrator=dag_orchestrator,
        name="test-dag",
        description="Test",
        nodes=[
            {"name": "a", "type": "subtask", "instructions": "do A"},
        ],
        edges=[],
    )
    assert "created" in result.lower() or "test-dag" in result


@pytest.mark.asyncio
async def test_dag_manage_list(dag_store: DAGStore, dag_orchestrator: DAGOrchestrator):
    from nous.api.tools import _dag_manage_handler
    result = await _dag_manage_handler(
        store=dag_store,
        orchestrator=dag_orchestrator,
        action="list",
    )
    assert "dag" in result.lower() or "no active" in result.lower()


@pytest.mark.asyncio
async def test_dag_manage_cancel(dag_store: DAGStore, dag_orchestrator: DAGOrchestrator):
    from nous.dag.schemas import DAGCreateRequest, DAGNodeSpec
    dag = await dag_store.create(DAGCreateRequest(
        name="to-cancel",
        nodes=[DAGNodeSpec(name="a", type="subtask", instructions="x")],
        edges=[],
    ))
    await dag_orchestrator.start_dag(dag.id)

    from nous.api.tools import _dag_manage_handler
    result = await _dag_manage_handler(
        store=dag_store,
        orchestrator=dag_orchestrator,
        action="cancel",
        dag_id=str(dag.id),
    )
    assert "cancelled" in result.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_dag_tools.py -v`
Expected: `ImportError: cannot import name '_dag_create_handler'`

- [ ] **Step 3: Add `register_dag_tools()` to `nous/api/tools.py`**

Add at the end of the file, before any final exports:

```python
# ---------------------------------------------------------------------------
# F038: DAG Orchestration tools
# ---------------------------------------------------------------------------


async def _dag_create_handler(
    store: "Any",
    orchestrator: "Any",
    name: str,
    nodes: list[dict],
    edges: list[dict] | None = None,
    description: str = "",
    source: str = "conversation",
    original_request: str | None = None,
    token_budget: int | None = None,
) -> str:
    """Create and start a new DAG."""
    from nous.dag.schemas import DAGCreateRequest, DAGNodeSpec, DAGEdgeSpec

    try:
        node_specs = [DAGNodeSpec(**n) for n in nodes]
        edge_specs = [DAGEdgeSpec(**e) for e in (edges or [])]
        request = DAGCreateRequest(
            name=name,
            description=description,
            source=source,
            original_request=original_request,
            token_budget=token_budget,
            nodes=node_specs,
            edges=edge_specs,
        )
        dag = await store.create(request)
        await orchestrator.start_dag(dag.id)
        waves = request.compute_waves()
        wave_summary = {}
        for n, w in waves.items():
            wave_summary.setdefault(w, []).append(n)
        wave_str = " → ".join(
            f"Wave {w}: [{', '.join(ns)}]" for w, ns in sorted(wave_summary.items())
        )
        return (
            f"DAG '{name}' created and started (id: {dag.id.hex[:8]}). "
            f"{len(node_specs)} nodes, {len(edge_specs)} edges. "
            f"Execution plan: {wave_str}"
        )
    except ValueError as e:
        return f"DAG creation failed: {e}"
    except Exception as e:
        return f"DAG creation error: {e}"


async def _dag_manage_handler(
    store: "Any",
    orchestrator: "Any",
    action: str,
    dag_id: str | None = None,
    node_name: str | None = None,
) -> str:
    """List, inspect, cancel, or retry DAG nodes."""
    from uuid import UUID

    try:
        if action == "list":
            active = await store.get_active_dags()
            if not active:
                return "No active DAGs."
            lines = []
            for dag in active:
                completed = sum(1 for n in dag.nodes if n.status == "completed")
                total = len(dag.nodes)
                lines.append(f"• {dag.name} ({dag.id.hex[:8]}) — {dag.status} — {completed}/{total} nodes done")
            return "Active DAGs:\n" + "\n".join(lines)

        if not dag_id:
            return f"dag_id required for action '{action}'"

        did = UUID(dag_id) if len(dag_id) > 8 else None
        if did is None:
            # Try short hex lookup
            active = await store.get_active_dags()
            recent = await store.get_recent_dags(limit=20)
            all_dags = active + recent
            matches = [d for d in all_dags if d.id.hex.startswith(dag_id)]
            if len(matches) == 1:
                did = matches[0].id
            else:
                return f"Could not resolve dag_id '{dag_id}' — found {len(matches)} matches"

        if action == "status":
            dag = await store.get_dag(did)
            if not dag:
                return f"DAG {dag_id} not found"
            lines = [f"DAG: {dag.name} — Status: {dag.status}"]
            if dag.token_budget:
                lines.append(f"Budget: {dag.tokens_consumed}/{dag.token_budget} tokens")
            lines.append(f"Nodes ({len(dag.nodes)}):")
            for node in sorted(dag.nodes, key=lambda n: (n.wave, n.name)):
                status_icon = {"completed": "✓", "failed": "✗", "running": "⟳", "blocked": "⊘", "cancelled": "—", "pending": "○", "ready": "◉"}.get(node.status, "?")
                line = f"  [{status_icon}] {node.name} ({node.node_type}, wave {node.wave}) — {node.status}"
                if node.error:
                    line += f" — {node.error[:80]}"
                lines.append(line)
            return "\n".join(lines)

        elif action == "cancel":
            await orchestrator.cancel_dag(did, reason="user_cancelled")
            return f"DAG {dag_id} cancelled."

        elif action == "retry_node":
            if not node_name:
                return "node_name required for retry_node"
            await orchestrator.retry_node(did, node_name)
            return f"Node '{node_name}' in DAG {dag_id} retried."

        else:
            return f"Unknown action: {action}. Use list, status, cancel, or retry_node."

    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: {e}"


def register_dag_tools(
    dispatcher: "ToolDispatcher",
    store: "Any",
    orchestrator: "Any",
) -> None:
    """F038: Register DAG orchestration tools."""

    async def dag_create(**kwargs) -> dict:
        result = await _dag_create_handler(store=store, orchestrator=orchestrator, **kwargs)
        return {"result": result}

    async def dag_manage(**kwargs) -> dict:
        result = await _dag_manage_handler(store=store, orchestrator=orchestrator, **kwargs)
        return {"result": result}

    dispatcher.register(
        "dag_create",
        dag_create,
        {
            "name": "dag_create",
            "description": "Create a DAG (directed acyclic graph) to orchestrate multiple subtasks and checks with dependency tracking, cascade cancel, and context flow.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Human-readable DAG name"},
                    "description": {"type": "string", "description": "What this DAG accomplishes"},
                    "nodes": {
                        "type": "array",
                        "description": "List of nodes (max 10)",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "type": {"type": "string", "enum": ["subtask", "check", "gate", "callback"]},
                                "instructions": {"type": "string"},
                                "tools": {"type": "array", "items": {"type": "string"}},
                                "frame_type": {"type": "string"},
                                "model": {"type": "string"},
                                "timeout_seconds": {"type": "integer", "default": 120},
                                "completion_condition": {"type": "string"},
                            },
                            "required": ["name", "type", "instructions"],
                        },
                    },
                    "edges": {
                        "type": "array",
                        "description": "Dependency edges between nodes",
                        "items": {
                            "type": "object",
                            "properties": {
                                "from_node": {"type": "string"},
                                "to_node": {"type": "string"},
                                "edge_type": {"type": "string", "enum": ["dependency", "cancel_cascade", "context_flow"], "default": "dependency"},
                            },
                            "required": ["from_node", "to_node"],
                        },
                    },
                    "source": {"type": "string", "default": "conversation"},
                    "token_budget": {"type": "integer"},
                },
                "required": ["name", "nodes", "edges"],
            },
        },
        frames=["conversation", "debug", "task"],
    )

    dispatcher.register(
        "dag_manage",
        dag_manage,
        {
            "name": "dag_manage",
            "description": "List, inspect, cancel, or retry nodes in DAGs.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "status", "cancel", "retry_node"]},
                    "dag_id": {"type": "string", "description": "DAG ID (full UUID or 8-char prefix)"},
                    "node_name": {"type": "string", "description": "Node name (for retry_node)"},
                },
                "required": ["action"],
            },
        },
        frames=["conversation", "debug", "task", "question"],
    )

    logger.info("F038: Registered dag_create and dag_manage tools")
```

Note: The `dispatcher.register()` call takes a `frames` keyword — check the existing `register_heartbeat_tools` function for the exact signature. If `frames` is not a parameter, pass it via the schema dict or adjust to match the existing pattern.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_dag_tools.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add nous/api/tools.py tests/test_dag_tools.py
git commit -m "feat(f038): add dag_create and dag_manage tools"
```

---

## Task 8: Config + Main Wiring

**Files:**
- Modify: `nous/config.py` — add `NOUS_DAG_ENABLED`
- Modify: `nous/main.py` — wire DAGStore, DAGOrchestrator, register tools

- [ ] **Step 1: Add config setting**

In `nous/config.py`, find the heartbeat settings section and add nearby:

```python
# F038: DAG Orchestration
dag_enabled: bool = Field(default=True, alias="NOUS_DAG_ENABLED")
```

- [ ] **Step 2: Wire in `nous/main.py`**

After the heartbeat runner wiring block (after `await heartbeat_runner.start()`) and before the tool registration block, add:

```python
    # F038: DAG Orchestration
    dag_orchestrator = None
    if settings.dag_enabled:
        try:
            from nous.dag.store import DAGStore
            from nous.dag.orchestrator import DAGOrchestrator

            dag_store = DAGStore(database, agent_id=settings.agent_id)
            dag_orchestrator = DAGOrchestrator(
                store=dag_store,
                subtask_mgr=heart.subtasks if hasattr(heart, 'subtasks') else None,
                dynamic_loader=dynamic_loader if 'dynamic_loader' in dir() else None,
                bus=bus,
            )

            # Wire into heartbeat runner if available
            if heartbeat_runner is not None:
                heartbeat_runner.dag_orchestrator = dag_orchestrator
                logger.info("F038: DAG orchestrator wired to heartbeat runner")

            # Register tools
            from nous.api.tools import register_dag_tools
            register_dag_tools(dispatcher, dag_store, dag_orchestrator)

            logger.info("F038: DAG orchestration enabled")
        except ImportError:
            logger.debug("F038: DAG module not available yet")
```

Add `dag_orchestrator` to the return dict:

```python
"dag_orchestrator": dag_orchestrator,
```

- [ ] **Step 3: Hook into heartbeat runner loop**

In `nous/heartbeat/runner.py`, add to `__init__`:

```python
self.dag_orchestrator: Any | None = None
```

In `_loop()`, after the `await self._tick()` call and before the dynamic check sync block, add:

```python
                # F038: Advance DAG orchestrator
                if self.dag_orchestrator is not None:
                    try:
                        await self.dag_orchestrator.tick()
                    except Exception:
                        logger.exception("F038: DAG orchestrator tick failed")
```

- [ ] **Step 4: Run existing tests to verify nothing breaks**

Run: `uv run pytest tests/ -x -q --timeout=60`
Expected: All existing tests still pass

- [ ] **Step 5: Commit**

```bash
git add nous/config.py nous/main.py nous/heartbeat/runner.py
git commit -m "feat(f038): wire DAG orchestrator into main and heartbeat runner"
```

---

## Task 9: Dashboard Backend

**Files:**
- Modify: `nous/api/dashboard_queries.py` — add `get_dag_dashboard_data()`
- Modify: `nous/api/rest.py` — add `GET /dashboard/dag` endpoint
- Create: `tests/test_dag_dashboard.py`

- [ ] **Step 1: Write dashboard endpoint test**

```python
"""Tests for DAG dashboard endpoint."""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock

from nous.dag.store import DAGStore
from nous.dag.schemas import DAGCreateRequest, DAGNodeSpec, DAGEdgeSpec
from nous.storage.database import Database


@pytest_asyncio.fixture
async def store(test_database: Database) -> DAGStore:
    return DAGStore(test_database, agent_id="test-agent")


@pytest.mark.asyncio
async def test_get_dag_dashboard_data_empty(test_database: Database):
    from nous.api.dashboard_queries import get_dag_dashboard_data
    async with test_database.session() as session:
        data = await get_dag_dashboard_data(session, "test-agent")
    assert data["stats"]["active_count"] == 0
    assert data["active_dags"] == []
    assert data["recent_dags"] == []


@pytest.mark.asyncio
async def test_get_dag_dashboard_data_with_dag(store: DAGStore, test_database: Database):
    req = DAGCreateRequest(
        name="dashboard-test",
        nodes=[
            DAGNodeSpec(name="a", type="subtask", instructions="A"),
            DAGNodeSpec(name="b", type="check", instructions="B"),
        ],
        edges=[DAGEdgeSpec(from_node="a", to_node="b")],
    )
    dag = await store.create(req)
    await store.update_dag_status(dag.id, "running")

    from nous.api.dashboard_queries import get_dag_dashboard_data
    async with test_database.session() as session:
        data = await get_dag_dashboard_data(session, "test-agent")

    assert data["stats"]["active_count"] == 1
    assert len(data["active_dags"]) == 1
    assert data["active_dags"][0]["name"] == "dashboard-test"
    assert len(data["active_dags"][0]["nodes"]) == 2
    assert len(data["active_dags"][0]["edges"]) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_dag_dashboard.py -v`
Expected: `ImportError: cannot import name 'get_dag_dashboard_data'`

- [ ] **Step 3: Add `get_dag_dashboard_data()` to `nous/api/dashboard_queries.py`**

```python
async def get_dag_dashboard_data(session: AsyncSession, agent_id: str) -> dict:
    """F038: Query DAG dashboard data."""
    from sqlalchemy import text

    # Active DAGs with nodes and edges
    active_rows = (await session.execute(text("""
        SELECT d.id, d.name, d.description, d.status, d.source,
               d.created_at, d.started_at, d.completed_at,
               d.token_budget, d.tokens_consumed, d.result_summary,
               d.postmortem
        FROM nous_system.execution_dags d
        WHERE d.agent_id = :agent_id
          AND d.status IN ('pending', 'running')
        ORDER BY d.created_at
    """), {"agent_id": agent_id})).fetchall()

    active_dags = []
    for row in active_rows:
        dag_id = str(row.id)
        nodes = (await session.execute(text("""
            SELECT id, name, description, node_type, wave, status,
                   instructions, subtask_id, check_name,
                   result, error, tokens_used, started_at, completed_at
            FROM nous_system.dag_nodes
            WHERE dag_id = :dag_id
            ORDER BY wave, name
        """), {"dag_id": dag_id})).fetchall()

        edges = (await session.execute(text("""
            SELECT id, from_node_id, to_node_id, edge_type
            FROM nous_system.dag_edges
            WHERE dag_id = :dag_id
        """), {"dag_id": dag_id})).fetchall()

        active_dags.append({
            "id": dag_id,
            "name": row.name,
            "description": row.description,
            "status": row.status,
            "source": row.source,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "token_budget": row.token_budget,
            "tokens_consumed": row.tokens_consumed,
            "nodes": [{
                "id": str(n.id),
                "name": n.name,
                "description": n.description,
                "node_type": n.node_type,
                "wave": n.wave,
                "status": n.status,
                "result": (n.result or "")[:200],
                "error": (n.error or "")[:200],
                "tokens_used": n.tokens_used,
                "started_at": n.started_at.isoformat() if n.started_at else None,
                "completed_at": n.completed_at.isoformat() if n.completed_at else None,
            } for n in nodes],
            "edges": [{
                "id": str(e.id),
                "from_node_id": str(e.from_node_id),
                "to_node_id": str(e.to_node_id),
                "edge_type": e.edge_type,
            } for e in edges],
        })

    # Recent completed/failed/cancelled DAGs
    recent_rows = (await session.execute(text("""
        SELECT d.id, d.name, d.status, d.source,
               d.created_at, d.completed_at,
               d.token_budget, d.tokens_consumed, d.result_summary,
               d.postmortem,
               (SELECT count(*) FROM nous_system.dag_nodes n WHERE n.dag_id = d.id) as node_count,
               (SELECT count(*) FROM nous_system.dag_nodes n WHERE n.dag_id = d.id AND n.status = 'completed') as completed_count
        FROM nous_system.execution_dags d
        WHERE d.agent_id = :agent_id
          AND d.status NOT IN ('pending', 'running')
        ORDER BY d.completed_at DESC NULLS LAST
        LIMIT 20
    """), {"agent_id": agent_id})).fetchall()

    recent_dags = [{
        "id": str(r.id),
        "name": r.name,
        "status": r.status,
        "source": r.source,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        "token_budget": r.token_budget,
        "tokens_consumed": r.tokens_consumed,
        "result_summary": r.result_summary,
        "postmortem": r.postmortem,
        "node_count": r.node_count,
        "completed_count": r.completed_count,
    } for r in recent_rows]

    # Stats
    stats_row = (await session.execute(text("""
        SELECT
            count(*) FILTER (WHERE status IN ('pending', 'running')) as active_count,
            count(*) FILTER (WHERE status = 'completed'
                AND completed_at > now() - interval '24 hours') as completed_24h,
            count(*) FILTER (WHERE status IN ('completed', 'failed', 'cancelled', 'partial')) as total_finished,
            count(*) FILTER (WHERE status = 'completed') as total_completed,
            avg(EXTRACT(EPOCH FROM (completed_at - created_at)))
                FILTER (WHERE status = 'completed' AND completed_at IS NOT NULL) as avg_completion_seconds
        FROM nous_system.execution_dags
        WHERE agent_id = :agent_id
    """), {"agent_id": agent_id})).fetchone()

    # Nodes completed in 24h
    nodes_24h = (await session.execute(text("""
        SELECT count(*)
        FROM nous_system.dag_nodes n
        JOIN nous_system.execution_dags d ON d.id = n.dag_id
        WHERE d.agent_id = :agent_id
          AND n.status = 'completed'
          AND n.completed_at > now() - interval '24 hours'
    """), {"agent_id": agent_id})).scalar() or 0

    total_finished = stats_row.total_finished or 0
    total_completed = stats_row.total_completed or 0
    success_rate = (total_completed / total_finished) if total_finished > 0 else 0.0

    return {
        "active_dags": active_dags,
        "recent_dags": recent_dags,
        "stats": {
            "active_count": stats_row.active_count or 0,
            "nodes_completed_24h": nodes_24h,
            "success_rate": round(success_rate, 3),
            "avg_completion_seconds": round(stats_row.avg_completion_seconds or 0, 1),
        },
    }
```

- [ ] **Step 4: Add REST endpoint to `nous/api/rest.py`**

Find the `dashboard_heartbeat` endpoint function and add after it:

```python
    async def dashboard_dag(request: Request) -> JSONResponse:
        """GET /dashboard/dag — DAG orchestration dashboard data."""
        try:
            from nous.api.dashboard_queries import get_dag_dashboard_data

            async with database.session() as session:
                data = await get_dag_dashboard_data(session, settings.agent_id)
            return JSONResponse(data)
        except Exception as e:
            logger.error("Dashboard DAG error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)
```

Add the route registration. Find the existing route list and add before the static mount:

```python
        Route("/dashboard/dag", dashboard_dag),
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_dag_dashboard.py -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add nous/api/dashboard_queries.py nous/api/rest.py tests/test_dag_dashboard.py
git commit -m "feat(f038): add DAG dashboard backend endpoint"
```

---

## Task 10: Dashboard Frontend

**Files:**
- Create: `static/dashboard/js/dag.js`
- Modify: `static/dashboard/index.html`
- Modify: `static/dashboard/css/dashboard.css`

- [ ] **Step 1: Add nav link and view container to `index.html`**

Find the last `nav-link` entry (likely observability or cache) and add after it:

```html
<a href="#/dag" class="nav-link" data-view="dag">
    <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <circle cx="5" cy="6" r="2"/><circle cx="12" cy="6" r="2"/><circle cx="19" cy="18" r="2"/>
        <circle cx="5" cy="18" r="2"/><circle cx="12" cy="18" r="2"/>
        <line x1="5" y1="8" x2="5" y2="16"/><line x1="12" y1="8" x2="12" y2="16"/>
        <line x1="7" y1="7" x2="17" y2="17"/><line x1="14" y1="7" x2="17" y2="16"/>
    </svg>
    <span>DAG Orchestrator</span>
</a>
```

Add the view container (after the last `<div id="view-..." class="view">` block):

```html
<div id="view-dag" class="view"></div>
```

Add the script include (after the last `<script src="js/...">` tag):

```html
<script src="js/dag.js"></script>
```

- [ ] **Step 2: Add DAG-specific CSS to `dashboard.css`**

Append to the end of `static/dashboard/css/dashboard.css`:

```css
/* F038: DAG Orchestrator Dashboard */
.dag-graph-container {
    position: relative;
    width: 100%;
    min-height: 400px;
    background: var(--surface);
    border-radius: var(--radius);
    border: 1px solid rgba(124,106,247,0.1);
    overflow: hidden;
}
.dag-graph-container svg { width: 100%; height: 100%; }
.dag-node { cursor: pointer; transition: opacity 0.2s; }
.dag-node:hover { opacity: 0.85; }
.dag-node-label {
    font-family: var(--font-mono);
    font-size: 11px;
    fill: var(--text);
    text-anchor: middle;
    pointer-events: none;
}
.dag-edge { fill: none; stroke-width: 1.5; }
.dag-edge.dependency { stroke: var(--muted); }
.dag-edge.cancel_cascade { stroke: var(--red); stroke-dasharray: 6 3; }
.dag-edge.context_flow { stroke: var(--accent); stroke-dasharray: 3 3; }
.dag-edge-arrow { fill: var(--muted); }
.dag-wave-label {
    font-family: var(--font-ui);
    font-size: 10px;
    fill: var(--muted);
    text-anchor: start;
}
.dag-node-detail {
    position: absolute;
    top: 12px;
    right: 12px;
    width: 280px;
    background: var(--bg);
    border: 1px solid rgba(124,106,247,0.15);
    border-radius: var(--radius-sm);
    padding: 16px;
    font-size: 13px;
    z-index: 10;
    display: none;
}
.dag-node-detail.visible { display: block; }
.dag-node-detail h4 { margin: 0 0 8px; color: var(--accent); }
.dag-node-detail .detail-row { display: flex; justify-content: space-between; margin: 4px 0; }
.dag-node-detail .detail-label { color: var(--muted); }
.dag-progress {
    height: 6px;
    background: rgba(255,255,255,0.05);
    border-radius: 3px;
    overflow: hidden;
}
.dag-progress-fill {
    height: 100%;
    border-radius: 3px;
    transition: width 0.3s ease;
}
.dag-list-table { width: 100%; border-collapse: collapse; }
.dag-list-table th { text-align: left; color: var(--muted); font-weight: 500; padding: 8px 12px; font-size: 12px; border-bottom: 1px solid rgba(255,255,255,0.06); }
.dag-list-table td { padding: 10px 12px; font-size: 13px; border-bottom: 1px solid rgba(255,255,255,0.03); }
.dag-list-table tr:hover td { background: rgba(124,106,247,0.03); }
@keyframes pulse-running { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
.badge-running { animation: pulse-running 2s ease-in-out infinite; }
```

- [ ] **Step 3: Create `static/dashboard/js/dag.js`**

```javascript
/* F038: DAG Orchestrator Dashboard */

Dashboard.registerView('dag', async function(container) {
    Dashboard.showLoading(container);
    try {
        var data = await Dashboard.apiGet('/dashboard/dag');
        renderDag(container, data);
        startDagAutoRefresh(container);
    } catch (err) {
        Dashboard.showError(container, 'Failed to load DAG data', function() {
            Dashboard.reloadView('dag');
        });
    }
});

var _dagRefreshInterval = null;
var _dagSelectedDag = null;

function startDagAutoRefresh(container) {
    if (_dagRefreshInterval) clearInterval(_dagRefreshInterval);
    _dagRefreshInterval = setInterval(async function() {
        if (Dashboard.currentView !== 'dag') {
            clearInterval(_dagRefreshInterval);
            _dagRefreshInterval = null;
            return;
        }
        try {
            var data = await Dashboard.apiGet('/dashboard/dag');
            renderDag(container, data);
        } catch (e) { /* ignore refresh errors */ }
    }, 15000);
}

function renderDag(container, data) {
    // Destroy old charts
    if (Dashboard.charts['dag']) {
        Dashboard.charts['dag'].forEach(function(c) { try { c.destroy(); } catch(e) {} });
    }
    Dashboard.charts['dag'] = [];

    var stats = data.stats;
    var html = '<div class="view-header"><h1>DAG Orchestrator</h1><p class="subtitle">Unified task orchestration with dependency tracking</p></div>';

    // Stat cards
    html += '<div class="stat-grid">';
    html += buildStatCard('Active DAGs', stats.active_count, null, 'var(--accent)');
    html += buildStatCard('Nodes (24h)', stats.nodes_completed_24h, null, 'var(--green)');
    html += buildStatCard('Success Rate', (stats.success_rate * 100).toFixed(1) + '%', null,
        stats.success_rate >= 0.8 ? 'var(--green)' : stats.success_rate >= 0.5 ? 'var(--yellow)' : 'var(--red)');
    html += buildStatCard('Avg Duration', humanizeDuration(stats.avg_completion_seconds), null, 'var(--muted)');
    html += '</div>';

    // Active DAGs section
    html += '<div class="chart-card" style="margin-top:20px"><h3>Active DAGs</h3>';
    if (data.active_dags.length === 0) {
        html += '<div class="empty-state">No active DAGs</div>';
    } else {
        html += '<table class="dag-list-table"><thead><tr><th>Name</th><th>Status</th><th>Source</th><th>Progress</th><th>Created</th><th>Actions</th></tr></thead><tbody>';
        data.active_dags.forEach(function(dag) {
            var completed = dag.nodes.filter(function(n) { return n.status === 'completed'; }).length;
            var total = dag.nodes.length;
            var pct = total > 0 ? Math.round(completed / total * 100) : 0;
            html += '<tr>';
            html += '<td><strong>' + esc(dag.name) + '</strong><br><span style="color:var(--muted);font-size:11px">' + dag.id.substring(0,8) + '</span></td>';
            html += '<td>' + statusBadge(dag.status) + '</td>';
            html += '<td>' + esc(dag.source) + '</td>';
            html += '<td><div class="dag-progress"><div class="dag-progress-fill" style="width:' + pct + '%;background:var(--green)"></div></div><span style="font-size:11px;color:var(--muted)">' + completed + '/' + total + '</span></td>';
            html += '<td>' + Dashboard.formatDateTime(dag.created_at) + '</td>';
            html += '<td><button class="btn-sm dag-view-btn" data-dag-id="' + dag.id + '">View Graph</button></td>';
            html += '</tr>';
        });
        html += '</tbody></table>';
    }
    html += '</div>';

    // DAG Graph section (shown when a DAG is selected)
    html += '<div id="dag-graph-section" class="chart-card" style="margin-top:20px;display:none">';
    html += '<div style="display:flex;justify-content:space-between;align-items:center"><h3 id="dag-graph-title">DAG Graph</h3><button class="btn-sm" id="dag-close-graph">Close</button></div>';
    html += '<div class="dag-graph-container" id="dag-graph-canvas" style="height:450px"></div>';
    html += '<div class="dag-node-detail" id="dag-node-detail"></div>';
    html += '</div>';

    // Recent DAGs
    html += '<div class="chart-card" style="margin-top:20px"><h3>Recent DAGs</h3>';
    if (data.recent_dags.length === 0) {
        html += '<div class="empty-state">No completed DAGs yet</div>';
    } else {
        html += '<table class="dag-list-table"><thead><tr><th>Name</th><th>Status</th><th>Nodes</th><th>Tokens</th><th>Completed</th></tr></thead><tbody>';
        data.recent_dags.forEach(function(dag) {
            html += '<tr>';
            html += '<td>' + esc(dag.name) + '</td>';
            html += '<td>' + statusBadge(dag.status) + '</td>';
            html += '<td>' + (dag.completed_count || 0) + '/' + (dag.node_count || 0) + '</td>';
            html += '<td>' + Dashboard.formatNumber(dag.tokens_consumed || 0) + '</td>';
            html += '<td>' + Dashboard.formatDateTime(dag.completed_at) + '</td>';
            html += '</tr>';
        });
        html += '</tbody></table>';
    }
    html += '</div>';

    // Budget chart for active DAGs with budgets
    var budgetDags = data.active_dags.filter(function(d) { return d.token_budget; });
    if (budgetDags.length > 0) {
        html += '<div class="chart-card" style="margin-top:20px"><h3>Token Budget</h3><canvas id="dag-budget-chart" height="200"></canvas></div>';
    }

    container.innerHTML = html;

    // Wire view graph buttons
    container.querySelectorAll('.dag-view-btn').forEach(function(btn) {
        btn.addEventListener('click', function() {
            var dagId = this.getAttribute('data-dag-id');
            var dag = data.active_dags.find(function(d) { return d.id === dagId; });
            if (dag) showDagGraph(dag);
        });
    });

    // Wire close graph
    var closeBtn = document.getElementById('dag-close-graph');
    if (closeBtn) {
        closeBtn.addEventListener('click', function() {
            document.getElementById('dag-graph-section').style.display = 'none';
            _dagSelectedDag = null;
        });
    }

    // Budget chart
    if (budgetDags.length > 0) {
        var canvas = document.getElementById('dag-budget-chart');
        if (canvas) {
            var chart = new Chart(canvas, {
                type: 'bar',
                data: {
                    labels: budgetDags.map(function(d) { return d.name; }),
                    datasets: [
                        { label: 'Used', data: budgetDags.map(function(d) { return d.tokens_consumed; }), backgroundColor: 'rgba(124,106,247,0.7)' },
                        { label: 'Remaining', data: budgetDags.map(function(d) { return Math.max(0, d.token_budget - d.tokens_consumed); }), backgroundColor: 'rgba(255,255,255,0.05)' },
                    ],
                },
                options: { responsive: true, scales: { x: { stacked: true }, y: { stacked: true, beginAtZero: true } }, plugins: { legend: { position: Dashboard.legendPosition() } } },
            });
            Dashboard.trackChart(chart);
        }
    }

    // Restore selected DAG
    if (_dagSelectedDag) {
        var dag = data.active_dags.find(function(d) { return d.id === _dagSelectedDag; });
        if (dag) showDagGraph(dag);
    }
}

function showDagGraph(dag) {
    _dagSelectedDag = dag.id;
    var section = document.getElementById('dag-graph-section');
    var title = document.getElementById('dag-graph-title');
    var canvas = document.getElementById('dag-graph-canvas');
    var detailPanel = document.getElementById('dag-node-detail');

    section.style.display = 'block';
    title.textContent = dag.name;
    detailPanel.classList.remove('visible');

    // Clear previous
    canvas.innerHTML = '';

    // D3 force-directed graph
    var width = canvas.clientWidth || 800;
    var height = 450;
    var nodeById = {};
    dag.nodes.forEach(function(n) { nodeById[n.id] = n; });

    // Compute wave positions
    var waves = {};
    dag.nodes.forEach(function(n) { waves[n.wave] = (waves[n.wave] || 0) + 1; });
    var maxWave = Math.max.apply(null, Object.keys(waves).map(Number));
    var waveX = {};
    for (var w = 0; w <= maxWave; w++) { waveX[w] = (w + 0.5) * (width / (maxWave + 1)); }

    var nodes = dag.nodes.map(function(n, i) {
        var waveNodes = dag.nodes.filter(function(nn) { return nn.wave === n.wave; });
        var idx = waveNodes.indexOf(n);
        var total = waveNodes.length;
        return {
            id: n.id, name: n.name, type: n.node_type, status: n.status,
            wave: n.wave, data: n,
            x: waveX[n.wave],
            y: (idx + 1) * (height / (total + 1)),
        };
    });

    var links = dag.edges.map(function(e) {
        return { source: e.from_node_id, target: e.to_node_id, type: e.edge_type };
    });

    var svg = d3.select(canvas).append('svg')
        .attr('width', width).attr('height', height)
        .attr('viewBox', '0 0 ' + width + ' ' + height);

    // Arrow marker
    svg.append('defs').append('marker')
        .attr('id', 'dag-arrow').attr('viewBox', '0 0 10 10')
        .attr('refX', 20).attr('refY', 5)
        .attr('markerWidth', 6).attr('markerHeight', 6)
        .attr('orient', 'auto-start-reverse')
        .append('path').attr('d', 'M 0 0 L 10 5 L 0 10 z').attr('class', 'dag-edge-arrow');

    // Wave labels
    for (var w = 0; w <= maxWave; w++) {
        svg.append('text')
            .attr('class', 'dag-wave-label')
            .attr('x', waveX[w] - 20).attr('y', 16)
            .text('Wave ' + w);
        svg.append('line')
            .attr('x1', waveX[w] - 40).attr('y1', 24)
            .attr('x2', waveX[w] - 40).attr('y2', height - 10)
            .attr('stroke', 'rgba(255,255,255,0.03)').attr('stroke-width', 1);
    }

    // Edges
    var link = svg.selectAll('.dag-edge')
        .data(links).enter().append('line')
        .attr('class', function(d) { return 'dag-edge ' + d.type; })
        .attr('marker-end', 'url(#dag-arrow)')
        .attr('x1', function(d) { var n = nodes.find(function(n) { return n.id === d.source; }); return n ? n.x : 0; })
        .attr('y1', function(d) { var n = nodes.find(function(n) { return n.id === d.source; }); return n ? n.y : 0; })
        .attr('x2', function(d) { var n = nodes.find(function(n) { return n.id === d.target; }); return n ? n.x : 0; })
        .attr('y2', function(d) { var n = nodes.find(function(n) { return n.id === d.target; }); return n ? n.y : 0; });

    // Nodes
    var nodeColors = {
        pending: '#6b6b8a', ready: '#60a5fa', running: '#fbbf24',
        completed: '#34d399', failed: '#f87171', blocked: '#991b1b', cancelled: '#4b5563'
    };
    var nodeShapes = {
        subtask: function(g, r) { g.append('circle').attr('r', r); },
        check: function(g, r) { g.append('rect').attr('x', -r).attr('y', -r).attr('width', r*2).attr('height', r*2).attr('transform', 'rotate(45)').attr('rx', 2); },
        gate: function(g, r) {
            var pts = [];
            for (var i = 0; i < 6; i++) { var a = Math.PI/3*i - Math.PI/6; pts.push(r*Math.cos(a)+','+r*Math.sin(a)); }
            g.append('polygon').attr('points', pts.join(' '));
        },
        callback: function(g, r) { g.append('polygon').attr('points', '0,-'+r+' '+r+','+r+' -'+r+','+r); },
    };

    var nodeG = svg.selectAll('.dag-node')
        .data(nodes).enter().append('g')
        .attr('class', function(d) { return 'dag-node' + (d.status === 'running' ? ' badge-running' : ''); })
        .attr('transform', function(d) { return 'translate(' + d.x + ',' + d.y + ')'; });

    nodeG.each(function(d) {
        var g = d3.select(this);
        var shapeFn = nodeShapes[d.type] || nodeShapes.subtask;
        shapeFn(g, 18);
        g.select('circle, rect, polygon')
            .attr('fill', nodeColors[d.status] || '#6b6b8a')
            .attr('stroke', 'rgba(255,255,255,0.15)')
            .attr('stroke-width', 1.5);
    });

    nodeG.append('text')
        .attr('class', 'dag-node-label')
        .attr('dy', 30)
        .text(function(d) { return d.name; });

    // Click handler for node detail
    nodeG.on('click', function(event, d) {
        var n = d.data;
        detailPanel.innerHTML = '<h4>' + esc(n.name) + '</h4>' +
            '<div class="detail-row"><span class="detail-label">Type</span><span>' + n.node_type + '</span></div>' +
            '<div class="detail-row"><span class="detail-label">Status</span><span>' + statusBadge(n.status) + '</span></div>' +
            '<div class="detail-row"><span class="detail-label">Wave</span><span>' + n.wave + '</span></div>' +
            (n.started_at ? '<div class="detail-row"><span class="detail-label">Started</span><span>' + Dashboard.formatDateTime(n.started_at) + '</span></div>' : '') +
            (n.completed_at ? '<div class="detail-row"><span class="detail-label">Completed</span><span>' + Dashboard.formatDateTime(n.completed_at) + '</span></div>' : '') +
            (n.tokens_used ? '<div class="detail-row"><span class="detail-label">Tokens</span><span>' + Dashboard.formatNumber(n.tokens_used) + '</span></div>' : '') +
            (n.result ? '<div style="margin-top:8px"><span class="detail-label">Result</span><pre style="font-size:11px;margin:4px 0;white-space:pre-wrap;color:var(--text)">' + esc(n.result) + '</pre></div>' : '') +
            (n.error ? '<div style="margin-top:8px"><span class="detail-label" style="color:var(--red)">Error</span><pre style="font-size:11px;margin:4px 0;white-space:pre-wrap;color:var(--red)">' + esc(n.error) + '</pre></div>' : '');
        detailPanel.classList.add('visible');
    });
}

// Helpers
function statusBadge(status) {
    var colors = {
        pending: 'var(--muted)', running: 'var(--yellow)', completed: 'var(--green)',
        failed: 'var(--red)', cancelled: 'var(--muted)', blocked: '#991b1b', partial: 'var(--yellow)',
        ready: 'var(--accent)',
    };
    var cls = status === 'running' ? ' badge-running' : '';
    return '<span class="badge' + cls + '" style="background:' + (colors[status] || 'var(--muted)') + '">' + status + '</span>';
}

function buildStatCard(label, value, sub, color) {
    return '<div class="stat-card"><div class="stat-value" style="color:' + color + '">' + value + '</div><div class="stat-label">' + label + '</div>' + (sub ? '<div class="stat-sub">' + sub + '</div>' : '') + '</div>';
}

function humanizeDuration(seconds) {
    if (!seconds || seconds === 0) return '—';
    if (seconds < 60) return Math.round(seconds) + 's';
    if (seconds < 3600) return Math.round(seconds / 60) + 'm';
    return (seconds / 3600).toFixed(1) + 'h';
}

function esc(s) {
    if (!s) return '';
    var d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
}
```

- [ ] **Step 4: Verify by visual inspection**

Start the dev server and navigate to the dashboard. The DAG tab should appear in the sidebar navigation and load with empty state (no active DAGs).

Run: `uv run python -m nous.main` (confirm no import errors)

- [ ] **Step 5: Commit**

```bash
git add static/dashboard/js/dag.js static/dashboard/index.html static/dashboard/css/dashboard.css
git commit -m "feat(f038): add DAG orchestrator dashboard frontend"
```

---

## Task 11: Full Test Suite Run

- [ ] **Step 1: Run the complete test suite**

Run: `uv run pytest tests/ -v --timeout=120 -x`
Expected: All tests pass, including the new DAG tests

- [ ] **Step 2: Run linting**

Run: `uv run ruff check nous/dag/ tests/test_dag_*.py`
Expected: No errors

- [ ] **Step 3: Fix any issues found and re-run**

If any tests fail or lint issues exist, fix them and re-run until clean.

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "fix(f038): resolve test and lint issues"
```

---

## Dependency Graph

```
Task 1 (Migration) ─┐
                     ├─► Task 2 (ORM) ─► Task 3 (Schemas) ─┐
                     │                                       │
                     │                  ┌────────────────────┤
                     │                  │                    │
                     │           Task 4 (Store) ──► Task 5 (Orchestrator)
                     │                  │                    │
                     │           Task 6 (SubtaskMgr.get)     │
                     │                  │                    │
                     │                  ├────────────────────┤
                     │                  │                    │
                     │           Task 7 (Tools) ◄────────────┘
                     │                  │
                     │           Task 8 (Config + Wiring)
                     │                  │
                     │           Task 9 (Dashboard Backend)
                     │                  │
                     │           Task 10 (Dashboard Frontend)
                     │                  │
                     └──────────► Task 11 (Full Test Suite)
```

Tasks 1-3 are sequential. Tasks 4-6 can run in parallel after Task 3. Tasks 7-8 depend on 4-6. Tasks 9-10 depend on 8. Task 11 is the final validation.
