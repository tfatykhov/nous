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
    # Dynamic mock: return one zero-vector per input chunk so the new
    # length-check guard in ingest_document doesn't refuse the ingest.
    mock_embedder.embed_batch = AsyncMock(side_effect=lambda texts: [[0.0] * 1536 for _ in texts])

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


async def test_ingest_document_idempotent_for_same_source_ref(db, agent_id, fixture_episode):
    """Codex P1 (2026-05-26): a second call with the same source_ref under
    the same episode must NOT duplicate content. Pre-check rejects with
    a clear message; row count stays stable."""
    from unittest.mock import AsyncMock, MagicMock

    from nous.api.tools import create_nous_tools
    from nous.config import Settings

    settings = Settings().model_copy(update={"agent_id": agent_id, "document_chunk_min_chars": 50})
    mock_embedder = MagicMock()
    # Dynamic mock: return one zero-vector per input chunk so the new
    # length-check guard in ingest_document doesn't refuse the ingest.
    mock_embedder.embed_batch = AsyncMock(side_effect=lambda texts: [[0.0] * 1536 for _ in texts])
    heart_stub = MagicMock()
    heart_stub.db = db
    heart_stub.agent_id = agent_id
    heart_stub._embeddings = mock_embedder
    brain_stub = MagicMock()
    tools = create_nous_tools(brain_stub, heart_stub, settings)
    ingest_document = tools["ingest_document"]

    content = "Lorem ipsum dolor sit amet. " * 100  # 2.7K chars

    # First call: succeeds, writes chunks.
    r1 = await ingest_document(
        content=content,
        source_ref="https://example.com/dup-test.pdf",
        episode_id=str(fixture_episode),
    )
    assert "Ingested" in r1["content"][0]["text"]

    async with db.session() as session:
        cnt_row = await session.execute(
            text(
                "SELECT COUNT(*) FROM heart.episode_chunks "
                "WHERE agent_id = :a AND episode_id = :e AND source_ref = :r"
            ),
            {"a": agent_id, "e": fixture_episode, "r": "https://example.com/dup-test.pdf"},
        )
        n_after_first = cnt_row.scalar() or 0
    assert n_after_first > 0, "first call should have written chunks"

    # Second call with SAME source_ref: must be rejected, no new rows.
    r2 = await ingest_document(
        content=content,
        source_ref="https://example.com/dup-test.pdf",
        episode_id=str(fixture_episode),
    )
    assert "Already ingested" in r2["content"][0]["text"]

    async with db.session() as session:
        cnt_row = await session.execute(
            text(
                "SELECT COUNT(*) FROM heart.episode_chunks "
                "WHERE agent_id = :a AND episode_id = :e AND source_ref = :r"
            ),
            {"a": agent_id, "e": fixture_episode, "r": "https://example.com/dup-test.pdf"},
        )
        n_after_second = cnt_row.scalar() or 0
    assert n_after_second == n_after_first, (
        f"second call duplicated rows: {n_after_first} -> {n_after_second}"
    )

    # Third call with DIFFERENT source_ref: should succeed (different doc).
    r3 = await ingest_document(
        content=content,
        source_ref="https://example.com/different-doc.pdf",
        episode_id=str(fixture_episode),
    )
    assert "Ingested" in r3["content"][0]["text"]

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


