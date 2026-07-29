"""Episode <-> decision correlation window (2026-07-28).

`heart.episode_decisions` had a full write API and no runtime writer, so all
four of its readers were silently reading an empty table. Migration 068 drops
it; episode and decision are correlated instead through the session both rows
carry. These predicates are the single source of truth for that window.

The unit tests pin the SQL text and the two terms that a prod measurement
proved load-bearing. The Postgres test pins the behavior end to end.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from nous.brain.graph_constants import (
    EPISODE_DECISION_GRACE_SECONDS,
    episode_decision_bounds_sql,
    episode_decision_join_sql,
    episode_decisions_query,
)


class TestEpisodeDecisionSql:
    def test_join_carries_the_agent_term(self):
        """session_id is NOT agent-namespaced (heartbeat-<hex> / subtask-<hex>
        are generated identically per agent), so dropping this term
        materializes cross-agent pairs."""
        sql = episode_decision_join_sql()
        assert "d.agent_id = e.agent_id" in sql
        assert "d.session_id = e.session_id" in sql

    def test_window_has_a_grace_on_the_lower_bound(self):
        """pre_turn records a deliberation decision at step 4 and creates the
        episode at step 5 — a decision can predate its own episode. A strict
        `>= started_at` window loses 35 of prod's 87 matchable decisions."""
        assert EPISODE_DECISION_GRACE_SECONDS > 0
        bounds = episode_decision_bounds_sql()
        assert (
            f"started_at - interval '{EPISODE_DECISION_GRACE_SECONDS} seconds'"
            in bounds
        )
        assert "decision_window_start" in bounds

    def test_open_episodes_are_bounded_by_the_next_episode_in_the_session(self):
        """Otherwise a stuck-open episode in a reused session vacuums up every
        later decision (COALESCE(ended_at, now()) alone is unbounded)."""
        bounds = episode_decision_bounds_sql()
        assert "LEAD(started_at) OVER" in bounds
        assert "PARTITION BY agent_id, session_id ORDER BY started_at" in bounds
        assert "LEAST(" in bounds

    def test_bounds_are_agent_scoped_and_skip_null_sessions(self):
        bounds = episode_decision_bounds_sql()
        assert "session_id IS NOT NULL" in bounds
        assert "agent_id = :agent_id" in bounds
        assert "agent_id = :aid" in episode_decision_bounds_sql(agent_param="aid")

    def test_bounds_project_the_columns_episode_live_sql_needs(self):
        """graph_densifier applies episode_live_sql to the derived table."""
        bounds = episode_decision_bounds_sql()
        for col in ("active", "ended_at", "outcome", "id", "agent_id"):
            assert col in bounds

    def test_query_is_per_episode_and_oldest_first(self):
        sql = episode_decisions_query("d.id")
        assert "e.id = :episode_id" in sql
        assert "ORDER BY d.created_at" in sql
        assert "JOIN brain.decisions d" in sql


