"""Pydantic DTOs for all Heart inputs and outputs.

These models define the public contract for the Heart module.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from nous.heart.keys import normalize_key

logger = logging.getLogger(__name__)

# F075: strict YYYY-MM-DD shape gate. Python 3.12's date.fromisoformat()
# accepts alternate ISO forms ('20240310', '2024-W10-7') — the regex
# enforces the prompt/schema contract before the calendar-validity check.
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_date(v: object, field: str) -> date | None:
    """The one parser for a fact's dates (event_date, valid_from, valid_to).

    A datetime becomes its date (the columns are DATE). A string must be a
    real YYYY-MM-DD day: anything else is dropped with a warning, so LLM format
    drift shows in the logs instead of vanishing, and the fact is kept.
    """
    if v is None:
        return None
    # Order matters: datetime IS-A date in Python's stdlib, so test datetime FIRST.
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        if not _DATE_PATTERN.fullmatch(v):
            logger.warning("F075: dropped non-YYYY-MM-DD %s %r", field, v[:32])
            return None
        try:
            return date.fromisoformat(v)
        except ValueError:
            logger.warning("F075: dropped invalid-calendar %s %r", field, v[:32])
            return None
    return None


def date_was_dropped(raw: dict[str, object], parsed: FactInput) -> bool:
    """True when a producer got a date from the LLM that ``parsed`` could not keep.

    ``raw`` maps date field names to what the LLM emitted. The producers then
    leave ``event_date_classified_at`` NULL, so the F075.1 backfill retries the
    row instead of treating it as classified.
    """
    return any(value is not None and getattr(parsed, name) is None for name, value in raw.items())


# Type aliases using Literal for compile-time validation (P3-2)
MemoryType = Literal["fact", "procedure", "decision", "censor", "episode", "chunk"]
CensorAction = Literal["steer", "refuse", "abort"]  # F078 (was warn|block|absolute)
CensorProvenance = Literal["auto", "agent", "human"]  # F078: caps max tier at create
EpisodeOutcome = Literal["success", "partial", "failure", "ongoing", "abandoned"]
ProcedureOutcome = Literal["success", "failure", "neutral"]
# What a rendered procedure's turn outcome rests on (procedures.outcome_attribution).
ProcedureAttribution = Literal["turn_error", "core_tool_error", "unrelated_error", "none"]
# F102: how a procedure version came to be ("backfill" is migration 092's version 1).
ProcedureChangeSource = Literal["create", "update_body", "append_notes", "dedupe_merge", "restore", "backfill"]


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
    # 2026-07-28: replaces decision_ids. heart.episode_decisions was dropped
    # (migration 068) — decisions are correlated to an episode through the
    # session they share (brain.graph_constants.episode_decisions_query).
    session_id: str | None
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

    # 064 R1: normalized conflict-slot identifiers (lowercased, punctuation-
    # stripped — see normalize_key). Drive the R2 exact-key candidate lookup.
    subject_key: str | None = None
    attribute_key: str | None = None
    entity_keys: list[str] = Field(
        default_factory=list,
        description="R3.1: normalized keys of ALL participating entities (subject + proper-noun object/value side).",
    )
    # codex P2 round 9 (flipped from round 6's default True): True only when
    # an entity-aware producer ran entity extraction for this fact — controls
    # the backfill watermark (entity_keys_extracted_at). Legacy/non-entity-
    # aware producers (fact_extractor, learn_fact tool, REST endpoint) never
    # set this, so they default to False and correctly stay unstamped —
    # stamping them would falsely mark facts they never entity-extracted as
    # "done," permanently hiding them from the backfill script. An
    # entity-aware producer (the enumerative extractor) sets this True
    # whenever its raw response included the "entities" key at all — even
    # if every candidate then failed the stop-policy and entity_keys ended
    # up empty; zero ACCEPTED keys is still a VALID completion, and gating
    # the stamp on entity_keys being non-empty (round 6's mistake) would
    # leave those facts re-sent to the LLM by every backfill run forever.
    entity_extraction_complete: bool = False
    # 064 R1: positional reading-order ordinal — chunk_index * 1_000_000 +
    # in-chunk position; explicit statement numbers from the source are never
    # used (mixed-form comparisons invert reading order). Higher = later in the
    # source. The 'ordinal' supersession policy's authority signal.
    source_ordinal: int | None = None
    # 064 R2.4: statement contradicts widely-known world knowledge; rendered
    # with an override marker when NOUS_OVERRIDE_PRIOR_MARKING_ENABLED.
    overrides_prior: bool = False
    # 097 bi-temporal facts. Event time: the days the fact was true. A one-day
    # event has valid_from = valid_to = event_date, a span or a coarse date
    # ("Feb 2026") has both and no event_date, an open state ("since Jan") has
    # no valid_to. The validator below keeps these consistent for every producer.
    valid_from: date | None = None
    valid_to: date | None = None
    # Statement time: when the fact was said, the source episode's start. Set by
    # the producers; None means unknown, and readers fall back to learned_at.
    mentioned_at: datetime | None = None

    @field_validator("event_date", "valid_from", "valid_to", mode="before")
    @classmethod
    def _parse_dates(cls, v, info: ValidationInfo):
        return _parse_date(v, info.field_name)

    @field_validator("mentioned_at", mode="after")
    @classmethod
    def _mentioned_at_is_utc_when_naive(cls, v: datetime | None) -> datetime | None:
        """A naive timestamp is taken as UTC, as every producer writes UTC. Left
        naive, asyncpg would read it in the host's local zone and shift it."""
        if v is not None and v.tzinfo is None:
            return v.replace(tzinfo=UTC)
        return v

    @model_validator(mode="after")
    def _derive_interval(self) -> FactInput:
        """Keep the row invariant: an event_date is a one-day interval.

        event_date set: valid_from = valid_to = event_date, overriding a
        different interval (logged). event_date is never derived from the
        interval. valid_to before valid_from, or without it, is dropped (logged).
        """
        if self.event_date is not None:
            point = (self.event_date, self.event_date)
            if self.valid_from is not None and (self.valid_from, self.valid_to) != point:
                logger.warning(
                    "bi-temporal: event_date %s overrides valid_from %s / valid_to %s",
                    self.event_date,
                    self.valid_from,
                    self.valid_to,
                )
            self.valid_from, self.valid_to = point
        elif self.valid_to is not None and (self.valid_from is None or self.valid_to < self.valid_from):
            logger.warning("bi-temporal: dropped valid_to %s (valid_from %s)", self.valid_to, self.valid_from)
            self.valid_to = None
        return self

    # codex P2 round 7: canonicalize conflict-slot keys at the SINGLE write
    # boundary (R3.2: one canonicalizer). Direct Heart.learn callers were
    # storing subject_key/attribute_key RAW — under v2 normalization a
    # direct "api_gateway" never exact-matches the extractor's/backfill's
    # normalized "api gateway", so R2 same-key supersession silently missed
    # it. Running normalize_key here also double-normalizes values the
    # extractor already normalized upstream — harmless, since
    # normalize_key(normalize_key(x)) == normalize_key(x) always holds
    # (the fixpoint property normalize_key's own docstring guarantees).
    @field_validator("subject_key", mode="before")
    @classmethod
    def _normalize_subject_key(cls, v):
        return normalize_key(v)

    @field_validator("attribute_key", mode="before")
    @classmethod
    def _normalize_attribute_key(cls, v):
        return normalize_key(v, max_len=100)


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
    # R2.4: True when the extractor classified this fact as contradicting common
    # world knowledge (parametric override). Propagated to FactSummary for context rendering.
    overrides_prior: bool = False
    # 097: event time (the days the fact was true) and statement time (when it was said).
    valid_from: date | None = None
    valid_to: date | None = None
    mentioned_at: datetime | None = None


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
    # When the fact was stated (never updated after insert). The recall_deep
    # recency resolver treats the later-stated of two conflicting facts as current.
    learned_at: datetime | None = None
    # R2.4: True when the extractor classified this fact as contradicting common
    # world knowledge (parametric override). Propagated from ORM for context rendering.
    overrides_prior: bool = False
    # 097: event time (the days the fact was true) and statement time (when it was
    # said; None = unknown, fall back to learned_at). Carried, not yet read.
    valid_from: date | None = None
    valid_to: date | None = None
    mentioned_at: datetime | None = None
    # K-13: who wrote the fact (heart.facts.source). The renderers mark an inferred
    # or merged fact through FACT_SOURCE_MARKERS.
    source: str | None = None
    # Gap-2: transient pre-turn recency-resolution tags set by
    # ContextEngine._resolve_recency (current/superseded + YYYY-MM). NOT persisted;
    # default None keeps every other consumer byte-identical.
    recency_status: str | None = None
    recency_date: str | None = None


