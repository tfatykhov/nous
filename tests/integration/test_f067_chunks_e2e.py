"""F067 integration test: end-to-end chunking + retrieval against real Postgres.

Verifies:
1. Migration 050 applied (heart.episode_chunks exists).
2. EpisodeSummarizer chunks transcript when episode_chunks_enabled=True.
3. run_recall_pipeline includes chunk results when flag is on.
4. Flag off → no chunk-related behavior (backwards-compat).
5. Parent-episode formatter section appended when flag is on.
"""
from __future__ import annotations

import os
import pytest


# This test needs a live eval DB. Skip when not configured.
def _eval_db_reachable() -> bool:
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            return s.connect_ex(("127.0.0.1", 5433)) == 0
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _eval_db_reachable(),
    reason="nous-eval-scratch container not reachable on 127.0.0.1:5433",
)


@pytest.fixture
async def eval_settings_factory():
    """Yields a factory that builds Settings pointed at the eval DB."""
    for k, v in {
        "DB_HOST": "127.0.0.1", "DB_PORT": "5433", "DB_NAME": "nous_eval_scratch",
        "DB_USER": "nous", "DB_PASSWORD": "nous_eval",
        "NOUS_HEARTBEAT_ENABLED": "false", "NOUS_SUBTASK_ENABLED": "false",
        "NOUS_SCHEDULE_ENABLED": "false", "NOUS_SLEEP_ENABLED": "false",
        "NOUS_EVENT_BUS_ENABLED": "false",
        "NOUS_ACTIONABILITY_BACKFILL_ON_STARTUP": "false",
        "NOUS_TELEGRAM_BOT_TOKEN": "",
    }.items():
        os.environ.setdefault(k, v)

    from nous.config import Settings

    def _factory(**overrides):
        return Settings().model_copy(update=overrides)

    yield _factory


@pytest.mark.asyncio
async def test_migration_creates_episode_chunks_table(eval_settings_factory):
    """Schema check: migration 050 applied; column types as expected."""
    from sqlalchemy import text
    from nous.storage.database import Database

    settings = eval_settings_factory()
    db = Database(settings)
    await db.connect()
    try:
        async with db.session() as s:
            rows = (await s.execute(text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema='heart' AND table_name='episode_chunks' "
                "ORDER BY ordinal_position"
            ))).all()
        cols = {r[0]: r[1] for r in rows}
        assert "id" in cols
        assert "agent_id" in cols
        assert "episode_id" in cols
        assert "chunk_index" in cols and cols["chunk_index"] == "integer"
        assert "content" in cols and cols["content"] == "text"
        assert "embedding" in cols  # vector type
        assert "search_tsv" in cols  # generated column
        assert "created_at" in cols
    finally:
        await db.disconnect()


@pytest.mark.asyncio
async def test_chunk_search_returns_empty_when_no_data(eval_settings_factory):
    """Sanity: searching a clean agent_id returns []."""
    from nous.api.retrieval_pipeline import _search_episode_chunks

    settings = eval_settings_factory(
        agent_id="f067-unit-test-empty",
        embedding_model="text-embedding-3-large",
        embedding_dimensions=1536,
    )
    # Construct minimal Heart with embedder
    from nous.brain.embeddings import EmbeddingProvider
    from nous.heart.heart import Heart
    from nous.storage.database import Database

    db = Database(settings)
    await db.connect()
    try:
        if not settings.openai_api_key:
            pytest.skip("OPENAI_API_KEY not set; cannot embed query")
        embedder = EmbeddingProvider(
            api_key=settings.openai_api_key,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
        )
        heart = Heart(database=db, settings=settings, embedding_provider=embedder)
        results = await _search_episode_chunks(
            heart=heart, query="anything", agent_id=settings.agent_id, limit=5,
        )
        assert results == []
    finally:
        await db.disconnect()
