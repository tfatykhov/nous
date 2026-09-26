"""Unit tests for ConversationDeduplicator — async, uses MockEmbeddingProvider."""

import pytest

from tests.conftest import MockEmbeddingProvider
from nous.cognitive.dedup import ConversationDeduplicator


# ---------------------------------------------------------------------------
# No conversation messages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_dedup_without_messages():
    """Empty conversation -> all memories returned as not redundant."""
    dedup = ConversationDeduplicator(embedding_provider=MockEmbeddingProvider())
    results = await dedup.check(
        memories=[("mem-1", "some memory content")],
        conversation_messages=[],
    )

    assert len(results) == 1
    assert results[0].is_redundant is False
    assert results[0].max_similarity == 0.0


# ---------------------------------------------------------------------------
# Embedding-based dedup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redundant_memory_filtered():
    """Memory with same text as conversation message -> is_redundant=True.

    MockEmbeddingProvider produces identical embeddings for identical text,
    giving cosine similarity = 1.0 > threshold (0.85).
    """
    dedup = ConversationDeduplicator(embedding_provider=MockEmbeddingProvider())
    results = await dedup.check(
        memories=[("mem-1", "PostgreSQL is our database choice")],
        conversation_messages=["PostgreSQL is our database choice"],
    )

    assert len(results) == 1
    assert results[0].is_redundant is True
    assert results[0].max_similarity > 0.85


@pytest.mark.asyncio
async def test_novel_memory_kept():
    """Memory with completely different text -> is_redundant=False.

    MockEmbeddingProvider produces different embeddings for different text,
    giving low cosine similarity < threshold (0.85).
    """
    dedup = ConversationDeduplicator(embedding_provider=MockEmbeddingProvider())
    results = await dedup.check(
        memories=[("mem-1", "Redis caching strategy for session data")],
        conversation_messages=["The weather today is very sunny and warm"],
    )

    assert len(results) == 1
    assert results[0].is_redundant is False


# ---------------------------------------------------------------------------
# Keyword fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_keyword_overlap():
    """No embedding provider -> keyword-based dedup.

    High keyword overlap should trigger is_redundant=True.
    Containment threshold is 0.5.
    """
    dedup = ConversationDeduplicator(embedding_provider=None)
    results = await dedup.check(
        memories=[("mem-1", "the quick brown fox jumps over the lazy dog")],
        conversation_messages=["the quick brown fox jumps over the lazy dog"],
    )

    assert len(results) == 1
    assert results[0].is_redundant is True
    assert results[0].max_similarity > 0.5
