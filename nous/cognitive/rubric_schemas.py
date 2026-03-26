"""Pydantic DTOs for F024 Phase 3b — Self-Modifying Rubrics."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class OutcomeSignalType(str, Enum):
    CORRECTED = "corrected"
    COMPLETED = "completed"
    PRAISED = "praised"
    REWORKED = "reworked"
    SELF_CORRECTED = "self_corrected"


class RubricDimension(BaseModel):
    """A single evaluation dimension within a rubric."""
    name: str
    weight: float = Field(ge=0.0, le=1.0)
    description: str
    scoring_criteria: str
    min_weight: float = 0.10
    max_weight: float = 0.40

    @model_validator(mode="after")
    def weight_in_bounds(self) -> "RubricDimension":
        if self.weight < self.min_weight or self.weight > self.max_weight:
            raise ValueError(
                f"weight {self.weight} outside [{self.min_weight}, {self.max_weight}]"
            )
        return self


class RubricVersionDetail(BaseModel):
    """Full rubric version with all fields."""
    id: UUID
    agent_id: str
    version: str
    parent_version: str | None = None
    change_reason: str
    dimensions: list[RubricDimension]
    outcome_correlations: dict = Field(default_factory=dict)
    status: Literal["active", "superseded", "rollback"] = "active"
    created_at: datetime


class RubricVersionSummary(BaseModel):
    """Lightweight rubric version for listings."""
    id: UUID
    version: str
    status: str
    change_reason: str
    dimension_count: int
    created_at: datetime


class OutcomeSignalDetail(BaseModel):
    """A single outcome signal for an episode."""
    id: UUID
    agent_id: str
    episode_id: UUID
    signal_type: str
    confidence: float
    evidence: str | None = None
    self_improvement_scores: dict | None = None
    created_at: datetime


class CorrelationResult(BaseModel):
    """Correlation between a dimension and an outcome signal type."""
    dimension: str
    signal_type: str
    pearson_r: float
    spearman_rho: float
    sample_size: int


class CorrelationReport(BaseModel):
    """Full correlation analysis for a rubric version."""
    rubric_version: str
    correlations: list[CorrelationResult]
    suggested_weights: dict[str, float] | None = None
    suggested_splits: list[str] = Field(default_factory=list)
    suggested_merges: list[tuple[str, str]] = Field(default_factory=list)
    episode_count: int


class DimensionProposal(BaseModel):
    """Proposed new dimension for Tim's approval."""
    name: str
    description: str
    scoring_criteria: str
    evidence_episode_ids: list[UUID]
    gap_analysis: str
    suggested_weight: float = 0.15
