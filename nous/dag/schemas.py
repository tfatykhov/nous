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
    # F066.1 — fix-stage recovery. Fix nodes are dispatched by the
    # orchestrator's _try_fix_failed_nodes hook (NOT by _find_ready_nodes /
    # _dispatch_ready_nodes), so they sit in 'pending' until their parent
    # transitions to 'failed'.
    fix = "fix"
    # Harness Phase 3 — waits durably on a person's answer on a companion card.
    approval = "approval"


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
    # Harness Phase 3 — an approval node parked on a person's answer.
    awaiting_input = "awaiting_input"
    completed = "completed"
    failed = "failed"
    blocked = "blocked"
    cancelled = "cancelled"
    # F066.1 — terminal state set by fix-node action 'skip_and_continue'.
    # Treated like 'completed' for dependency resolution; distinguished in
    # telemetry so operators can see which nodes were intentionally bypassed.
    skipped = "skipped"


# ---------------------------------------------------------------------------
# Edge / Node specs
# ---------------------------------------------------------------------------

EdgeType = Literal["dependency", "cancel_cascade", "context_flow", "on_failure"]

# Harness Phase 3 §3.8: the ONE predecessor-edge set. Readiness, wave
# computation, failure propagation and retry's unblock all read it — the last
# two used to follow `dependency` alone, so a failed node's context_flow-only
# successor stayed pending forever and wedged its DAG `running`.
PREDECESSOR_EDGE_TYPES: frozenset[str] = frozenset({"dependency", "context_flow"})


# F066.1 — vocabulary for the `fix_actions` field on fix nodes.
FixAction = Literal[
    "retry_as_is",
    "retry_with_amended_prompt",
    "mark_unrecoverable",
    "skip_and_continue",
]
_VALID_FIX_ACTIONS = frozenset(
    {"retry_as_is", "retry_with_amended_prompt", "mark_unrecoverable", "skip_and_continue"}
)


class DAGEdgeSpec(BaseModel):
    """Specification for an edge between two DAG nodes."""

    from_node: str = Field(..., min_length=1, description="Source node name")
    to_node: str = Field(..., min_length=1, description="Target node name")
    edge_type: EdgeType = "dependency"


# Harness Phase 3 §3.1.
APPROVAL_QUESTION_MAX_CHARS = 2000
APPROVAL_MIN_WAIT_SECONDS = 900


