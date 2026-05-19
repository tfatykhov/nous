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
    timeout_seconds: int | None = Field(
        None,
        ge=1,
        description=(
            "Execution timeout (seconds). None means 'use NOUS_DAG_NODE_DEFAULT_TIMEOUT'. "
            "Values above NOUS_DAG_NODE_MAX_TIMEOUT are clamped at insert."
        ),
    )
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
    # F064.1: stall detection. Per-node override of the global default at
    # NOUS_DAG_NODE_DEFAULT_STALL_TIMEOUT. Cascade (matches
    # orchestrator._effective_stall_timeout):
    #   - None → use global default (which may itself be 0 to globally disable)
    #   - 0    → explicitly disabled for THIS node, regardless of global
    #            (Symphony §8.5 stall_timeout_ms <= 0 semantics)
    #   - >0   → use this value; clamped to NOUS_DAG_NODE_MAX_STALL_TIMEOUT
    # Must be <= the effective wall-clock timeout — enforced in DAGStore.create()
    # because it has access to Settings for the default-applies case.
    stall_timeout_seconds: int | None = Field(
        None,
        ge=0,
        description=(
            "Stall timeout (seconds). None = inherit the global default "
            "(NOUS_DAG_NODE_DEFAULT_STALL_TIMEOUT). 0 = explicitly disabled for this node. "
            "When > 0, the orchestrator fails the node if no activity ping arrived within "
            "this window. Clamped to NOUS_DAG_NODE_MAX_STALL_TIMEOUT at insert."
        ),
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
    # F064.2: per-DAG per-frame-type dispatch cap. None = no per-DAG cap
    # (operators may still set NOUS_DAG_GLOBAL_MAX_CONCURRENT_BY_FRAME).
    # Each value must be >= 1 — values < 1 would silently block the bucket.
    max_concurrent_by_frame_type: dict[str, int] | None = Field(
        None,
        description=(
            "Per-frame-type dispatch cap dict {frame_type: max_concurrent}. "
            "When NOUS_DAG_FRAME_CONCURRENCY_ENABLED=true, the orchestrator "
            "consults this dict (plus the env-var override) before launching "
            "a wave. Missing frames are uncapped; nodes with frame_type=None "
            "are bucketed under '_default'."
        ),
    )

    @model_validator(mode="after")
    def validate_dag(self) -> DAGCreateRequest:
        """Validate DAG structure: unique names, valid edges, no cycles, wave/parallel limits.

        F064.1 note: stall_timeout_seconds <= effective wall-clock timeout is
        enforced in DAGStore.create() (which has access to Settings and so
        can compare against the resolved default when timeout_seconds is
        omitted). The schema-level pure-data validator can't reach Settings
        without breaking layering — addressing codex P2 by relying on the
        store-level check exclusively avoids a half-enforcement that misses
        the default-applies case.
        """
        # F064.2: every per-frame cap must be a positive integer. A 0 would
        # silently block all DAGs of that frame — fail fast at construction.
        if self.max_concurrent_by_frame_type is not None:
            for frame, cap in self.max_concurrent_by_frame_type.items():
                if cap < 1:
                    raise ValueError(
                        f"max_concurrent_by_frame_type['{frame}']={cap} is invalid; "
                        "values must be >= 1"
                    )

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
