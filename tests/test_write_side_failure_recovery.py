"""Write-side failures that used to be silent or sticky (H-5, I-5, K-8).

- An episode whose embed failed keeps ``embedding IS NULL`` forever: nothing
  re-embedded it, so it was invisible to vector search and the densifier. The
  sleep cycle now re-embeds a bounded batch of closed episodes per run.
- A SQL error inside the censor check's semantic match aborted the caller's
  transaction, so the keyword match after it raised too.
- One episode whose F057 relink hit a DB error aborted the shared session: the
  next episode's anchor SELECT raised, the phase failed before its commit, and
  the same batch was re-selected every cycle.

All three need real Postgres (an aborted transaction is a server-side state).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import text as sql_text

from nous.config import Settings
from nous.events import EventBus

pytestmark = pytest.mark.postgres_only


def _sleep_handler(heart, settings):
    from nous.handlers.sleep_handler import SleepHandler

    bus = MagicMock(spec=EventBus)
    bus.on = MagicMock()
    bus.emit = AsyncMock()
    return SleepHandler(AsyncMock(), heart, settings, bus, AsyncMock())


async def _add_agent(db, agent_id: str) -> None:
    async with db.session() as s:
        await s.execute(
            sql_text("INSERT INTO nous_system.agents (id, name) VALUES (:aid, :aid) ON CONFLICT (id) DO NOTHING"),
            {"aid": agent_id},
        )
        await s.commit()


async def _drop_agent(db, agent_id: str) -> None:
    async with db.session() as s:
        for stmt in (
            "DELETE FROM brain.graph_edges WHERE agent_id = :aid",
            "DELETE FROM heart.facts WHERE agent_id = :aid",
            "DELETE FROM heart.episodes WHERE agent_id = :aid",
            "DELETE FROM nous_system.events WHERE agent_id = :aid",
            "DELETE FROM nous_system.agents WHERE id = :aid",
        ):
            await s.execute(sql_text(stmt), {"aid": agent_id})
        await s.commit()


# ---------------------------------------------------------------------------
# H-5: closed episodes with a NULL embedding are re-embedded by the sleep cycle
# ---------------------------------------------------------------------------


async def test_sleep_reembeds_a_closed_episode_with_a_null_embedding(db, mock_embeddings):
    from nous.heart import Heart
    from nous.heart.schemas import EpisodeInput

    agent_id = f"reembed-{uuid4().hex[:8]}"
    await _add_agent(db, agent_id)
    try:
        settings = Settings(_env_file=None)
        object.__setattr__(settings, "agent_id", agent_id)
        heart = Heart(db, settings, embedding_provider=mock_embeddings)

        ep = await heart.start_episode(EpisodeInput(title="Deploy", summary="Rolled out the cache fix"))
        await heart.end_episode(ep.id, "success", lessons_learned=["warm the cache first"])
        abandoned = await heart.start_episode(EpisodeInput(title="Stub", summary="no content"), dedup=False)
        await heart.end_episode(abandoned.id, "abandoned")
        vec_sql = "SELECT embedding::text AS vec FROM heart.episodes WHERE id = :id"
        async with db.session() as s:
            end_vector = (await s.execute(sql_text(vec_sql), {"id": ep.id})).one().vec
            await s.execute(
                sql_text("UPDATE heart.episodes SET embedding = NULL WHERE id = ANY(:ids)"),
                {"ids": [ep.id, abandoned.id]},
            )
            await s.commit()

        handler = _sleep_handler(heart, settings)
        assert handler._episode_summarizer is None  # the backfill must not depend on F060's summarizer
        sleep_stats: dict = {}
        assert await handler._phase_recover_abandoned_episodes(sleep_stats) is True

        async with db.session() as s:
            backfilled = (await s.execute(sql_text(vec_sql), {"id": ep.id})).one().vec
            abandoned_vec = (await s.execute(sql_text(vec_sql), {"id": abandoned.id})).one().vec
        assert backfilled is not None, "the closed episode's NULL embedding was not backfilled"
        assert backfilled == end_vector, "the backfill embedded different text from _end()"
        assert abandoned_vec is None, "an abandoned episode was re-embedded"
        assert sleep_stats.get("episodes_reembedded") == 1
        await heart.close()
    finally:
        await _drop_agent(db, agent_id)


class _ScriptedEmbedder:
    """Raises ``error`` for texts containing ``marker``; records every text it is given."""

    def __init__(self, marker: str, error: Exception) -> None:
        self.marker = marker
        self.error = error
        self.texts: list[str] = []

    async def embed(self, text: str, fail_fast: bool = False) -> list[float]:
        self.texts.append(text)
        if self.marker in text:
            raise self.error
        return [1.0] + [0.0] * 1535


def _http_error(status: int):
    import httpx

    request = httpx.Request("POST", "https://api.openai.com/v1/embeddings")
    return httpx.HTTPStatusError(f"{status}", request=request, response=httpx.Response(status, request=request))


async def _closed_episodes_without_vectors(db, agent_id: str, summaries: list[str]) -> list:
    """Insert closed, vector-less episodes; the first summary is the newest."""
    now = datetime.now(UTC)
    ids = []
    async with db.session() as s:
        for age, summary in enumerate(summaries):
            ep_id = uuid4()
            ids.append(ep_id)
            await s.execute(
                sql_text(
                    "INSERT INTO heart.episodes "
                    "(id, agent_id, title, summary, outcome, started_at, ended_at, active, tags) "
                    "VALUES (:id, :aid, 'Episode', :s, 'success', :t, :t, false, '{}')"
                ),
                {"id": ep_id, "aid": agent_id, "s": summary, "t": now - timedelta(hours=age + 1)},
            )
        await s.commit()
    return ids


async def _has_vector(db, ep_id) -> bool:
    async with db.session() as s:
        row = await s.execute(
            sql_text("SELECT embedding IS NOT NULL AS has_vec FROM heart.episodes WHERE id = :id"), {"id": ep_id}
        )
        return row.one().has_vec


@pytest.mark.parametrize("status", [400, 413, 422])
async def test_a_rejected_newest_episode_does_not_block_older_ones(db, status):
    """A 400/413/422 is about that row's text, not the provider: skip it, keep going.

    Before, the backfill stopped at any failure, and newest-first ordering
    made the same rejected row stop every later cycle too.
    """
    from nous.heart.episodes import EpisodeManager

    agent_id = f"reembed-{status}-{uuid4().hex[:8]}"
    await _add_agent(db, agent_id)
    try:
        rejected, older = await _closed_episodes_without_vectors(db, agent_id, ["REJECT me", "a normal summary"])
        manager = EpisodeManager(db, _ScriptedEmbedder("REJECT", _http_error(status)), agent_id)

        assert await manager.reembed_missing() == 1
        assert not await _has_vector(db, rejected)
        assert await _has_vector(db, older), "a rejected row stopped the older backlog"
    finally:
        await _drop_agent(db, agent_id)


@pytest.mark.parametrize("status", [401, 403, 429, 503])
async def test_a_provider_failure_still_stops_the_backfill(db, status):
    """A bad key, a forbidden account, rate limiting or a server error fails every row alike."""
    from nous.heart.episodes import EpisodeManager

    agent_id = f"reembed-{status}-{uuid4().hex[:8]}"
    await _add_agent(db, agent_id)
    try:
        _failing, older = await _closed_episodes_without_vectors(db, agent_id, ["FAIL here", "a normal summary"])
        embedder = _ScriptedEmbedder("FAIL", _http_error(status))
        manager = EpisodeManager(db, embedder, agent_id)

        assert await manager.reembed_missing() == 0
        assert len(embedder.texts) == 1, "kept calling a provider that is down"
        assert not await _has_vector(db, older)
    finally:
        await _drop_agent(db, agent_id)


async def test_backfill_text_is_capped_like_procedure_embeds(db):
    from nous.heart.episodes import EpisodeManager

    agent_id = f"reembed-cap-{uuid4().hex[:8]}"
    await _add_agent(db, agent_id)
    try:
        (huge,) = await _closed_episodes_without_vectors(db, agent_id, ["x" * 40_000])
        embedder = _ScriptedEmbedder("never", RuntimeError("unused"))
        manager = EpisodeManager(db, embedder, agent_id)

        assert await manager.reembed_missing() == 1
        assert len(embedder.texts[0]) == 16_000
        assert await _has_vector(db, huge)
    finally:
        await _drop_agent(db, agent_id)


class _TokenLimitEmbedder:
    """Rejects input over the 16,000-char cap with a 400, as the API does over its token limit."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    async def embed(self, text: str, fail_fast: bool = False) -> list[float]:
        self.texts.append(text)
        if len(text) > 16_000:
            raise _http_error(400)
        return [1.0] + [0.0] * 1535