# K-13: fact sources whose line ends with a provenance marker when
# fact_source_marker_enabled is on. The agent inferred these (a sleep reflection,
# a session lesson) or rewrote several facts into one (a merge), so they must not
# read like something the user said. ONE map shared by recall_deep and the
# pre-turn context. Every other source renders nothing.
FACT_SOURCE_MARKERS: dict[str, str] = {
    "sleep_reflection": "inferred",
    "reflection": "inferred",
    "cluster_consolidation": "merged",
    "contradiction_resolution": "merged",
}


def fact_source_marker(source: object) -> str:
    """`` [inferred]`` or `` [merged]`` for a source in FACT_SOURCE_MARKERS, else ``""``."""
    label = FACT_SOURCE_MARKERS.get(source) if isinstance(source, str) else None
    return f" [{label}]" if label else ""


# --- Procedures ---

# Reasoning Maps L1: the ``kind`` that marks a distilled strategy card. ONE
# definition shared by the distiller (write), ProcedureManager (SQL filters)
# and ContextEngine (prompt gate): a card is a lesson, not a how-to procedure,
# and every read path has to agree on which rows are cards.
STRATEGY_CARD_KIND = "strategy"


def is_strategy_card(procedure: object) -> bool:
    """True for a procedure row or DTO (ORM, ProcedureDetail, ProcedureSummary) that is a card."""
    return getattr(procedure, "kind", None) == STRATEGY_CARD_KIND


