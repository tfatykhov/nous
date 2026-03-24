"""Pydantic models for F024 Critic Agent."""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class RoutingMode(str, Enum):
    """Critic routing decision for the current turn."""
    PASSTHROUGH = "passthrough"
    SINGLE_ADVISED = "single_advised"


class DiagnosticResult(BaseModel):
    """Result from a single diagnostic critic."""
    critic_name: str
    intervention: str
    fired: bool = False


class CriticResult(BaseModel):
    """Output from CriticAgent classification."""
    routing: RoutingMode
    recommended_frame: str
    rationale: str
    complexity: str = "moderate"
    skills: list[str] = Field(default_factory=list)
    diagnostics: list[DiagnosticResult] = Field(default_factory=list)
    heuristic_frame: str | None = None
    latency_ms: int = 0
