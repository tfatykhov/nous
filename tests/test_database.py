"""Tests for database infrastructure: connection, schemas, extensions, tables, seed data, triggers."""

from sqlalchemy import text


async def test_connection(db):
    """Can connect to Postgres and execute a query."""
    async with db.engine.connect() as conn:
        result = await conn.execute(text("SELECT 1"))
        assert result.scalar() == 1


async def test_schemas_exist(db):
    """brain, heart, nous_system schemas are present."""
    async with db.engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name IN ('brain', 'heart', 'nous_system')"
            )
        )
        schemas = {row[0] for row in result}
    assert schemas == {"brain", "heart", "nous_system"}


async def test_extensions(db):
    """vector and pg_trgm extensions are installed."""
    async with db.engine.connect() as conn:
        result = await conn.execute(text("SELECT extname FROM pg_extension WHERE extname IN ('vector', 'pg_trgm')"))
        extensions = {row[0] for row in result}
    assert extensions == {"vector", "pg_trgm"}


async def test_all_tables_exist(db):
    """All 18 tables exist in correct schemas."""
    expected = {
        # nous_system (3)
        ("nous_system", "agents"),
        ("nous_system", "frames"),
        ("nous_system", "events"),
        # brain (8)
        ("brain", "decisions"),
        ("brain", "decision_tags"),
        ("brain", "decision_reasons"),
        ("brain", "decision_bridge"),
        ("brain", "thoughts"),
        ("brain", "graph_edges"),
        ("brain", "guardrails"),
        ("brain", "calibration_snapshots"),
        # heart (7)
        ("heart", "episodes"),
        ("heart", "episode_decisions"),
        ("heart", "facts"),
        ("heart", "procedures"),
        ("heart", "episode_procedures"),
        ("heart", "censors"),
        ("heart", "working_memory"),
    }
    async with db.engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT table_schema, table_name FROM information_schema.tables "
                "WHERE table_schema IN ('brain', 'heart', 'nous_system') "
                "AND table_type = 'BASE TABLE'"
            )
        )
        actual = {(row[0], row[1]) for row in result}
    assert actual == expected, f"Missing: {expected - actual}, Extra: {actual - expected}"


async def test_seed_agent(db):
    """Default agent exists with correct config."""
    async with db.engine.connect() as conn:
        result = await conn.execute(
            text("SELECT id, name, description, config FROM nous_system.agents WHERE id = 'nous-default'")
        )
        row = result.one()
    assert row[0] == "nous-default"
    assert row[1] == "Nous"
    assert row[2] == "A thinking agent that learns from experience"
    config = row[3]
    assert config["embedding_model"] == "text-embedding-3-small"
    assert config["embedding_dimensions"] == 1536
    assert config["working_memory_capacity"] == 20
    assert config["auto_extract_facts"] is True
    assert config["identity"]["traits"] == ["analytical", "cautious", "curious"]


async def test_seed_frames(db):
    """6 cognitive frames exist with correct names."""
    async with db.engine.connect() as conn:
        result = await conn.execute(
            text("SELECT id, name FROM nous_system.frames WHERE agent_id = 'nous-default' ORDER BY id")
        )
        frames = {row[0]: row[1] for row in result}
    expected_frames = {
        "task": "Task Execution",
        "question": "Question Answering",
        "decision": "Decision Making",
        "creative": "Creative",
        "conversation": "Conversation",
        "debug": "Debug",
    }
    assert frames == expected_frames


async def test_seed_guardrails(db):
    """4 guardrails exist with correct conditions."""
    async with db.engine.connect() as conn:
        result = await conn.execute(
            text("SELECT name, condition, severity FROM brain.guardrails WHERE agent_id = 'nous-default' ORDER BY name")
        )
        guardrails = {row[0]: {"condition": row[1], "severity": row[2]} for row in result}
    assert len(guardrails) == 4
    expected_cel = "decision.stakes == 'high' && decision.confidence < 0.5"
    assert guardrails["no-high-stakes-low-confidence"]["condition"] == {"cel": expected_cel}
    assert guardrails["no-high-stakes-low-confidence"]["severity"] == "block"
    assert guardrails["no-critical-without-review"]["condition"] == {"cel": "decision.stakes == 'critical'"}
    assert guardrails["require-reasons"]["condition"] == {"cel": "decision.reason_count < 1"}
    assert guardrails["low-quality-recording"]["condition"] == {"cel": "decision.quality_score < 0.5"}


async def test_updated_at_trigger(session):
    """Updating a row auto-updates updated_at via trigger."""
    conn = await session.connection()

    # Insert a test agent
    await conn.execute(
        text(
            "INSERT INTO nous_system.agents (id, name, config) "
            "VALUES ('test-trigger-agent', 'Trigger Test', '{}'::jsonb)"
        )
    )
    await session.commit()

    # Get the initial updated_at
    result = await conn.execute(text("SELECT updated_at FROM nous_system.agents WHERE id = 'test-trigger-agent'"))
    initial_updated_at = result.scalar()

    # Wait briefly to ensure timestamp difference
    await conn.execute(text("SELECT pg_sleep(0.05)"))

    # Update the row
    await conn.execute(
        text("UPDATE nous_system.agents SET name = 'Trigger Test Updated' WHERE id = 'test-trigger-agent'")
    )
    await session.commit()

    # Verify updated_at changed
    result = await conn.execute(text("SELECT updated_at FROM nous_system.agents WHERE id = 'test-trigger-agent'"))
    new_updated_at = result.scalar()

    assert new_updated_at > initial_updated_at, (
        f"updated_at should have advanced: {initial_updated_at} -> {new_updated_at}"
    )
