"""Pydantic DTOs for the Project Registry (F047)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

ProjectStatus = Literal["active", "paused", "completed", "abandoned"]
ProjectEventType = Literal["created", "session", "milestone", "blocker", "status_change", "note"]


class ProjectInput(BaseModel):
    """Input for registering a new project."""

    name: str
    title: str
    description: str = ""
    priority: float = Field(default=0.5, ge=0.0, le=1.0)
    tags: list[str] = Field(default_factory=list)


class ProjectUpdateInput(BaseModel):
    """Input for updating an existing project."""

    status: ProjectStatus | None = None
    priority: float | None = Field(default=None, ge=0.0, le=1.0)
    description: str | None = None
    title: str | None = None
    tags: list[str] | None = None


class ProjectNoteInput(BaseModel):
    """Input for adding a note/event to a project."""

    summary: str
    event_type: ProjectEventType = "note"
    episode_id: UUID | None = None


class ProjectDetail(BaseModel):
    """Full project with all fields."""

    id: UUID
    agent_id: str
    name: str
    title: str
    description: str
    status: ProjectStatus
    priority: float
    tags: list[str]
    source_decision_id: UUID | None = None
    created_at: datetime
    updated_at: datetime
    last_touched_at: datetime
    recent_events: list[ProjectEventDetail] = Field(default_factory=list)


class ProjectEventDetail(BaseModel):
    """Project event log entry."""

    id: UUID
    project_id: UUID
    event_type: ProjectEventType
    summary: str
    episode_id: UUID | None = None
    created_at: datetime
