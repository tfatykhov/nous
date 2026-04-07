"""SQLite in-memory test database compatibility layer.

Provides a TestDatabase that uses aiosqlite for offline testing.
The schema is a best-effort SQLite-compatible subset — columns using
PG-specific types (Vector, JSONB, ARRAY) are mapped to compatible
SQLite equivalents (TEXT/JSON). Tests that require real Postgres
features (pgvector, tsvector, JSONB operators, schema namespaces)
must be marked @pytest.mark.integration or @pytest.mark.postgres_only.
"""

from __future__ import annotations

import uuid

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    MetaData,
    String,
    Table,
    Text,
    Column,
    func,
    event,
)
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


# ---------------------------------------------------------------------------
# SQLite-compatible metadata (no schema namespaces, no PG-specific types)
# ---------------------------------------------------------------------------

metadata = MetaData()

# nous_system schema tables (prefixed: nous_system_*)
agents_table = Table(
    "nous_system_agents",
    metadata,
    Column("id", String(100), primary_key=True),
    Column("name", String(200), nullable=False),
    Column("description", Text),
    Column("config", JSON, nullable=False, server_default="{}"),
    Column("active", Boolean, server_default="1"),
    Column("is_initiated", Boolean, server_default="0"),
    Column("last_active", DateTime),
    Column("created_at", DateTime, server_default=func.now()),
    Column("updated_at", DateTime, server_default=func.now()),
)

agent_identity_table = Table(
    "nous_system_agent_identity",
    metadata,
    Column("id", String(36), primary_key=True, default=lambda: str(uuid.uuid4())),
    Column("agent_id", String(100), ForeignKey("nous_system_agents.id"), nullable=False),
    Column("section", String(50), nullable=False),
    Column("content", Text, nullable=False),
    Column("version", Integer, nullable=False, server_default="1"),
    Column("is_current", Boolean, nullable=False, server_default="1"),
    Column("updated_at", DateTime, server_default=func.now()),
    Column("updated_by", String(50)),
    Column("previous_version_id", String(36)),
)

frames_table = Table(
    "nous_system_frames",
    metadata,
    Column("id", String(100), primary_key=True),
    Column("agent_id", String(100), ForeignKey("nous_system_agents.id")),
    Column("name", String(200), nullable=False),
    Column("description", Text),
    Column("activation_patterns", JSON),
    Column("default_category", String(50)),
    Column("default_stakes", String(20)),
    Column("questions_to_ask", JSON),
    Column("agencies_to_activate", JSON),
    Column("suppressed_frames", JSON),
    Column("frame_censors", JSON),
    Column("usage_count", Integer, server_default="0"),
    Column("last_used", DateTime),
    Column("active", Boolean, server_default="1"),
    Column("created_at", DateTime, server_default=func.now()),
)

events_table = Table(
    "nous_system_events",
    metadata,
    Column("id", String(36), primary_key=True, default=lambda: str(uuid.uuid4())),
    Column("agent_id", String(100), nullable=False),
    Column("session_id", String(100)),
    Column("event_type", String(50), nullable=False),
    Column("data", JSON, nullable=False, server_default="{}"),
    Column("created_at", DateTime, server_default=func.now()),
)

# brain schema tables
decisions_table = Table(
    "brain_decisions",
    metadata,
    Column("id", String(36), primary_key=True, default=lambda: str(uuid.uuid4())),
    Column("agent_id", String(100), nullable=False),
    Column("session_id", String(100)),
    Column("question", Text, nullable=False),
    Column("choice", Text, nullable=False),
    Column("confidence", Float, nullable=False),
    Column("outcome", String(20)),
    Column("outcome_notes", Text),
    Column("stakes", String(20)),
    Column("category", String(50)),
    Column("tags", JSON),
    Column("embedding", Text),  # JSON array in SQLite
    Column("bridge", JSON),
    Column("frame_id", String(100)),
    Column("reviewed", Boolean, server_default="0"),
    Column("review_notes", Text),
    Column("quality_score", Float),
    Column("active", Boolean, server_default="1"),
    Column("created_at", DateTime, server_default=func.now()),
    Column("updated_at", DateTime, server_default=func.now()),
)

