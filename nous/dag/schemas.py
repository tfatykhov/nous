"""F038: Pydantic schemas for DAG orchestration."""

from __future__ import annotations

from collections import defaultdict
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class DAGNodeType(str, Enum):
    """Types of DAG execution nodes."""

    subtask = "subtask"
    check = "check"
    gate = "gate"
    callback = "callback"


class DAGStatus(str, Enum):
    """Status of a DAG execution."""

    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"
    partial = "partial"


class DAGNodeStatus(str, Enum):
    """Status of an individual DAG node."""

    pending = "pending"
    ready = "ready"
    running = "running"
    awaiting_check = "awaiting_check"
    completed = "completed"
    failed = "failed"
    blocked = "blocked"
    cancelled = "cancelled"


# ---------------------------------------------------------------------------
# Edge / Node specs
# ---------------------------------------------------------------------------

EdgeType = Literal["dependency", "cancel_cascade", "context_flow"]


class DAGEdgeSpec(BaseModel):
    """Specification for an edge between two DAG nodes."""

    from_node: str = Field(..., min_length=1, description="Source node name")
    to_node: str = Field(..., min_length=1, description="Target node name")
    edge_type: EdgeType = "dependency"


class DAGNodeSpec(BaseModel):
    """Specification for a single DAG node."""

    name: str = Field(..., min_length=1, max_length=100, description="Unique node name within the DAG")
    type: DAGNodeType = Field(..., description="Node execution type")
    instructions: str = Field("", description="Instructions or prompt for this node")
    description: str = Field("", description="Human-readable description")
    tools: list[str] | None = Field(None, description="Allowed tools for this node")
    frame_type: str | None = Field(None, description="Cognitive frame type")
    model: str | None = Field(None, description="LLM model override")
    timeout_seconds: int = Field(120, ge=1, le=600, description="Execution timeout")
    completion_condition: str | None = Field(None, description="Optional completion condition")
    completion_check: str | None = Field(
        None,
        description="Shell command polled each tick. Exit 0 = success, 1 = failed, 2 = still running."
    )
    completion_check_interval: int | None = Field(
        None, ge=1,
        description="Seconds between completion check polls. Default: every tick."
    )
    max_check_attempts: int | None = Field(
        None, ge=1,
        description="Max poll attempts before failure. Default: unlimited (timeout-based)."
    )


# ---------------------------------------------------------------------------
# DAG create request
# ---------------------------------------------------------------------------

MAX_NODES = 10
MAX_WAVES = 4  # waves 0-3
MAX_PARALLEL_PER_WAVE = 4


class DAGCreateRequest(BaseModel):
    """Request to create a new DAG execution."""

    name: str = Field(..., min_length=1, max_length=200, description="DAG name")
    description: str = Field("", description="DAG description")
    source: Literal["conversation", "critic", "heartbeat", "schedule"] = "conversation"
    original_request: str | None = Field(None, description="Original user request")
    token_budget: int | None = Field(None, gt=0, description="Token budget for the entire DAG")
    nodes: list[DAGNodeSpec] = Field(..., min_length=1, description="Nodes in the DAG")
    edges: list[DAGEdgeSpec] = Field(default_factory=list, description="Edges between nodes")

    @model_validator(mode="after")
    def validate_dag(self) -> DAGCreateRequest:
        """Validate DAG structure: unique names, valid edges, no cycles, wave/parallel limits."""
        # --- max nodes ---
        if len(self.nodes) > MAX_NODES:
            raise ValueError(f"DAG cannot have more than {MAX_NODES} nodes (got {len(self.nodes)})")

        # --- unique names ---
        names = [n.name for n in self.nodes]
        name_set = set(names)
        if len(name_set) != len(names):
            dupes = [n for n in names if names.count(n) > 1]
            raise ValueError(f"Duplicate node names: {set(dupes)}")

        # --- edge references exist ---
        for edge in self.edges:
            if edge.from_node not in name_set:
                raise ValueError(f"Edge references unknown node: '{edge.from_node}'")
            if edge.to_node not in name_set:
                raise ValueError(f"Edge references unknown node: '{edge.to_node}'")
            if edge.from_node == edge.to_node:
                raise ValueError(f"Self-loop detected on node: '{edge.from_node}'")

        # --- cycle detection + wave computation ---
        waves = self.compute_waves()

        # --- max waves ---
        if waves:
            max_wave = max(waves.values())
            if max_wave >= MAX_WAVES:
                raise ValueError(
                    f"DAG exceeds maximum {MAX_WAVES} waves (0-{MAX_WAVES - 1}), "
                    f"got wave {max_wave}"
                )

        # --- max parallel per wave ---
        wave_counts: dict[int, int] = defaultdict(int)
        for w in waves.values():
            wave_counts[w] += 1
        for w, count in wave_counts.items():
            if count > MAX_PARALLEL_PER_WAVE:
                raise ValueError(
                    f"Wave {w} has {count} parallel nodes, max is {MAX_PARALLEL_PER_WAVE}"
                )

        return self

    def compute_waves(self) -> dict[str, int]:
        """Topological sort to assign wave numbers to nodes.

        Returns a dict mapping node name -> wave number.
        Raises ValueError if the graph contains a cycle.
        """
        # Build adjacency and in-degree maps (dependency + context_flow edges)
        name_set = {n.name for n in self.nodes}
        adj: dict[str, list[str]] = {n.name: [] for n in self.nodes}
        in_degree: dict[str, int] = {n.name: 0 for n in self.nodes}

        for edge in self.edges:
            if edge.edge_type in ("dependency", "context_flow"):
                adj[edge.from_node].append(edge.to_node)
                in_degree[edge.to_node] += 1

        # Kahn's algorithm
        queue = [n for n in name_set if in_degree[n] == 0]
        waves: dict[str, int] = {}

        # Assign wave 0 to all nodes with no incoming dependency edges
        for n in queue:
            waves[n] = 0

        processed = 0
        while queue:
            next_queue: list[str] = []
            for node in queue:
                processed += 1
                for neighbor in adj[node]:
                    in_degree[neighbor] -= 1
                    if in_degree[neighbor] == 0:
                        waves[neighbor] = waves[node] + 1
                        next_queue.append(neighbor)
            queue = next_queue

        if processed != len(name_set):
            raise ValueError("DAG contains a cycle")

        return waves
