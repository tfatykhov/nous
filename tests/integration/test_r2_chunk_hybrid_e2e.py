"""R2 integration test: hybrid chunk search against real Postgres.

Verifies the actual SQL (FTS leg over the migration-050 search_tsv GIN
index, `= ANY(:ids)` content fetch) that the mocked unit tests in
tests/test_r2_chunk_hybrid.py cannot exercise.

Discrimination setup: the gold chunk contains a rare token but has NO
embedding — invisible to the vector-only leg (``embedding IS NOT NULL``),
findable only via FTS. Mirrors the MAB CR failure shape where the gold
token was present verbatim but cosine rank was 16-50.
"""
from __future__ import annotations

import os
import uuid

import pytest


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
async def eval_db():
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
    from nous.storage.database import Database

    db = Database(Settings())
    await db.connect()
    yield db
    await db.disconnect()


@pytest.mark.asyncio
async def test_hybrid_finds_keyword_only_gold_chunk(eval_db):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from sqlalchemy import text

    from nous.api.retrieval_pipeline import _search_episode_chunks

    agent_id = f"r2-test-{uuid.uuid4().hex[:8]}"
    episode_id = uuid.uuid4()
    gold_id = uuid.uuid4()      # rare token, NO embedding → FTS-only
    decoy_id = uuid.uuid4()     # embedded, no token

    vec = "[" + ",".join(["0.01"] * 1536) + "]"

    async with eval_db.session() as s:
        await s.execute(text(
            "INSERT INTO heart.episodes (id, agent_id, summary) "
            "VALUES (:id, :a, 'r2 test episode')"
        ), {"id": episode_id, "a": agent_id})
        await s.execute(text(
            "INSERT INTO heart.episode_chunks (id, agent_id, episode_id, chunk_index, content) "
            "VALUES (:id, :a, :ep, 0, 'The zanzibarite kumquat ledger was moved to Reykjavik.')"
        ), {"id": gold_id, "a": agent_id, "ep": episode_id})
        await s.execute(text(
            "INSERT INTO heart.episode_chunks (id, agent_id, episode_id, chunk_index, content, embedding) "
            "VALUES (:id, :a, :ep, 1, 'Unrelated small talk about the weather.', CAST(:v AS vector))"
        ), {"id": decoy_id, "a": agent_id, "ep": episode_id, "v": vec})
        await s.commit()

    embedder = MagicMock()
    embedder.embed = AsyncMock(return_value=[0.01] * 1536)
    heart = SimpleNamespace(db=eval_db, _embeddings=embedder, agent_id=agent_id)

    try:
        off = await _search_episode_chunks(
            heart=heart, query="zanzibarite kumquat ledger", agent_id=agent_id,
            limit=10, settings=SimpleNamespace(chunk_hybrid_search_enabled=False),
        )
        on = await _search_episode_chunks(
            heart=heart, query="zanzibarite kumquat ledger", agent_id=agent_id,
            limit=10, settings=SimpleNamespace(chunk_hybrid_search_enabled=True),
        )
    finally:
        async with eval_db.session() as s:
            await s.execute(text(
                "DELETE FROM heart.episodes WHERE id = :id"  # chunks cascade
            ), {"id": episode_id})
            await s.commit()

    # Vector-only: gold chunk (no embedding) is invisible
    off_ids = [r[0] for r in off]
    assert gold_id not in off_ids
    assert decoy_id in off_ids

    # Hybrid: FTS leg surfaces the gold chunk. (Ordering vs the decoy is
    # legitimately vector_weight-dependent — a perfect-cosine decoy MAY
    # outrank a keyword-only gold at the default 0.7 vector weight; the R2
    # win is that the gold is retrievable at all.)
    on_ids = [r[0] for r in on]
    assert gold_id in on_ids

    # 4-tuple shape with episode_id, scores on the normalized RRF [0,1] scale
    gold_row = next(r for r in on if r[0] == gold_id)
    assert gold_row[1].startswith("The zanzibarite")
    assert 0.0 < gold_row[2] <= 1.0
    assert gold_row[3] == episode_id