decision_reasons_table = Table(
    "brain_decision_reasons",
    metadata,
    Column("id", String(36), primary_key=True, default=lambda: str(uuid.uuid4())),
    Column("decision_id", String(36), ForeignKey("brain_decisions.id"), nullable=False),
    Column("reason", Text, nullable=False),
    Column("weight", Float, server_default="1.0"),
    Column("created_at", DateTime, server_default=func.now()),
)

guardrails_table = Table(
    "brain_guardrails",
    metadata,
    Column("id", String(36), primary_key=True, default=lambda: str(uuid.uuid4())),
    Column("agent_id", String(100), nullable=False),
    Column("name", String(200), nullable=False),
    Column("description", Text),
    Column("condition", JSON, nullable=False),
    Column("severity", String(20), nullable=False),
    Column("priority", Integer, server_default="0"),
    Column("active", Boolean, server_default="1"),
    Column("trigger_action", String(50)),
    Column("action_instruction", Text),
    Column("unblock_pattern", String(500)),
    Column("created_at", DateTime, server_default=func.now()),
    Column("updated_at", DateTime, server_default=func.now()),
)

graph_edges_table = Table(
    "brain_graph_edges",
    metadata,
    Column("id", String(36), primary_key=True, default=lambda: str(uuid.uuid4())),
    Column("agent_id", String(100), nullable=False),
    Column("source_type", String(50), nullable=False),
    Column("source_id", String(36), nullable=False),
    Column("target_type", String(50), nullable=False),
    Column("target_id", String(36), nullable=False),
    Column("edge_type", String(50), nullable=False),
    Column("weight", Float, server_default="1.0"),
    Column("metadata", JSON),
    Column("active", Boolean, server_default="1"),
    Column("created_at", DateTime, server_default=func.now()),
)

rubric_versions_table = Table(
    "brain_rubric_versions",
    metadata,
    Column("id", String(36), primary_key=True, default=lambda: str(uuid.uuid4())),
    Column("agent_id", String(100), nullable=False),
    Column("version", Integer, nullable=False),
    Column("dimensions", JSON, nullable=False),
    Column("is_current", Boolean, server_default="1"),
    Column("created_at", DateTime, server_default=func.now()),
    Column("evolved_from_id", String(36)),
    Column("evolution_reason", Text),
)

dimension_proposals_table = Table(
    "brain_dimension_proposals",
    metadata,
    Column("id", String(36), primary_key=True, default=lambda: str(uuid.uuid4())),
    Column("agent_id", String(100), nullable=False),
    Column("name", String(100), nullable=False),
    Column("description", Text),
    Column("rationale", Text),
    Column("status", String(20), server_default="pending"),
    Column("proposed_at", DateTime, server_default=func.now()),
    Column("reviewed_at", DateTime),
)

outcome_signals_table = Table(
    "brain_outcome_signals",
    metadata,
    Column("id", String(36), primary_key=True, default=lambda: str(uuid.uuid4())),
    Column("agent_id", String(100), nullable=False),
    Column("episode_id", String(36)),
    Column("signal_type", String(50), nullable=False),
    Column("signal_value", Float),
    Column("metadata", JSON),
    Column("created_at", DateTime, server_default=func.now()),
)

# heart schema tables
episodes_table = Table(
    "heart_episodes",
    metadata,
    Column("id", String(36), primary_key=True, default=lambda: str(uuid.uuid4())),
    Column("agent_id", String(100), nullable=False),
    Column("session_id", String(100)),
    Column("title", String(500)),
    Column("summary", Text),
    Column("transcript", Text),
    Column("outcome", String(20)),
    Column("tags", JSON),
    Column("embedding", Text),
    Column("frame_id", String(100)),
    Column("topic", String(200)),
    Column("active", Boolean, server_default="1"),
    Column("created_at", DateTime, server_default=func.now()),
    Column("updated_at", DateTime, server_default=func.now()),
    Column("started_at", DateTime),
    Column("ended_at", DateTime),
)

