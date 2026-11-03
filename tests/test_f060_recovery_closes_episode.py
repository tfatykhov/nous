"""F060 recovery closes the episode it recovers (H-3/K-4), on real Postgres.

Loop A of ``SleepHandler._phase_recover_abandoned_episodes`` used to write
``structured_summary`` and leave the row ``active=true, ended_at NULL,
outcome NULL`` for good, so a recovered episode ranked as a live one forever.
The phase now closes every open episode that already has a structured summary,
is older than the min age, and whose session has had no working-memory write
inside that window. A session that is still live keeps its episode open until
a later cycle finds it idle.

The phase opens its own sessions on ``heart.db``, so these tests commit rows
under a per-test agent_id and delete them afterwards.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text

from nous.brain.graph_constants import episode_dead_sql, episode_decisions_query, episode_live_sql
from nous.config import Settings
from nous.handlers.sleep_handler import SleepHandler
from nous.heart import Heart
from nous.storage.models import Episode

pytestmark = pytest.mark.postgres_only

_PLAIN_SUMMARY = "User asked how to rotate the NOUS_MAX_TOKENS setting"


def _settings(agent_id: str) -> Settings:
    s = Settings(_env_file=None)
    object.__setattr__(s, "agent_id", agent_id)
    object.__setattr__(s, "abandoned_recovery_enabled", True)
    object.__setattr__(s, "abandoned_recovery_min_age_hours", 24)
    object.__setattr__(s, "abandoned_recovery_max_per_cycle", 50)
    object.__setattr__(s, "abandoned_recovery_min_transcript_chars", 50)
    object.__setattr__(s, "abandoned_recovery_summary_fallback_enabled", True)
    object.__setattr__(s, "abandoned_recovery_min_summary_chars", 20)
    object.__setattr__(s, "abandoned_recovery_mark_abandoned_enabled", True)
    object.__setattr__(s, "abandoned_recovery_mark_age_days", 7)
    object.__setattr__(s, "abandoned_recovery_mark_max_per_cycle", 200)
    return s


class _WritingSummarizer:
    """Stands in for EpisodeSummarizer.summarize_episode: persists a summary."""

    def __init__(self, db) -> None:
        self._db = db
        self.calls: list[UUID] = []

    async def summarize_episode(self, episode_id, transcript, agent_id=None):
        self.calls.append(episode_id)
        async with self._db.session() as s:
            await s.execute(
                text("UPDATE heart.episodes SET structured_summary = CAST(:j AS jsonb) WHERE id = :id"),
                {"j": '{"title": "recovered"}', "id": episode_id},
            )
            await s.commit()
        return {"title": "recovered"}


def _handler(db, settings: Settings, summarizer) -> SleepHandler:
    heart = MagicMock()
    heart.db = db
    bus = MagicMock()
    bus.on = MagicMock()
    handler = SleepHandler(MagicMock(), heart, settings, bus, llm_client=None)
    handler._episode_summarizer = summarizer
    return handler


async def _seed_episode(db, agent_id: str, session_id: str | None, hours_ago: int) -> UUID:
    async with db.session() as s:
        row = await s.execute(
            text(
                "INSERT INTO heart.episodes (agent_id, summary, session_id, started_at) "
                "VALUES (:a, :summary, :sid, now() - make_interval(hours => :h)) RETURNING id"
            ),
            {"a": agent_id, "summary": _PLAIN_SUMMARY, "sid": session_id, "h": hours_ago},
        )
        episode_id = row.scalar_one()
        await s.commit()
    return episode_id


async def _touch_working_memory(db, agent_id: str, session_id: str) -> None:
    async with db.session() as s:
        await s.execute(
            text("INSERT INTO heart.working_memory (agent_id, session_id) VALUES (:a, :sid)"),
            {"a": agent_id, "sid": session_id},
        )
        await s.commit()


async def _row(db, episode_id: UUID):
    async with db.session() as s:
        row = await s.execute(
            text(
                "SELECT ended_at, active, outcome, duration_seconds, structured_summary, "
                f"{episode_live_sql()} AS live, {episode_dead_sql()} AS dead "
                "FROM heart.episodes WHERE id = :id"
            ),
            {"id": episode_id},
        )
        return row.one()


async def _open_episode_for_session(db, agent_id: str, session_id: str) -> UUID | None:
    """The query CognitiveLayer.warm_active_episode uses to find a live episode."""
    async with db.session() as s:
        result = await s.execute(
            select(Episode.id).where(
                Episode.agent_id == agent_id,
                Episode.session_id == session_id,
                Episode.ended_at.is_(None),
                Episode.active.is_(True),
            )
        )
        return result.scalar_one_or_none()


async def _cleanup(db, agent_id: str) -> None:
    async with db.session() as s:
        for table in (
            "brain.decisions",
            "brain.graph_edges",
            "heart.facts",
            "heart.episodes",
            "heart.working_memory",
            "nous_system.events",
        ):
            await s.execute(text(f"DELETE FROM {table} WHERE agent_id = :a"), {"a": agent_id})
        await s.commit()


@pytest.fixture
def agent_id() -> str:
    return f"f060-close-{uuid4().hex[:10]}"


async def test_recovery_closes_the_episode_it_recovers(db, agent_id):
    settings = _settings(agent_id)
    summarizer = _WritingSummarizer(db)
    handler = _handler(db, settings, summarizer)
    try:
        with_session = await _seed_episode(db, agent_id, "f060-idle-session", hours_ago=30)
        no_session = await _seed_episode(db, agent_id, None, hours_ago=30)

        stats: dict = {}
        assert await handler._phase_recover_abandoned_episodes(stats) is True

        assert set(summarizer.calls) == {with_session, no_session}
        for episode_id in (with_session, no_session):
            row = await _row(db, episode_id)
            assert row.structured_summary is not None
            assert row.ended_at is not None
            assert row.active is False
            assert row.outcome == "partial"
            # Nothing bounds it and nothing shows later activity: it ends at its
            # start, and its duration is unknown rather than invented.
            assert row.duration_seconds is None
            # Closed, not deleted: still searchable, and F053 does not prune its edges.
            assert row.live is True
            assert row.dead is False
        assert await _open_episode_for_session(db, agent_id, "f060-idle-session") is None
        assert stats["episodes_recovery_closed"] == 2
    finally:
        await _cleanup(db, agent_id)


async def test_end_after_recovery_close_does_not_raise(db, agent_id, mock_embeddings):
    """A still-running process can hold the closed id in _active_episodes and end it later."""
    settings = _settings(agent_id)
    handler = _handler(db, settings, _WritingSummarizer(db))
    heart = Heart(db, settings, embedding_provider=mock_embeddings)
    try:
        episode_id = await _seed_episode(db, agent_id, "f060-late-end", hours_ago=30)
        await handler._phase_recover_abandoned_episodes({})
        assert (await _row(db, episode_id)).outcome == "partial"

        ended = await heart.end_episode(episode_id, outcome="success")

        assert ended.outcome == "success"
        assert ended.active is False
        assert ended.ended_at is not None
    finally:
        await heart.close()
        await _cleanup(db, agent_id)


async def test_recovery_leaves_a_live_session_open_and_closes_it_once_idle(db, agent_id):
    settings = _settings(agent_id)
    summarizer = _WritingSummarizer(db)
    handler = _handler(db, settings, summarizer)
    try:
        episode_id = await _seed_episode(db, agent_id, "f060-live-session", hours_ago=30)
        await _touch_working_memory(db, agent_id, "f060-live-session")

        stats: dict = {}
        await handler._phase_recover_abandoned_episodes(stats)

        row = await _row(db, episode_id)
        assert row.structured_summary is not None  # the summary step is unchanged
        assert row.ended_at is None
        assert row.active is True
        assert "episodes_recovery_closed" not in stats

        # The session goes idle: end_session or the F049 TTL sweep removes its
        # working-memory row. The next cycle closes the already-summarized row
        # without summarizing it again.
        async with db.session() as s:
            await s.execute(text("DELETE FROM heart.working_memory WHERE agent_id = :a"), {"a": agent_id})
            await s.commit()
        stats = {}
        await handler._phase_recover_abandoned_episodes(stats)

        row = await _row(db, episode_id)
        assert summarizer.calls == [episode_id]
        assert row.ended_at is not None
        assert row.active is False
        assert row.outcome == "partial"
        assert stats["episodes_recovery_closed"] == 1
    finally:
        await _cleanup(db, agent_id)


async def test_recovery_does_not_close_an_episode_it_could_not_summarize(db, agent_id):
    settings = _settings(agent_id)
    failing = SimpleNamespace(calls=[])

    async def _no_summary(episode_id, transcript, agent_id=None):
        failing.calls.append(episode_id)
        return None

    failing.summarize_episode = _no_summary
    handler = _handler(db, settings, failing)
    try:
        episode_id = await _seed_episode(db, agent_id, "f060-unsummarized", hours_ago=30)

        stats: dict = {}
        await handler._phase_recover_abandoned_episodes(stats)

        row = await _row(db, episode_id)
        assert failing.calls == [episode_id]
        assert row.structured_summary is None
        assert row.ended_at is None
        assert row.active is True
        assert "episodes_recovery_closed" not in stats
    finally:
        await _cleanup(db, agent_id)


async def test_f057_relinks_an_episode_the_recovery_sweep_closed(db, agent_id, mock_embeddings):
    """The close emits no episode_ended, so the live F022 linker never runs for
    the episode: F057, later in the same cycle, is its only deterministic
    relink path and must still select it once it is closed."""
    settings = _settings(agent_id)
    object.__setattr__(settings, "episode_relink_enabled", True)
    object.__setattr__(settings, "episode_relink_min_age_hours", 24)
    object.__setattr__(settings, "episode_relink_max_per_cycle", 10)
    heart = Heart(db, settings, embedding_provider=mock_embeddings)
    bus = MagicMock()
    bus.on = MagicMock()
    handler = SleepHandler(MagicMock(), heart, settings, bus, llm_client=None)
    handler._episode_summarizer = _WritingSummarizer(db)
    try:
        episode_id = await _seed_episode(db, agent_id, "f060-f057-session", hours_ago=30)
        fact_id = uuid4()
        async with db.session() as s:
            await s.execute(
                text(
                    "INSERT INTO heart.facts (id, agent_id, content, source_episode_id) "
                    "VALUES (:id, :a, 'fact learned in the recovered session', :e)"
                ),
                {"id": fact_id, "a": agent_id, "e": episode_id},
            )
            await s.commit()

        # Same order as the sleep cycle: recover_abandoned_episodes, then relink_open_episodes.
        await handler._phase_recover_abandoned_episodes({})
        row = await _row(db, episode_id)
        assert (row.active, row.outcome) == (False, "partial")

        stats: dict = {}
        assert await handler._phase_relink_open_episodes(stats) is True

        assert stats["episodes_relinked"] == 1
        async with db.session() as s:
            edges = await s.execute(
                text(
                    "SELECT source_id FROM brain.graph_edges WHERE agent_id = :a "
                    "AND source_type = 'fact' AND target_type = 'episode' AND target_id = :e"
                ),
                {"a": agent_id, "e": episode_id},
            )
            assert {r.source_id for r in edges.all()} == {fact_id}
    finally:
        await heart.close()
        await _cleanup(db, agent_id)


# ---------------------------------------------------------------------------
# Closing an open episode must not move any decision window (codex P2, #825)
# ---------------------------------------------------------------------------
# An open episode's decision window ends where its successor in the same
# session begins, less the grace band; a closed one's ends at ended_at. A close
# at now() made a stuck-open predecessor claim its successor's decisions.


async def _insert_episode(db, agent_id, session_id, started, *, ended=None, summary=_PLAIN_SUMMARY) -> UUID:
    async with db.session() as s:
        row = await s.execute(
            text(
                "INSERT INTO heart.episodes (agent_id, summary, session_id, started_at, ended_at, active, outcome) "
                "VALUES (:a, :summary, :sid, :st, :en, :active, :outcome) RETURNING id"
            ),
            {
                "a": agent_id,
                "summary": summary,
                "sid": session_id,
                "st": started,
                "en": ended,
                "active": ended is None,
                "outcome": None if ended is None else "success",
            },
        )
        episode_id = row.scalar_one()
        await s.commit()
    return episode_id


async def _insert_decision(db, agent_id, session_id, created) -> UUID:
    decision_id = uuid4()
    async with db.session() as s:
        await s.execute(
            text(
                "INSERT INTO brain.decisions (id, agent_id, description, confidence, category, stakes, "
                "session_id, created_at) VALUES (:id, :a, 'fixture decision', 0.8, 'process', 'low', :sid, :ts)"
            ),
            {"id": decision_id, "a": agent_id, "sid": session_id, "ts": created},
        )
        await s.commit()
    return decision_id


async def _decisions_of(db, agent_id, episode_id) -> set[UUID]:
    async with db.session() as s:
        rows = await s.execute(
            text(episode_decisions_query("d.id")),
            {"agent_id": agent_id, "episode_id": episode_id},
        )
        return {r[0] for r in rows.all()}


async def test_recovery_close_keeps_every_decision_in_its_episode(db, agent_id):
    settings = _settings(agent_id)
    handler = _handler(db, settings, _WritingSummarizer(db))
    t0 = datetime.now(UTC)
    sid = "f060-decision-session"
    try:
        stuck = await _insert_episode(db, agent_id, sid, t0 - timedelta(hours=30))
        successor_start = t0 - timedelta(hours=10)
        successor = await _insert_episode(db, agent_id, sid, successor_start, ended=t0 - timedelta(hours=9))
        in_stuck = await _insert_decision(db, agent_id, sid, t0 - timedelta(hours=20))
        in_grace_band = await _insert_decision(db, agent_id, sid, successor_start - timedelta(seconds=30))
        in_successor = await _insert_decision(db, agent_id, sid, t0 - timedelta(hours=9, minutes=30))

        before = {ep: await _decisions_of(db, agent_id, ep) for ep in (stuck, successor)}
        assert before == {stuck: {in_stuck}, successor: {in_grace_band, in_successor}}

        stats: dict = {}
        await handler._phase_recover_abandoned_episodes(stats)

        row = await _row(db, stuck)
        assert (row.active, row.outcome) == (False, "partial")
        assert stats["episodes_recovery_closed"] == 1
        after = {ep: await _decisions_of(db, agent_id, ep) for ep in (stuck, successor)}
        assert after == before
    finally:
        await _cleanup(db, agent_id)


async def test_abandon_close_ends_before_the_successor(db, agent_id):
    """Loop B closes an open row the same way: it ends where its window did."""
    settings = _settings(agent_id)
    handler = _handler(db, settings, _WritingSummarizer(db))
    t0 = datetime.now(UTC)
    sid = "f060-abandon-session"
    try:
        empty = await _insert_episode(db, agent_id, sid, t0 - timedelta(days=8), summary="hi")
        successor_start = t0 - timedelta(days=7, hours=12)
        await _insert_episode(db, agent_id, sid, successor_start, ended=successor_start + timedelta(hours=1))

        stats: dict = {}
        await handler._phase_recover_abandoned_episodes(stats)

        assert stats["episodes_marked_abandoned"] == 1
        async with db.session() as s:
            row = (
                await s.execute(text("SELECT ended_at, outcome FROM heart.episodes WHERE id = :id"), {"id": empty})
            ).one()
        assert row.outcome == "abandoned"
        assert row.ended_at == successor_start - timedelta(seconds=60, microseconds=1)
    finally:
        await _cleanup(db, agent_id)


# ---------------------------------------------------------------------------
# An episode with no successor bound ends at its last real activity
# ---------------------------------------------------------------------------
# Without a later episode in its session, nothing bounds an open episode, and
# now() would stamp a months-old episode as ending today. The close takes the
# latest write a live turn made for it; what the recovery itself writes now
# (extracted facts, dialogue chunks) is not evidence. No evidence: it ends at
# its start and its duration stays unknown.


async def _insert_fact(db, agent_id, episode_id, source, created) -> None:
    async with db.session() as s:
        await s.execute(
            text(
                "INSERT INTO heart.facts (agent_id, content, source, source_episode_id, created_at) "
                "VALUES (:a, :c, :src, :e, :ts)"
            ),
            {"a": agent_id, "c": f"fixture fact from {source}", "src": source, "e": episode_id, "ts": created},
        )
        await s.commit()


async def _close_fields(db, episode_id):
    async with db.session() as s:
        return (
            await s.execute(
                text("SELECT started_at, ended_at, duration_seconds, outcome FROM heart.episodes WHERE id = :id"),
                {"id": episode_id},
            )
        ).one()


async def test_close_without_session_ends_at_last_live_activity(db, agent_id):
    settings = _settings(agent_id)
    handler = _handler(db, settings, _WritingSummarizer(db))
    t0 = datetime.now(UTC)
    try:
        episode_id = await _insert_episode(db, agent_id, None, t0 - timedelta(days=30))
        last_activity = t0 - timedelta(days=29)
        await _insert_fact(db, agent_id, episode_id, "user_direct", t0 - timedelta(days=29, hours=5))
        await _insert_fact(db, agent_id, episode_id, "telegram-attachment", t0 - timedelta(days=29, hours=1))
        # post_turn's inline correction (F039) is written inside a live turn too.
        await _insert_fact(db, agent_id, episode_id, "inline_correction", last_activity)
        # Written now, after the session died: not activity.
        await _insert_fact(db, agent_id, episode_id, "fact_extractor", t0)
        async with db.session() as s:
            await s.execute(
                text(
                    "INSERT INTO heart.episode_chunks (agent_id, episode_id, chunk_index, content) "
                    "VALUES (:a, :e, 0, 'dialogue chunk the summarizer wrote')"
                ),
                {"a": agent_id, "e": episode_id},
            )
            await s.commit()

        await handler._phase_recover_abandoned_episodes({})

        row = await _close_fields(db, episode_id)
        assert row.outcome == "partial"
        assert row.ended_at == last_activity
        assert row.duration_seconds == int((last_activity - row.started_at).total_seconds())
    finally:
        await _cleanup(db, agent_id)


async def test_close_without_successor_counts_the_sessions_decisions(db, agent_id):
    settings = _settings(agent_id)
    handler = _handler(db, settings, _WritingSummarizer(db))
    t0 = datetime.now(UTC)
    sid = "f060-last-decision-session"
    try:
        episode_id = await _insert_episode(db, agent_id, sid, t0 - timedelta(days=30))
        last_decision = t0 - timedelta(days=29, hours=20)
        await _insert_decision(db, agent_id, sid, t0 - timedelta(days=29, hours=23))
        await _insert_decision(db, agent_id, sid, last_decision)

        await handler._phase_recover_abandoned_episodes({})

        row = await _close_fields(db, episode_id)
        assert row.outcome == "partial"
        assert row.ended_at == last_decision
        assert await _decisions_of(db, agent_id, episode_id) == await _decisions_in_session(db, agent_id, sid)
    finally:
        await _cleanup(db, agent_id)


async def test_close_without_evidence_ends_at_start_with_unknown_duration(db, agent_id):
    settings = _settings(agent_id)
    handler = _handler(db, settings, _WritingSummarizer(db))
    t0 = datetime.now(UTC)
    try:
        recovered = await _insert_episode(db, agent_id, None, t0 - timedelta(days=30))
        abandoned = await _insert_episode(db, agent_id, None, t0 - timedelta(days=30), summary="hi")

        stats: dict = {}
        await handler._phase_recover_abandoned_episodes(stats)

        assert stats["episodes_recovery_closed"] == 1
        assert stats["episodes_marked_abandoned"] == 1
        for episode_id, outcome in ((recovered, "partial"), (abandoned, "abandoned")):
            row = await _close_fields(db, episode_id)
            assert row.outcome == outcome
            assert row.ended_at == row.started_at
            assert row.duration_seconds is None
    finally:
        await _cleanup(db, agent_id)


async def _decisions_in_session(db, agent_id, session_id) -> set[UUID]:
    async with db.session() as s:
        rows = await s.execute(
            text("SELECT id FROM brain.decisions WHERE agent_id = :a AND session_id = :sid"),
            {"a": agent_id, "sid": session_id},
        )
        return {r[0] for r in rows.all()}