async def test_an_oversized_summary_gets_a_vector_at_start(db):
    """The live writes cap their embed text too, so they do not rely on the backfill."""
    from nous.heart.episodes import EpisodeManager
    from nous.heart.schemas import EpisodeInput

    agent_id = f"embed-cap-start-{uuid4().hex[:8]}"
    await _add_agent(db, agent_id)
    try:
        embedder = _TokenLimitEmbedder()
        ep = await EpisodeManager(db, embedder, agent_id).start(EpisodeInput(title="Long", summary="y" * 40_000))
        assert await _has_vector(db, ep.id), "start() stored no vector for an oversized summary"
        assert len(embedder.texts[0]) == 16_000
    finally:
        await _drop_agent(db, agent_id)


async def test_an_oversized_summary_gets_a_vector_at_end(db):
    from nous.heart.episodes import EpisodeManager

    agent_id = f"embed-cap-end-{uuid4().hex[:8]}"
    await _add_agent(db, agent_id)
    try:
        ep_id = uuid4()
        async with db.session() as s:  # an open episode whose start() embed failed
            await s.execute(
                sql_text(
                    "INSERT INTO heart.episodes (id, agent_id, title, summary, started_at, active, tags) "
                    "VALUES (:id, :aid, 'Long', :s, now(), true, '{}')"
                ),
                {"id": ep_id, "aid": agent_id, "s": "y" * 40_000},
            )
            await s.commit()
        embedder = _TokenLimitEmbedder()
        await EpisodeManager(db, embedder, agent_id).end(ep_id, "success", lessons_learned=["keep it short"])
        assert await _has_vector(db, ep_id), "_end() stored no vector for an oversized summary"
        assert len(embedder.texts[0]) == 16_000
    finally:
        await _drop_agent(db, agent_id)