facts_table = Table(
    "heart_facts",
    metadata,
    Column("id", String(36), primary_key=True, default=lambda: str(uuid.uuid4())),
    Column("agent_id", String(100), nullable=False),
    Column("content", Text, nullable=False),
    Column("source", String(200)),
    Column("confidence", Float, server_default="1.0"),
    Column("tags", JSON),
    Column("embedding", Text),
    Column("domain", String(100)),
    Column("user_id", String(100)),
    Column("frame_id", String(100)),
    Column("superseded_by", String(36)),
    Column("admission_scores", JSON),
    Column("active", Boolean, server_default="1"),
    Column("created_at", DateTime, server_default=func.now()),
    Column("updated_at", DateTime, server_default=func.now()),
)

procedures_table = Table(
    "heart_procedures",
    metadata,
    Column("id", String(36), primary_key=True, default=lambda: str(uuid.uuid4())),
    Column("agent_id", String(100), nullable=False),
    Column("name", String(200), nullable=False),
    Column("description", Text),
    Column("steps", JSON, nullable=False),
    Column("tags", JSON),
    Column("embedding", Text),
    Column("usage_count", Integer, server_default="0"),
    Column("last_used", DateTime),
    Column("source_decision_ids", JSON),
    Column("frame_id", String(100)),
    Column("active", Boolean, server_default="1"),
    Column("created_at", DateTime, server_default=func.now()),
    Column("updated_at", DateTime, server_default=func.now()),
)

censors_table = Table(
    "heart_censors",
    metadata,
    Column("id", String(36), primary_key=True, default=lambda: str(uuid.uuid4())),
    Column("agent_id", String(100), nullable=False),
    Column("name", String(200), nullable=False),
    Column("description", Text),
    Column("pattern", Text),
    Column("severity", String(20)),
    Column("trigger_action", String(50)),
    Column("action_instruction", Text),
    Column("unblock_pattern", String(500)),
    Column("active", Boolean, server_default="1"),
    Column("frame_ids", JSON),
    Column("created_at", DateTime, server_default=func.now()),
    Column("updated_at", DateTime, server_default=func.now()),
)

working_memory_table = Table(
    "heart_working_memory",
    metadata,
    Column("id", String(36), primary_key=True, default=lambda: str(uuid.uuid4())),
    Column("agent_id", String(100), nullable=False),
    Column("session_id", String(100)),
    Column("key", String(200)),
    Column("value", Text),
    Column("expires_at", DateTime),
    Column("created_at", DateTime, server_default=func.now()),
    Column("updated_at", DateTime, server_default=func.now()),
)

subtasks_table = Table(
    "heart_subtasks",
    metadata,
    Column("id", String(36), primary_key=True, default=lambda: str(uuid.uuid4())),
    Column("agent_id", String(100), nullable=False),
    Column("parent_session_id", String(100)),
    Column("title", String(500)),
    Column("description", Text),
    Column("status", String(20), server_default="pending"),
    Column("result", Text),
    Column("error", Text),
    Column("metadata", JSON),
    Column("created_at", DateTime, server_default=func.now()),
    Column("updated_at", DateTime, server_default=func.now()),
    Column("started_at", DateTime),
    Column("completed_at", DateTime),
)

schedules_table = Table(
    "heart_schedules",
    metadata,
    Column("id", String(36), primary_key=True, default=lambda: str(uuid.uuid4())),
    Column("agent_id", String(100), nullable=False),
    Column("name", String(200)),
    Column("description", Text),
    Column("task_prompt", Text, nullable=False),
    Column("schedule_type", String(20), nullable=False),
    Column("cron_expression", String(100)),
    Column("run_at", DateTime),
    Column("last_run_at", DateTime),
    Column("next_run_at", DateTime),
    Column("run_count", Integer, server_default="0"),
    Column("active", Boolean, server_default="1"),
    Column("created_at", DateTime, server_default=func.now()),
    Column("updated_at", DateTime, server_default=func.now()),
)

