"""F047: Goal / Project Registry — Phase 1.

Provides persistent project tracking, pre-turn resolution, and
context injection for active workstreams.
"""

from nous.projects.registry import ProjectRegistry
from nous.projects.resolver import ProjectResolver
from nous.projects.context import ProjectContextInjector
from nous.projects.schemas import (
    ProjectDetail,
    ProjectEventDetail,
    ProjectInput,
    ProjectNoteInput,
    ProjectUpdateInput,
)

__all__ = [
    "ProjectRegistry",
    "ProjectResolver",
    "ProjectContextInjector",
    "ProjectDetail",
    "ProjectEventDetail",
    "ProjectInput",
    "ProjectNoteInput",
    "ProjectUpdateInput",
]
