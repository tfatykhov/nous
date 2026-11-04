"""H-7: the turn-aware chunker on the store paths (episode_chunk_turn_aware).

Postgres only: the summarizer's chunk store and the backfill script commit through
their own sessions, so each test uses its own agent and deletes its rows.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from nous.config import Settings
from nous.handlers.episode_summarizer import EpisodeSummarizer
from nous.heart.chunking import chunk_text, chunk_transcript

pytestmark = pytest.mark.postgres_only

TRANSCRIPT = "\n\n".join(
    [
        "User: " + "Where did we leave the migration plan? " * 6,
        "Assistant: " + "We kept the backfill separate from the schema change. " * 30,
        "User: " + "And who owns the rollback? " * 4,
        "Assistant: " + "The operator owns it, after the dry run. " * 12,
    ]
)


class _Embedder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[0.1] * 1536 for _ in texts]


def _settings(agent: str, **overrides) -> Settings:
    return Settings().model_copy(update={"agent_id": agent, "episode_chunk_max_per_episode": 0, **overrides})


@pytest.fixture
async def episode(db):
    agent = f"h7-{uuid.uuid4().hex[:8]}"
    episode_id = uuid.uuid4()
    async with db.session() as s:
        await s.execute(text(
            "INSERT INTO heart.episodes (id, agent_id, session_id, summary, started_at, active) "
            "VALUES (:i, :a, 's-h7', 'x', NOW(), false)"
        ), {"i": episode_id, "a": agent})
        await s.commit()
    yield agent, episode_id
    async with db.session() as s:
        await s.execute(text("DELETE FROM heart.episode_chunks WHERE agent_id = :a"), {"a": agent})
        await s.execute(text("DELETE FROM heart.episodes WHERE agent_id = :a"), {"a": agent})
        await s.commit()


async def _rows(db, agent: str) -> list[tuple[int, str, str]]:
    async with db.session() as s:
        rows = await s.execute(text(
            "SELECT chunk_index, source_kind, content FROM heart.episode_chunks "
            "WHERE agent_id = :a ORDER BY chunk_index"
        ), {"a": agent})
        return [tuple(r) for r in rows]


def _summarizer(db, settings: Settings, embedder: _Embedder) -> EpisodeSummarizer:
    s = EpisodeSummarizer.__new__(EpisodeSummarizer)
    s._settings = settings
    s._heart = SimpleNamespace(db=db, _embeddings=embedder)
    s._embedder = None
    return s


def _windows(settings: Settings) -> list[str]:
    return chunk_text(
        TRANSCRIPT, chunk_size=settings.episode_chunk_size, overlap=settings.episode_chunk_overlap,
        min_chars=settings.episode_chunk_min_transcript_chars,
    )


def _turns(settings: Settings) -> list[str]:
    return chunk_transcript(
        TRANSCRIPT, chunk_size=settings.episode_chunk_size, overlap=settings.episode_chunk_overlap,
        min_chars=settings.episode_chunk_min_transcript_chars,
    )


async def test_flag_off_stores_the_window_chunks(db, episode):
    agent, episode_id = episode
    settings = _settings(agent)
    assert settings.episode_chunk_turn_aware is False
    await _summarizer(db, settings, _Embedder())._chunk_and_store_transcript(episode_id, agent, TRANSCRIPT)
    assert [c for _i, _k, c in await _rows(db, agent)] == _windows(settings)


async def test_flag_on_stores_turn_aware_chunks(db, episode):
    agent, episode_id = episode
    settings = _settings(agent, episode_chunk_turn_aware=True)
    embedder = _Embedder()
    written = await _summarizer(db, settings, embedder)._chunk_and_store_transcript(episode_id, agent, TRANSCRIPT)
    expected = _turns(settings)
    assert expected != _windows(settings)
    assert written == len(expected)
    assert embedder.calls == [expected]
    rows = await _rows(db, agent)
    assert [c for _i, _k, c in rows] == expected
    assert {k for _i, k, _c in rows} == {"dialogue"}


async def test_rerun_is_stopped_by_the_dialogue_check_not_an_index_conflict(db, episode):
    # A document chunk already holds index 0, so the dialogue set is allocated from
    # MAX+1. A second run would allocate past the first set's indexes and never hit
    # the (episode_id, chunk_index) conflict: only the per-kind existence check under
    # the advisory lock keeps it from appending a second copy.
    agent, episode_id = episode
    async with db.session() as s:
        await s.execute(text(
            "INSERT INTO heart.episode_chunks (agent_id, episode_id, chunk_index, content, source_kind) "
            "VALUES (:a, :e, 0, 'a document chunk', 'document')"
        ), {"a": agent, "e": episode_id})
        await s.commit()
    settings = _settings(agent, episode_chunk_turn_aware=True)
    summarizer = _summarizer(db, settings, _Embedder())

    first = await summarizer._chunk_and_store_transcript(episode_id, agent, TRANSCRIPT)
    rows = await _rows(db, agent)
    second = await summarizer._chunk_and_store_transcript(episode_id, agent, TRANSCRIPT)

    assert first == len(_turns(settings)) and second == 0
    assert await _rows(db, agent) == rows
    assert [i for i, k, _c in rows if k == "dialogue"] == list(range(1, first + 1))


async def test_backfill_honours_the_flag_except_in_repair(db, episode):
    from scripts import backfill_episode_chunks as backfill

    agent, episode_id = episode
    settings = _settings(agent, episode_chunk_turn_aware=True)
    stats = backfill.Stats()

    assert await backfill._process_episode(db, _Embedder(), settings, str(episode_id), agent, TRANSCRIPT, stats)
    assert [c for _i, _k, c in await _rows(db, agent)] == _turns(settings)

    # --repair-dialogue rebuilds with the window chunker whatever the flag says.
    assert await backfill._process_episode(
        db, _Embedder(), settings, str(episode_id), agent, TRANSCRIPT, stats, repair=True,
    )
    assert [c for _i, _k, c in await _rows(db, agent)] == _windows(settings)