# The ``kind`` that marks a job lesson: a lesson learned from the bad runs of one
# recurring background job, named by ``runtime_metadata["job"]["schedule_id"]`` and
# meant for that job's runs only. ONE definition shared by the learner (write),
# ProcedureManager (per-job reads) and every read path that has to tell them apart.
JOB_LESSON_KIND = "job_lesson"


def is_job_lesson(procedure: object) -> bool:
    """True for a procedure row, DTO or input whose kind marks a job lesson."""
    return getattr(procedure, "kind", None) == JOB_LESSON_KIND


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
    # Reasoning Maps L1 (migration 079): 'strategy' marks distilled strategy cards.
    kind: str | None = None
    # F102: a create request's key. A store() with a key already used returns that
    # procedure; the same key with a different request is a ProcedureConflictError.
    idempotency_key: str | None = Field(default=None, min_length=1)


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
    # Reasoning Maps L1 (migration 079): 'strategy' marks distilled strategy cards.
    kind: str | None = None
    # F102: the body's version (1 at creation, +1 per body write).
    version: int = 1


class ProcedureVersionDetail(BaseModel):
    """One recorded version of a procedure's body (F102)."""

    procedure_id: UUID
    version: int
    name: str
    domain: str | None
    description: str | None
    goals: list[str]
    core_patterns: list[str]
    core_tools: list[str]
    core_concepts: list[str]
    implementation_notes: list[str]
    tags: list[str]
    kind: str | None
    change_source: ProcedureChangeSource
    reason: str | None
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
    kind: str | None = None
    # F079 P1: optional body fields, populated only on the recall search path so the
    # pull (recall_deep) can return usable steps. Default empty -> other construction
    # sites (list_all etc.) are unaffected.
    core_patterns: list[str] = []
    implementation_notes: list[str] = []


class JobLesson(BaseModel):
    """A job lesson (a procedure row of kind ``job_lesson``) read for its job."""

    id: UUID
    schedule_id: str
    name: str
    cue: str  # when it applies: the procedure's first goal
    lesson: str  # what to do: the "lesson: " implementation note, prefix removed
    learned_from_run: str | None
    baseline: dict | None  # the job's runs when it was learned
    retired: dict | None  # runtime_metadata["retired"]: why it was retired
    active: bool
    created_at: datetime


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
    action: CensorAction = "steer"  # F078: steer | refuse | abort (was warn)
    domain: str | None = None
    learned_from_decision: UUID | None = None
    learned_from_episode: UUID | None = None
    trigger_action: dict | None = None  # F031: e.g. {"tool": "recall", "args": {...}}
    action_instruction: str | None = None  # F031: human-readable instruction
    unblock_pattern: str | None = None  # F031: regex — if action results match, downgrade refuse→steer
    # F078: creation provenance — caps the max tier (auto<=steer, agent<=refuse, human<=abort).
    provenance: CensorProvenance = "human"
    # F078: when true, a refuse-tier censor does NOT strip tools for the turn.
    refuse_keep_tools: bool = False


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
    # F078: creation provenance + refuse opt-out.
    provenance: CensorProvenance = "human"
    refuse_keep_tools: bool = False


class CensorMatch(BaseModel):
    """Result from check_censors -- a censor that matched."""

    id: UUID
    trigger_pattern: str
    action: CensorAction  # P3-2: Literal type
    reason: str
    domain: str | None
    score: float | None = None  # Search relevance score
    # Which leg found it. "semantic" scores a cosine. search(): "regex" (pattern
    # hit, embedding not close) scores the semantic floor. check(): "regex"
    # scores 1.0, and "both" (pattern hit and cosine over the gate) the cosine.
    matched_by: Literal["semantic", "regex", "both"] | None = None
    trigger_action: dict | None = None  # F031
    action_instruction: str | None = None  # F031
    unblock_pattern: str | None = None  # F031
    # F078: surfaced so the turn-enforcer can honor the refuse tool-strip opt-out.
    refuse_keep_tools: bool = False


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