@pytest.mark.postgres_only
async def test_window_selects_only_same_session_same_agent_in_window(db):
    """End-to-end against real Postgres: five decisions around one episode,
    exactly one of which belongs to it."""
    agent_a = f"edw-a-{uuid4().hex[:8]}"
    agent_b = f"edw-b-{uuid4().hex[:8]}"
    session_id = f"edw-session-{uuid4().hex[:8]}"
    started = datetime.now(UTC) - timedelta(hours=2)
    ended = started + timedelta(hours=1)
    ep_id = uuid4()
    hit = uuid4()
    grace_hit = uuid4()

    async def _decision(sess, did, agent, sid, ts, desc):
        await sess.execute(text(
            "INSERT INTO brain.decisions "
            "(id, agent_id, description, confidence, category, stakes, "
            " session_id, created_at) "
            "VALUES (:id, :aid, :d, 0.8, 'process', 'low', :sid, :ts)"
        ), {"id": did, "aid": agent, "d": desc, "sid": sid, "ts": ts})

    try:
        async with db.session() as fs:
            for aid in (agent_a, agent_b):
                await fs.execute(text(
                    "INSERT INTO nous_system.agents (id, name) "
                    "VALUES (:aid, 'x') ON CONFLICT (id) DO NOTHING"
                ), {"aid": aid})
            await fs.execute(text(
                "INSERT INTO heart.episodes "
                "(id, agent_id, summary, started_at, ended_at, active, "
                " session_id, tags) "
                "VALUES (:id, :aid, 'window fixture', :st, :en, false, "
                "        :sid, '{}')"
            ), {
                "id": ep_id, "aid": agent_a, "st": started, "en": ended,
                "sid": session_id,
            })
            await _decision(fs, hit, agent_a, session_id,
                            started + timedelta(minutes=10), "in window")
            # Recorded BEFORE the episode row exists — the pre_turn ordering
            # the grace interval covers.
            await _decision(
                fs, grace_hit, agent_a, session_id,
                started - timedelta(seconds=EPISODE_DECISION_GRACE_SECONDS - 5),
                "just before the episode was created",
            )
            await _decision(fs, uuid4(), agent_a, session_id,
                            ended + timedelta(minutes=5), "after close")
            await _decision(fs, uuid4(), agent_b, session_id,
                            started + timedelta(minutes=10), "other agent")
            await _decision(fs, uuid4(), agent_a, f"{session_id}-other",
                            started + timedelta(minutes=10), "other session")
            await fs.commit()

        async with db.session() as vs:
            rows = await vs.execute(
                text(episode_decisions_query("d.id")),
                {"agent_id": agent_a, "episode_id": ep_id},
            )
            found = [r[0] for r in rows.all()]
        assert set(found) == {hit, grace_hit}, found
    finally:
        async with db.session() as cs:
            await cs.execute(text(
                "DELETE FROM brain.decisions WHERE agent_id IN (:a, :b)"
            ), {"a": agent_a, "b": agent_b})
            await cs.execute(text(
                "DELETE FROM heart.episodes WHERE agent_id IN (:a, :b)"
            ), {"a": agent_a, "b": agent_b})
            await cs.execute(text(
                "DELETE FROM nous_system.agents WHERE id IN (:a, :b)"
            ), {"a": agent_a, "b": agent_b})
            await cs.commit()


@pytest.mark.postgres_only
async def test_open_episode_stops_at_the_next_episode_in_the_session(db):
    """A stuck-open episode must not claim decisions made after the session's
    next episode started."""
    agent = f"edw-open-{uuid4().hex[:8]}"
    session_id = f"edw-open-session-{uuid4().hex[:8]}"
    first_start = datetime.now(UTC) - timedelta(hours=3)
    second_start = first_start + timedelta(hours=1)
    ep_open = uuid4()
    ep_next = uuid4()
    early = uuid4()
    late = uuid4()

    try:
        async with db.session() as fs:
            await fs.execute(text(
                "INSERT INTO nous_system.agents (id, name) "
                "VALUES (:aid, 'x') ON CONFLICT (id) DO NOTHING"
            ), {"aid": agent})
            for eid, st in ((ep_open, first_start), (ep_next, second_start)):
                await fs.execute(text(
                    "INSERT INTO heart.episodes "
                    "(id, agent_id, summary, started_at, ended_at, active, "
                    " session_id, tags) "
                    "VALUES (:id, :aid, 'open fixture', :st, NULL, true, "
                    "        :sid, '{}')"
                ), {"id": eid, "aid": agent, "st": st, "sid": session_id})
            for did, ts in (
                (early, first_start + timedelta(minutes=5)),
                (late, second_start + timedelta(minutes=5)),
            ):
                await fs.execute(text(
                    "INSERT INTO brain.decisions "
                    "(id, agent_id, description, confidence, category, "
                    " stakes, session_id, created_at) "
                    "VALUES (:id, :aid, 'open fixture decision', 0.8, "
                    "        'process', 'low', :sid, :ts)"
                ), {"id": did, "aid": agent, "sid": session_id, "ts": ts})
            await fs.commit()

        async with db.session() as vs:
            first = [r[0] for r in (await vs.execute(
                text(episode_decisions_query("d.id")),
                {"agent_id": agent, "episode_id": ep_open},
            )).all()]
            second = [r[0] for r in (await vs.execute(
                text(episode_decisions_query("d.id")),
                {"agent_id": agent, "episode_id": ep_next},
            )).all()]
        assert first == [early], first
        assert second == [late], second
    finally:
        async with db.session() as cs:
            await cs.execute(text(
                "DELETE FROM brain.decisions WHERE agent_id = :a"
            ), {"a": agent})
            await cs.execute(text(
                "DELETE FROM heart.episodes WHERE agent_id = :a"
            ), {"a": agent})
            await cs.execute(text(
                "DELETE FROM nous_system.agents WHERE id = :a"
            ), {"a": agent})
            await cs.commit()