async def test_ingest_document_concurrent_different_source_refs_no_collision(db, agent_id, fixture_episode):
    """Codex P1 round 4 (2026-05-26): two concurrent ingest_document
    calls targeting the same episode with DIFFERENT source_refs must
    not collide on chunk_index. Pre-fix: separate (episode,source_ref)
    locks let both see the same MAX+1 start_idx, both INSERT, ON CONFLICT
    silently dropped one caller's rows. Episode-scoped lock fixes this.

    Verifies: both calls return success AND all chunks from both docs
    end up in the DB with disjoint chunk_index ranges (no row loss).
    """
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from nous.api.tools import create_nous_tools
    from nous.config import Settings

    settings = Settings().model_copy(update={"agent_id": agent_id, "document_chunk_min_chars": 50})
    mock_embedder = MagicMock()
    mock_embedder.embed_batch = AsyncMock(side_effect=lambda texts: [[0.0] * 1536 for _ in texts])
    heart_stub = MagicMock()
    heart_stub.db = db
    heart_stub.agent_id = agent_id
    heart_stub._embeddings = mock_embedder
    brain_stub = MagicMock()
    tools = create_nous_tools(brain_stub, heart_stub, settings)
    ingest_document = tools["ingest_document"]

    content_a = "Alpha alpha alpha. " * 150  # ~2.8K chars
    content_b = "Beta beta beta. " * 150     # ~2.4K chars

    # Fire both concurrently against the same episode.
    r_a, r_b = await asyncio.gather(
        ingest_document(
            content=content_a,
            source_ref="test://doc-A.pdf",
            episode_id=str(fixture_episode),
        ),
        ingest_document(
            content=content_b,
            source_ref="test://doc-B.pdf",
            episode_id=str(fixture_episode),
        ),
    )

    # Both should have succeeded (no "Already ingested" since different source_refs).
    assert "Ingested" in r_a["content"][0]["text"], f"A: {r_a['content'][0]['text']!r}"
    assert "Ingested" in r_b["content"][0]["text"], f"B: {r_b['content'][0]['text']!r}"

    # Pull all chunks for this episode and verify (1) disjoint chunk_index
    # ranges per source_ref, (2) no duplicate chunk_index across the two.
    async with db.session() as session:
        rows = await session.execute(
            text(
                "SELECT chunk_index, source_ref FROM heart.episode_chunks "
                "WHERE agent_id = :a AND episode_id = :e "
                "ORDER BY chunk_index"
            ),
            {"a": agent_id, "e": fixture_episode},
        )
        all_chunks = rows.fetchall()

    chunk_indices = [c.chunk_index for c in all_chunks]
    assert len(chunk_indices) == len(set(chunk_indices)), (
        f"duplicate chunk_index detected (ON CONFLICT silently dropped rows): {chunk_indices}"
    )

    by_source = {}
    for c in all_chunks:
        by_source.setdefault(c.source_ref, []).append(c.chunk_index)

    assert "test://doc-A.pdf" in by_source, "doc A wrote no chunks"
    assert "test://doc-B.pdf" in by_source, "doc B wrote no chunks"

    a_indices = sorted(by_source["test://doc-A.pdf"])
    b_indices = sorted(by_source["test://doc-B.pdf"])
    assert not (set(a_indices) & set(b_indices)), (
        f"chunk_index overlap between docs: A={a_indices} B={b_indices}"
    )

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


async def test_ingest_document_rejects_partial_embed_batch(db, agent_id, fixture_episode):
    """Codex P2 round 3 (2026-05-26): if embedder returns fewer vectors
    than chunks, the tool must refuse to write a partial ingest rather
    than silently zip-truncate."""
    from unittest.mock import AsyncMock, MagicMock

    from nous.api.tools import create_nous_tools
    from nous.config import Settings

    settings = Settings().model_copy(update={"agent_id": agent_id, "document_chunk_min_chars": 50})
    # Return ONE vector for many chunks → length mismatch.
    mock_embedder = MagicMock()
    mock_embedder.embed_batch = AsyncMock(return_value=[[0.0] * 1536])
    heart_stub = MagicMock()
    heart_stub.db = db
    heart_stub.agent_id = agent_id
    heart_stub._embeddings = mock_embedder
    brain_stub = MagicMock()
    tools = create_nous_tools(brain_stub, heart_stub, settings)
    ingest_document = tools["ingest_document"]

    # Content large enough to produce several chunks (>1 vector required).
    content = "Sentence sentence sentence. " * 200  # ~5.6K chars
    result = await ingest_document(
        content=content,
        source_ref="test://partial-embed",
        episode_id=str(fixture_episode),
    )

    text_out = result["content"][0]["text"]
    assert "refusing to write a partial ingest" in text_out, f"unexpected: {text_out!r}"

    # No rows should have been written.
    async with db.session() as session:
        cnt_row = await session.execute(
            text(
                "SELECT COUNT(*) FROM heart.episode_chunks "
                "WHERE agent_id = :a AND episode_id = :e"
            ),
            {"a": agent_id, "e": fixture_episode},
        )
        n = cnt_row.scalar() or 0
    assert n == 0, f"partial ingest wrote {n} rows when it should have refused"

    # Cleanup
    async with db.session() as session:
        await session.execute(
            text("DELETE FROM heart.episodes WHERE agent_id = :a"),
            {"a": agent_id},
        )
        await session.commit()


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
