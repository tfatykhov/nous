"""F069: integration test for the ingest_document tool against real Postgres.

Requires NOUS_TEST_DB=postgres (the eval scratch container is fine). Tests
the full happy path: tool creates chunks under an episode, persists them
to heart.episode_chunks with source_kind='document', and they're queryable
via SQL afterwards.

Tests the failure paths in test_document_chunker.py — this one is just the
DB-roundtrip smoke.
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text

pytestmark = [pytest.mark.postgres_only, pytest.mark.asyncio]


@pytest.fixture
def agent_id() -> str:
    return f"test-f069-{uuid.uuid4().hex[:8]}"


@pytest.fixture
async def fixture_episode(db, agent_id):
    """Insert a single active episode for the test agent."""
    episode_id = uuid.uuid4()
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO heart.episodes "
                "(id, agent_id, session_id, summary, started_at, active) "
                "VALUES (:i, :a, :s, 'test ep', NOW(), true)"
            ),
            {"i": episode_id, "a": agent_id, "s": "test-session-f069"},
        )
        await session.commit()
    return episode_id


async def test_ingest_document_creates_chunks(db, agent_id, fixture_episode):
    """Happy path: ingest a synthetic doc, verify rows land in episode_chunks."""
    from unittest.mock import AsyncMock, MagicMock

    from nous.api.tools import create_nous_tools
    from nous.config import Settings

    # Build a Heart-like stub: only needs .db, .agent_id, ._embeddings
    settings = Settings()
    settings = settings.model_copy(update={"agent_id": agent_id, "document_chunk_min_chars": 50})

    # Mock embeddings to return a 1536-dim zero vector per chunk (cheap).
    mock_embedder = MagicMock()
    mock_embedder.embed_batch = AsyncMock(return_value=[[0.0] * 1536 for _ in range(20)])

    heart_stub = MagicMock()
    heart_stub.db = db
    heart_stub.agent_id = agent_id
    heart_stub._embeddings = mock_embedder

    brain_stub = MagicMock()  # ingest_document doesn't touch brain
    tools = create_nous_tools(brain_stub, heart_stub, settings)
    ingest_document = tools["ingest_document"]

    # Synthetic doc: 4 paragraphs of ~800 chars each = ~3.2K total.
    paragraphs = [
        f"Paragraph {i}. " + ("Lorem ipsum dolor sit amet. " * 30)
        for i in range(4)
    ]
    content = "\n\n".join(paragraphs)

    result = await ingest_document(
        content=content,
        source_ref="https://example.com/test-paper.pdf",
        episode_id=str(fixture_episode),
    )

    assert "Ingested" in result["content"][0]["text"]
    assert "chunks" in result["content"][0]["text"]

    # Verify chunks landed in DB with correct source_kind and source_ref.
    async with db.session() as session:
        rows = await session.execute(
            text(
                "SELECT chunk_index, source_kind, source_ref, length(content) AS clen "
                "FROM heart.episode_chunks "
                "WHERE agent_id = :a AND episode_id = :e "
                "ORDER BY chunk_index"
            ),
            {"a": agent_id, "e": fixture_episode},
        )
        chunks = rows.fetchall()

    assert len(chunks) >= 2, f"expected >=2 chunks for 3.2K text, got {len(chunks)}"
    for c in chunks:
        assert c.source_kind == "document"
        assert c.source_ref == "https://example.com/test-paper.pdf"
        assert c.clen > 0

    # Cleanup
    async with db.session() as session:
        await session.execute(
            text("DELETE FROM heart.episode_chunks WHERE agent_id = :a"),
            {"a": agent_id},
        )
        await session.execute(
            text("DELETE FROM heart.episodes WHERE agent_id = :a"),
            {"a": agent_id},
        )
        await session.commit()


async def test_ingest_document_rejects_missing_episode(db, agent_id):
    """No active episode, no episode_id → returns error message, no rows."""
    from unittest.mock import AsyncMock, MagicMock

    from nous.api.tools import create_nous_tools
    from nous.config import Settings

    settings = Settings().model_copy(update={"agent_id": agent_id})
    heart_stub = MagicMock()
    heart_stub.db = db
    heart_stub.agent_id = agent_id
    heart_stub._embeddings = MagicMock()
    heart_stub._embeddings.embed_batch = AsyncMock(return_value=[])

    brain_stub = MagicMock()
    tools = create_nous_tools(brain_stub, heart_stub, settings)
    ingest_document = tools["ingest_document"]

    result = await ingest_document(
        content="x" * 200,
        source_ref="test://nowhere",
        _session_id="nonexistent-session",
    )

    assert "no active episode" in result["content"][0]["text"].lower()


async def test_ingest_document_rejects_short_content(db, agent_id, fixture_episode):
    """Content below min_chars → reject without DB write."""
    from unittest.mock import AsyncMock, MagicMock

    from nous.api.tools import create_nous_tools
    from nous.config import Settings

    settings = Settings().model_copy(update={"agent_id": agent_id, "document_chunk_min_chars": 500})
    heart_stub = MagicMock()
    heart_stub.db = db
    heart_stub.agent_id = agent_id
    heart_stub._embeddings = MagicMock()
    heart_stub._embeddings.embed_batch = AsyncMock(return_value=[])

    brain_stub = MagicMock()
    tools = create_nous_tools(brain_stub, heart_stub, settings)
    ingest_document = tools["ingest_document"]

    result = await ingest_document(
        content="too short",
        source_ref="test://short",
        episode_id=str(fixture_episode),
    )

    assert "Ingest skipped" in result["content"][0]["text"]

    # Cleanup
    async with db.session() as session:
        await session.execute(
            text("DELETE FROM heart.episodes WHERE agent_id = :a"),
            {"a": agent_id},
        )
        await session.commit()