# ---------------------------------------------------------------------------
# I-5: a SQL error in the censor's semantic match leaves the keyword match working
# ---------------------------------------------------------------------------


async def test_censor_semantic_sql_error_does_not_break_keyword_match(heart, session):
    from nous.heart import CensorInput

    await heart.add_censor(
        CensorInput(trigger_pattern="drop table", reason="destructive", action="steer"),
        session=session,
    )

    async def poisoned_semantic_match(embedding, domain, session):
        await session.execute(sql_text("SELECT 1/0"))
        return []

    heart.censors._semantic_match = poisoned_semantic_match

    matches = await heart.check_censors("please drop table users", session=session)
    assert [m.trigger_pattern for m in matches] == ["drop table"]


# ---------------------------------------------------------------------------
# K-8: one poisoned episode is skipped; the rest of the F057 batch commits
# ---------------------------------------------------------------------------


async def test_f057_skips_a_poisoned_episode_and_commits_the_rest(db, mock_embeddings):
    from nous.brain.graph_linker import GraphLinker
    from nous.heart import Heart

    agent_id = f"f057-poison-{uuid4().hex[:8]}"
    old = datetime.now(UTC) - timedelta(days=3)
    # Oldest first: the poisoned episode is processed before the healthy ones.
    poisoned, healthy_a, healthy_b = uuid4(), uuid4(), uuid4()
    facts = {poisoned: uuid4(), healthy_a: uuid4(), healthy_b: uuid4()}

    await _add_agent(db, agent_id)
    try:
        async with db.session() as s:
            for offset, ep_id in enumerate((poisoned, healthy_a, healthy_b)):
                await s.execute(
                    sql_text(
                        "INSERT INTO heart.episodes (id, agent_id, summary, started_at, active, tags) "
                        "VALUES (:id, :aid, :s, :t, true, '{}')"
                    ),
                    {"id": ep_id, "aid": agent_id, "s": f"episode {offset}", "t": old + timedelta(minutes=offset)},
                )
                await s.execute(
                    sql_text(
                        "INSERT INTO heart.facts (id, agent_id, content, source_episode_id, active, created_at) "
                        "VALUES (:id, :aid, :c, :eid, true, :t)"
                    ),
                    {"id": facts[ep_id], "aid": agent_id, "c": f"fact for episode {offset}", "eid": ep_id, "t": old},
                )
            await s.commit()

        settings = Settings(_env_file=None)
        object.__setattr__(settings, "agent_id", agent_id)
        object.__setattr__(settings, "episode_relink_enabled", True)
        object.__setattr__(settings, "episode_relink_min_age_hours", 1)
        object.__setattr__(settings, "episode_relink_max_per_cycle", 10)
        heart = Heart(db, settings, embedding_provider=mock_embeddings)
        handler = _sleep_handler(heart, settings)

        linker = GraphLinker(db, mock_embeddings, settings, agent_id)
        real_link = linker.link_episode_deterministic

        async def link_or_poison(episode_id, decision_ids, fact_ids, session):
            if episode_id == poisoned:
                await session.execute(sql_text("SELECT 1/0"))  # a DB error: aborts the transaction
            return await real_link(episode_id=episode_id, decision_ids=decision_ids, fact_ids=fact_ids, session=session)

        linker.link_episode_deterministic = link_or_poison
        handler._graph_linker = linker

        sleep_stats: dict = {}
        assert await handler._phase_relink_open_episodes(sleep_stats) is True
        assert sleep_stats.get("episodes_relinked") == 2
        assert sleep_stats.get("episode_relink_errors") == 1

        async with db.session() as s:
            linked = {
                r.target_id
                for r in (
                    await s.execute(
                        sql_text(
                            "SELECT target_id FROM brain.graph_edges "
                            "WHERE agent_id = :aid AND target_type = 'episode' AND source_type = 'fact'"
                        ),
                        {"aid": agent_id},
                    )
                ).all()
            }
        assert linked == {healthy_a, healthy_b}
        await heart.close()
    finally:
        await _drop_agent(db, agent_id)
