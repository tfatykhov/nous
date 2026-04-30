"""SQLAlchemy ORM models for all 20 Nous tables across 3 schemas."""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Single declarative base for all schemas."""

    pass


# =============================================================================
# NOUS_SYSTEM SCHEMA (3 tables)
# =============================================================================


class Agent(Base):
    __tablename__ = "agents"
    __table_args__ = {"schema": "nous_system"}

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    active: Mapped[bool | None] = mapped_column(Boolean, server_default="true")
    is_initiated: Mapped[bool | None] = mapped_column(Boolean, server_default="false")
    last_active: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    frames: Mapped[list["Frame"]] = relationship(back_populates="agent")


class AgentIdentity(Base):
    __tablename__ = "agent_identity"
    __table_args__ = {"schema": "nous_system"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    agent_id: Mapped[str] = mapped_column(String(100), ForeignKey("nous_system.agents.id"), nullable=False)
    section: Mapped[str] = mapped_column(String(50), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_by: Mapped[str | None] = mapped_column(String(50))
    previous_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("nous_system.agent_identity.id"))


class Frame(Base):
    __tablename__ = "frames"
    __table_args__ = {"schema": "nous_system"}

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    agent_id: Mapped[str | None] = mapped_column(String(100), ForeignKey("nous_system.agents.id"))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    activation_patterns = mapped_column(ARRAY(Text), nullable=True)
    default_category: Mapped[str | None] = mapped_column(String(50))
    default_stakes: Mapped[str | None] = mapped_column(String(20))
    questions_to_ask = mapped_column(ARRAY(Text), nullable=True)
    agencies_to_activate = mapped_column(ARRAY(Text), nullable=True)
    suppressed_frames = mapped_column(ARRAY(Text), nullable=True)
    frame_censors = mapped_column(ARRAY(Text), nullable=True)
    usage_count: Mapped[int | None] = mapped_column(Integer, server_default="0")
    last_used: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool | None] = mapped_column(Boolean, server_default="true")
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    agent: Mapped["Agent | None"] = relationship(back_populates="frames")


class Event(Base):
    __tablename__ = "events"
    __table_args__ = {"schema": "nous_system"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    agent_id: Mapped[str] = mapped_column(String(100), nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(100))
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # F035.2: Causal chain tracing
    event_id: Mapped[str | None] = mapped_column(String(12), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(12), nullable=True, index=True)
    caused_by: Mapped[str | None] = mapped_column(String(12), nullable=True)


# =============================================================================
# BRAIN SCHEMA (8 tables)
# =============================================================================


class Decision(Base):
    __tablename__ = "decisions"
    __table_args__ = (
        CheckConstraint(
            "category IN ('architecture', 'process', 'tooling', 'security', 'integration')",
            name="ck_decisions_category",
        ),
        CheckConstraint(
            "stakes IN ('low', 'medium', 'high', 'critical')",
            name="ck_decisions_stakes",
        ),
        CheckConstraint(
            "outcome IN ('pending', 'success', 'partial', 'failure')",
            name="ck_decisions_outcome",
        ),
        {"schema": "brain"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    agent_id: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[str | None] = mapped_column(Text)
    pattern: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    # F058: pre-calibration confidence as recorded by the agent.
    # `confidence` (above) holds the calibrated value used by all gates;
    # `confidence_raw` preserves the original for calibration eval.
    confidence_raw: Mapped[float | None] = mapped_column(Float, nullable=True)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    stakes: Mapped[str] = mapped_column(String(20), nullable=False)
    quality_score: Mapped[float | None] = mapped_column(Float)
    outcome: Mapped[str | None] = mapped_column(String(20), server_default="pending")
    outcome_result: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    session_id: Mapped[str | None] = mapped_column(String(100))
    reviewer: Mapped[str | None] = mapped_column(String(50))
    embedding = mapped_column(Vector(1536), nullable=True)
    # search_tsv is GENERATED ALWAYS — do not map, read-only DB-side
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    tags: Mapped[list["DecisionTag"]] = relationship(back_populates="decision", cascade="all, delete-orphan")
    reasons: Mapped[list["DecisionReason"]] = relationship(back_populates="decision", cascade="all, delete-orphan")
    bridge: Mapped["DecisionBridge | None"] = relationship(
        back_populates="decision", cascade="all, delete-orphan", uselist=False
    )
    thoughts: Mapped[list["Thought"]] = relationship(back_populates="decision", cascade="all, delete-orphan")


class DecisionTag(Base):
    __tablename__ = "decision_tags"
    __table_args__ = {"schema": "brain"}

    decision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("brain.decisions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tag: Mapped[str] = mapped_column(String(100), primary_key=True)

    # Relationships
    decision: Mapped["Decision"] = relationship(back_populates="tags")


class DecisionReason(Base):
    __tablename__ = "decision_reasons"
    __table_args__ = (
        CheckConstraint(
            "type IN ('analysis', 'pattern', 'empirical', 'authority',"
            " 'intuition', 'analogy', 'elimination', 'constraint')",
            name="ck_reasons_type",
        ),
        {"schema": "brain"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    decision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("brain.decisions.id", ondelete="CASCADE"),
        nullable=False,
    )
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    decision: Mapped["Decision"] = relationship(back_populates="reasons")


class DecisionBridge(Base):
    __tablename__ = "decision_bridge"
    __table_args__ = {"schema": "brain"}

    decision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("brain.decisions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    structure: Mapped[str | None] = mapped_column(Text)
    function: Mapped[str | None] = mapped_column(Text)

    # Relationships
    decision: Mapped["Decision"] = relationship(back_populates="bridge")


class Thought(Base):
    __tablename__ = "thoughts"
    __table_args__ = {"schema": "brain"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    decision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("brain.decisions.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_id: Mapped[str] = mapped_column(String(100), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    decision: Mapped["Decision"] = relationship(back_populates="thoughts")


class GraphEdge(Base):
    __tablename__ = "graph_edges"
    __table_args__ = (
        UniqueConstraint("source_id", "target_id", "relation", name="uq_edges_src_tgt_rel"),
        CheckConstraint(
            "relation IN ('supports', 'contradicts', 'supersedes', 'related_to', 'caused_by', "
            "'informed_by', 'evidence_for', 'discussed_in', 'extracted_from')",
            name="ck_edges_relation",
        ),
        CheckConstraint(
            "source_type IN ('decision', 'fact', 'episode', 'procedure')",
            name="ck_edges_source_type",
        ),
        CheckConstraint(
            "target_type IN ('decision', 'fact', 'episode', 'procedure')",
            name="ck_edges_target_type",
        ),
        {"schema": "brain"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_type: Mapped[str] = mapped_column(String(20), nullable=False, server_default="decision")
    target_type: Mapped[str] = mapped_column(String(20), nullable=False, server_default="decision")
    agent_id: Mapped[str] = mapped_column(String(100), nullable=False)
    relation: Mapped[str] = mapped_column(String(50), nullable=False)
    weight: Mapped[float | None] = mapped_column(Float, server_default="1.0")
    auto_linked: Mapped[bool | None] = mapped_column(Boolean, server_default="false")
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Guardrail(Base):
    __tablename__ = "guardrails"
    __table_args__ = (
        UniqueConstraint("agent_id", "name", name="uq_guardrails_agent_name"),
        CheckConstraint(
            "severity IN ('warn', 'block', 'absolute')",
            name="ck_guardrails_severity",
        ),
        {"schema": "brain"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    agent_id: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    condition: Mapped[dict] = mapped_column(JSONB, nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, server_default="warn")
    priority: Mapped[int | None] = mapped_column(Integer, server_default="100")
    activation_count: Mapped[int | None] = mapped_column(Integer, server_default="0")
    last_activated: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool | None] = mapped_column(Boolean, server_default="true")
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CalibrationSnapshot(Base):
    __tablename__ = "calibration_snapshots"
    __table_args__ = {"schema": "brain"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    agent_id: Mapped[str] = mapped_column(String(100), nullable=False)
    total_decisions: Mapped[int | None] = mapped_column(Integer)
    reviewed_decisions: Mapped[int | None] = mapped_column(Integer)
    brier_score: Mapped[float | None] = mapped_column(Float)
    accuracy: Mapped[float | None] = mapped_column(Float)
    confidence_mean: Mapped[float | None] = mapped_column(Float)
    confidence_stddev: Mapped[float | None] = mapped_column(Float)
    category_stats: Mapped[dict | None] = mapped_column(JSONB)
    reason_stats: Mapped[dict | None] = mapped_column(JSONB)
    snapshot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())


# =============================================================================
# HEART SCHEMA (8 tables)
# =============================================================================


class Episode(Base):
    __tablename__ = "episodes"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('success', 'partial', 'failure', 'ongoing', 'abandoned')",
            name="ck_episodes_outcome",
        ),
        {"schema": "heart"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    agent_id: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str | None] = mapped_column(String(500))
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    frame_used: Mapped[str | None] = mapped_column(String(100))
    trigger: Mapped[str | None] = mapped_column(String(100))
    participants = mapped_column(ARRAY(Text), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(50))
    surprise_level: Mapped[float | None] = mapped_column(Float)
    lessons_learned = mapped_column(ARRAY(Text), nullable=True)
    embedding = mapped_column(Vector(1536), nullable=True)
    tags = mapped_column(ARRAY(Text), nullable=True)
    # search_tsv is GENERATED ALWAYS — do not map, read-only DB-side
    active: Mapped[bool | None] = mapped_column(Boolean, server_default="true")
    encoded_censors = mapped_column(JSONB, nullable=True)
    compression_tier: Mapped[str | None] = mapped_column(String(20), server_default="raw")
    structured_summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)  # F025 P3-C
    compaction_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    user_display_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    episode_decisions: Mapped[list["EpisodeDecision"]] = relationship(
        back_populates="episode", cascade="all, delete-orphan"
    )
    episode_procedures: Mapped[list["EpisodeProcedure"]] = relationship(
        back_populates="episode", cascade="all, delete-orphan"
    )


class EpisodeDecision(Base):
    __tablename__ = "episode_decisions"
    __table_args__ = {"schema": "heart"}

    episode_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("heart.episodes.id", ondelete="CASCADE"),
        primary_key=True,
    )
    decision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("brain.decisions.id", ondelete="CASCADE"),
        primary_key=True,
    )

    # Relationships
    episode: Mapped["Episode"] = relationship(back_populates="episode_decisions")
    decision: Mapped["Decision"] = relationship()


class Fact(Base):
    __tablename__ = "facts"
    __table_args__ = {"schema": "heart"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    agent_id: Mapped[str] = mapped_column(String(100), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(String(100))
    subject: Mapped[str | None] = mapped_column(String(500))
    confidence: Mapped[float | None] = mapped_column(Float, server_default="1.0")
    source: Mapped[str | None] = mapped_column(String(500))
    source_episode_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("heart.episodes.id"))
    source_decision_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("brain.decisions.id"))
    learned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_confirmed: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmation_count: Mapped[int | None] = mapped_column(Integer, server_default="0")
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("heart.facts.id"))
    contradiction_of: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("heart.facts.id"))
    embedding = mapped_column(Vector(1536), nullable=True)
    tags = mapped_column(ARRAY(Text), nullable=True)
    # search_tsv is GENERATED ALWAYS — do not map, read-only DB-side
    active: Mapped[bool | None] = mapped_column(Boolean, server_default="true")
    encoded_frame: Mapped[str | None] = mapped_column(String(100))
    encoded_censors = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # F023: Memory Admission Control
    admission_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    recall_count: Mapped[int | None] = mapped_column(Integer, server_default="0")
    last_recalled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # F021.1: Per-dimension scores for dashboard analytics
    admission_scores = mapped_column(JSONB, nullable=True, default=None)

    # F047: Actionability classification (learn-time verdict)
    actionable: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)
    actionable_confidence: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)

    # Relationships
    source_episode: Mapped["Episode | None"] = relationship(foreign_keys=[source_episode_id])
    source_decision: Mapped["Decision | None"] = relationship(foreign_keys=[source_decision_id])
    superseding_fact: Mapped["Fact | None"] = relationship(foreign_keys=[superseded_by], remote_side="Fact.id")
    contradicting_fact: Mapped["Fact | None"] = relationship(foreign_keys=[contradiction_of], remote_side="Fact.id")


class Procedure(Base):
    __tablename__ = "procedures"
    __table_args__ = {"schema": "heart"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    agent_id: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    domain: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    goals = mapped_column(ARRAY(Text), nullable=True)
    core_patterns = mapped_column(ARRAY(Text), nullable=True)
    core_tools = mapped_column(ARRAY(Text), nullable=True)
    core_concepts = mapped_column(ARRAY(Text), nullable=True)
    implementation_notes = mapped_column(ARRAY(Text), nullable=True)
    activation_count: Mapped[int | None] = mapped_column(Integer, server_default="0")
    success_count: Mapped[int | None] = mapped_column(Integer, server_default="0")
    failure_count: Mapped[int | None] = mapped_column(Integer, server_default="0")
    neutral_count: Mapped[int | None] = mapped_column(Integer, server_default="0")
    last_activated: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    related_procedures = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=True)
    censor_ids = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=True)
    embedding = mapped_column(Vector(1536), nullable=True)
    tags = mapped_column(ARRAY(Text), nullable=True)
    # search_tsv is GENERATED ALWAYS — do not map, read-only DB-side
    active: Mapped[bool | None] = mapped_column(Boolean, server_default="true")
    encoded_frame: Mapped[str | None] = mapped_column(String(100))
    encoded_censors = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    episode_procedures: Mapped[list["EpisodeProcedure"]] = relationship(
        back_populates="procedure", cascade="all, delete-orphan"
    )
    task_affinities: Mapped[list["ProcedureTaskAffinity"]] = relationship(
        back_populates="procedure", cascade="all, delete-orphan"
    )


class EpisodeProcedure(Base):
    __tablename__ = "episode_procedures"
    __table_args__ = (
        CheckConstraint(
            "effectiveness IN ('helped', 'neutral', 'hindered')",
            name="ck_ep_proc_effectiveness",
        ),
        {"schema": "heart"},
    )

    episode_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("heart.episodes.id", ondelete="CASCADE"),
        primary_key=True,
    )
    procedure_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("heart.procedures.id", ondelete="CASCADE"),
        primary_key=True,
    )
    effectiveness: Mapped[str | None] = mapped_column(String(20))

    # Relationships
    episode: Mapped["Episode"] = relationship(back_populates="episode_procedures")
    procedure: Mapped["Procedure"] = relationship(back_populates="episode_procedures")


class ProcedureTaskAffinity(Base):
    """Tracks per-frame-type activation and outcome counts for a procedure (F037)."""

    __tablename__ = "procedure_task_affinity"
    __table_args__ = (
        UniqueConstraint("procedure_id", "frame_type", "agent_id", name="uq_proc_affinity"),
        {"schema": "heart"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    procedure_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("heart.procedures.id", ondelete="CASCADE"),
        nullable=False,
    )
    frame_type: Mapped[str] = mapped_column(String(100), nullable=False)
    activation_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    success_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    agent_id: Mapped[str] = mapped_column(String(100), nullable=False)
    active: Mapped[bool | None] = mapped_column(Boolean, server_default="true")

    # Relationship
    procedure: Mapped["Procedure"] = relationship(back_populates="task_affinities")


class RubricVersion(Base):
    __tablename__ = "rubric_versions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'superseded', 'rollback')",
            name="ck_rubric_versions_status",
        ),
        {"schema": "heart"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    agent_id: Mapped[str] = mapped_column(String(100), ForeignKey("nous_system.agents.id"), nullable=False)
    version: Mapped[str] = mapped_column(String(20), nullable=False)
    parent_version: Mapped[str | None] = mapped_column(String(20))
    change_reason: Mapped[str] = mapped_column(Text, nullable=False)
    dimensions: Mapped[list] = mapped_column(JSONB, nullable=False)
    outcome_correlations: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OutcomeSignal(Base):
    __tablename__ = "outcome_signals"
    __table_args__ = (
        CheckConstraint(
            "signal_type IN ('corrected', 'completed', 'praised', 'reworked', 'self_corrected')",
            name="ck_outcome_signals_type",
        ),
        CheckConstraint(
            "confidence BETWEEN 0 AND 1",
            name="ck_outcome_signals_confidence",
        ),
        {"schema": "heart"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    agent_id: Mapped[str] = mapped_column(String(100), ForeignKey("nous_system.agents.id"), nullable=False)
    episode_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("heart.episodes.id", ondelete="CASCADE"), nullable=False)
    signal_type: Mapped[str] = mapped_column(String(30), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, server_default="0.5")
    evidence: Mapped[str | None] = mapped_column(Text)
    self_improvement_scores: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Censor(Base):
    __tablename__ = "censors"
    __table_args__ = (
        CheckConstraint(
            "action IN ('warn', 'block', 'absolute')",
            name="ck_censors_action",
        ),
        CheckConstraint(
            "created_by IN ('manual', 'auto_failure', 'auto_escalation')",
            name="ck_censors_created_by",
        ),
        {"schema": "heart"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    agent_id: Mapped[str] = mapped_column(String(100), nullable=False)
    trigger_pattern: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False, server_default="warn")
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    domain: Mapped[str | None] = mapped_column(String(100))
    learned_from_decision: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("brain.decisions.id")
    )
    learned_from_episode: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("heart.episodes.id"))
    created_by: Mapped[str | None] = mapped_column(String(50), server_default="manual")
    activation_count: Mapped[int | None] = mapped_column(Integer, server_default="0")
    last_activated: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    false_positive_count: Mapped[int | None] = mapped_column(Integer, server_default="0")
    last_false_positive: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    escalation_threshold: Mapped[int | None] = mapped_column(Integer, server_default="3")
    embedding = mapped_column(Vector(1536), nullable=True)
    active: Mapped[bool | None] = mapped_column(Boolean, server_default="true")
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())

    trigger_action: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    action_instruction: Mapped[str | None] = mapped_column(Text, nullable=True)
    unblock_pattern: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    source_decision: Mapped["Decision | None"] = relationship(foreign_keys=[learned_from_decision])
    source_episode: Mapped["Episode | None"] = relationship(foreign_keys=[learned_from_episode])


class WorkingMemory(Base):
    __tablename__ = "working_memory"
    __table_args__ = (
        UniqueConstraint("agent_id", "session_id", name="uq_wm_agent_session"),
        {"schema": "heart"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    agent_id: Mapped[str] = mapped_column(String(100), nullable=False)
    session_id: Mapped[str] = mapped_column(String(100), nullable=False)
    current_task: Mapped[str | None] = mapped_column(Text)
    current_frame: Mapped[str | None] = mapped_column(String(100))
    items: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="[]")
    open_threads: Mapped[dict | None] = mapped_column(JSONB, server_default="[]")
    max_items: Mapped[int | None] = mapped_column(Integer, server_default="20")
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ConversationState(Base):
    __tablename__ = "conversation_state"
    __table_args__ = (
        UniqueConstraint("agent_id", "session_id", name="uq_conversation_state_agent_session"),
        {"schema": "heart"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    session_id: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    messages: Mapped[dict | None] = mapped_column(JSONB)
    turn_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    compaction_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


# ---------------------------------------------------------------------------
# 011.1: Subtasks & Scheduling (F009)
# ---------------------------------------------------------------------------


class Subtask(Base):
    """Background subtask queue entry."""

    __tablename__ = "subtasks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'cancelled')",
            name="chk_subtask_status",
        ),
        {"schema": "heart"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    agent_id: Mapped[str] = mapped_column(String(100), nullable=False)
    parent_session_id: Mapped[str | None] = mapped_column(String(200))
    task: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default="100")
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending")
    result: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    worker_id: Mapped[str | None] = mapped_column(String(100))
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="120")
    frame_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    notify: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    delivered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, server_default="{}"
    )


class Schedule(Base):
    """Scheduled or recurring task."""

    __tablename__ = "schedules"
    __table_args__ = (
        CheckConstraint(
            "schedule_type IN ('once', 'recurring')",
            name="chk_schedule_type",
        ),
        {"schema": "heart"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    agent_id: Mapped[str] = mapped_column(String(100), nullable=False)
    task: Mapped[str] = mapped_column(Text, nullable=False)
    schedule_type: Mapped[str] = mapped_column(String(20), nullable=False)
    fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    interval_seconds: Mapped[int | None] = mapped_column(Integer)
    cron_expr: Mapped[str | None] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fire_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_fires: Mapped[int | None] = mapped_column(Integer)
    notify: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="120")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    created_by_session: Mapped[str | None] = mapped_column(String(200))
    metadata_: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, server_default="{}"
    )
    model: Mapped[str | None] = mapped_column(String(100))
    frame_type: Mapped[str | None] = mapped_column(String(20))


class ToolCache(Base):
    """Cached original content of compressed tool results (F020)."""

    __tablename__ = "tool_cache"
    __table_args__ = (
        UniqueConstraint("session_id", "hash_key", name="uq_tool_cache_session_hash"),
        {"schema": "heart"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    session_id: Mapped[str] = mapped_column(Text, nullable=False)
    hash_key: Mapped[str] = mapped_column(Text, nullable=False)
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    tool_input: Mapped[dict | None] = mapped_column(JSONB)
    original_content: Mapped[str] = mapped_column(Text, nullable=False)
    compressed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    item_count: Mapped[int | None] = mapped_column(Integer)


class DynamicCheckModel(Base):
    """F034.5: Dynamic heartbeat check definition."""

    __tablename__ = "dynamic_checks"
    __table_args__ = {"schema": "nous_system"}

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    agent_id: Mapped[str] = mapped_column(String, nullable=False, default="nous")
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(String, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    tools: Mapped[list] = mapped_column(ARRAY(String), server_default="{}")
    cron_expr: Mapped[str | None] = mapped_column(String, nullable=True)
    interval_seconds: Mapped[int] = mapped_column(Integer, default=3600)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=30)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    urgent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    created_by: Mapped[str] = mapped_column(String, default="conversation")
    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    run_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)
    on_complete_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    on_complete_tools: Mapped[list] = mapped_column(ARRAY(String), nullable=False, server_default="{}")
    metadata_: Mapped[dict] = mapped_column(
        "metadata", JSONB, server_default="{}"
    )


# ---------------------------------------------------------------------------
# F038: Unified DAG Orchestration
# ---------------------------------------------------------------------------


class ExecutionDAG(Base):
    """Top-level DAG orchestration unit."""

    __tablename__ = "execution_dags"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'cancelled', 'partial')",
            name="chk_dag_status",
        ),
        CheckConstraint(
            "source IN ('conversation', 'critic', 'heartbeat', 'schedule')",
            name="chk_dag_source",
        ),
        {"schema": "nous_system"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    agent_id: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    source: Mapped[str] = mapped_column(
        String(30), nullable=False, default="conversation", server_default="conversation"
    )
    original_request: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    token_budget: Mapped[int | None] = mapped_column(Integer)
    tokens_consumed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    result_summary: Mapped[str | None] = mapped_column(Text)
    postmortem: Mapped[dict | None] = mapped_column(JSONB)

    nodes: Mapped[list["DAGNode"]] = relationship(
        "DAGNode",
        back_populates="dag",
        cascade="all, delete-orphan",
        order_by="DAGNode.wave, DAGNode.name",
    )
    edges: Mapped[list["DAGEdge"]] = relationship(
        "DAGEdge", back_populates="dag", cascade="all, delete-orphan"
    )

    def __init__(self, **kwargs: object) -> None:
        kwargs.setdefault("description", "")
        kwargs.setdefault("status", "pending")
        kwargs.setdefault("source", "conversation")
        kwargs.setdefault("tokens_consumed", 0)
        super().__init__(**kwargs)


class DAGNode(Base):
    """Individual execution unit within a DAG."""

    __tablename__ = "dag_nodes"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'ready', 'running', 'awaiting_check', 'completed', 'failed', 'blocked', 'cancelled')",
            name="chk_dag_node_status",
        ),
        CheckConstraint(
            "node_type IN ('subtask', 'check', 'gate', 'callback')",
            name="chk_dag_node_type",
        ),
        UniqueConstraint("dag_id", "name", name="uq_dag_node_name"),
        {"schema": "nous_system"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    dag_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("nous_system.execution_dags.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    node_type: Mapped[str] = mapped_column(String(20), nullable=False)
    subtask_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    check_name: Mapped[str | None] = mapped_column(String(200))
    wave: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    instructions: Mapped[str | None] = mapped_column(Text)
    tools: Mapped[list | None] = mapped_column(JSONB)
    frame_type: Mapped[str | None] = mapped_column(String(30))
    model: Mapped[str | None] = mapped_column(String(100))
    timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=120, server_default="120"
    )
    completion_condition: Mapped[str | None] = mapped_column(String(100))
    completion_check: Mapped[str | None] = mapped_column(Text)
    completion_check_interval: Mapped[int | None] = mapped_column(Integer)
    max_check_attempts: Mapped[int | None] = mapped_column(Integer)
    check_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    awaiting_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    tokens_used: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    injected_context: Mapped[str | None] = mapped_column(Text)

    dag: Mapped["ExecutionDAG"] = relationship("ExecutionDAG", back_populates="nodes")

    def __init__(self, **kwargs: object) -> None:
        kwargs.setdefault("description", "")
        kwargs.setdefault("status", "pending")
        kwargs.setdefault("wave", 0)
        kwargs.setdefault("timeout_seconds", 120)
        kwargs.setdefault("tokens_used", 0)
        kwargs.setdefault("check_attempts", 0)
        super().__init__(**kwargs)


class DAGEdge(Base):
    """Dependency/cascade/context relationship between DAG nodes."""

    __tablename__ = "dag_edges"
    __table_args__ = (
        UniqueConstraint(
            "dag_id", "from_node_id", "to_node_id", "edge_type", name="uq_dag_edge"
        ),
        {"schema": "nous_system"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    dag_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("nous_system.execution_dags.id", ondelete="CASCADE"),
        nullable=False,
    )
    from_node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("nous_system.dag_nodes.id", ondelete="CASCADE"),
        nullable=False,
    )
    to_node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("nous_system.dag_nodes.id", ondelete="CASCADE"),
        nullable=False,
    )
    edge_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="dependency", server_default="dependency"
    )

    dag: Mapped["ExecutionDAG"] = relationship("ExecutionDAG", back_populates="edges")

    def __init__(self, **kwargs: object) -> None:
        kwargs.setdefault("edge_type", "dependency")
        super().__init__(**kwargs)