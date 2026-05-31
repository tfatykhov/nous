"""Pydantic DTOs for all Brain inputs and outputs.

These models define the public contract for the Brain module.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

# Type aliases using Literal for compile-time validation
CategoryType = Literal["architecture", "process", "tooling", "security", "integration"]
StakesType = Literal["low", "medium", "high", "critical"]
OutcomeType = Literal["pending", "success", "partial", "failure"]
RelationType = Literal[
    "supports", "contradicts", "supersedes", "related_to", "caused_by",
    "informed_by", "evidence_for", "discussed_in", "extracted_from",
    # F070 (2026-05-25): chunk graph edges
    "part_of", "summarized_by",
]
NodeType = Literal["decision", "fact", "episode", "procedure", "chunk"]
ReasonType = Literal[
    "analysis",
    "pattern",
    "empirical",
    "authority",
    "intuition",
    "analogy",
    "elimination",
    "constraint",
]


class ReasonInput(BaseModel):
    """A single reason supporting a decision."""

    type: ReasonType
    text: str


class RecordInput(BaseModel):
    """Input for brain.record()."""

    description: str
    confidence: float = Field(ge=0.0, le=1.0)
    category: CategoryType
    stakes: StakesType
    context: str | None = None
    pattern: str | None = None
    tags: list[str] = []
    reasons: list[ReasonInput] = []
    session_id: str | None = None


class ReviewInput(BaseModel):
    """Input for brain.review(). Validates outcome to prevent opaque DB errors."""

    outcome: Literal["success", "partial", "failure"]
    result: str | None = None
    reviewer: str | None = None


class BridgeInfo(BaseModel):
    """Structure + function bridge definition for a decision."""

    structure: str | None = None
    function: str | None = None


class ThoughtInfo(BaseModel):
    """A deliberation thought attached to a decision."""

    id: UUID
    text: str
    created_at: datetime


class DecisionSummary(BaseModel):
    """Lightweight decision returned from queries."""

    id: UUID
    description: str
    confidence: float
    category: CategoryType
    stakes: StakesType
    outcome: OutcomeType
    pattern: str | None = None
    tags: list[str] = []
    score: float | None = None  # Relevance score from search
    reviewed_at: datetime | None = None
    created_at: datetime


class DecisionDetail(BaseModel):
    """Full decision with all relations."""

    id: UUID
    agent_id: str
    description: str
    context: str | None = None
    pattern: str | None = None
    confidence: float
    category: CategoryType
    stakes: StakesType
    quality_score: float | None = None
    outcome: OutcomeType
    outcome_result: str | None = None
    reviewed_at: datetime | None = None
    reviewer: str | None = None
    created_at: datetime
    updated_at: datetime
    tags: list[str] = []
    reasons: list[ReasonInput] = []
    bridge: BridgeInfo | None = None
    thoughts: list[ThoughtInfo] = []


class GuardrailResult(BaseModel):
    """Result of guardrail check."""

    allowed: bool
    blocked_by: list[str] = []  # Names of blocking guardrails
    warnings: list[str] = []  # Names of warning guardrails


class CalibrationReport(BaseModel):
    """Calibration metrics."""

    total_decisions: int
    reviewed_decisions: int
    brier_score: float | None = None
    accuracy: float | None = None
    confidence_mean: float | None = None
    confidence_stddev: float | None = None
    category_stats: dict = {}  # {category: {count, accuracy, brier}}
    reason_type_stats: dict = {}  # {type: {count, brier}}


class GraphEdgeInfo(BaseModel):
    """A graph edge between two nodes (decisions, facts, episodes, procedures)."""

    source_id: UUID
    target_id: UUID
    source_type: str = "decision"
    target_type: str = "decision"
    relation: RelationType
    weight: float
    auto_linked: bool


class NeighborResult(BaseModel):
    """A graph neighbor with edge metadata."""

    id: UUID
    node_type: str
    description: str
    score: float | None = None
    edge_relation: str
    edge_weight: float
    created_at: datetime
    # F065: provenance tier of the edge connecting this neighbor.
    # Default 'heuristic' (fail-open) for any synthetic NeighborResult
    # constructed outside _neighbors (e.g. spreading-activation rows)
    # or for rows that somehow slip through the migration with NULL.
    extraction_method: str = "heuristic"
    # Retrieval score of the SEED this neighbor was expanded from (Path A
    # graph-neighbor scoring fix). Set by run_recall_pipeline Stage 2b when
    # the neighbor is collected; consumed by _heart_graph_memory_to_pipeline
    # when graph_neighbor_seed_score_enabled. None for neighbors built outside
    # that path (e.g. decision expansion, spreading activation).
    seed_score: float | None = None