class ApprovalOption(BaseModel):
    """One answer on an approval card."""

    id: str = Field(..., pattern=r"^[a-z0-9_-]{1,40}$")
    label: str = Field(..., min_length=1, max_length=80)
    outcome: Literal["proceed", "stop"]


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

    # F066.1 — fix-stage recovery. Set only when type=='fix'.
    parent_node: str | None = Field(
        None,
        description=(
            "F066.1: for fix nodes only — name of the node this fix attaches "
            "to. The fix fires when the parent transitions to 'failed' "
            "(after F061's bounded retry, where applicable)."
        ),
    )
    fix_actions: list[FixAction] | None = Field(
        None,
        description=(
            "F066.1: for fix nodes only — allowed action vocabulary the LLM "
            "may choose from. Must be a non-empty subset of "
            "{retry_as_is, retry_with_amended_prompt, mark_unrecoverable, "
            "skip_and_continue}."
        ),
    )
    max_fix_attempts: int = Field(
        1,
        ge=1,
        le=3,
        description=(
            "F066.1: maximum number of fix attempts per parent failure. "
            "Default 1 (one fix attempt). After exhaustion the parent stays "
            "in its terminal state."
        ),
    )
    expected_modes: list[str] = Field(
        default_factory=list,
        description=(
            "F066.1: declared failure modes for typed dispatch (Phase 2). "
            "Phase 1 ignores this field — kept in the schema for forward "
            "compatibility with the eventual typed-dispatch executor."
        ),
    )

    # Harness Phase 2.8 — undoable declaration. When true, the harness
    # enforces at runtime that every tool call from this node is compensable.
    undoable: bool = Field(
        False,
        description="Declare that this node's effects can be undone. Runtime-enforced: "
        "non-compensable tool calls are refused.",
    )

    # Harness Phase 3 — approval nodes (type='approval' only).
    options: list[ApprovalOption] | None = Field(
        None, description="2-4 answers, each 'proceed' or 'stop'; at least one of each."
    )
    default_option: str | None = Field(
        None, description="Option id applied when nobody answers by the deadline.",
    )
    recommended_option: str | None = Field(
        None, description="Option id highlighted on the card. Default: none."
    )
    answer_timeout_seconds: int | None = Field(
        None, ge=APPROVAL_MIN_WAIT_SECONDS,
        description="Seconds allowed for an answer (default NOUS_DAG_APPROVAL_DEFAULT_WAIT_SECONDS).",
    )

    @model_validator(mode="after")
    def _validate_approval_fields(self) -> DAGNodeSpec:
        approval_only = {
            "options": self.options,
            "default_option": self.default_option,
            "recommended_option": self.recommended_option,
            "answer_timeout_seconds": self.answer_timeout_seconds,
        }
        if self.type != DAGNodeType.approval:
            given = sorted(k for k, v in approval_only.items() if v)
            if given:
                raise ValueError(
                    f"Node '{self.name}': {given} are allowed only on approval nodes"
                )
            return self
        # dag_create passes every one of these as n.get(...), and LLM-authored
        # JSON routinely emits [] / "" / 0 for "none" — all falsy values count
        # as "not given"; a real value would be silently meaningless, so reject it.
        runs_nothing = {
            "tools": self.tools, "frame_type": self.frame_type, "model": self.model,
            "timeout_seconds": self.timeout_seconds,
            "stall_timeout_seconds": self.stall_timeout_seconds,
            "completion_condition": self.completion_condition,
            "completion_check": self.completion_check,
            "completion_check_interval": self.completion_check_interval,
            "max_check_attempts": self.max_check_attempts,
            "parent_node": self.parent_node, "fix_actions": self.fix_actions,
        }
        given = sorted(k for k, v in runs_nothing.items() if v)
        if given:
            raise ValueError(
                f"Approval node '{self.name}' does not take {given}: it runs nothing"
            )
        if not self.instructions.strip():
            raise ValueError(
                f"Approval node '{self.name}' needs the question in 'instructions'"
            )
        if len(self.instructions) > APPROVAL_QUESTION_MAX_CHARS:
            raise ValueError(
                f"Approval node '{self.name}': the question is capped at "
                f"{APPROVAL_QUESTION_MAX_CHARS} characters"
            )
        options = self.options or []
        if not 2 <= len(options) <= 4:
            raise ValueError(f"Approval node '{self.name}' needs 2-4 options")
        ids = [o.id for o in options]
        if len(set(ids)) != len(ids):
            raise ValueError(f"Approval node '{self.name}': option ids must be unique")
        if {o.outcome for o in options} != {"proceed", "stop"}:
            raise ValueError(
                f"Approval node '{self.name}' needs at least one 'proceed' and one 'stop' option"
            )
        by_id = {o.id: o for o in options}
        if self.default_option not in by_id:
            raise ValueError(
                f"Approval node '{self.name}': default_option must name one of {ids}"
            )
        # A 'proceed' default is allowed ONLY when the graph validator
        # confirms all downstream nodes are undoable AND the flag is on.
        # The per-node validator stores the outcome for the graph check.
        # (see validate_dag's _validate_proceed_defaults)
        if self.recommended_option is not None and self.recommended_option not in by_id:
            raise ValueError(
                f"Approval node '{self.name}': recommended_option must name one of {ids}"
            )
        return self


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

        # F064.3: insert-time sanitize node names against the workspace
        # safety regex. Loaded lazily to keep schema → settings layering
        # one-directional (settings imports schemas, not the other way).
        try:
            from nous.config import Settings as _Settings
            from nous.dag._workspace import sanitize_segment
            _s = _Settings()
            if _s.dag_workspace_safety_enabled:
                for n in self.nodes:
                    sanitize_segment(n.name)  # raises on unsafe — surfaced to caller
        except ImportError:  # pragma: no cover — defensive
            pass

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

        # --- F066.1: fix-node structural constraints ---
        nodes_by_name: dict[str, DAGNodeSpec] = {n.name: n for n in self.nodes}
        fix_nodes = [n for n in self.nodes if n.type == DAGNodeType.fix]
        for fn in fix_nodes:
            # Fix node MUST declare parent_node + fix_actions.
            if not fn.parent_node:
                raise ValueError(
                    f"Fix node '{fn.name}' must declare parent_node"
                )
            if not fn.fix_actions:
                raise ValueError(
                    f"Fix node '{fn.name}' must declare a non-empty fix_actions list"
                )
            invalid = [a for a in fn.fix_actions if a not in _VALID_FIX_ACTIONS]
            if invalid:
                raise ValueError(
                    f"Fix node '{fn.name}' has invalid actions: {invalid}. "
                    f"Allowed: {sorted(_VALID_FIX_ACTIONS)}"
                )
            # parent_node must reference a real node in the DAG.
            if fn.parent_node not in name_set:
                raise ValueError(
                    f"Fix node '{fn.name}' references unknown parent_node '{fn.parent_node}'"
                )
            # No fix-of-fix.
            parent = nodes_by_name[fn.parent_node]
            if parent.type == DAGNodeType.fix:
                raise ValueError(
                    f"Fix node '{fn.name}' has another fix node as parent — fix-of-fix is forbidden"
                )

        # At most one fix child per parent (count on_failure edges per source).
        on_failure_count_by_parent: dict[str, int] = {}
        on_failure_inbound_by_fix: dict[str, int] = {}
        for edge in self.edges:
            if edge.edge_type == "on_failure":
                # The TARGET must be a fix node; the SOURCE is the parent.
                tgt = nodes_by_name.get(edge.to_node)
                src = nodes_by_name.get(edge.from_node)
                if tgt is None or tgt.type != DAGNodeType.fix:
                    raise ValueError(
                        f"on_failure edge target '{edge.to_node}' must be a fix node"
                    )
                if src is None or src.type == DAGNodeType.fix:
                    raise ValueError(
                        f"on_failure edge source '{edge.from_node}' cannot be another fix node"
                    )
                on_failure_count_by_parent[edge.from_node] = (
                    on_failure_count_by_parent.get(edge.from_node, 0) + 1
                )
                on_failure_inbound_by_fix[edge.to_node] = (
                    on_failure_inbound_by_fix.get(edge.to_node, 0) + 1
                )

        # Every fix node MUST have exactly one on_failure inbound edge,
        # AND that edge's source MUST equal fn.parent_node.
        # Build (fix_name → set of edge sources) so we can compare.
        on_failure_sources_by_fix: dict[str, list[str]] = {}
        for edge in self.edges:
            if edge.edge_type == "on_failure":
                on_failure_sources_by_fix.setdefault(edge.to_node, []).append(edge.from_node)

        for fn in fix_nodes:
            inbound = on_failure_inbound_by_fix.get(fn.name, 0)
            if inbound != 1:
                raise ValueError(
                    f"Fix node '{fn.name}' must have exactly one on_failure "
                    f"inbound edge; found {inbound}"
                )
            # Source of the on_failure edge MUST match parent_node, or
            # runtime lookup (by parent_node string) won't find this fix
            # when the cited edge's source fails (Codex round-2 P2).
            sources = on_failure_sources_by_fix.get(fn.name, [])
            if sources and sources[0] != fn.parent_node:
                raise ValueError(
                    f"Fix node '{fn.name}' parent_node='{fn.parent_node}' "
                    f"but its on_failure edge source is '{sources[0]}'. "
                    f"These must match — the runtime fix dispatcher keys "
                    f"by parent_node string."
                )
        # At most one fix child per parent.
        for parent_name, count in on_failure_count_by_parent.items():
            if count > 1:
                raise ValueError(
                    f"Node '{parent_name}' has {count} fix children; at most "
                    "one is allowed"
                )

        # --- Harness Phase 3 §3.1: approval-node structure ---
        approval_names = {n.name for n in self.nodes if n.type == DAGNodeType.approval}
        if approval_names:
            gating = {e.from_node for e in self.edges if e.edge_type in PREDECESSOR_EDGE_TYPES}
            for name in sorted(approval_names - gating):
                raise ValueError(
                    f"Approval node '{name}' gates nothing: add a context_flow edge "
                    "from it to the node it guards"
                )
            downstream = self._downstream_of(approval_names)
            for fn in fix_nodes:
                if fn.parent_node in approval_names:
                    raise ValueError(
                        f"Fix node '{fn.name}' cannot attach to approval node "
                        f"'{fn.parent_node}': a declined answer is an answer, not a "
                        "failure to repair"
                    )
                if fn.parent_node in downstream and "retry_with_amended_prompt" in (
                    fn.fix_actions or []
                ):
                    raise ValueError(
                        f"Fix node '{fn.name}' may not use retry_with_amended_prompt: "
                        f"'{fn.parent_node}' runs under an approval, and amending its "
                        "instructions would run text nobody approved (use retry_as_is)"
                    )

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

        # --- Harness Phase 2.8: proceed-default requires all downstream undoable ---
        self._validate_proceed_defaults(nodes_by_name)

        return self

    def _validate_proceed_defaults(self, nodes_by_name: dict[str, DAGNodeSpec]) -> None:
        """A 'proceed' default on an approval node is allowed ONLY when
        ``NOUS_DAG_APPROVAL_PROCEED_DEFAULT_ENABLED`` is true and every
        downstream acting node is declared ``undoable``."""
        approvals_with_proceed_default: list[DAGNodeSpec] = []
        for n in self.nodes:
            if n.type != DAGNodeType.approval or not n.options or not n.default_option:
                continue
            by_id = {o.id: o for o in n.options}
            opt = by_id.get(n.default_option)
            if opt and opt.outcome == "proceed":
                approvals_with_proceed_default.append(n)

        if not approvals_with_proceed_default:
            return

        try:
            from nous.config import Settings as _Settings
            enabled = _Settings().dag_approval_proceed_default_enabled
        except ImportError:  # pragma: no cover
            enabled = False

        for appr in approvals_with_proceed_default:
            if not enabled:
                raise ValueError(
                    f"Approval node '{appr.name}': default_option must be a 'stop' option — "
                    "an unanswered card must never approve the action it guards "
                    "(set NOUS_DAG_APPROVAL_PROCEED_DEFAULT_ENABLED=true to allow "
                    "proceed-defaults when downstream nodes are undoable)"
                )
            downstream = self._downstream_of({appr.name})
            acting = [
                nodes_by_name[name] for name in downstream
                if name in nodes_by_name
                and nodes_by_name[name].type in (DAGNodeType.subtask, DAGNodeType.callback)
            ]
            non_undoable = [n.name for n in acting if not n.undoable]
            if non_undoable:
                raise ValueError(
                    f"Approval node '{appr.name}': default_option is a 'proceed' option, "
                    f"but downstream acting nodes {non_undoable} are not declared undoable"
                )

    def _downstream_of(self, roots: set[str]) -> set[str]:
        """Every node reachable from ``roots`` along PREDECESSOR_EDGE_TYPES."""
        adj: dict[str, list[str]] = defaultdict(list)
        for e in self.edges:
            if e.edge_type in PREDECESSOR_EDGE_TYPES:
                adj[e.from_node].append(e.to_node)
        seen: set[str] = set()
        stack = list(roots)
        while stack:
            for child in adj[stack.pop()]:
                if child not in seen:
                    seen.add(child)
                    stack.append(child)
        return seen

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
            if edge.edge_type in PREDECESSOR_EDGE_TYPES:
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
