"""Pydantic DTOs for all Heart inputs and outputs.

These models define the public contract for the Heart module.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

# F075: strict YYYY-MM-DD shape gate. Python 3.12's date.fromisoformat()
# accepts alternate ISO forms ('20240310', '2024-W10-7') — the regex
# enforces the prompt/schema contract before the calendar-validity check.
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Type aliases using Literal for compile-time validation (P3-2)
MemoryType = Literal["fact", "procedure", "decision", "censor", "episode", "chunk"]
CensorAction = Literal["warn", "block", "absolute"]
EpisodeOutcome = Literal["success", "partial", "failure", "ongoing", "abandoned"]
ProcedureOutcome = Literal["success", "failure", "neutral"]


# --- Episodes ---


class EpisodeInput(BaseModel):
    """Input for starting a new episode."""

    title: str | None = None
    summary: str
    detail: str | None = None
    frame_used: str | None = None
    trigger: str | None = None  # user_message, cron, hook, etc.
    participants: list[str] = []
    tags: list[str] = []
    user_id: str | None = None
    user_display_name: str | None = None
    # F022 follow-up: tag the episode with the conversation session that
    # produced it so get_active_episode_id can fall back to a DB lookup
    # after process restart wipes the in-memory map.
    session_id: str | None = None


class EpisodeDetail(BaseModel):
    """Full episode with all fields."""

    id: UUID
    agent_id: str
    title: str | None
    summary: str
    detail: str | None
    started_at: datetime
    ended_at: datetime | None
    duration_seconds: int | None
    frame_used: str | None
    trigger: str | None
    participants: list[str]
    outcome: EpisodeOutcome | None
    surprise_level: float | None
    lessons_learned: list[str]
    tags: list[str]
    decision_ids: list[UUID]  # From episode_decisions join
    active: bool = True
    structured_summary: dict | None = None
    user_id: str | None = None
    user_display_name: str | None = None
    created_at: datetime
    compaction_count: int = 0


class EpisodeSummary(BaseModel):
    """Lightweight episode returned from searches and listings."""

    id: UUID
    title: str | None
    summary: str
    outcome: EpisodeOutcome | None
    started_at: datetime
    tags: list[str]
    structured_summary: dict | None = None
    score: float | None = None  # Relevance from search


# --- Facts ---


class FactInput(BaseModel):
    """Input for learning a new fact."""

    content: str
    category: str | None = None  # preference, technical, person, tool, concept, rule
    subject: str | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source: str | None = None
    source_episode_id: UUID | None = None
    source_decision_id: UUID | None = None
    contradiction_of: UUID | None = None  # P1-4: for direct insertion cases
    tags: list[str] = []
    source_timestamp: datetime | None = None  # F023: when the source info was produced
    source_text: str | None = None  # F025 P2-E: original transcript for admission grounding (not persisted)
    # F075: date-anchored event (ISO YYYY-MM-DD); None = stable fact
    event_date: date | None = None
    # F075: classification marker — set by F075-aware producer paths only
    # (summarizer/extractor with the temporal flag on, or backfill script).
    # Non-F075 callers leave this None so the backfill remains eligible.
    event_date_classified_at: datetime | None = None

    @field_validator("event_date", mode="before")
    @classmethod
    def _parse_event_date(cls, v):
        if v is None:
            return v
        # Order matters: datetime IS-A date in Python's stdlib, so test
        # datetime FIRST. A datetime input gets explicitly coerced to date
        # (DB column is DATE, not DATETIME).
        if isinstance(v, datetime):
            return v.date()
        if isinstance(v, date):
            return v
        if isinstance(v, str):
            # Regex enforces surface shape (strictly YYYY-MM-DD).
            if not _DATE_PATTERN.fullmatch(v):
                # F075: log dropped dates so LLM format drift is observable
                # rather than vanishing silently (SFH final-review Medium).
                logger.warning("F075: dropped non-YYYY-MM-DD event_date %r", v[:32])
                return None
            try:
                return date.fromisoformat(v)
            except ValueError:
                logger.warning("F075: dropped invalid-calendar event_date %r", v[:32])
                return None  # fail-soft: drop bad date, keep fact
        return None


class ContradictionWarning(BaseModel):
    """Warning returned when a potential contradiction is detected during learn().

    Attached to the returned FactDetail.contradiction_warning field.
    The new fact IS the FactDetail this warning is attached to.
    """

    existing_fact_id: UUID
    existing_content: str
    similarity: float
    message: str


class FactDetail(BaseModel):
    """Full fact with all fields."""

    id: UUID
    agent_id: str
    content: str
    category: str | None
    subject: str | None
    confidence: float
    source: str | None
    source_episode_id: UUID | None
    source_decision_id: UUID | None
    learned_at: datetime
    last_confirmed: datetime | None
    confirmation_count: int
    superseded_by: UUID | None
    contradiction_of: UUID | None  # P1-4: column exists in init.sql:255 and ORM
    active: bool
    tags: list[str]
    created_at: datetime
    contradiction_warning: ContradictionWarning | None = None
    # F047: Actionability classification
    actionable: bool | None = None
    actionable_confidence: float | None = None
    # F075: Temporal event date (None for stable facts)
    event_date: date | None = None


class FactRejected(BaseModel):
    """Returned when a fact is rejected by admission control."""

    admitted: bool = False
    content: str
    composite_score: float
    threshold: float
    scores: dict[str, float]
    explanation: str


class FactSummary(BaseModel):
    """Lightweight fact returned from searches."""

    id: UUID
    content: str
    category: str | None
    subject: str | None
    confidence: float
    active: bool
    score: float | None = None
    superseded_by: UUID | None = None
    # F047: Propagated from ORM so downstream consumers (heartbeat, etc.)
    # can act on the persisted actionability verdict without a second DB hit.
    actionable: bool | None = None
    actionable_confidence: float | None = None
    tags: list[str] = []
    # F075: Propagated from ORM for the dedup-bypass rule + Layer 3 boost.
    # Distinct event_dates between candidate and existing => distinct events.
    event_date: date | None = None
    # Gap-2: transient pre-turn recency-resolution tags set by
    # ContextEngine._resolve_recency (current/superseded + YYYY-MM). NOT persisted;
    # default None keeps every other consumer byte-identical.
    recency_status: str | None = None
    recency_date: str | None = None


# --- Procedures ---


class ProcedureInput(BaseModel):
    """Input for storing a new procedure."""

    name: str
    domain: str | None = None  # architecture, debugging, deployment, trading, research
    description: str | None = None
    goals: list[str] = []  # Upper fringe
    core_patterns: list[str] = []  # Core
    core_tools: list[str] = []  # Core
    core_concepts: list[str] = []  # Core
    implementation_notes: list[str] = []  # Lower fringe
    tags: list[str] = []
    active: bool | None = None  # None = use default (True), False = register as inactive
    # F064.4: skill runtime hints surfaced from SkillManifest. Always
    # persisted when any of {concurrency_cap, timeout_override_seconds,
    # hooks, requires_human_review} is declared on the manifest. Consumer
    # wiring deferred to F064.4-v2.
    runtime_metadata: dict | None = None


class ProcedureDetail(BaseModel):
    """Full procedure with all fields."""

    id: UUID
    agent_id: str
    name: str
    domain: str | None
    description: str | None
    goals: list[str]
    core_patterns: list[str]
    core_tools: list[str]
    core_concepts: list[str]
    implementation_notes: list[str]
    activation_count: int
    success_count: int
    failure_count: int
    neutral_count: int
    last_activated: datetime | None
    effectiveness: float | None  # success_count / (success + failure) if > 0
    related_procedures: list[UUID] = []  # P2-5: exists in DB/ORM, reserved for future
    censor_ids: list[UUID] = []  # P2-5: exists in DB/ORM, reserved for future
    tags: list[str]
    active: bool
    created_at: datetime


class ProcedureSummary(BaseModel):
    """Lightweight procedure returned from searches."""

    id: UUID
    name: str
    domain: str | None
    description: str | None = None
    activation_count: int
    effectiveness: float | None
    score: float | None = None


class EvolutionCandidate(BaseModel):
    """A procedure flagged for rewriting, retirement, or investigation (F037)."""

    id: UUID
    name: str
    category: Literal["retire", "rewrite", "investigate", "star"]
    effectiveness: float
    activation_count: int
    reason: str


# --- Censors ---


class CensorInput(BaseModel):
    """Input for adding a new censor."""

    trigger_pattern: str
    reason: str
    action: CensorAction = "warn"  # P3-2: Literal type
    domain: str | None = None
    learned_from_decision: UUID | None = None
    learned_from_episode: UUID | None = None
    trigger_action: dict | None = None  # F031: e.g. {"tool": "recall", "args": {...}}
    action_instruction: str | None = None  # F031: human-readable instruction
    unblock_pattern: str | None = None  # F031: regex — if action results match, downgrade block→warn


class CensorDetail(BaseModel):
    """Full censor with all fields."""

    id: UUID
    agent_id: str
    trigger_pattern: str
    action: CensorAction  # P3-2: Literal type
    reason: str
    domain: str | None
    learned_from_decision: UUID | None
    learned_from_episode: UUID | None
    created_by: str
    activation_count: int
    last_activated: datetime | None
    false_positive_count: int
    escalation_threshold: int
    active: bool
    created_at: datetime
    trigger_action: dict | None = None  # F031
    action_instruction: str | None = None  # F031
    unblock_pattern: str | None = None  # F031


class CensorMatch(BaseModel):
    """Result from check_censors -- a censor that matched."""

    id: UUID
    trigger_pattern: str
    action: CensorAction  # P3-2: Literal type
    reason: str
    domain: str | None
    score: float | None = None  # Search relevance score
    trigger_action: dict | None = None  # F031
    action_instruction: str | None = None  # F031
    unblock_pattern: str | None = None  # F031


# --- Working Memory ---


class WorkingMemoryItem(BaseModel):
    """A single item loaded into working memory."""

    type: MemoryType  # P3-2: Literal type
    ref_id: UUID
    summary: str
    relevance: float = Field(ge=0.0, le=1.0)
    loaded_at: datetime


class OpenThread(BaseModel):
    """An open thread (pending item) in working memory."""

    description: str
    decision_id: UUID | None = None
    priority: str = "medium"  # low, medium, high
    created_at: datetime


class WorkingMemoryState(BaseModel):
    """Current working memory session state."""

    agent_id: str
    session_id: str
    current_task: str | None
    current_frame: str | None
    items: list[WorkingMemoryItem]
    open_threads: list[OpenThread]
    max_items: int
    item_count: int


# --- Unified Recall ---


class RecallResult(BaseModel):
    """A single result from unified recall across memory types."""

    type: MemoryType  # P3-2: Literal type
    id: UUID
    summary: str
    score: float
    metadata: dict = {}  # Type-specific fields
