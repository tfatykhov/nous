"""Heart module — Memory system organ for Nous agents.

Public API: Heart class + all schema types from schemas.py.
"""

from nous.heart.heart import Heart
from nous.heart.procedures import ProcedureConflictError
from nous.heart.schemas import (
    CensorAction,
    CensorDetail,
    CensorInput,
    CensorMatch,
    ContradictionWarning,
    EpisodeDetail,
    EpisodeInput,
    EpisodeOutcome,
    EpisodeSummary,
    FactDetail,
    FactInput,
    FactSummary,
    MemoryType,
    OpenThread,
    ProcedureDetail,
    ProcedureInput,
    ProcedureOutcome,
    ProcedureSummary,
    ProcedureVersionDetail,
    RecallResult,
    WorkingMemoryItem,
    WorkingMemoryState,
)

__all__ = [
    "Heart",
    # Type aliases
    "CensorAction",
    "EpisodeOutcome",
    "MemoryType",
    "ProcedureOutcome",
    # Episodes
    "EpisodeDetail",
    "EpisodeInput",
    "EpisodeSummary",
    # Facts
    "ContradictionWarning",
    "FactDetail",
    "FactInput",
    "FactSummary",
    # Procedures
    "ProcedureDetail",
    "ProcedureInput",
    "ProcedureSummary",
    "ProcedureVersionDetail",
    "ProcedureConflictError",
    # Censors
    "CensorDetail",
    "CensorInput",
    "CensorMatch",
    # Working Memory
    "OpenThread",
    "WorkingMemoryItem",
    "WorkingMemoryState",
    # Recall
    "RecallResult",
]