heartbeat_findings_table = Table(
    "nous_system_heartbeat_findings",
    metadata,
    Column("id", String(36), primary_key=True, default=lambda: str(uuid.uuid4())),
    Column("agent_id", String(100), nullable=False),
    Column("fingerprint", String(200), nullable=False),
    Column("check_name", String(100)),
    Column("title", String(500)),
    Column("description", Text),
    Column("urgency", String(20)),
    Column("state", String(20), server_default="active"),
    Column("metadata", JSON),
    Column("first_seen_at", DateTime, server_default=func.now()),
    Column("last_seen_at", DateTime, server_default=func.now()),
    Column("resolved_at", DateTime),
    Column("acknowledged_at", DateTime),
)

heartbeat_dynamic_checks_table = Table(
    "nous_system_heartbeat_dynamic_checks",
    metadata,
    Column("id", String(36), primary_key=True, default=lambda: str(uuid.uuid4())),
    Column("agent_id", String(100), nullable=False),
    Column("name", String(200), nullable=False),
    Column("description", Text),
    Column("prompt", Text),
    Column("prompt_embedding", Text),
    Column("interval_seconds", Integer, server_default="1800"),
    Column("enabled", Boolean, server_default="1"),
    Column("last_run_at", DateTime),
    Column("last_result", JSON),
    Column("created_at", DateTime, server_default=func.now()),
    Column("updated_at", DateTime, server_default=func.now()),
)

execution_ledger_table = Table(
    "nous_system_execution_ledger",
    metadata,
    Column("id", String(36), primary_key=True, default=lambda: str(uuid.uuid4())),
    Column("agent_id", String(100), nullable=False),
    Column("session_id", String(100)),
    Column("action_type", String(50)),
    Column("action_input", JSON),
    Column("action_output", JSON),
    Column("status", String(20)),
    Column("side_effect_class", String(50)),
    Column("claimed_actions", JSON),
    Column("verified", Boolean, server_default="0"),
    Column("created_at", DateTime, server_default=func.now()),
)

prompt_cache_table = Table(
    "nous_system_prompt_cache",
    metadata,
    Column("id", String(36), primary_key=True, default=lambda: str(uuid.uuid4())),
    Column("agent_id", String(100), nullable=False),
    Column("frame_id", String(100)),
    Column("tier", Integer),
    Column("content_hash", String(64)),
    Column("token_count", Integer),
    Column("metadata", JSON),
    Column("created_at", DateTime, server_default=func.now()),
    Column("updated_at", DateTime, server_default=func.now()),
)


# ---------------------------------------------------------------------------
# TestDatabase class
# ---------------------------------------------------------------------------


class TestDatabase:
    """In-memory SQLite database for offline testing.

    Provides the same interface as ``nous.storage.database.Database``
    but backed by aiosqlite instead of asyncpg + Postgres. The schema
    is a simplified SQLite-compatible version with JSON columns in place
    of JSONB/ARRAY/Vector, and without schema namespaces.

    Tests that rely on Postgres-specific features (pgvector cosine search,
    JSONB operators, tsvector, schema introspection) must be marked
    ``@pytest.mark.integration`` or ``@pytest.mark.postgres_only`` and
    are excluded from offline runs.
    """

    def __init__(self) -> None:
        self.engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            echo=False,
            connect_args={"check_same_thread": False},
        )
        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        # Enable foreign keys for SQLite
        @event.listens_for(self.engine.sync_engine, "connect")
        def set_sqlite_pragma(dbapi_conn, connection_record):  # noqa: ARG001
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    async def connect(self) -> None:
        """Create all SQLite-compatible tables."""
        async with self.engine.begin() as conn:
            await conn.run_sync(metadata.create_all)

    async def disconnect(self) -> None:
        """Dispose of connection pool."""
        await self.engine.dispose()

    async def __aenter__(self) -> "TestDatabase":
        await self.connect()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.disconnect()
